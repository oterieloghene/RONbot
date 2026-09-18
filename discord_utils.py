"""Tiny shared helpers so cogs don't each reimplement role/channel lookup.

Roles are looked up by name, not stored ID -- every role name in this game
is unique across the whole server (see roles_seed.sql).

Channels are trickier: a channel name alone repeats across states (e.g.
"arrival-terminal" exists in Delta, Lagos, and Abuja), but the CATEGORY
it sits under has the state baked into it ("Delta Border & Entry" vs
"Lagos Border & Entry" vs ...) -- same (state, category, channel) triple
locations_seed.sql already keys everything by. So (category, channel)
together is unique, and get_channel below finds it that way instead of
needing a stored ID. Matching is normalized (case/punctuation-insensitive,
"&" treated as "and") since Discord category names won't necessarily be
typed with exact matching punctuation.
"""

import logging
import re

import discord

import config
import database
import permissions


def get_role(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=name)


def _normalize(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z ]", " ", s)
    return " ".join(s.split())


def _is_text_channel(channel) -> bool:
    """True for the one channel type a location can live in: a plain text
    channel. Test stand-ins have no .type attribute and are treated as
    text."""
    return getattr(channel, "type", discord.ChannelType.text) == discord.ChannelType.text


def get_channel(
    guild: discord.Guild, state: str, game_category: str, channel_name: str
) -> discord.abc.GuildChannel | None:
    """e.g. get_channel(guild, "Delta", "BORDER & ENTRY", "immigration-office")
    finds the channel named "immigration-office" under whichever category
    normalizes to "delta border and entry".

    Text channels only: a location is a channel players type in, and the
    server keeps voice channels that reuse the location names (court-room,
    city-hall) under the same categories. Matching one of those would bind
    the location's join key to a voice channel, and Discord rejects a
    send-messages overwrite on a voice channel (403)."""
    target = _normalize(f"{state} {game_category}")
    for category in guild.categories:
        if _normalize(category.name) == target:
            for channel in category.channels:
                if channel.name == channel_name and _is_text_channel(channel):
                    return channel
    return None


# Staff keep the game running regardless of their own tracked location:
# officers run !name/!immigrate at the front desk, the marshal oversees the
# whole bureau. They need Send Messages on every location channel.
STAFF_ROLE_NAMES = (
    config.IMMIGRATION_OFFICER_ROLE_NAME,
    "Chief Immigration Marshal",
)


def _target_label(target) -> str:
    name = getattr(target, "name", None)
    return f"'{name}' ({target.id})" if name else f"({target.id})"


def _check_overwrite_capacity(guild: discord.Guild) -> None:
    """Log an actionable warning if the bot cannot manage channel overwrites.

    Discord 403s (code 50013) an overwrite edit when either:
      * the bot lacks Manage Roles on that channel -- the permission the
        API actually checks for editing channel permission overwrites
        (NOT Manage Channel Permissions, which only governs channel
        create/rename/delete), or
      * the target role/member ranks above the bot's highest role in the
        server's role list -- the usual culprit when every permission bit
        is already on: overwrites that target a state role (Delta, ...) or
        a player whose top role sits above the bot are all rejected, on
        every channel, no matter how the individual channels are gated.
    Neither is fixable from code -- the operator must fix Server Settings ->
    Roles -- so we say exactly what to change before the per-channel skips
    start piling up.
    """
    me = guild.me
    if me is None:
        return
    if not me.guild_permissions.manage_roles:
        logging.warning(
            "Permission setup for %s: bot has NO Manage Roles permission. "
            "That is the permission Discord checks when editing channel "
            "overwrites (NOT Manage Channel Permissions -- the one that "
            "has been on from the start). Grant 'Manage Roles' to the bot's "
            "role in Server Settings -> Roles -> [bot role] -> Advanced, "
            "otherwise every overwrite below will be rejected with 50013 "
            "and channels stay as-is.",
            guild.name,
        )
    top = me.top_role
    higher = [role for role in guild.roles if role > top]
    if higher:
        names = ", ".join(f"'{role.name}'" for role in higher)
        logging.warning(
            "Permission setup for %s: these roles rank ABOVE the bot's top "
            "role '%s': %s. Discord rejects with 50013 every overwrite "
            "that targets a role above the bot's top role -- or a member "
            "whose top role does -- so the staff re-grant and the "
            "per-player travel sync both fail on every channel until the "
            "bot's role sits above them. Drag the bot's role up in Server "
            "Settings -> Roles (admin/owner roles may stay above it).",
            guild.name, top.name, names,
        )


def log_role_ladder(guild: discord.Guild) -> None:
    """Log the role-ladder facts at startup in ONE compact line.

    Only what 50013 debugging actually needs: the bot's top role, the
    MANAGE_ROLES bit (the permission Discord checks when editing channel
    overwrites), and any roles ranking above the bot -- the only overwrites
    the API can reject on rank grounds. The old one-line-per-role dump
    (140+ roles on this server) made every Render deploy log too long to
    read; if a full ladder dump is ever needed again, it can be fetched
    from the API -- the startup log only needs the failure-relevant facts.
    """
    me = guild.me
    if me is None:
        return
    top = me.top_role
    mr = "ON" if me.guild_permissions.manage_roles else "OFF"
    roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)
    higher = [role for role in roles if role > top]
    if higher:
        above = ", ".join(f"'{r.name}' (pos={r.position})" for r in higher)
        higher_text = f"; {len(higher)} role(s) ABOVE bot: {above}"
    else:
        higher_text = "; no roles above bot"
    logging.info(
        "Role ladder for %s: bot id %s, top role '%s' (%s), MANAGE_ROLES: %s, "
        "%d roles total%s",
        guild.name, me.id, top.name, top.id, mr, len(roles), higher_text,
    )


async def _apply_overwrite(channel, target, *, reason: str, **overwrite_kwargs) -> bool:
    """Apply one permission overwrite; on 403 (50013) log and return False
    instead of raising.

    A single rejected overwrite must not abort the rest of the setup -- the
    bindings and the overwrites Discord does allow still have to land.
    """
    try:
        await channel.set_permissions(target, reason=reason, **overwrite_kwargs)
        return True
    except discord.Forbidden as exc:
        # Log the RAW Discord response: this is ground truth. The rank_hint
        # is only an interpretation -- when the role ladder looks fine in the
        # UI, the log line tells us exactly what Discord actually rejected.
        top_role = getattr(getattr(target, "top_role", None), "name", None)
        rank_hint = (
            f" -- '{top_role}' ranks above the bot's top role; drag the bot's "
            f"role above it in Server Settings -> Roles"
            if top_role
            else " -- check Manage Roles on this channel/category too"
        )
        logging.warning(
            "Overwrite REJECTED on #%s (%s) for %s: HTTP %s -- Discord: %s.%s",
            channel.name, channel.id, _target_label(target),
            exc.status, exc.text or "no response body", rank_hint,
        )
        return False


async def setup_permissions(guild: discord.Guild) -> dict:
    """Idempotent permission setup, run once per bot startup (on_ready).

    Three jobs, in order:

    1. BIND -- locations.channel_id is the join key between the DB and the
       live Discord channels, and nothing else writes it. Resolve every
       seeded location to its real channel via get_channel() and persist
       the ID. Without this, get_state_location_channels() returns zero
       rows and sync_location_permissions() silently no-ops -- the whole
       writability model is dead for exactly that reason.
    2. LOCKDOWN -- deny @everyone View Channel + Send Messages on every
       role-gated location channel (send on every channel). The per-member
       overwrite model (write only at your current location) only works
       when the base permission is deny; otherwise any member the bot
       hasn't processed yet can write everywhere. View must be denied
       explicitly here: set_permissions is a full-replace PUT, so without
       the view bit in the same call, every startup would wipe the
       @everyone View Channel deny the role-gate depends on.
    3. REGRANT + RESYNC -- give the staff roles a Send overwrite on every
       location channel, then re-run sync_location_permissions for every
       arrived player, repairing stale/missing per-member overwrites (bot
       was offline during a move, or rows predate the binding fix).

    Every step is idempotent, so re-running on reconnect is safe.
    Returns a summary dict for logging.
    """
    _check_overwrite_capacity(guild)
    summary = {"bound": 0, "missing": 0, "channels": 0, "resynced": 0, "skipped": 0, "denied": 0}

    locations = await database.get_all_locations()
    location_channels = []
    for row in locations:
        channel = get_channel(guild, row["state"], row["category"], row["channel_name"])
        if channel is None:
            summary["missing"] += 1
            continue
        if row["channel_id"] != channel.id:
            await database.bind_location_channel(row["id"], channel.id)
            summary["bound"] += 1
        location_channels.append((channel, row["role_gated"]))
    summary["channels"] = len(location_channels)

    # -- 2. LOCKDOWN -----------------------------------------------------------
    # For role-gated channels, deny BOTH View Channel and Send Messages so
    # non-members can neither see nor type.  For non-gated channels, only
    # deny Send Messages (visibility is controlled elsewhere).
    # CRITICAL: set_permissions is a full-replace PUT — sending only
    # send_messages wipes any existing view_channel deny the role-gate
    # depends on, so we must include both bits in a single call.
    for channel, role_gated in location_channels:
        overwrite_kwargs = {"send_messages": False}
        if role_gated:
            overwrite_kwargs["view_channel"] = False
        if not await _apply_overwrite(
            channel, guild.default_role,
            reason="RONbot startup: writability follows travel",
            **overwrite_kwargs,
        ):
            summary["denied"] += 1

    # -- 3. REGRANT staff ------------------------------------------------------
    for role_name in STAFF_ROLE_NAMES:
        role = get_role(guild, role_name)
        if role is None:
            continue
        for channel, _gated in location_channels:
            if not await _apply_overwrite(
                channel, role, send_messages=True,
                reason="RONbot startup: staff can run the game everywhere",
            ):
                summary["denied"] += 1

    for player in await database.get_players_needing_sync():
        member = guild.get_member(player["discord_id"])
        if member is None:
            summary["skipped"] += 1
            continue
        await sync_location_permissions(
            guild, member, player["current_state"], player["current_location_id"]
        )
        summary["resynced"] += 1

    return summary


async def sync_location_permissions(
    guild: discord.Guild,
    member: discord.Member,
    state: str,
    current_location_id: int | None = None,
) -> None:
    """Re-sync per-member send overwrites across EVERY channel of `state`.

    Visibility is role-gated (the state role shows/hides channels in one
    shot); writability is travel-gated. The two are orthogonal, so after
    any event that changes either layer -- arrival, !immigrate, car
    arrival/breakdown, bus alight, or boarding/departure (in transit) --
    walk every location channel in the state and set an explicit
    per-member overwrite:

      * send_messages=True  -- the channel's location is the player's
        current_location_id, or a (transitive) sublocation of it
      * send_messages=False -- everything else, including the in-transit
        case where current_location_id is None (read-only everywhere)

    view_channel is deliberately left untouched: role membership alone
    still controls visibility, and an explicit send overwrite on an
    invisible channel is inert. This is what stops a player who just
    gained the state role via !immigrate from being able to type in
    channels they can only now *see* but have never travelled to.
    """
    rows = await database.get_state_location_channels(state)
    if not rows:
        return
    if member.id == guild.owner_id:
        # The server owner is exempt from every overwrite: an explicit
        # member overwrite on the owner always 50013s (the owner's implicit
        # rank sits above every role, so the API rejects it), and the owner
        # ignores channel denies anyway (implicit Administrator) -- the
        # @everyone send=False lockdown never applies to them.
        return
    parent_map = {row["id"]: row["parent_location_id"] for row in rows}
    writable = permissions.writable_location_ids(
        current_location_id, permissions.sublocations_of(parent_map)
    )
    for row in rows:
        channel = guild.get_channel(row["channel_id"])
        # A stale row may still point at a voice channel that reused the
        # location name (bound before get_channel learned to skip voice).
        # An overwrite there can never succeed, so skip it until the next
        # startup rebinds the location to its text channel.
        if channel is None or not _is_text_channel(channel):
            continue  # channel deleted or not a text channel -- nothing to sync
        await _apply_overwrite(
            channel, member,
            send_messages=row["id"] in writable,
            reason="RONbot location sync: writability follows travel",
        )
