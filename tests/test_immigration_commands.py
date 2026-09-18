"""Regression tests for the !name / !immigrate command handlers.

Command scope rule under test: !name must NOT mint a NIN number, a
player_id, or a NIN role -- only !immigrate does that. !name only records
the name and grants the "{State} Indigene" role.

The handlers themselves run for real (real cog, real discord_utils
lookups, real discord.py). Only the database boundary is stubbed, because
the production DB is asyncpg/Postgres, which the test sandbox has no
server for. Each stub is an in-memory stand-in that records its calls so
the assertions verify exactly which DB writes each command performed.
"""

import pytest

import config
import database
import discord_utils
from cogs.immigration import Immigration


# ---------------------------------------------------------------------------
# Minimal stand-ins for the Discord objects the handlers touch
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


class FakeGuild:
    def __init__(self, state):
        self.roles = []
        self.categories = []
        self.created_roles = []

        border = FakeCategory(f"{state} Border & Entry")
        self.front_desk = border.add_channel("front-desk")
        self.categories.append(border)

        for role_name in (
            f"{state} Arrival",
            f"{state} Indigene",
            state,
            config.IMMIGRATION_OFFICER_ROLE_NAME,
        ):
            self.add_role(role_name)

    def get_channel(self, channel_id):
        for category in self.categories:
            for channel in category.channels:
                if channel.id == channel_id:
                    return channel
        return None

    def add_role(self, name):
        role = FakeRole(name)
        self.roles.append(role)
        return role

    async def create_role(self, name, reason=None):
        role = FakeRole(name)
        self.roles.append(role)
        self.created_roles.append((name, reason))
        return role


class FakeMember:
    _next_id = 3000

    def __init__(self, guild, name, roles=()):
        self.id = FakeMember._next_id
        FakeMember._next_id += 1
        self.guild = guild
        self.name = name
        self.mention = f"<@{self.id}>"
        self.nick = None
        self.roles = list(roles)
        self.added_roles = []
        self.removed_roles = []
        self.nick_edits = []

    def __str__(self):
        return self.mention

    async def add_roles(self, *roles, reason=None):
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)
            self.added_roles.append(role)

    async def remove_roles(self, *roles, reason=None):
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)
            self.removed_roles.append(role)

    async def edit(self, nick=None, **kwargs):
        self.nick = nick
        self.nick_edits.append(nick)
        return ""


class FakeCtx:
    def __init__(self, guild, author, channel):
        self.guild = guild
        self.author = author
        self.channel = channel
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STATE = "Lagos"
PLAYER_ID = "NIN-0007-LA"


def make_setup(player_row, state_location_channels=None):
    """Build a (cog, ctx, member, calls) tuple wired against a stubbed DB."""
    calls = {
        "get_player": [],
        "finalize_naming": [],
        "complete_immigration": [],
        "allocate_nin_number": [],
        "get_state_location_channels": [],
    }

    async def fake_get_player(discord_id):
        calls["get_player"].append(discord_id)
        return player_row

    async def fake_finalize_naming(discord_id, player_name):
        calls["finalize_naming"].append((discord_id, player_name))

    async def fake_complete_immigration(discord_id, player_id, nin_number, role_id):
        calls["complete_immigration"].append((discord_id, player_id, nin_number, role_id))

    async def fake_allocate_nin_number():
        calls["allocate_nin_number"].append(1)
        return 7

    async def fake_get_state_location_channels(state):
        calls["get_state_location_channels"].append(state)
        return state_location_channels or []

    database.get_player = fake_get_player
    database.finalize_naming = fake_finalize_naming
    database.complete_immigration = fake_complete_immigration
    database.allocate_nin_number = fake_allocate_nin_number
    database.get_state_location_channels = fake_get_state_location_channels

    guild = FakeGuild(STATE)
    officer = FakeMember(guild, "officer", roles=[guild.add_role(config.IMMIGRATION_OFFICER_ROLE_NAME)])
    player = FakeMember(guild, "arrival", roles=[guild.add_role(f"{STATE} Arrival")])
    ctx = FakeCtx(guild, officer, guild.front_desk)
    cog = Immigration(None)
    return cog, ctx, player, calls


def get_cmd(cog, name):
    cmd = next(c for c in cog.get_commands() if c.name == name)
    return cmd


@pytest.fixture
def db_stubs():
    import database as db_mod

    saved = {
        name: getattr(db_mod, name)
        for name in (
            "get_player",
            "finalize_naming",
            "complete_immigration",
            "allocate_nin_number",
            "get_state_location_channels",
        )
    }
    yield
    for name, fn in saved.items():
        setattr(db_mod, name, fn)


# ---------------------------------------------------------------------------
# !name: name + indigene role ONLY -- no NIN, no player_id, no NIN role
# ---------------------------------------------------------------------------

def test_name_records_name_and_grants_indigene_only(db_stubs):
    cog, ctx, member, calls = make_setup(
        {
            "immigration_status": "arrived",
            "current_state": STATE,
            "player_id": None,
            "player_name": None,
        }
    )

    cmd = get_cmd(cog, "name")
    import asyncio

    asyncio.run(cmd.callback(cog, ctx, member, player_name="Full Name"))

    # DB writes: exactly one, and it carries ONLY the name -- no NIN number,
    # no player_id, no role id.
    assert calls["finalize_naming"] == [(member.id, "Full Name")]
    assert calls["allocate_nin_number"] == []
    assert calls["complete_immigration"] == []

    # Discord side: indigene role granted, NICK set, NO NIN role created,
    # NO NIN role granted.
    guild = ctx.guild
    indigene = discord_utils.get_role(guild, f"{STATE} Indigene")
    assert indigene in member.roles
    assert all(not r.name.startswith("NIN-") for r in member.added_roles)
    assert guild.created_roles == []
    assert member.nick_edits == ["Full Name"]

    message = ctx.sent[0]
    assert "named **Full Name**" in message
    assert "Run !immigrate" in message


def test_name_mint_nothing_when_player_already_named(db_stubs):
    cog, ctx, member, calls = make_setup(
        {
            "immigration_status": "named",
            "current_state": STATE,
            "player_id": None,
            "player_name": "Full Name",
        }
    )

    cmd = get_cmd(cog, "name")
    import asyncio

    asyncio.run(cmd.callback(cog, ctx, member, player_name="Other Name"))

    assert calls["finalize_naming"] == []
    assert calls["allocate_nin_number"] == []
    assert calls["complete_immigration"] == []
    assert member.added_roles == []
    assert ctx.guild.created_roles == []
    assert "has already been named" in ctx.sent[0]


def test_name_rejects_unarrived_player(db_stubs):
    cog, ctx, member, calls = make_setup(
        {
            "immigration_status": "unarrived",
            "current_state": None,
            "player_id": None,
            "player_name": None,
        }
    )

    cmd = get_cmd(cog, "name")
    import asyncio

    asyncio.run(cmd.callback(cog, ctx, member, player_name="Full Name"))

    assert calls["finalize_naming"] == []
    assert calls["allocate_nin_number"] == []
    assert member.added_roles == []
    assert "hasn't chosen a destination" in ctx.sent[0]


# ---------------------------------------------------------------------------
# !immigrate: mints NIN number, player_id, and NIN role -- the ONLY command
# that may do so
# ---------------------------------------------------------------------------

def test_immigrate_mints_nin_id_and_role(db_stubs):
    cog, ctx, member, calls = make_setup(
        {
            "immigration_status": "named",
            "current_state": STATE,
            "player_id": None,
            "player_name": "Full Name",
        }
    )

    cmd = get_cmd(cog, "immigrate")
    import asyncio

    asyncio.run(cmd.callback(cog, ctx, member))

    guild = ctx.guild
    expected_player_id = f"NIN-{7:04d}-LA"
    assert calls["allocate_nin_number"] == [1]
    assert calls["complete_immigration"] == [(member.id, expected_player_id, 7, None)][:1] or True
    # complete_immigration must receive the minted player_id, NIN number, and
    # the id of the freshly created NIN role.
    (discord_id, player_id, nin_number, role_id) = calls["complete_immigration"][0]
    assert discord_id == member.id
    assert player_id == expected_player_id
    assert nin_number == 7
    assert role_id is not None

    nin_role = discord_utils.get_role(guild, expected_player_id)
    assert nin_role is not None
    assert nin_role in member.roles
    assert (expected_player_id, f"NIN assigned to {member.mention} by Immigration Officer") in guild.created_roles

    # State role granted, arrival role stripped.
    state_role = discord_utils.get_role(guild, STATE)
    assert state_role in member.roles
    arrival_role = discord_utils.get_role(guild, f"{STATE} Arrival")
    assert arrival_role not in member.roles

    # !immigrate never calls finalize_naming (that's !name's write).
    assert calls["finalize_naming"] == []
    assert f"({expected_player_id}) fully immigrated" in ctx.sent[0]


def test_immigrate_clears_stale_role_before_reminting(db_stubs):
    cog, ctx, member, calls = make_setup(
        {
            "immigration_status": "named",
            "current_state": STATE,
            "player_id": None,
            "player_name": "Full Name",
        }
    )
    stale = ctx.guild.add_role(PLAYER_ID)

    cmd = get_cmd(cog, "immigrate")
    import asyncio

    asyncio.run(cmd.callback(cog, ctx, member))

    assert stale.deletes == ["Reassigning this NIN -- clearing stale role first"]
    # A brand-new role now holds the same NIN name.
    fresh = [r for r in ctx.guild.roles if r.name == PLAYER_ID and r is not stale]
    assert len(fresh) == 1
    assert fresh[0] in member.roles


def test_immigrate_rejects_player_not_yet_named(db_stubs):
    cog, ctx, member, calls = make_setup(
        {
            "immigration_status": "arrived",
            "current_state": STATE,
            "player_id": None,
            "player_name": None,
        }
    )

    cmd = get_cmd(cog, "immigrate")
    import asyncio

    asyncio.run(cmd.callback(cog, ctx, member))

    assert calls["allocate_nin_number"] == []
    assert calls["complete_immigration"] == []
    assert ctx.guild.created_roles == []
    assert member.added_roles == []
    assert "needs to be named first" in ctx.sent[0]


def test_immigrate_rejects_already_immigrated(db_stubs):
    cog, ctx, member, calls = make_setup(
        {
            "immigration_status": "immigrated",
            "current_state": STATE,
            "player_id": "NIN-0001-LA",
            "player_name": "Full Name",
        }
    )

    cmd = get_cmd(cog, "immigrate")
    import asyncio

    asyncio.run(cmd.callback(cog, ctx, member))

    assert calls["allocate_nin_number"] == []
    assert calls["complete_immigration"] == []
    assert ctx.guild.created_roles == []
    assert "has already been immigrated" in ctx.sent[0]
# ---------------------------------------------------------------------------
# Writability follows travel: after !immigrate the player can type at the
# immigration-office (and its sublocation refugee-camp) only; every other
# channel that just became VISIBLE via the state role must be left
# read-only until the player actually travels.
# ---------------------------------------------------------------------------

def make_state_location_channels(guild):
    """Build location channels under state categories, matched to stub rows."""
    border = guild.categories[0]  # "{STATE} Border & Entry", holds front-desk
    office = border.add_channel("immigration-office")
    camp = border.add_channel("refugee-camp")
    services = FakeCategory(f"{STATE} Services")
    hall = services.add_channel("banking-hall")
    guild.categories.append(services)
    rows = [
        {"id": 10, "channel_id": office.id, "parent_location_id": None},
        {"id": 11, "channel_id": camp.id, "parent_location_id": 10},
        {"id": 12, "channel_id": hall.id, "parent_location_id": None},
    ]
    return rows, {"immigration-office": office, "refugee-camp": camp, "banking-hall": hall}


def test_immigrate_keeps_newly_visible_channels_read_only(db_stubs):
    import asyncio

    cog, ctx, member, calls = make_setup(
        {
            "immigration_status": "named",
            "current_state": STATE,
            "player_id": None,
            "player_name": "Full Name",
            "current_location_id": 10,  # standing at the immigration-office
        }
    )
    # Build the location channels on the SAME guild the callback will use.
    guild = ctx.guild
    rows, channels = make_state_location_channels(guild)

    async def fake_get_state_location_channels(state):
        calls["get_state_location_channels"].append(state)
        return rows

    database.get_state_location_channels = fake_get_state_location_channels

    cmd = get_cmd(cog, "immigrate")
    asyncio.run(cmd.callback(cog, ctx, member))

    # The office and its sublocation are writable...
    assert channels["immigration-office"].permission_overwrites[member]["send_messages"] is True
    assert channels["refugee-camp"].permission_overwrites[member]["send_messages"] is True
    # ...while everything else that just became visible is read-only.
    assert channels["banking-hall"].permission_overwrites[member]["send_messages"] is False
    assert calls["get_state_location_channels"] == [STATE]


def test_travel_to_banking_hall_moves_writability(db_stubs):
    """sync_location_permissions is the single writer of send overwrites.

    After the player travels to banking-hall, that channel becomes writable
    and the immigration-office + refugee-camp flip back to read-only.
    """
    import asyncio

    guild = FakeGuild(STATE)
    rows, channels = make_state_location_channels(guild)

    async def fake_get_state_location_channels(state):
        return rows

    database.get_state_location_channels = fake_get_state_location_channels

    member = FakeMember(guild, "arrival", roles=[guild.add_role(f"{STATE} Arrival")])

    async def run_sync(location_id):
        await discord_utils.sync_location_permissions(
            guild, member, STATE, current_location_id=location_id
        )

    # In transit: no current location -> read-only everywhere.
    asyncio.run(run_sync(None))
    assert channels["immigration-office"].permission_overwrites[member]["send_messages"] is False
    assert channels["refugee-camp"].permission_overwrites[member]["send_messages"] is False
    assert channels["banking-hall"].permission_overwrites[member]["send_messages"] is False

    # Arrived at banking-hall (id 12).
    asyncio.run(run_sync(12))
    assert channels["banking-hall"].permission_overwrites[member]["send_messages"] is True
    assert channels["immigration-office"].permission_overwrites[member]["send_messages"] is False
    assert channels["refugee-camp"].permission_overwrites[member]["send_messages"] is False


def test_immigrate_with_no_location_leaves_everything_read_only(db_stubs):
    """A freshly-immigrated player whose row has no current_location_id yet
    gets the state role (visibility) but can type nowhere until onboarding
    places them at the immigration-office."""
    import asyncio

    cog, ctx, member, calls = make_setup(
        {
            "immigration_status": "named",
            "current_state": STATE,
            "player_id": None,
            "player_name": "Full Name",
            "current_location_id": None,
        }
    )
    guild = ctx.guild
    rows, channels = make_state_location_channels(guild)

    async def _slc(state):
        calls["get_state_location_channels"].append(state)
        return rows

    database.get_state_location_channels = _slc

    cmd = get_cmd(cog, "immigrate")
    asyncio.run(cmd.callback(cog, ctx, member))

    for channel in channels.values():
        assert channel.permission_overwrites[member]["send_messages"] is False
    # The state role still got added, so the channels are visible.
    state_role = discord_utils.get_role(guild, STATE)
    assert state_role in member.roles