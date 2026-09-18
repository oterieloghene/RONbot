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


def get_channel(
    guild: discord.Guild, state: str, game_category: str, channel_name: str
) -> discord.abc.GuildChannel | None:
    """e.g. get_channel(guild, "Delta", "BORDER & ENTRY", "immigration-office")
    finds the channel named "immigration-office" under whichever category
    normalizes to "delta border and entry"."""
    target = _normalize(f"{state} {game_category}")
    for category in guild.categories:
        if _normalize(category.name) == target:
            return discord.utils.get(category.channels, name=channel_name)
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
        create/rename/delete -- a classic confusion), or
      * the target role/member ranks above the bot's highest role.
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
    for role_name in STAFF_ROLE_NAMES:
        role = get_role(guild, role_name)
        if role is not None and role > top:
            logging.warning(
                "Permission setup for %s: staff role '%s' ranks ABOVE the bot's "
                "top role '%s' -- Discord will reject every overwrite on that "
                "role. Drag the bot's role above it in Server Settings -> Roles.",
                guild.name, role_name, top.name,
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
    except discord.Forbidden:
        logging.warning(
            "Overwrite REJECTED on #%s (%s) for %s: bot is missing Manage "
            "Roles there (the permission Discord checks for overwrites -- "
            "not Manage Channel Permissions), or its top role ranks below "
            "the target. Fix in Server Settings -> Roles.",
            channel.name, channel.id, _target_label(target),
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
    2. LOCKDOWN -- deny @everyone Send Messages on every location channel.
       The per-member overwrite model (write only at your current location)
       only works when the base permission is deny; otherwise any member
       the bot hasn't processed yet can write everywhere.
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
        location_channels.append(channel)

    for role_name in STAFF_ROLE_NAMES:
        role = get_role(guild, role_name)
        if role is None:
            continue
        for channel in location_channels:
            if not await _apply_overwrite(
                channel, role, send_messages=True,
                reason="RONbot startup: staff can run the game everywhere",
            ):
                summary["denied"] += 1
    for channel in location_channels:
        if not await _apply_overwrite(
            channel, guild.default_role, send_messages=False,
            reason="RONbot startup: writability follows travel",
        ):
            summary["denied"] += 1
    summary["channels"] = len(location_channels)

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
    parent_map = {row["id"]: row["parent_location_id"] for row in rows}
    writable = permissions.writable_location_ids(
        current_location_id, permissions.sublocations_of(parent_map)
    )
    for row in rows:
        channel = guild.get_channel(row["channel_id"])
        if channel is None:
            continue  # channel deleted or not in cache -- nothing to sync
        await _apply_overwrite(
            channel, member,
            send_messages=row["id"] in writable,
            reason="RONbot location sync: writability follows travel",
        )
