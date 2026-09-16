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

-- Every RP channel across all three states, one row per channel. This is raw
-- reference data only -- it does not yet distinguish location "tiers"
-- (district / venue / room), assign Discord channel IDs, or define what
-- role_gated actually restricts. Those get filled in once locations and
-- sub-locations are specified. current_location / residence on the players
-- table (not added yet) will eventually reference rows here.

CREATE TABLE IF NOT EXISTS locations (
    id           SERIAL PRIMARY KEY,
    state        TEXT NOT NULL,       -- DELTA / LAGOS / ABUJA
    category     TEXT NOT NULL,       -- venue grouping, e.g. "BANK PLC", "HIGH CLASS HOUSING"
    channel_name TEXT NOT NULL,       -- e.g. "banking-hall"
    is_voice     BOOLEAN NOT NULL DEFAULT FALSE,
    role_gated   BOOLEAN NOT NULL DEFAULT FALSE,  -- placeholder -- gating role TBD per location
    channel_id   BIGINT,              -- Discord channel ID, filled in once channels exist
    UNIQUE (state, category, channel_name)
);
