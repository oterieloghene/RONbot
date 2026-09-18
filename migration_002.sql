-- One-time migration: brings an existing live database up to date with the
-- NIN (National ID Number) rework -- a global, reusable 4-digit number
-- embedded in a bot-manufactured Discord role (e.g. "NIN-0001-LA"),
-- replacing the old per-state sequential "DL-000123" scheme.
--
-- Safe to run on every startup -- everything here checks first and only
-- acts if the change hasn't already been applied.

ALTER TABLE players ADD COLUMN IF NOT EXISTS nin_number INTEGER UNIQUE;
ALTER TABLE players ADD COLUMN IF NOT EXISTS nin_role_id BIGINT;

-- Numbers released back to the pool when a player leaves the server.
-- Lowest number is always handed out next (see database.allocate_nin_number).
CREATE TABLE IF NOT EXISTS freed_nin_numbers (
    number    INTEGER PRIMARY KEY,
    freed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- player_id_seq is no longer used (numbers now come from nin_number /
-- freed_nin_numbers instead of an ever-climbing sequence) -- drop it so it
-- doesn't linger as dead, confusing state.
DROP SEQUENCE IF EXISTS player_id_seq;
