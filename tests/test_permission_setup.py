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
import logging
import types

import discord
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

    def __init__(self, name, position=0):
        self.id = FakeRole._next_id
        FakeRole._next_id += 1
        self.name = name
        self.position = position

    def __gt__(self, other):
        return self.position > other.position

    def __repr__(self):
        return f"<FakeRole {self.name}>"


class FakeChannel:
    _next_id = 6000

    def __init__(self, name):
        self.id = FakeChannel._next_id
        FakeChannel._next_id += 1
        self.name = name
        self.permission_overwrites = {}
        # targets whose set_permissions must 403 (a real server where the
        # bot lacks Manage Channels, or is ranked below the target)
        self.deny_targets = set()

    async def set_permissions(self, target, **kwargs):
        if target in self.deny_targets:
            response = types.SimpleNamespace(status=403, reason="Forbidden")
            raise discord.Forbidden(
                response, {"code": 50013, "message": "Missing Permissions"}
            )
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


class FakeMe:
    """guild.me -- the bot's own member object."""

    def __init__(self, top_role, manage_roles=True):
        self.id = 9001
        self.name = "RONbot"
        self.guild_permissions = types.SimpleNamespace(manage_roles=manage_roles)
        self.top_role = top_role


class FakeGuild:
    def __init__(self):
        self.roles = []
        self.categories = []
        self.default_role = FakeRole("@everyone")
        self._members = {}
        self._channels = {}
        self.name = f"{STATE} Roleplay"
        self.me = None
        self.owner_id = None

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

    def add_role(self, name, position=0):
        role = FakeRole(name, position=position)
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


def test_get_channel_ignores_voice_channels(db_stubs):
    """get_channel must never bind a location to a voice channel that
    reuses the same name -- a send_messages overwrite on a voice channel
    403s, and the join key points at the wrong channel."""
    guild = FakeGuild()
    border = guild.categories[0]
    original = border.channels[0]  # the seeded text "immigration-office"

    # Drop a voice channel with the same name in front of the text one.
    voice_ch = border.add_channel("immigration-office")
    voice_ch.type = discord.ChannelType.voice
    border.channels.remove(voice_ch)
    border.channels.insert(0, voice_ch)

    result = discord_utils.get_channel(guild, STATE, "BORDER & ENTRY", "immigration-office")
    assert result is original
    assert result is not voice_ch


def test_get_channel_returns_none_when_only_voice_channel_exists(db_stubs):
    """A location whose only same-named channel is a voice channel counts
    as missing -- it must not be silently bound to the voice channel."""
    guild = FakeGuild()
    border = guild.categories[0]
    border.channels.pop(0)  # drop the seeded text "immigration-office"

    voice_ch = border.add_channel("immigration-office")
    voice_ch.type = discord.ChannelType.voice

    assert discord_utils.get_channel(
        guild, STATE, "BORDER & ENTRY", "immigration-office"
    ) is None


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


def test_setup_denies_everyone_view_on_role_gated_channels_only(db_stubs):
    """Role-gated channels must get a View Channel deny on @everyone in the
    SAME PUT as the Send deny: set_permissions is a full-replace, so a
    lockdown that only sends send_messages=False wipes any existing
    view_channel deny on every startup (regression: 'all channels still
    visible after rejoin'). Non-gated channels must not gain a view
    overwrite -- their visibility is controlled by role membership alone."""
    guild = FakeGuild()
    # gate exactly one channel: refugee-camp (index 2 in LOCATION_ROWS)
    db_stubs["location_rows"][2]["role_gated"] = True

    asyncio.run(discord_utils.setup_permissions(guild))

    gated = guild.channels["refugee-camp"]
    gated_overwrite = gated.permission_overwrites[guild.default_role]
    assert gated_overwrite["view_channel"] is False
    assert gated_overwrite["send_messages"] is False

    for row in LOCATION_ROWS:
        if row["channel_name"] == "refugee-camp":
            continue
        channel = guild.channels[row["channel_name"]]
        overwrite = channel.permission_overwrites[guild.default_role]
        assert "view_channel" not in overwrite
        assert overwrite["send_messages"] is False


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


def test_sync_skips_stale_voice_channel_binding(db_stubs):
    """A location row whose channel_id still points at a voice channel
    (bound before get_channel learned to skip voice) must not receive
    per-member overwrites -- they can never succeed on a voice channel."""
    guild = FakeGuild()
    border = guild.categories[0]
    voice_ch = border.add_channel("immigration-office")
    voice_ch.type = discord.ChannelType.voice
    guild._channels[voice_ch.id] = voice_ch

    # Point location 1 (immigration-office) at the voice channel.
    for row in db_stubs["location_rows"]:
        if row["id"] == 1:
            row["channel_id"] = voice_ch.id

    at_office = _add_arrived_player(db_stubs, guild, 1)

    asyncio.run(discord_utils.sync_location_permissions(
        guild, at_office, STATE, 1))

    assert at_office not in voice_ch.permission_overwrites


# ---------------------------------------------------------------------------
# Forbidden (50013) fault tolerance
# ---------------------------------------------------------------------------

def test_setup_continues_when_staff_overwrite_rejected(db_stubs, caplog):
    """Discord 403s an overwrite when the bot ranks below the target role
    (or lacks Manage Channels). setup must skip the rejected ones, count
    them, and still land the rest -- never raise."""
    guild = FakeGuild()
    officer = guild.add_role(config.IMMIGRATION_OFFICER_ROLE_NAME)
    marshal = guild.add_role("Chief Immigration Marshal")
    for channel in guild.channels.values():
        channel.deny_targets.add(officer)

    with caplog.at_level(logging.WARNING):
        summary = asyncio.run(discord_utils.setup_permissions(guild))

    assert summary["denied"] == len(LOCATION_ROWS)
    for channel in guild.channels.values():
        assert officer not in channel.permission_overwrites
        assert channel.permission_overwrites[marshal]["send_messages"] is True
        assert channel.permission_overwrites[guild.default_role]["send_messages"] is False

    rejected = [r for r in caplog.records if "Overwrite REJECTED" in r.message]
    assert len(rejected) == len(LOCATION_ROWS)


def test_setup_continues_when_everyone_lockdown_rejected(db_stubs):
    """A rejected @everyone lockdown must not stop staff re-grants or the
    per-member resync from running."""
    guild = FakeGuild()
    for channel in guild.channels.values():
        channel.deny_targets.add(guild.default_role)
    player = guild.add_member("runner")
    db_stubs["player_rows"].append({
        "discord_id": player.id,
        "current_state": STATE,
        "current_location_id": 1,
    })

    summary = asyncio.run(discord_utils.setup_permissions(guild))

    assert summary["denied"] == len(LOCATION_ROWS)
    for channel in guild.channels.values():
        assert guild.default_role not in channel.permission_overwrites
    office = guild.channels["immigration-office"]
    assert office.permission_overwrites[player]["send_messages"] is True
    assert summary["resynced"] == 1


def test_sync_swallowed_forbidden_member_overwrite(db_stubs):
    """A rejected per-member overwrite during a travel sync must not raise
    -- the other channels of the same sync still get their overwrites."""
    guild = FakeGuild()
    player = guild.add_member("runner")
    for row in db_stubs["location_rows"]:
        row["channel_id"] = guild.channels[row["channel_name"]].id
    guild.channels["banking-hall"].deny_targets.add(player)

    asyncio.run(discord_utils.sync_location_permissions(guild, player, STATE, 1))

    assert player not in guild.channels["banking-hall"].permission_overwrites
    assert guild.channels["immigration-office"].permission_overwrites[player]["send_messages"] is True


def test_setup_warns_when_bot_lacks_manage_roles(db_stubs, caplog):
    """Missing Manage Roles is not fixable from code -- the warning must
    tell the operator exactly where to click."""
    guild = FakeGuild()
    guild.me = FakeMe(FakeRole("@bot"), manage_roles=False)

    with caplog.at_level(logging.WARNING):
        asyncio.run(discord_utils.setup_permissions(guild))

    records = [r for r in caplog.records if "NO Manage Roles" in r.message]
    assert len(records) == 1


def test_setup_warns_when_staff_role_ranks_above_bot(db_stubs, caplog):
    """A role above the bot's top role guarantees 50013 on every overwrite
    targeting it (staff re-grant AND per-player sync) -- the one consolidated
    warning must name the offending role."""
    guild = FakeGuild()
    guild.me = FakeMe(FakeRole("@bot", position=1))
    guild.add_role(config.IMMIGRATION_OFFICER_ROLE_NAME, position=5)
    guild.add_role("Delta", position=3)

    with caplog.at_level(logging.WARNING):
        asyncio.run(discord_utils.setup_permissions(guild))

    records = [r for r in caplog.records if "rank ABOVE" in r.message]
    assert len(records) == 1
    assert config.IMMIGRATION_OFFICER_ROLE_NAME in records[0].message
    assert "Delta" in records[0].message


def test_setup_skips_owner_member_overwrites(db_stubs, caplog):
    """An explicit member overwrite on the server owner always 50013s (the
    owner's implicit rank sits above every role, and the owner ignores
    channel denies anyway) -- the sync must skip the owner instead of
    logging a fake per-channel failure."""
    guild = FakeGuild()
    owner = guild.add_member("server-owner")
    guild.owner_id = owner.id
    for channel in guild.channels.values():
        channel.deny_targets.add(owner)  # would 50013 if attempted
    db_stubs["player_rows"].append({
        "discord_id": owner.id,
        "current_state": STATE,
        "current_location_id": 1,
    })

    with caplog.at_level(logging.WARNING):
        summary = asyncio.run(discord_utils.setup_permissions(guild))

    assert summary["denied"] == 0
    assert summary["resynced"] == 1
    for channel in guild.channels.values():
        assert owner not in channel.permission_overwrites


def test_log_role_ladder_marks_bot_top_role(db_stubs, caplog):
    """The ladder summary is the startup ground truth: one line with the
    bot's top role, the MANAGE_ROLES bit, and exactly which roles rank above
    the bot -- the only roles Discord's 50013 check can reject. The old
    one-line-per-role dump (140+ lines per deploy) is gone."""
    guild = FakeGuild()
    bot_role = FakeRole("@bot", position=9)
    guild.me = FakeMe(bot_role, manage_roles=True)
    guild.roles.append(bot_role)  # real guild.roles includes the bot's own role
    guild.add_role("Delta", position=3)
    guild.add_role("@everyone", position=0)

    with caplog.at_level(logging.INFO):
        discord_utils.log_role_ladder(guild)

    ladder = [r.message for r in caplog.records if "Role ladder" in r.message]
    assert len(ladder) == 1
    header = ladder[0]
    assert "MANAGE_ROLES: ON" in header
    assert "top role '@bot'" in header
    assert "3 roles" in header
    assert "no roles above bot" in header
    # No per-role lines leak into the log any more.
    assert [r.message for r in caplog.records if "pos=" in r.message] == []


def test_log_role_ladder_flags_roles_above_bot(db_stubs, caplog):
    """Roles ranked above the bot's top role are named in the summary line --
    that list is the 50013 ground truth, so it must stay in the log."""
    guild = FakeGuild()
    bot_role = FakeRole("@bot", position=4)
    guild.me = FakeMe(bot_role, manage_roles=True)
    guild.roles.append(bot_role)  # real guild.roles includes the bot's own role
    guild.add_role("Owner", position=5)
    guild.add_role("Delta", position=3)

    with caplog.at_level(logging.INFO):
        discord_utils.log_role_ladder(guild)

    header = [r.message for r in caplog.records if "Role ladder" in r.message]
    assert len(header) == 1
    assert "role(s) ABOVE bot: 'Owner' (pos=5)" in header[0]
    assert "'Delta'" not in header[0]


def test_log_role_ladder_flags_missing_manage_roles(db_stubs, caplog):
    guild = FakeGuild()
    guild.me = FakeMe(FakeRole("@bot"), manage_roles=False)
    guild.add_role("Delta", position=3)

    with caplog.at_level(logging.INFO):
        discord_utils.log_role_ladder(guild)

    header = [r.message for r in caplog.records if "Role ladder" in r.message]
    assert len(header) == 1
    assert "MANAGE_ROLES: OFF" in header[0]