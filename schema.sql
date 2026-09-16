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
-- (district / venue / room) or assign Discord channel IDs. Those get filled
-- in once locations and sub-locations are specified. current_location /
-- residence on the players table (not added yet) will eventually reference
-- rows here. See `roles` and `location_roles` below for what role_gated
-- locations are actually gated by.

CREATE TABLE IF NOT EXISTS locations (
    id           SERIAL PRIMARY KEY,
    state        TEXT NOT NULL,       -- DELTA / LAGOS / ABUJA
    category     TEXT NOT NULL,       -- venue grouping, e.g. "BANK PLC", "HIGH CLASS HOUSING"
    channel_name TEXT NOT NULL,       -- e.g. "banking-hall"
    is_voice     BOOLEAN NOT NULL DEFAULT FALSE,
    role_gated   BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE if only specific roles (see location_roles) may post here
    channel_id   BIGINT,              -- Discord channel ID, filled in once channels exist
    UNIQUE (state, category, channel_name)
);

-- Every role named in a state's design doc (e.g. "Bank Staff (Delta Employee)",
-- "Ughelli Resident", "Immigration Officer"). One row per distinct role string --
-- shared/cross-state roles ("President") appear once, not once per state.
CREATE TABLE IF NOT EXISTS roles (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    role_id  BIGINT              -- Discord role ID, filled in once the role exists in the server
);

-- Which role(s) are associated with which location. A location can have several
-- (e.g. holding-cell -> Police Officer, Jailed, Arrested); a role can apply to
-- several locations. role_gated on `locations` is the actual enforcement switch --
-- this table is what it's TRUE *because of*.
CREATE TABLE IF NOT EXISTS location_roles (
    location_id  INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    role_id      INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (location_id, role_id)
);
