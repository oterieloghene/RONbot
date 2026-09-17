-- Transportation system: zones, routes, buses, BRT cards, bookings.
-- Run after schema.sql (players/locations/roles already exist).

-- Every state is split into 3 zones (A/B/C) for bus routing. A zone is a
-- named grouping of one or more `locations.category` values -- e.g. Lagos
-- zone A ("Ghetto") = the GHETTO and FARMS categories.
CREATE TABLE IF NOT EXISTS zones (
    id     SERIAL PRIMARY KEY,
    state  TEXT NOT NULL,   -- DELTA / LAGOS / ABUJA
    code   TEXT NOT NULL,   -- 'A' / 'B' / 'C'
    name   TEXT NOT NULL,   -- e.g. 'Ghetto', 'Mainland', 'Island'
    UNIQUE (state, code)
);

-- Which location categories fall inside which zone. A bus assigned to a
-- route touches every individual channel (every `locations` row) under
-- every category listed here for either end of its route.
CREATE TABLE IF NOT EXISTS zone_categories (
    zone_id  INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    PRIMARY KEY (zone_id, category)
);

-- A fixed route between two zones within one state. Exactly 3 per state
-- (every pair of its 3 zones): A-B, B-C, A-C. Routes exist independently of
-- whether a bus has been bought for them yet.
CREATE TABLE IF NOT EXISTS routes (
    id        SERIAL PRIMARY KEY,
    state     TEXT NOT NULL,
    zone_a_id INTEGER NOT NULL REFERENCES zones(id),
    zone_b_id INTEGER NOT NULL REFERENCES zones(id),
    fare      NUMERIC(12,2) NOT NULL DEFAULT 100,
    UNIQUE (state, zone_a_id, zone_b_id)
);

-- !buy-brt requests a bus for a route; a Commissioner of Finance approval
-- turns this into a row in `buses` and debits the state treasury.
CREATE TABLE IF NOT EXISTS bus_purchase_requests (
    id           SERIAL PRIMARY KEY,
    route_id     INTEGER NOT NULL REFERENCES routes(id),
    requested_by BIGINT NOT NULL,
    price        NUMERIC(12,2) NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / denied
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by   BIGINT,
    decided_at   TIMESTAMPTZ
);

-- A purchased, running bus. `stop_index` walks a virtual stop list built at
-- runtime from the route's two zones' channels (all of zone_a's locations,
-- in id order, followed by all of zone_b's); `forward` = TRUE means walking
-- that list 0 -> end, FALSE means end -> 0. It's not materialized here so
-- adding/removing channels from a zone's categories later doesn't need a
-- migration -- see `list_route_stops()` in cogs/transportation.py.
CREATE TABLE IF NOT EXISTS buses (
    id                   SERIAL PRIMARY KEY,
    route_id             INTEGER NOT NULL REFERENCES routes(id),
    forward              BOOLEAN NOT NULL DEFAULT TRUE,
    stop_index           INTEGER NOT NULL DEFAULT 0,
    current_location_id  INTEGER REFERENCES locations(id),
    next_move_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    purchased_by         BIGINT,
    purchased_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    price_paid           NUMERIC(12,2)
);

-- One prepaid transit card per player per state -- a Delta card doesn't
-- work in Lagos. Purchasing/recharging is deferred to a future `!phone`
-- command; until then, rows here have to be created/topped up by hand.
CREATE TABLE IF NOT EXISTS brt_cards (
    player_discord_id  BIGINT NOT NULL REFERENCES players(discord_id),
    state               TEXT NOT NULL,
    balance             NUMERIC(12,2) NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_discord_id, state)
);

-- A player's booking. Funds are only checked (not debited) when they'd
-- board; failing that check drops the booking entirely -- "you just missed
-- the bus" -- rather than re-queuing them for the next one. Debit happens
-- only once, on arrival at the destination.
CREATE TABLE IF NOT EXISTS bus_bookings (
    id                    SERIAL PRIMARY KEY,
    player_discord_id     BIGINT NOT NULL REFERENCES players(discord_id),
    route_id              INTEGER NOT NULL REFERENCES routes(id),
    origin_zone_id        INTEGER NOT NULL REFERENCES zones(id),
    destination_zone_id   INTEGER NOT NULL REFERENCES zones(id),
    origin_location_id    INTEGER NOT NULL REFERENCES locations(id),
    status                TEXT NOT NULL DEFAULT 'waiting',  -- waiting / boarded
    bus_id                INTEGER REFERENCES buses(id),
    booked_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- STAND-IN ONLY: schema_banking.sql / org_accounts_seed.sql (mentioned as
-- already built) weren't included in this upload, so real treasury and
-- Ministry of Commerce accounts couldn't be wired in directly. This table
-- gives !buy-brt and fare payments somewhere real to move money to/from in
-- the meantime. Swap `debit_treasury()` / `credit_ministry_of_commerce()` in
-- cogs/transportation.py for calls into the real banking module once it's
-- available, and drop this table.
CREATE TABLE IF NOT EXISTS state_accounts (
    state        TEXT NOT NULL,
    account_type TEXT NOT NULL,  -- 'treasury' / 'ministry_of_commerce'
    balance      NUMERIC(14,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (state, account_type)
);

-- Channel-level location tracking for players, needed to know which zone
-- (and which exact channel) someone is booking from / currently occupies.
ALTER TABLE players ADD COLUMN IF NOT EXISTS current_location_id INTEGER REFERENCES locations(id);
