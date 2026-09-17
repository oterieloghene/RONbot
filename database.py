import pathlib
import asyncpg

import config

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Create the connection pool and make sure the schema exists.
    Call this once, in on_ready or before the bot logs in."""
    global _pool
    _pool = await asyncpg.create_pool(dsn=config.DATABASE_URL)

    schema_path = pathlib.Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()
    async with _pool.acquire() as conn:
        await conn.execute(schema_sql)

    transport_schema_path = pathlib.Path(__file__).parent / "schema_transportation.sql"
    if transport_schema_path.exists():
        async with _pool.acquire() as conn:
            await conn.execute(transport_schema_path.read_text())

    seed_path = pathlib.Path(__file__).parent / "locations_seed.sql"
    if seed_path.exists():
        seed_sql = seed_path.read_text()
        async with _pool.acquire() as conn:
            await conn.execute(seed_sql)

    # roles_seed.sql / location_roles_seed.sql are still run by hand (same as
    # today) -- they depend on locations_seed.sql already having run.
    # zones_routes_seed.sql depends on locations_seed.sql too (zone_categories
    # don't reference locations directly, but keeping the order consistent
    # avoids surprises), so it's loaded last, automatically.
    zones_seed_path = pathlib.Path(__file__).parent / "zones_routes_seed.sql"
    if zones_seed_path.exists():
        async with _pool.acquire() as conn:
            await conn.execute(zones_seed_path.read_text())

    # Banking backend (bank_accounts, players.cash) -- schema_transportation.sql's
    # state_accounts / credit_ministry_of_commerce() stand-in still handles the
    # *organization* side (treasury, Ministry of Commerce); this is the real
    # player-side account backend that banking.py already assumed existed.
    banking_schema_path = pathlib.Path(__file__).parent / "schema_banking.sql"
    if banking_schema_path.exists():
        async with _pool.acquire() as conn:
            await conn.execute(banking_schema_path.read_text())

    # Private car system: tiers, dealership catalog, ownership, fuel,
    # distances, interstate routes, trips. See schema_vehicles.sql.
    vehicles_schema_path = pathlib.Path(__file__).parent / "schema_vehicles.sql"
    if vehicles_schema_path.exists():
        async with _pool.acquire() as conn:
            await conn.execute(vehicles_schema_path.read_text())

    for seed_name in (
        "vehicle_tiers_seed.sql",
        "fuel_stations_seed.sql",
        "interstate_routes_seed.sql",
        "location_coordinates_seed.sql",
    ):
        seed_file = pathlib.Path(__file__).parent / seed_name
        if seed_file.exists():
            async with _pool.acquire() as conn:
                await conn.execute(seed_file.read_text())

    # Oil/fuel economy: drilling, refining, the trailer/tanker fleet, and
    # the national treasury. See schema_petroleum.sql and cogs/petroleum.py.
    petroleum_schema_path = pathlib.Path(__file__).parent / "schema_petroleum.sql"
    if petroleum_schema_path.exists():
        async with _pool.acquire() as conn:
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
    """Called when a player picks a destination in the select menu."""
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


async def complete_immigration(discord_id: int, player_name: str) -> str:
    """Called when an Immigration Officer processes the player.
    Assigns a sequential player_id like 'DL-000123' and returns it."""
    row = await pool().fetchrow(
        "SELECT current_state FROM players WHERE discord_id = $1", discord_id
    )
    state = row["current_state"]
    state_code = "".join(c for c in state.upper() if c.isalpha())[:2]

    seq_val = await pool().fetchval("SELECT nextval('player_id_seq')")
    player_id = f"{state_code}-{seq_val:06d}"

    await pool().execute(
        """
        UPDATE players
        SET player_name = $2,
            player_id = $3,
            immigration_status = 'immigrated',
            immigrated_at = now()
        WHERE discord_id = $1
        """,
        discord_id,
        player_name,
        player_id,
    )
    return player_id


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


async def set_player_location(discord_id: int, location_id: int | None) -> None:
    await pool().execute(
        "UPDATE players SET current_location_id = $2 WHERE discord_id = $1",
        discord_id,
        location_id,
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
