import os
import pathlib
import asyncpg

import config

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Create the connection pool and make sure the schema exists.
    Call this once, in on_ready or before the bot logs in.

    Setting WIPEDB=1 in the service environment wipes the entire public
    schema right before the schema+seeds below rebuild it -- a full reset
    to a fresh database. Remove the env var afterwards, or the DB will be
    wiped again on every restart."""
    global _pool
    if os.getenv("WIPEDB") == "1":
        conn = await asyncpg.connect(dsn=config.DATABASE_URL)
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
        print("WIPEDB=1: dropped and recreated schema public -- rebuilding from scratch")
        await conn.close()

    _pool = await asyncpg.create_pool(dsn=config.DATABASE_URL)
    await _rebuild_schema_and_seeds(_pool)


async def reset_database() -> None:
    """Full factory reset behind !resetdatabase: drop the entire public
    schema and rebuild it from schema.sql + seeds -- the same reset WIPEDB=1
    performs at startup, but triggerable live from Discord. Every player row,
    bank account, vehicle, card, trip, and pending request is gone; the world
    (locations, roles, routes, state economy) comes back exactly as the seed
    files define it, so the next player to arrive starts completely fresh."""
    global _pool

    conn = await asyncpg.connect(dsn=config.DATABASE_URL)
    try:
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
    finally:
        await conn.close()

    # Pool connections may hold cached statement plans against the pre-drop
    # tables, so close the pool and build a fresh one rather than reusing a
    # possibly-stale connection.
    if _pool is not None:
        await _pool.close()
    _pool = await asyncpg.create_pool(dsn=config.DATABASE_URL)
    await _rebuild_schema_and_seeds(_pool)


def _sql_file(name: str) -> pathlib.Path:
    return pathlib.Path(__file__).parent / name


async def _rebuild_schema_and_seeds(p: asyncpg.Pool) -> None:
    """Apply schema.sql, the numbered migrations, the subsystem schemas, and
    the seed files in the exact order the bot has always used. Shared by
    first-time startup (init_pool) and !resetdatabase (reset_database) so
    the two paths can never drift apart."""
    schema_path = _sql_file("schema.sql")
    if schema_path.exists():
        async with p.acquire() as conn:
            await conn.execute(schema_path.read_text())

    # One-time (but safe-to-repeat) fixes for schema changes made after this
    # DB was first created -- see migration_001.sql's own header. Must run
    # right after schema.sql, before anything below that depends on the
    # fixed shape (location_roles_seed.sql needs location_roles' group_id
    # column to exist).
    migration_path = _sql_file("migration_001.sql")
    if migration_path.exists():
        async with p.acquire() as conn:
            await conn.execute(migration_path.read_text())

    migration_002_path = _sql_file("migration_002.sql")
    if migration_002_path.exists():
        async with p.acquire() as conn:
            await conn.execute(migration_002_path.read_text())

    # Location hierarchy (parent_location_id) + wiring refugee-camp under
    # immigration-office. See migration_003.sql's own header.
    migration_003_path = _sql_file("migration_003.sql")
    if migration_003_path.exists():
        async with p.acquire() as conn:
            await conn.execute(migration_003_path.read_text())

    transport_schema_path = _sql_file("schema_transportation.sql")
    if transport_schema_path.exists():
        async with p.acquire() as conn:
            await conn.execute(transport_schema_path.read_text())

    seed_path = _sql_file("locations_seed.sql")
    if seed_path.exists():
        seed_sql = seed_path.read_text()
        async with p.acquire() as conn:
            await conn.execute(seed_sql)

    # roles_seed.sql / location_roles_seed.sql -- both auto-run now (used to
    # require running by hand, which is exactly the kind of step that's easy
    # to forget after just editing the file and redeploying). Order matters:
    # both depend on locations_seed.sql above already having run, and
    # location_roles_seed.sql additionally depends on roles_seed.sql.
    roles_seed_path = _sql_file("roles_seed.sql")
    if roles_seed_path.exists():
        async with p.acquire() as conn:
            await conn.execute(roles_seed_path.read_text())

    location_roles_seed_path = _sql_file("location_roles_seed.sql")
    if location_roles_seed_path.exists():
        async with p.acquire() as conn:
            await conn.execute(location_roles_seed_path.read_text())

    # zones_routes_seed.sql depends on locations_seed.sql too (zone_categories
    # don't reference locations directly, but keeping the order consistent
    # avoids surprises), so it's loaded last, automatically.
    zones_seed_path = _sql_file("zones_routes_seed.sql")
    if zones_seed_path.exists():
        async with p.acquire() as conn:
            await conn.execute(zones_seed_path.read_text())

    # Banking backend (bank_accounts, players.cash) -- schema_transportation.sql's
    # state_accounts / credit_ministry_of_commerce() stand-in still handles the
    # *organization* side (treasury, Ministry of Commerce); this is the real
    # player-side account backend that banking.py already assumed existed.
    banking_schema_path = _sql_file("schema_banking.sql")
    if banking_schema_path.exists():
        async with p.acquire() as conn:
            await conn.execute(banking_schema_path.read_text())

    # Private car system: tiers, dealership catalog, ownership, fuel,
    # distances, interstate routes, trips. See schema_vehicles.sql.
    vehicles_schema_path = _sql_file("schema_vehicles.sql")
    if vehicles_schema_path.exists():
        async with p.acquire() as conn:
            await conn.execute(vehicles_schema_path.read_text())

    for seed_name in (
        "vehicle_tiers_seed.sql",
        "fuel_stations_seed.sql",
        "interstate_routes_seed.sql",
        "location_coordinates_seed.sql",
    ):
        seed_file = _sql_file(seed_name)
        if seed_file.exists():
            async with p.acquire() as conn:
                await conn.execute(seed_file.read_text())

    # Oil/fuel economy: drilling, refining, the trailer/tanker fleet, and
    # the national treasury. See schema_petroleum.sql and cogs/petroleum.py.
    petroleum_schema_path = _sql_file("schema_petroleum.sql")
    if petroleum_schema_path.exists():
        async with p.acquire() as conn:
            await conn.execute(petroleum_schema_path.read_text())


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized -- call init_pool() first")
    return _pool


# ---------------------------------------------------------------------------
# Player queries used by the onboarding / immigration flow
# ---------------------------------------------------------------------------

async def get_player(discord_id: int) -> asyncpg.Record | None:
    return await pool().fetchrow(
        "SELECT * FROM players WHERE discord_id = $1", discord_id
    )


async def count_players() -> int:
    return await pool().fetchval("SELECT COUNT(*) FROM players")


async def ensure_player_exists(discord_id: int) -> None:
    """Create a bare row for a brand-new member if one doesn't exist yet."""
    await pool().execute(
        """
        INSERT INTO players (discord_id)
        VALUES ($1)
        ON CONFLICT (discord_id) DO NOTHING
        """,
        discord_id,
    )


async def record_arrival(discord_id: int, state: str) -> None:
    """Called when a player picks a destination in the select menu.

    Places the player at the state's immigration-office -- the only
    top-level pre-immigration location -- so current_location_id starts
    at immigration-office, never at refugee-camp (a sublocation of it,
    reached by being present at the office, not by travelling there).
    """
    office_id = await get_immigration_office_location_id(state)
    if office_id is not None:
        await pool().execute(
            """
            UPDATE players
            SET current_state = $2,
                immigration_status = 'arrived',
                current_location_id = $3,
                arrived_at = now()
            WHERE discord_id = $1
            """,
            discord_id,
            state,
            office_id,
        )
    else:
        await pool().execute(
            """
            UPDATE players
            SET current_state = $2,
                immigration_status = 'arrived',
                arrived_at = now()
            WHERE discord_id = $1
            """,
            discord_id,
            state,
        )


async def get_immigration_office_location_id(state: str) -> int | None:
    """ID of the state's immigration-office location, or None if it hasn't
    been seeded yet. This is where every player starts pre-immigration."""
    row = await pool().fetchrow(
        "SELECT id FROM locations WHERE state = $1 AND channel_name = 'immigration-office'",
        state,
    )
    return row["id"] if row else None


async def get_parent_map() -> dict[int, int | None]:
    """location_id -> parent_location_id for the whole hierarchy.
    Feeds permissions.is_travel_target / writable_location_ids."""
    rows = await pool().fetch("SELECT id, parent_location_id FROM locations")
    return {row["id"]: row["parent_location_id"] for row in rows}


_NIN_ALLOCATION_LOCK_KEY = 872341  # arbitrary constant, just needs to be stable


async def allocate_nin_number() -> int:
    """Called by !immigrate -- the final step after !name. Hands out
    the lowest currently-unused NIN number: pulls from freed_nin_numbers
    first (numbers released the instant a previous holder left the server,
    see reset_player_on_leave), and only mints a new one past the highest
    number ever assigned if the pool is empty. Locked with a transaction-
    scoped advisory lock so two !immigrate calls can't race for the same
    number.

    Does NOT touch the players row -- the caller (cogs/immigration.py) needs
    this number first to build the NIN string and manufacture the Discord
    role before persisting anything, so call finalize_naming() afterward.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", _NIN_ALLOCATION_LOCK_KEY
            )
            freed = await conn.fetchval(
                "SELECT number FROM freed_nin_numbers ORDER BY number ASC LIMIT 1"
            )
            if freed is not None:
                await conn.execute(
                    "DELETE FROM freed_nin_numbers WHERE number = $1", freed
                )
                return freed

            highest = await conn.fetchval("SELECT MAX(nin_number) FROM players")
            return (highest or 0) + 1


async def finalize_naming(discord_id: int, player_name: str) -> None:
    """Called by !name. Persists only the name: sets player_name, moves the
    status 'arrived' -> 'named', and stamps named_at. Deliberately does NOT
    touch player_id, nin_number, or nin_role_id -- !name grants no NIN number,
    no player_id, and no NIN role. Those are minted and persisted by
    !immigrate via complete_immigration(). The Indigene role is also the
    caller's job (it needs a discord.Member, not just a DB connection); the
    arrival role is left alone -- !immigrate removes it."""
    await pool().execute(
        """
        UPDATE players
        SET player_name = $2,
            immigration_status = 'named',
            named_at = now()
        WHERE discord_id = $1
        """,
        discord_id,
        player_name,
    )


async def reset_player_on_leave(discord_id: int) -> int | None:
    """Called from on_member_remove. A member who leaves has every Discord
    role stripped automatically -- this is the DB-side equivalent: their
    progress is wiped back to a brand-new player ('unarrived', no name, no
    state) so a rejoin goes through arrival and !name from scratch instead
    of being silently skipped as "already arrived". Their NIN number (if
    any) is freed back to the pool immediately, not deferred until someone
    else needs it.

    Returns the Discord role ID of their manufactured NIN role, if they had
    one, so the caller can delete it -- reassigning a freed number always
    deletes the old role and creates a fresh one rather than reusing the
    role object, so it can't be left dangling on the server. Returns None
    if the member had no player row at all (e.g. left before ever arriving).
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT nin_number, nin_role_id FROM players WHERE discord_id = $1",
                discord_id,
            )
            if row is None:
                return None

            if row["nin_number"] is not None:
                await conn.execute(
                    """
                    INSERT INTO freed_nin_numbers (number) VALUES ($1)
                    ON CONFLICT (number) DO NOTHING
                    """,
                    row["nin_number"],
                )

            await conn.execute(
                """
                UPDATE players
                SET immigration_status = 'unarrived',
                    current_state = NULL,
                    player_name = NULL,
                    player_id = NULL,
                    nin_number = NULL,
                    nin_role_id = NULL,
                    arrived_at = NULL,
                    named_at = NULL,
                    immigrated_at = NULL
                WHERE discord_id = $1
                """,
                discord_id,
            )

            return row["nin_role_id"]


async def complete_immigration(
    discord_id: int, player_id: str, nin_number: int, role_id: int
) -> None:
    """Called by !immigrate -- the final step, after !name. Marks the player
    fully immigrated and persists the identity that only this step mints:
    the formatted player_id (e.g. "NIN-0001-LA"), the underlying nin_number,
    and the manufactured NIN role's Discord ID (kept so reset_player_on_leave
    can delete it if this player later leaves). named_at is already stamped
    by !name; if it's somehow NULL (player immigrated without a naming
    record), stamp it too so the audit trail isn't half-empty. Granting the
    general state role, the Indigene role, and removing the arrival role is
    the caller's job (cogs/immigration.py)."""
    await pool().execute(
        """
        UPDATE players
        SET immigration_status = 'immigrated',
            immigrated_at = now(),
            player_id = $2,
            nin_number = $3,
            nin_role_id = $4,
            named_at = COALESCE(named_at, now())
        WHERE discord_id = $1
        """,
        discord_id,
        player_id,
        nin_number,
        role_id,
    )


# ---------------------------------------------------------------------------
# Shared location / role lookups (used by banking.py and cogs/transportation.py)
# ---------------------------------------------------------------------------

async def get_location_by_channel(channel_id: int) -> asyncpg.Record | None:
    return await pool().fetchrow(
        "SELECT * FROM locations WHERE channel_id = $1", channel_id
    )


async def get_role_id(name: str) -> int | None:
    return await pool().fetchval(
        "SELECT role_id FROM roles WHERE name = $1", name
    )


async def get_access_groups_for_location(location_id: int) -> list[list[str]]:
    """Role-name groups that grant access to a location. A member gets in if
    they hold EVERY role name in at least one returned group (AND within a
    group, OR between groups) -- see the group_id note on location_roles in
    schema.sql. E.g. a Delta medic-only room returns
    [["Medic Staff", "Delta Employee"]]."""
    rows = await pool().fetch(
        """
        SELECT lr.group_id, r.name
        FROM location_roles lr
        JOIN roles r ON r.id = lr.role_id
        WHERE lr.location_id = $1
        ORDER BY lr.group_id
        """,
        location_id,
    )
    groups: dict[int, list[str]] = {}
    for row in rows:
        groups.setdefault(row["group_id"], []).append(row["name"])
    return list(groups.values())


async def set_player_location(discord_id: int, location_id: int | None) -> None:
    await pool().execute(
        "UPDATE players SET current_location_id = $2 WHERE discord_id = $1",
        discord_id,
        location_id,
    )


async def set_player_state(discord_id: int, state: str) -> None:
    """Updates players.current_state only -- the Discord-side role swap
    (removing the old state's role, adding the new one) is the caller's
    job, since that needs a discord.Member, not just a DB connection.
    See cogs/cars.py's _resolve_trip for the interstate-driving caller."""
    await pool().execute(
        "UPDATE players SET current_state = $2 WHERE discord_id = $1",
        discord_id,
        state,
    )


# ---------------------------------------------------------------------------
# Banking -- backs cogs/banking.py (deposit/withdraw/transfer/get_personal_account/
# create_personal_account/get_cash_balance/get_transaction_log_channel_id all
# already called that cog before any of this existed). Cash (players.cash) and
# bank balance (bank_accounts.balance) are separate pools; !dep and !with move
# money between them, !transfer moves bank balance to another account.
# ---------------------------------------------------------------------------

class InsufficientFunds(Exception):
    pass


class NoBankAccount(ValueError):
    """Subclasses ValueError so banking.py's existing
    `except (database.InsufficientFunds, ValueError)` already catches this
    without banking.py needing to change."""
    pass


def _bank_state_code(state: str) -> str:
    return "".join(c for c in state.upper() if c.isalpha())[:2]


async def get_personal_account(discord_id: int) -> asyncpg.Record | None:
    return await pool().fetchrow(
        "SELECT * FROM bank_accounts WHERE discord_id = $1", discord_id
    )


async def create_personal_account(discord_id: int, state: str, created_by: int) -> str:
    seq_val = await pool().fetchval("SELECT nextval('bank_account_seq')")
    account_number = f"{_bank_state_code(state)}-BK-{seq_val:06d}"
    await pool().execute(
        """
        INSERT INTO bank_accounts (account_number, discord_id, state, created_by)
        VALUES ($1, $2, $3, $4)
        """,
        account_number,
        discord_id,
        state,
        created_by,
    )
    return account_number


async def get_cash_balance(discord_id: int) -> float:
    value = await pool().fetchval(
        "SELECT cash FROM players WHERE discord_id = $1", discord_id
    )
    return float(value or 0)


async def get_transaction_log_channel_id(state: str) -> int | None:
    return await pool().fetchval(
        """
        SELECT channel_id FROM locations
        WHERE state = $1 AND category = 'BANK PLC' AND channel_name = 'transaction-log'
        """,
        state.upper(),
    )


async def deposit(discord_id: int, amount: float, performed_by: int, location_state: str) -> dict:
    """Moves cash -> bank balance (a player handing physical cash to Bank Staff)."""
    if amount <= 0:
        raise ValueError("Amount must be positive.")

    async with pool().acquire() as conn:
        async with conn.transaction():
            account = await conn.fetchrow(
                "SELECT * FROM bank_accounts WHERE discord_id = $1 FOR UPDATE", discord_id
            )
            if account is None:
                raise NoBankAccount("That player doesn't have a bank account yet.")

            cash = await conn.fetchval(
                "SELECT cash FROM players WHERE discord_id = $1 FOR UPDATE", discord_id
            )
            if float(cash or 0) < amount:
                raise InsufficientFunds("That player doesn't have that much cash on hand.")

            await conn.execute(
                "UPDATE players SET cash = cash - $2 WHERE discord_id = $1", discord_id, amount
            )
            account = await conn.fetchrow(
                """
                UPDATE bank_accounts SET balance = balance + $2
                WHERE discord_id = $1
                RETURNING *
                """,
                discord_id,
                amount,
            )
    return {"account": account}


async def withdraw(discord_id: int, amount: float, location_state: str) -> dict:
    """Moves bank balance -> cash."""
    if amount <= 0:
        raise ValueError("Amount must be positive.")

    async with pool().acquire() as conn:
        async with conn.transaction():
            account = await conn.fetchrow(
                "SELECT * FROM bank_accounts WHERE discord_id = $1 FOR UPDATE", discord_id
            )
            if account is None:
                raise NoBankAccount("You don't have a bank account yet.")
            if float(account["balance"]) < amount:
                raise InsufficientFunds("Insufficient funds in your bank account.")

            account = await conn.fetchrow(
                """
                UPDATE bank_accounts SET balance = balance - $2
                WHERE discord_id = $1
                RETURNING *
                """,
                discord_id,
                amount,
            )
            await conn.execute(
                "UPDATE players SET cash = cash + $2 WHERE discord_id = $1", discord_id, amount
            )
    return {"account": account}


async def transfer(from_discord_id: int, to_account_number: str, amount: float, location_state: str) -> dict:
    """Moves bank balance -> another account's bank balance."""
    if amount <= 0:
        raise ValueError("Amount must be positive.")

    async with pool().acquire() as conn:
        async with conn.transaction():
            from_account = await conn.fetchrow(
                "SELECT * FROM bank_accounts WHERE discord_id = $1 FOR UPDATE", from_discord_id
            )
            if from_account is None:
                raise NoBankAccount("You don't have a bank account yet.")
            if float(from_account["balance"]) < amount:
                raise InsufficientFunds("Insufficient funds in your bank account.")

            to_account = await conn.fetchrow(
                "SELECT * FROM bank_accounts WHERE account_number = $1 FOR UPDATE", to_account_number
            )
            if to_account is None:
                raise ValueError(f"No account `{to_account_number}` exists.")
            if to_account["account_number"] == from_account["account_number"]:
                raise ValueError("You can't transfer to your own account.")

            from_account = await conn.fetchrow(
                "UPDATE bank_accounts SET balance = balance - $2 WHERE discord_id = $1 RETURNING *",
                from_discord_id,
                amount,
            )
            to_account = await conn.fetchrow(
                "UPDATE bank_accounts SET balance = balance + $2 WHERE account_number = $1 RETURNING *",
                to_account_number,
                amount,
            )
    return {"from_account": from_account, "to_account": to_account}


async def debit_bank_account(discord_id: int, amount: float) -> asyncpg.Record:
    """Programmatic debit straight from a player's bank balance -- for
    purchases like !refuel / !buy-car rather than a player-initiated banking
    command. Raises NoBankAccount / InsufficientFunds; returns the updated
    account row on success."""
    if amount <= 0:
        raise ValueError("Amount must be positive.")

    async with pool().acquire() as conn:
        async with conn.transaction():
            account = await conn.fetchrow(
                "SELECT * FROM bank_accounts WHERE discord_id = $1 FOR UPDATE", discord_id
            )
            if account is None:
                raise NoBankAccount("You don't have a bank account yet -- get one opened at a bank first.")
            if float(account["balance"]) < amount:
                raise InsufficientFunds("Insufficient funds in your bank account.")

            account = await conn.fetchrow(
                "UPDATE bank_accounts SET balance = balance - $2 WHERE discord_id = $1 RETURNING *",
                discord_id,
                amount,
            )
    return account
