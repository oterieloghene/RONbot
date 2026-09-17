-- Oil & fuel economy: Delta-only crude drilling, refining, and the
-- state/Nigeria-owned trailer & tanker fleet that hauls crude to a
-- refinery and refined fuel to an NNPC station. Also the single-row
-- national treasury backing the President's/Minister of Trade and
-- Commerce's vehicle revenue (channel: Abuja > Central Bank of Nigeria >
-- national-treasury -- see locations_seed.sql / roles_seed.sql for the
-- channel + CBN Governor role added alongside this file).
--
-- Run after schema.sql, schema_transportation.sql (debit_treasury /
-- credit_ministry_of_commerce / InsufficientTreasuryFunds live in
-- cogs/transportation.py and are reused here) and schema_vehicles.sql
-- (fuel_stations.stock_litres is credited by `!offload fuel` at an
-- nnpc-fuel-station -- see cogs/petroleum.py).

-- One row per state that can drill (currently just DELTA, but keyed by
-- state so a second oil-producing state later is just another row).
-- Only one drill can be "in progress" at a time per state:
-- drilling_ready_at is set the moment a drill is approved and cleared
-- once its barrels land in crude_barrels; cooldown_until then blocks a
-- new !drill until it passes.
CREATE TABLE IF NOT EXISTS oil_well_stock (
    state             TEXT PRIMARY KEY,
    crude_barrels     NUMERIC(10,2) NOT NULL DEFAULT 0,
    drilling_ready_at TIMESTAMPTZ,
    cooldown_until    TIMESTAMPTZ
);

-- !drill costs treasury (equipment) and needs that state's Commissioner
-- of Finance's approval before the 15-minute drill actually starts --
-- same pending/approved/denied shape as bus_purchase_requests.
CREATE TABLE IF NOT EXISTS drill_requests (
    id           SERIAL PRIMARY KEY,
    state        TEXT NOT NULL,
    requested_by BIGINT NOT NULL,
    cost         NUMERIC(14,2) NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / denied
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by   BIGINT,
    decided_at   TIMESTAMPTZ
);

-- One row per state's refinery (DELTA/LAGOS/ABUJA all have a refinery
-- channel -- see locations_seed.sql). crude_barrels here is what's been
-- `!offload crude`-ed off a trailer, separate from oil_well_stock's
-- crude_barrels at the well itself. Only one `!refine crude` batch can
-- be in progress at a time per state, same one-job-at-a-time pattern as
-- drilling.
CREATE TABLE IF NOT EXISTS refinery_stock (
    state             TEXT PRIMARY KEY,
    crude_barrels     NUMERIC(10,2) NOT NULL DEFAULT 0,
    fuel_litres       NUMERIC(10,2) NOT NULL DEFAULT 0,
    gas_kg            NUMERIC(10,2) NOT NULL DEFAULT 0,
    refining_barrels  NUMERIC(10,2),
    refining_ready_at TIMESTAMPTZ
);

-- !buy-trailer/!buy-tanker costs treasury (or the national treasury, for
-- Nigeria's fleet) and needs that state's Commissioner of Finance --or,
-- for Nigeria, the Minister of Finance-- to !approve-vehicle it before
-- the state_vehicles row is actually created. Same pending/approved/
-- denied shape as drill_requests.
CREATE TABLE IF NOT EXISTS vehicle_purchase_requests (
    id           SERIAL PRIMARY KEY,
    owner_state  TEXT NOT NULL,       -- DELTA / LAGOS / ABUJA / NIGERIA
    vehicle_type TEXT NOT NULL,       -- trailer / tanker
    requested_by BIGINT NOT NULL,
    cost         NUMERIC(14,2) NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / denied
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by   BIGINT,
    decided_at   TIMESTAMPTZ
);

-- A state- or Nigeria-owned trailer/tanker. unit_number is per
-- (owner_state, vehicle_type), used for its display name (e.g. "Lagos
-- Trailer 2", "Nigeria Tanker 1" -- see _vehicle_name in
-- cogs/petroleum.py). cargo_type/cargo_qty is whatever it's currently
-- hauling -- 'crude' for a trailer, 'fuel' for a tanker (refined gas
-- can't be loaded yet). status 'pending' covers a vehicle whose hire
-- request is awaiting the owning side's approval; 'en_route' blocks
-- !load/!offload and a new order until the matching vehicle_trips row
-- resolves.
CREATE TABLE IF NOT EXISTS state_vehicles (
    id                   SERIAL PRIMARY KEY,
    owner_state          TEXT NOT NULL,       -- DELTA / LAGOS / ABUJA / NIGERIA
    vehicle_type         TEXT NOT NULL,       -- trailer / tanker
    unit_number          INTEGER NOT NULL,
    status               TEXT NOT NULL DEFAULT 'idle',  -- idle / pending / en_route
    cargo_type           TEXT,                -- crude / fuel / NULL if empty
    cargo_qty            NUMERIC(10,2) NOT NULL DEFAULT 0,
    current_location_id  INTEGER REFERENCES locations(id),
    purchased_by         BIGINT NOT NULL,
    purchased_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    price_paid           NUMERIC(14,2) NOT NULL,
    UNIQUE (owner_state, vehicle_type, unit_number)
);

-- An !order-trailer/!order-tanker that crossed a state line (or pulled a
-- Nigeria-owned vehicle for use spanning two states) and so needs the
-- owning side to sign off -- and confirm payment -- before it's
-- dispatched. Posted as a button card (see HireDecisionView in
-- cogs/petroleum.py) to the owning state's (or Nigeria's) Ministry of
-- Commerce channel; there's no typed !approve-hire/!decline-hire, same
-- as the existing note that bus approvals are meant to be button clicks.
-- Free / no-approval-needed orders (see the pricing table in
-- cogs/petroleum.py) never create a row here -- they dispatch straight
-- away.
CREATE TABLE IF NOT EXISTS vehicle_hire_requests (
    id                        SERIAL PRIMARY KEY,
    vehicle_id                INTEGER NOT NULL REFERENCES state_vehicles(id),
    requested_by              BIGINT NOT NULL,
    requester_state           TEXT NOT NULL,   -- DELTA / LAGOS / ABUJA / NIGERIA -- whose treasury pays
    destination_location_id   INTEGER NOT NULL REFERENCES locations(id),
    distance_km               NUMERIC(10,2) NOT NULL,
    fare                      NUMERIC(14,2) NOT NULL,
    status                    TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / denied
    requested_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by                BIGINT,
    decided_at                TIMESTAMPTZ
);

-- A vehicle currently moving to pick up / deliver. One active trip per
-- vehicle (state_vehicles.status = 'en_route' while a row exists here).
CREATE TABLE IF NOT EXISTS vehicle_trips (
    id                        SERIAL PRIMARY KEY,
    vehicle_id                INTEGER NOT NULL UNIQUE REFERENCES state_vehicles(id),
    origin_location_id        INTEGER NOT NULL REFERENCES locations(id),
    destination_location_id   INTEGER NOT NULL REFERENCES locations(id),
    fare_paid                 NUMERIC(14,2) NOT NULL DEFAULT 0,
    started_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    arrives_at                TIMESTAMPTZ NOT NULL
);

-- Single-row national treasury -- the President's/Minister of Trade and
-- Commerce's vehicle purchases, hire fares, and hire revenue all move
-- through this balance. The national-treasury channel (Abuja > Central
-- Bank of Nigeria) is just where those moves get posted, like a
-- transaction log -- the actual money lives here, not in state_accounts,
-- and not in FCT's own treasury.
CREATE TABLE IF NOT EXISTS national_treasury (
    id       INTEGER PRIMARY KEY DEFAULT 1,
    balance  NUMERIC(16,2) NOT NULL DEFAULT 0,
    CHECK (id = 1)
);
INSERT INTO national_treasury (id, balance) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
