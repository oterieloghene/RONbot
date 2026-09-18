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

import re

import discord

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
        await channel.set_permissions(
            member,
            send_messages=row["id"] in writable,
            reason="RONbot location sync: writability follows travel",
        )
