-- Core player record. This is the central record referenced in step 4 of the
-- plan. Only entry/immigration-relevant columns are included here -- add the
-- rest (bank, vehicles, condition stats, etc.) as later features land, since
-- each system should own the migration for its own columns.

CREATE TABLE IF NOT EXISTS players (
    discord_id          BIGINT PRIMARY KEY,
    player_id           TEXT UNIQUE,        -- e.g. "DL-000123", assigned at immigration
    player_name         TEXT,               -- given by the immigration officer
    current_state       TEXT,               -- Abuja / Lagos / Delta
    immigration_status  TEXT NOT NULL DEFAULT 'unarrived',
                         -- 'unarrived' -> 'arrived' -> 'immigrated'
    arrived_at          TIMESTAMPTZ,
    immigrated_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Sequence used to generate sequential player IDs at immigration time.
CREATE SEQUENCE IF NOT EXISTS player_id_seq START 1;
