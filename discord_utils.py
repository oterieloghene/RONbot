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
