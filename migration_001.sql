-- One-time migration: brings an existing live database up to date with
-- schema.sql changes made after the DB was first created. Run this once,
-- then re-run location_roles_seed.sql (it's empty/stale until this runs).
--
-- Safe to run even if some parts are already applied -- everything here
-- is idempotent (IF NOT EXISTS / IF EXISTS guards).

-- 1. players.named_at (added for the !name / !immigrate two-step flow)
ALTER TABLE players ADD COLUMN IF NOT EXISTS named_at TIMESTAMPTZ;

-- 2. location_roles: group_id support (AND/OR groups per location).
-- This table is pure seed/reference data (not anything a player generates),
-- so the safe move is to drop and let location_roles_seed.sql fully
-- repopulate it -- no data loss for anything that matters.
DROP TABLE IF EXISTS location_roles;

CREATE TABLE location_roles (
    location_id  INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    group_id     INTEGER NOT NULL,
    role_id      INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (location_id, group_id, role_id)
);
