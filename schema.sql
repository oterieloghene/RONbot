-- Core player record. This is the central record referenced in step 4 of the
-- plan. Only entry/immigration-relevant columns are included here -- add the
-- rest (bank, vehicles, condition stats, etc.) as later features land, since
-- each system should own the migration for its own columns.

CREATE TABLE IF NOT EXISTS players (
    discord_id          BIGINT PRIMARY KEY,
    player_id           TEXT UNIQUE,        -- e.g. "NIN-0001-LA", assigned at !immigrate
    player_name         TEXT,               -- given by the immigration officer
    current_state       TEXT,               -- Abuja / Lagos / Delta
    immigration_status  TEXT NOT NULL DEFAULT 'unarrived',
                         -- 'unarrived' -> 'arrived' -> 'named' -> 'immigrated'
    nin_number           INTEGER UNIQUE,     -- the "0001" in player_id; global, reused lowest-first
    nin_role_id          BIGINT,             -- Discord ID of the bot-manufactured NIN role currently held
    arrived_at          TIMESTAMPTZ,
    named_at            TIMESTAMPTZ,
    immigrated_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- NIN numbers released back to the pool the instant a player leaves the
-- server (see database.reset_player_on_leave). database.allocate_nin_number
-- always hands out the lowest number sitting in here before minting a new
-- one, so numbers get reused rather than climbing forever.
CREATE TABLE IF NOT EXISTS freed_nin_numbers (
    number    INTEGER PRIMARY KEY,
    freed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
    parent_location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,  -- set when this location is a SUBLOCATION of another (e.g. refugee-camp under immigration-office); NULL = top-level, walkable/drivable/bus stop
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

-- Which role(s) grant access to which location. role_gated on `locations` is
-- the actual enforcement switch -- this table is what it's TRUE *because of*.
--
-- group_id expresses AND/OR: rows sharing a (location_id, group_id) must ALL
-- be held by the member (AND); different group_ids are alternatives, any one
-- of which is sufficient (OR). E.g. holding-cell might be:
--   group 1: Police Officer + Delta Employee   (both required)
--   group 2: Jailed                            (alone is enough)
--   group 3: Arrested                          (alone is enough)
-- A plain single-role requirement (most locations) is just a group of one.
CREATE TABLE IF NOT EXISTS location_roles (
    location_id  INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    group_id     INTEGER NOT NULL,
    role_id      INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (location_id, group_id, role_id)
);
