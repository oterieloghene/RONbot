"""Regression tests for the onboarding cog's rejoin handling.

Two production incidents under test:

1. A player who left the server and joined again still had their old
   state's roles, and therefore access to their old state's channels,
   with no welcome message. _handle_arrival must strip every stale
   state role (every other state's "{State}" / "{State} Arrival" roles,
   plus the new state's own settlement role) before recording the fresh
   arrival -- while keeping the "{State} Arrival" role that triggered it.
2. The welcome send crashed the entire on_member_update handler with
   discord.Forbidden (403/50013 Missing Permissions) on Render. It must
   log the permission gap instead of crashing, with the DB arrival still
   recorded and other members' arrivals unaffected.

Only the database boundary is stubbed (same approach as
tests/test_immigration_commands.py); the cog, discord_utils lookups, and
discord.py types are all real.
"""

import asyncio
import logging

import pytest

import config
import database
import discord
from cogs.onboarding import Onboarding

STATE = "Delta"

# A second-life player: previously immigrated to Lagos, left, now picking
# Delta in the onboarding question.
REJOINED_ROW = {
    "immigration_status": "immigrated",
    "current_state": "Lagos",
}

STALE_ROLE_NAMES = ("Lagos", "Lagos Arrival", "Abuja", "Abuja Arrival")


# ---------------------------------------------------------------------------
# Minimal stand-ins for the Discord objects the handler touches
# ---------------------------------------------------------------------------

class FakeRole:
    _next_id = 1000

    def __init__(self, name):
        self.id = FakeRole._next_id
        FakeRole._next_id += 1
        self.name = name
        self.deletes = []

    async def delete(self, reason=None):
        self.deletes.append(reason)


class FakeChannel:
    _next_id = 2000

    def __init__(self, name, raise_forbidden=False):
        self.id = FakeChannel._next_id
        FakeChannel._next_id += 1
        self.name = name
        self.sent = []
        self.raise_forbidden = raise_forbidden

    async def send(self, content):
        if self.raise_forbidden:
            raise _make_forbidden()
        self.sent.append(content)


class FakeCategory:
    def __init__(self, name):
        self.name = name
        self.channels = []

    def add_channel(self, name, **kwargs):
        ch = FakeChannel(name, **kwargs)
        self.channels.append(ch)
        return ch


class FakeGuild:
    def __init__(self, state):
        self.roles = []
        self.categories = []
        self._role_by_id = {}

        border = FakeCategory(f"{state} Border & Entry")
        self.terminal = border.add_channel("arrival-terminal")
        self.categories.append(border)

        self.add_role(f"{state} Arrival")
        self.add_role(state)
        for other_state in config.STATES:
            if other_state != state:
                self.add_role(other_state)
                self.add_role(f"{other_state} Arrival")

    def add_role(self, name):
        role = FakeRole(name)
        self.roles.append(role)
        self._role_by_id[role.id] = role
        return role

    def role_by_name(self, name):
        return discord.utils.get(self.roles, name=name)

    def get_role(self, role_id):
        return self._role_by_id.get(role_id)


class FakeMember:
    _next_id = 3000

    def __init__(self, guild, name, roles=()):
        self.id = FakeMember._next_id
        FakeMember._next_id += 1
        self.guild = guild
        self.name = name
        self.mention = f"<@{self.id}>"
        self.roles = list(roles)
        self.removed_roles = []

    async def remove_roles(self, *roles, reason=None):
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)
            self.removed_roles.append(role)


class _FakeResponse:
    status = 403
    reason = "Forbidden"


def _make_forbidden():
    return discord.Forbidden(
        _FakeResponse(),
        "403 Forbidden (error code: 50013): Missing Permissions",
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def db_stubs():
    saved = {
        name: getattr(database, name)
        for name in (
            "get_player",
            "reset_player_on_leave",
            "ensure_player_exists",
            "record_arrival",
        )
    }
    yield
    for name, fn in saved.items():
        setattr(database, name, fn)


def make_setup(player_row=REJOINED_ROW, welcome_forbidden=False):
    """Build the rejoin scenario wired against a stubbed DB.

    Returns (cog, before, after, terminal, calls, nin_role). `before` and
    `after` are the two member snapshots on_member_update receives: the
    player holds their old state's roles (the bot missed the leave event)
    and the onboarding question just granted the fresh "{STATE} Arrival"
    role to `after`.
    """
    calls = {
        "get_player": [],
        "reset_player_on_leave": [],
        "ensure_player_exists": [],
        "record_arrival": [],
    }

    async def fake_get_player(discord_id):
        calls["get_player"].append(discord_id)
        return player_row

    async def fake_reset_player_on_leave(discord_id):
        calls["reset_player_on_leave"].append(discord_id)
        return nin_role.id

    async def fake_ensure_player_exists(discord_id):
        calls["ensure_player_exists"].append(discord_id)

    async def fake_record_arrival(discord_id, state):
        calls["record_arrival"].append((discord_id, state))

    guild = FakeGuild(STATE)
    guild.terminal.raise_forbidden = welcome_forbidden
    nin_role = guild.add_role("NIN-0007-LA")
    stale_roles = [guild.role_by_name(name) for name in STALE_ROLE_NAMES]
    arrival_role = guild.role_by_name(f"{STATE} Arrival")

    after = FakeMember(guild, "rejoiner", roles=stale_roles + [arrival_role])
    before = FakeMember(guild, "rejoiner", roles=stale_roles)
    before.id = after.id  # same member, pre- and post-role-grant

    database.get_player = fake_get_player
    database.reset_player_on_leave = fake_reset_player_on_leave
    database.ensure_player_exists = fake_ensure_player_exists
    database.record_arrival = fake_record_arrival

    cog = Onboarding(None)
    return cog, before, after, guild.terminal, calls, nin_role


# ---------------------------------------------------------------------------
# Rejoin: stale role cleanup + welcome
# ---------------------------------------------------------------------------

def test_rejoin_strips_stale_state_roles_and_welcomes(db_stubs):
    cog, before, after, terminal, calls, nin_role = make_setup()

    asyncio.run(cog.on_member_update(before, after))

    removed_names = {r.name for r in after.removed_roles}
    assert removed_names == set(STALE_ROLE_NAMES)
    # The fresh arrival role is what the flow depends on -- never stripped.
    assert f"{STATE} Arrival" in [r.name for r in after.roles]

    # Stale 'immigrated' row reset (frees the NIN and deletes its role) and
    # fresh arrival recorded for the new state.
    assert calls["reset_player_on_leave"] == [after.id]
    assert nin_role.deletes == ["Player re-arrived -- stale NIN role freed for reuse"]
    assert calls["ensure_player_exists"] == [after.id]
    assert calls["record_arrival"] == [(after.id, STATE)]

    assert terminal.sent == [
        f"<@{after.id}> has arrived in {STATE}. Welcome to {STATE} State."
    ]


def test_rejoin_stale_roles_stripped_even_without_old_db_row(db_stubs):
    """A first-time member whose roles somehow survived (bot missed the
    leave event, or a re-onboard) must still be cleaned up -- the strip
    must not depend on the DB row being stale."""
    cog, before, after, terminal, calls, _ = make_setup(player_row=None)

    asyncio.run(cog.on_member_update(before, after))

    assert {r.name for r in after.removed_roles} == set(STALE_ROLE_NAMES)
    assert calls["reset_player_on_leave"] == []
    assert calls["record_arrival"] == [(after.id, STATE)]
    assert len(terminal.sent) == 1


# ---------------------------------------------------------------------------
# Rejoin: welcome send Forbidden (50013) must not crash the handler
# ---------------------------------------------------------------------------

def test_rejoin_forbidden_welcome_logs_warning_without_crashing(db_stubs, caplog):
    cog, before, after, terminal, calls, _ = make_setup(welcome_forbidden=True)

    with caplog.at_level(logging.WARNING, logger="cogs.onboarding"):
        asyncio.run(cog.on_member_update(before, after))  # must not raise

    assert "Arrival Terminal" in caplog.text
    # The permission gap must not undo the work: roles stripped, arrival
    # recorded -- only the public welcome is missing.
    assert {r.name for r in after.removed_roles} == set(STALE_ROLE_NAMES)
    assert calls["record_arrival"] == [(after.id, STATE)]
    assert terminal.sent == []