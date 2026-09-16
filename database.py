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

    seed_path = pathlib.Path(__file__).parent / "locations_seed.sql"
    if seed_path.exists():
        seed_sql = seed_path.read_text()
        async with _pool.acquire() as conn:
            await conn.execute(seed_sql)


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
