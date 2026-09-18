"""Unit tests for the startup permission setup (setup_permissions).

setup_permissions is the single code path that joins the `locations`
table to the live Discord channels (BIND), denies @everyone send
(LOCKDOWN), re-grants staff, and repairs per-member overwrites for every
arrived player (RESYNC). It runs once per bot startup (on_ready).

Only the database boundary is stubbed, mirroring
test_immigration_commands.py: the real discord_utils / permissions code
runs against fake Discord objects that record the overwrites they were
told to set.
"""

import asyncio

import pytest

import config
import database
import discord_utils


STATE = "Lagos"

# (state, category, channel_name, parent) -- a slice of locations_seed.sql
# covering one parent + two children (refugee-camp nests under
# immigration-office in the seed) and one banking channel.
LOCATION_ROWS = [
    {"id": 1, "state": STATE, "category": "BORDER & ENTRY",
     "channel_name": "immigration-office", "parent": None},
    {"id": 2, "state": STATE, "category": "BORDER & ENTRY",
     "channel_name": "front-desk", "parent": None},
    {"id": 3, "state": STATE, "category": "BORDER & ENTRY",
     "channel_name": "refugee-camp", "parent": 1},
    {"id": 4, "state": STATE, "category": "BANK PLC",
     "channel_name": "banking-hall", "parent": None},
]


# ---------------------------------------------------------------------------
# Minimal stand-ins for the Discord objects setup_permissions touches
# ---------------------------------------------------------------------------

class FakeRole:
    _next_id = 5000

    def __init__(self, name):
        self.id = FakeRole._next_id
        FakeRole._next_id += 1
        self.name = name

    def __repr__(self):
        return f"<FakeRole {self.name}>"


class FakeChannel:
    _next_id = 6000

    def __init__(self, name):
        self.id = FakeChannel._next_id
        FakeChannel._next_id += 1
        self.name = name
        self.permission_overwrites = {}

    async def set_permissions(self, target, **kwargs):
        self.permission_overwrites[target] = kwargs


class FakeCategory:
    def __init__(self, name):
        self.name = name
        self.channels = []

    def add_channel(self, name):
        ch = FakeChannel(name)
        self.channels.append(ch)
        return ch


class FakeMember:
    _next_id = 7000

    def __init__(self, guild, name):
        self.id = FakeMember._next_id
        FakeMember._next_id += 1
        self.guild = guild
        self.name = name
        self.mention = f"<@{self.id}>"
        self.nick = None
        self.roles = []


class FakeGuild:
    def __init__(self):
        self.roles = []
        self.categories = []
        self.default_role = FakeRole("@everyone")
        self._members = {}
        self._channels = {}

        border = FakeCategory(f"{STATE} Border & Entry")
        self.bank = FakeCategory(f"{STATE} Bank PLC")
        self.categories = [border, self.bank]

        self.channels = {
            "immigration-office": border.add_channel("immigration-office"),
            "front-desk": border.add_channel("front-desk"),
            "refugee-camp": border.add_channel("refugee-camp"),
            "banking-hall": self.bank.add_channel("banking-hall"),
        }
        for ch in self.channels.values():
            self._channels[ch.id] = ch

    def add_role(self, name):
        role = FakeRole(name)
        self.roles.append(role)
        return role

    def add_member(self, name):
        member = FakeMember(self, name)
        self._members[member.id] = member
        return member

    def get_member(self, discord_id):
        return self._members.get(discord_id)

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_stubs():
    """Stub the database functions setup_permissions / sync_location_permissions
    call, recording every write; restore the real functions afterwards."""
    import database as db_mod

    calls = {
        "get_all_locations": [],
        "bind_location_channel": [],
        "get_players_needing_sync": [],
        "get_state_location_channels": [],
    }

    location_rows = [dict(row, is_voice=False, role_gated=False, channel_id=None)
                     for row in LOCATION_ROWS]
    player_rows = []

    async def fake_get_all_locations():
        calls["get_all_locations"].append(None)
        return [dict(row) for row in location_rows]

    async def fake_bind_location_channel(location_id, channel_id):
        calls["bind_location_channel"].append((location_id, channel_id))
        for row in location_rows:
            if row["id"] == location_id:
                row["channel_id"] = channel_id

    async def fake_get_players_needing_sync():
        calls["get_players_needing_sync"].append(None)
        return [dict(row) for row in player_rows]

    async def fake_get_state_location_channels(state):
        calls["get_state_location_channels"].append(state)
        rows = []
        for row in location_rows:
            if row["state"] == state:
                rows.append({
                    "id": row["id"],
                    "channel_id": row["channel_id"],
                    "parent_location_id": row["parent"],
                })
        return rows

    database.get_all_locations = fake_get_all_locations
    database.bind_location_channel = fake_bind_location_channel
    database.get_players_needing_sync = fake_get_players_needing_sync
    database.get_state_location_channels = fake_get_state_location_channels

    holder = {"calls": calls, "location_rows": location_rows,
              "player_rows": player_rows}
    yield holder

    for name in ("get_all_locations", "bind_location_channel",
                 "get_players_needing_sync", "get_state_location_channels"):
        delattr(db_mod, name)


# ---------------------------------------------------------------------------
# BIND
# ---------------------------------------------------------------------------

def test_setup_binds_unbound_location_channels(db_stubs):
    guild = FakeGuild()

    summary = asyncio.run(discord_utils.setup_permissions(guild))

    expected = {
        (row["id"], guild.channels[row["channel_name"]].id)
        for row in LOCATION_ROWS
    }
    assert set(db_stubs["calls"]["bind_location_channel"]) == expected
    assert summary["bound"] == len(LOCATION_ROWS)
    assert summary["channels"] == len(LOCATION_ROWS)
    assert summary["missing"] == 0


def test_setup_is_idempotent_on_second_run(db_stubs):
    guild = FakeGuild()

    first = asyncio.run(discord_utils.setup_permissions(guild))
    second = asyncio.run(discord_utils.setup_permissions(guild))

    assert first["bound"] == len(LOCATION_ROWS)
    # Every row already carries the right channel_id, so nothing re-binds.
    assert second["bound"] == 0
    assert second["channels"] == len(LOCATION_ROWS)


def test_setup_counts_missing_channels_without_crashing(db_stubs):
    guild = FakeGuild()
    db_stubs["location_rows"].append({
        "id": 99, "state": STATE, "category": "GHOST", "channel_name": "void",
        "is_voice": False, "role_gated": False, "channel_id": None,
        "parent": None,
    })

    summary = asyncio.run(discord_utils.setup_permissions(guild))

    assert summary["missing"] == 1
    assert summary["channels"] == len(LOCATION_ROWS)
    assert all(lid != 99 for lid, _ in db_stubs["calls"]["bind_location_channel"])


# ---------------------------------------------------------------------------
# LOCKDOWN
# ---------------------------------------------------------------------------

def test_setup_denies_everyone_send_on_all_location_channels(db_stubs):
    guild = FakeGuild()

    asyncio.run(discord_utils.setup_permissions(guild))

    for row in LOCATION_ROWS:
        channel = guild.channels[row["channel_name"]]
        overwrite = channel.permission_overwrites[guild.default_role]
        assert overwrite["send_messages"] is False
        assert "RONbot startup" in overwrite["reason"]


# ---------------------------------------------------------------------------
# REGRANT
# ---------------------------------------------------------------------------

def test_setup_grants_staff_roles_send_on_all_location_channels(db_stubs):
    guild = FakeGuild()
    officer = guild.add_role(config.IMMIGRATION_OFFICER_ROLE_NAME)
    marshal = guild.add_role("Chief Immigration Marshal")

    asyncio.run(discord_utils.setup_permissions(guild))

    for role in (officer, marshal):
        for row in LOCATION_ROWS:
            channel = guild.channels[row["channel_name"]]
            assert channel.permission_overwrites[role]["send_messages"] is True


def test_setup_skips_missing_staff_roles(db_stubs):
    """A server where the marshal role was never created must not crash."""
    guild = FakeGuild()  # no staff roles added at all
    guild.add_role(config.IMMIGRATION_OFFICER_ROLE_NAME)

    summary = asyncio.run(discord_utils.setup_permissions(guild))

    assert summary["channels"] == len(LOCATION_ROWS)


# ---------------------------------------------------------------------------
# RESYNC
# ---------------------------------------------------------------------------

def _add_arrived_player(db_stubs, guild, location_id):
    member = guild.add_member(f"player-{location_id}")
    db_stubs["player_rows"].append({
        "discord_id": member.id,
        "current_state": STATE,
        "current_location_id": location_id,
    })
    return member


def test_setup_resyncs_arrived_players(db_stubs):
    guild = FakeGuild()
    # id 1 = immigration-office; its child refugee-camp (id 3) is writable
    # too via the parent_location_id chain.
    at_office = _add_arrived_player(db_stubs, guild, 1)
    at_bank = _add_arrived_player(db_stubs, guild, 4)

    summary = asyncio.run(discord_utils.setup_permissions(guild))

    assert summary["resynced"] == 2
    assert summary["skipped"] == 0

    office = guild.channels["immigration-office"]
    camp = guild.channels["refugee-camp"]
    bank = guild.channels["banking-hall"]

    # Writable exactly where the player stands (plus sublocations).
    assert office.permission_overwrites[at_office]["send_messages"] is True
    assert camp.permission_overwrites[at_office]["send_messages"] is True
    assert bank.permission_overwrites[at_office]["send_messages"] is False

    assert bank.permission_overwrites[at_bank]["send_messages"] is True
    assert office.permission_overwrites[at_bank]["send_messages"] is False
    assert camp.permission_overwrites[at_bank]["send_messages"] is False


def test_setup_resync_in_transit_player_is_readonly_everywhere(db_stubs):
    """A player mid-transit (current_location_id=None) gets an explicit
    send=False on every channel of their state -- the 'read-only
    everywhere' case from the sync docstring."""
    guild = FakeGuild()
    in_transit = guild.add_member("traveler")
    db_stubs["player_rows"].append({
        "discord_id": in_transit.id,
        "current_state": STATE,
        "current_location_id": None,
    })

    asyncio.run(discord_utils.setup_permissions(guild))

    for row in LOCATION_ROWS:
        channel = guild.channels[row["channel_name"]]
        assert channel.permission_overwrites[in_transit]["send_messages"] is False


def test_setup_skips_players_not_in_guild(db_stubs):
    guild = FakeGuild()
    # A discord_id with no corresponding member (left the server, or never
    # synced into the cache).
    db_stubs["player_rows"].append({
        "discord_id": 424242,
        "current_state": STATE,
        "current_location_id": 1,
    })

    summary = asyncio.run(discord_utils.setup_permissions(guild))

    assert summary["skipped"] == 1
    assert summary["resynced"] == 0