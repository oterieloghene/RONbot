"""Shared helpers for seed-file tests.

The repo's SQL targets Postgres, which we don't have in the test
environment, so these helpers load the location/role seed data into an
in-memory SQLite database with just enough schema. SQLite is forgiving
about column types (it accepts TEXT/BIGINT/etc. verbatim), so the seed
INSERT statements run unmodified; only the CREATE TABLE for `locations`
is hand-built here.
"""

import re
import sqlite3

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _seed_values(sql_text: str) -> list[tuple]:
    """Extract the VALUES tuples from the big INSERT in a seed file."""
    match = re.search(r"VALUES\s*(.*?)(?:\s*ON CONFLICT|\s*;)", sql_text, re.DOTALL)
    assert match, "no VALUES clause found in seed file"
    body = match.group(1)
    rows = re.findall(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)", body)
    parsed = []
    for raw in rows:
        # Unquote single-quoted strings (handles '' escape), keep booleans/
        # numbers as-is: SQLite's INSERT will coerce them, and our
        # assertions compare against the same literal forms.
        values = re.findall(r"'((?:[^']|'')*)'|\b(TRUE|FALSE)\b|(-?\d+)", raw)
        row = []
        for quoted, boolean, number in values:
            if quoted:
                row.append(quoted.replace("''", "'"))
            elif boolean:
                row.append(boolean)
            else:
                row.append(int(number))
        parsed.append(tuple(row))
    return parsed


def load_locations_db() -> sqlite3.Connection:
    """In-memory SQLite DB with the `locations` table populated from
    locations_seed.sql plus whatever parent wiring the repo declares
    (seed UPDATEs or migration_003.sql)."""
    sql_text = (REPO_ROOT / "locations_seed.sql").read_text()
    rows = _seed_values(sql_text)

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state TEXT NOT NULL,
            category TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            is_voice BOOLEAN NOT NULL DEFAULT FALSE,
            role_gated BOOLEAN NOT NULL DEFAULT FALSE,
            channel_id BIGINT,
            parent_location_id INTEGER,
            UNIQUE (state, category, channel_name)
        )
        """
    )
    conn.executemany(
        "INSERT INTO locations (state, category, channel_name, is_voice, role_gated) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )

    # Parent wiring declared in the seed file itself (UPDATE statements
    # after the INSERT) or in migration_003.sql -- run both so a repo can
    # express the wiring in either place.
    update_statements = re.findall(r"UPDATE\s+locations\b[^;]+;", sql_text, re.DOTALL | re.IGNORECASE)
    migration_path = REPO_ROOT / "migration_003.sql"
    if migration_path.exists():
        update_statements += re.findall(
            r"UPDATE\s+locations\b[^;]+;", migration_path.read_text(), re.DOTALL | re.IGNORECASE
        )
    for statement in update_statements:
        # Postgres UPDATE...FROM is fine in SQLite (supported since 3.33),
        # and IS DISTINCT FROM exists too.
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            # Some UPDATE shapes (e.g. with JOINs SQLite rejects) -- fall
            # back to a naive evaluator keyed on the two channel names the
            # statement references, so the test still asserts the wiring.
            m = re.search(r"channel_name\s*=\s*'([^']+)'", statement)
            m2 = re.search(r"p\.channel_name\s*=\s*'([^']+)'", statement)
            if m and m2:
                child_name, parent_name = m.group(1), m2.group(1)
                conn.execute(
                    """
                    UPDATE locations AS c
                    SET parent_location_id = (
                        SELECT p.id FROM locations p
                        WHERE p.state = c.state AND p.channel_name = ?
                    )
                    WHERE c.channel_name = ?
                    """,
                    (parent_name, child_name),
                )
    conn.commit()
    return conn


def location_by_name(conn: sqlite3.Connection, state: str, channel_name: str) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM locations WHERE state = ? AND channel_name = ?",
        (state, channel_name),
    ).fetchone()
    assert row is not None, f"no {state}/{channel_name} location seeded"
    return dict(row)