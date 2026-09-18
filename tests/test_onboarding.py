"""Regression tests for the Onboarding cog event handlers.

The handlers run for real (real cog, real discord_utils lookups, real
discord.py). Only the database boundary is stubbed, because production
uses asyncpg/Postgres, which the test sandbox has no server for. Each
stub is an in-memory stand-in that records its calls so the assertions
verify exactly which DB writes each event performed.

The central case under test: a member leaves and re-joins, and Discord's
onboarding auto-assigns their "{State} Arrival" role in the very
GUILD_MEMBER_ADD payload. on_member_update never fires for a role a
member already has at join time -- only on_member_join does, so a bot
that only listens to member_update welcomes nobody and resets nothing
visible ('No welcome message nothing' after the last deploy).
"""

import pytest

import config
import database
from cogs.onboarding import Onboarding


# ---------------------------------------------------------------------------
# Minimal stand-ins for the Discord objects the handlers touch
# ---------------------------------------------------------------------------

class FakeRole:
    _next_id = 5000

    def __init__(self, name):
        self.id = FakeRole._next_id
        FakeRole._next_id += 1
        self.name = name
        self.deletes = []

    async def delete(self, reason=None):
        self.deletes.append(reason)


class FakeChannel:
    _next_id = 6000

    def __init__(self, name):
        self.id = FakeChannel._next_id
        FakeChannel._next_id += 1
        self.name = name
        self.permission_overwrites = {}
        self.sent = []

    async def send(self, content):
        self.sent.append(content)

    async def set_permissions(self, target, **kwargs):
        self.permission_overwrites[target] = kwargs


class FakeCategory:
    def __init__(self, name):
        self.name = name
        self.channels = []

    def add_channel(self, name):
        channel = FakeChannel(name)
        self.channels.append(channel)
        return channel


class FakeGuild:
    def __init__(self, state):
        self.state = state
        self.roles = []
        self.categories = []
        self.owner_id = None

        border = FakeCategory(f"{state} Border & Entry")
        self.arrival_terminal = border.add_channel("arrival-terminal")
        self.categories.append(border)

        self.arrival_role = self.add_role(f"{state} Arrival")

    def get_channel(self, channel_id):
        for category in self.categories:
            for channel in category.channels:
                if channel.id == channel_id:
                    return channel
        return None

    def get_role(self, role_id):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    def add_role(self, name):
        role = FakeRole(name)
        self.roles.append(role)
        return role


class FakeMember:
    _next_id = 7000

    def __init__(self, guild, name, roles=()):
        self.id = FakeMember._next_id
        FakeMember._next_id += 1
        self.guild = guild
        self.name = name
        self.mention = f"<@{self.id}>"
        self.roles = list(roles)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STATE = "Lagos"


def make_setup(player_row, state_location_channels=None, reset_role_id=None):
    """Build a (cog, guild, calls) tuple wired against a stubbed DB."""
    calls = {
        "get_player": [],
        "ensure_player_exists": [],
        "record_arrival": [],
        "get_state_location_channels": [],
        "reset_player_on_leave": [],
    }

    async def fake_get_player(discord_id):
        calls["get_player"].append(discord_id)
        return player_row

    async def fake_ensure_player_exists(discord_id):
        calls["ensure_player_exists"].append(discord_id)

    async def fake_record_arrival(discord_id, state):
        calls["record_arrival"].append((discord_id, state))

    async def fake_get_state_location_channels(state):
        calls["get_state_location_channels"].append(state)
        return state_location_channels or []

    async def fake_reset_player_on_leave(discord_id):
        calls["reset_player_on_leave"].append(discord_id)
        return reset_role_id

    database.get_player = fake_get_player
    database.ensure_player_exists = fake_ensure_player_exists
    database.record_arrival = fake_record_arrival
    database.get_state_location_channels = fake_get_state_location_channels
    database.reset_player_on_leave = fake_reset_player_on_leave

    guild = FakeGuild(STATE)
    cog = Onboarding(None)
    return cog, guild, calls


def location_row(channel):
    """A location row shape good enough for sync_location_permissions."""
    return {
        "id": 42,
        "parent_location_id": None,
        "channel_id": channel.id,
    }


@pytest.fixture
def db_stubs():
    import database as db_mod

    saved = {
        name: getattr(db_mod, name)
        for name in (
            "get_player",
            "ensure_player_exists",
            "record_arrival",
            "get_state_location_channels",
            "reset_player_on_leave",
        )
    }
    yield
    for name, fn in saved.items():
        setattr(db_mod, name, fn)


# ---------------------------------------------------------------------------
# on_member_join: the auto-assigned-on-join Arrival role path
# ---------------------------------------------------------------------------

def test_join_with_arrival_role_records_arrival_and_welcomes(db_stubs):
    """A rejoining member whose Arrival role was auto-assigned at join
    time must still get record_arrival + the welcome message. This is
    the 'left n joined, no welcome message nothing' regression: the
    role is already present in GUILD_MEMBER_ADD, so on_member_update
    never fires and only on_member_join can see it."""
    cog, guild, calls = make_setup(player_row=None)
    member = FakeMember(guild, "returner", roles=[guild.arrival_role])

    import asyncio
    asyncio.run(cog.on_member_join(member))

    assert calls["ensure_player_exists"] == [member.id]
    assert calls["record_arrival"] == [(member.id, STATE)]
    assert len(guild.arrival_terminal.sent) == 1
    assert f"{member.mention} has arrived in {STATE}" in guild.arrival_terminal.sent[0]


def test_join_without_arrival_role_is_ignored(db_stubs):
    cog, guild, calls = make_setup(player_row=None)
    member = FakeMember(guild, "wanderer")

    import asyncio
    asyncio.run(cog.on_member_join(member))

    assert calls["get_player"] == []
    assert calls["ensure_player_exists"] == []
    assert calls["record_arrival"] == []
    assert guild.arrival_terminal.sent == []


def test_join_with_already_arrived_player_is_skipped(db_stubs):
    """A member whose DB record is already progressed (e.g. role re-added
    by a moderator after full immigration) must not be re-processed."""
    player_row = {
        "discord_id": 999,
        "immigration_status": "immigrated",
        "current_state": STATE,
        "current_location_id": 42,
    }
    cog, guild, calls = make_setup(player_row)
    member = FakeMember(guild, "veteran", roles=[guild.arrival_role])

    import asyncio
    asyncio.run(cog.on_member_join(member))

    assert calls["get_player"] == [member.id]
    assert calls["ensure_player_exists"] == []
    assert calls["record_arrival"] == []
    assert guild.arrival_terminal.sent == []


# ---------------------------------------------------------------------------
# on_member_join also runs sync_location_permissions, so a rejoining
# player is read-only everywhere until they actually travel.
# ---------------------------------------------------------------------------

def test_join_arrival_syncs_writability_to_current_location(db_stubs):
    """A rejoining player lands at the immigration office: the post-arrival
    get_player returns their new location, so the sync grants send there
    (and nowhere else)."""
    office = FakeChannel("immigration-office")
    rows = [location_row(office)]
    player_row = {
        "discord_id": 999,
        "immigration_status": "unarrived",
        "current_state": STATE,
        "current_location_id": 42,  # office's location id (see location_row)
    }
    cog, guild, calls = make_setup(player_row, state_location_channels=rows)
    # sync_location_permissions resolves channels via guild.get_channel(),
    # which only walks categories -- hang the office under one.
    locations_category = FakeCategory(f"{STATE} Locations")
    locations_category.channels.append(office)
    guild.categories.append(locations_category)
    member = FakeMember(guild, "returner", roles=[guild.arrival_role])

    import asyncio
    asyncio.run(cog.on_member_join(member))

    assert office.permission_overwrites[member]["send_messages"] is True
    assert calls["get_state_location_channels"] == [STATE]


# ---------------------------------------------------------------------------
# on_member_update: the normal path (role added after join)
# ---------------------------------------------------------------------------

def test_member_update_with_new_arrival_role_handles_arrival(db_stubs):
    cog, guild, calls = make_setup(player_row=None)
    before = FakeMember(guild, "newcomer")
    after = FakeMember(guild, "newcomer", roles=[guild.arrival_role])

    import asyncio
    asyncio.run(cog.on_member_update(before, after))

    assert calls["record_arrival"] == [(after.id, STATE)]
    assert len(guild.arrival_terminal.sent) == 1


def test_member_update_without_new_roles_does_nothing(db_stubs):
    cog, guild, calls = make_setup(player_row=None)
    member = FakeMember(guild, "newcomer")

    import asyncio
    asyncio.run(cog.on_member_update(member, member))

    assert calls["get_player"] == []
    assert calls["record_arrival"] == []
    assert guild.arrival_terminal.sent == []


# ---------------------------------------------------------------------------
# on_member_remove: announce departure, reset the row, free the NIN role
# ---------------------------------------------------------------------------

def test_member_remove_announces_and_resets_player(db_stubs):
    player_row = {
        "discord_id": 999,
        "immigration_status": "immigrated",
        "current_state": STATE,
        "current_location_id": 42,
    }
    nin_role = FakeRole(f"{config.STATES[STATE]['state_code']}-NIN-001")
    cog, guild, calls = make_setup(player_row, reset_role_id=nin_role.id)
    guild.roles.append(nin_role)
    member = FakeMember(guild, "leaver")

    import asyncio
    asyncio.run(cog.on_member_remove(member))

    assert calls["reset_player_on_leave"] == [member.id]
    assert len(guild.arrival_terminal.sent) == 1
    assert "just left the Republic of Nigeria" in guild.arrival_terminal.sent[0]
    assert nin_role.deletes == ["Player left the server -- NIN freed for reuse"]


def test_member_remove_without_state_skips_announcement_but_resets(db_stubs):
    """A player who never picked a destination has no state to announce
    from: no farewell, but the row is still reset."""
    player_row = {
        "discord_id": 999,
        "immigration_status": "unarrived",
        "current_state": None,
        "current_location_id": None,
    }
    cog, guild, calls = make_setup(player_row)
    member = FakeMember(guild, "ghost")

    import asyncio
    asyncio.run(cog.on_member_remove(member))

    assert calls["reset_player_on_leave"] == [member.id]
    assert guild.arrival_terminal.sent == []