-- One-time migration: brings an existing live database up to date with
-- schema.sql changes made after the DB was first created.
--
-- Safe to run on every startup -- everything here checks first and only
-- acts if the change hasn't already been applied.

ALTER TABLE players ADD COLUMN IF NOT EXISTS named_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'location_roles' AND column_name = 'group_id'
    ) THEN
        DROP TABLE IF EXISTS location_roles;
        CREATE TABLE location_roles (
            location_id  INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            group_id     INTEGER NOT NULL,
            role_id      INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            PRIMARY KEY (location_id, group_id, role_id)
        );
    END IF;
END $$;
