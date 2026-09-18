-- One-time migration: adds the location hierarchy (parent_location_id) to
-- an existing live database and wires each state's refugee-camp as a
-- SUBLOCATION of its immigration-office.
--
-- This column is what makes "immigration-office is the only walkable/
-- drivable top-level pre-immigration location" enforceable:
--   * normal travel (walk/drive/bus) only targets top-level locations
--     (parent_location_id IS NULL), so refugee-camp can't be a travel
--     destination,
--   * writability flows downward -- standing at immigration-office makes
--     BOTH immigration-office and refugee-camp writable at once.
--
-- Safe to run on every startup -- every statement is idempotent.

ALTER TABLE locations
    ADD COLUMN IF NOT EXISTS parent_location_id INTEGER
        REFERENCES locations(id)
        ON DELETE SET NULL;

-- Each state's refugee-camp sits under that same state's immigration-office.
UPDATE locations AS c
SET parent_location_id = (
        SELECT p.id
        FROM locations p
        WHERE p.state = c.state
          AND p.channel_name = 'immigration-office'
    )
WHERE c.channel_name = 'refugee-camp'
  AND c.parent_location_id IS NULL;