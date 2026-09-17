-- Private car system: vehicle tiers/models, ownership, fuel, condition,
-- distances, interstate travel, and driving trips.
-- Run after schema.sql and schema_banking.sql (players/bank_accounts exist).

-- Shared stats per tier. Real numbers -- tune freely; nothing else in this
-- file depends on the exact values.
--   speed_kmh          -- base speed at 100% condition
--   fuel_use_l_per_km   -- base consumption at 100% condition
--   wear_pct_per_km     -- condition points lost per km driven
--   tank_capacity_l     -- max litres the tank holds
CREATE TABLE IF NOT EXISTS vehicle_tiers (
    tier              TEXT PRIMARY KEY,   -- 'standard' / 'premium' / 'luxury'
    speed_kmh         NUMERIC NOT NULL,
    fuel_use_l_per_km NUMERIC NOT NULL,
    wear_pct_per_km   NUMERIC NOT NULL,
    tank_capacity_l   NUMERIC NOT NULL
);

-- Dealership catalog. Empty on purpose -- real car names and prices get
-- added later (one row per named car); every car just points at a tier for
-- its speed/fuel/wear stats. `!car` lists these, `!buy-car <name>` buys one.
CREATE TABLE IF NOT EXISTS vehicle_models (
    name    TEXT PRIMARY KEY,
    tier    TEXT NOT NULL REFERENCES vehicle_tiers(tier),
    price   NUMERIC(14,2) NOT NULL,
    active  BOOLEAN NOT NULL DEFAULT TRUE  -- FALSE to pull a car from the dealership without deleting owned copies
);

-- A player's owned car. condition/fuel_level_l are per-vehicle state that
-- changes as it's driven. current_location_id is where the car is physically
-- parked -- a player has to be there to !use-car / !drive / !refuel it, and
-- it stays there (doesn't follow the player) until driven again.
-- status='broken_down' blocks driving/refueling until a future Mechanic
-- command resolves the matching repair_requests row.
CREATE TABLE IF NOT EXISTS player_vehicles (
    id                   SERIAL PRIMARY KEY,
    owner_discord_id     BIGINT NOT NULL REFERENCES players(discord_id),
    model_name           TEXT NOT NULL REFERENCES vehicle_models(name),
    condition            NUMERIC(5,2) NOT NULL DEFAULT 100,
    fuel_level_l         NUMERIC(8,2) NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'ok',  -- ok / broken_down
    current_location_id  INTEGER REFERENCES locations(id),
    purchased_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_discord_id, model_name)  -- can't own two of the same named car
);

-- Which of a player's (up to two -- enforced in cogs/cars.py, not here)
-- vehicles !drive/!refuel act on.
ALTER TABLE players ADD COLUMN IF NOT EXISTS active_vehicle_id INTEGER REFERENCES player_vehicles(id);

-- Blocks ALL interstate driving for this player (any state pair) until this
-- timestamp, regardless of which pair triggered it.
ALTER TABLE players ADD COLUMN IF NOT EXISTS interstate_cooldown_until TIMESTAMPTZ;

-- Every location's small local (x_km, y_km) position -- see
-- location_coordinates_seed.sql for how these are generated. This is the
-- real per-location distance source: cogs/cars.py computes the Euclidean
-- distance between any two same-state locations from this table, so every
-- location pair gets an actual distance, not a same-zone/different-zone
-- bucket.
CREATE TABLE IF NOT EXISTS location_coordinates (
    location_id  INTEGER PRIMARY KEY REFERENCES locations(id),
    x_km         NUMERIC NOT NULL,
    y_km         NUMERIC NOT NULL
);

-- Manual override for a specific pair, if a straight-line coordinate
-- distance is ever wrong for some pair (a river with no bridge, etc).
-- Optional -- location_coordinates already covers every pair by default.
-- Looked up in both directions.
CREATE TABLE IF NOT EXISTS location_distances (
    location_a_id  INTEGER NOT NULL REFERENCES locations(id),
    location_b_id  INTEGER NOT NULL REFERENCES locations(id),
    distance_km    NUMERIC NOT NULL,
    PRIMARY KEY (location_a_id, location_b_id)
);

-- Fixed interstate legs. state_a/state_b are stored alphabetically and
-- looked up in both directions in code. distance_km is derived from your
-- given travel times at a standard-tier car's base speed (60 km/h), so a
-- faster/slower or beat-up vehicle still naturally takes more or less time
-- on the same leg instead of the time being flat.
CREATE TABLE IF NOT EXISTS interstate_routes (
    state_a         TEXT NOT NULL,
    state_b         TEXT NOT NULL,
    distance_km     NUMERIC NOT NULL,
    cooldown_hours  NUMERIC NOT NULL,
    PRIMARY KEY (state_a, state_b)
);

-- One NNPC station per state (matches the single 'nnpc-fuel-station'
-- location seeded per state). Stock depletes as players !refuel and is
-- restocked by that state's Commissioner of Petroleum (!restock-fuel) --
-- for now that's a free, direct set (no treasury cost); a real
-- tanker-delivery mechanic can replace it later without changing this
-- table. price_per_litre is also Commissioner-of-Petroleum-settable
-- (!set-fuel-price).
CREATE TABLE IF NOT EXISTS fuel_stations (
    state             TEXT PRIMARY KEY,
    price_per_litre   NUMERIC(10,2) NOT NULL DEFAULT 200,
    stock_litres      NUMERIC(12,2) NOT NULL DEFAULT 5000
);

-- A player's in-progress drive. One active trip per player. If
-- will_break_down is TRUE, the trip was already known at !drive time to run
-- out of fuel or cross the condition breakdown threshold before arriving --
-- the background tick unlocks breakdown_state's Auto Repair channel instead
-- of destination_location_id when arrives_at is reached.
CREATE TABLE IF NOT EXISTS car_trips (
    id                        SERIAL PRIMARY KEY,
    player_discord_id         BIGINT NOT NULL UNIQUE REFERENCES players(discord_id),
    vehicle_id                INTEGER NOT NULL REFERENCES player_vehicles(id),
    origin_location_id        INTEGER NOT NULL REFERENCES locations(id),
    destination_location_id   INTEGER NOT NULL REFERENCES locations(id),
    is_interstate             BOOLEAN NOT NULL DEFAULT FALSE,
    total_distance_km         NUMERIC NOT NULL,
    fuel_required_l           NUMERIC NOT NULL,
    wear_cost                 NUMERIC NOT NULL,
    will_break_down           BOOLEAN NOT NULL DEFAULT FALSE,
    breakdown_state           TEXT,
    started_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    arrives_at                TIMESTAMPTZ NOT NULL
);

-- STUB ONLY -- no Mechanic command reads/writes this yet (players can't
-- repair their own cars, and that role/command isn't built in this pass).
-- A broken-down vehicle gets a 'pending' row here automatically; whatever
-- future Mechanic command gets built should set it to 'repaired', restore
-- player_vehicles.status to 'ok', and reset condition.
CREATE TABLE IF NOT EXISTS repair_requests (
    id            SERIAL PRIMARY KEY,
    vehicle_id    INTEGER NOT NULL REFERENCES player_vehicles(id),
    requested_by  BIGINT NOT NULL REFERENCES players(discord_id),
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending / repaired
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    repaired_by   BIGINT,
    repaired_at   TIMESTAMPTZ
);
