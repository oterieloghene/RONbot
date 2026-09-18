"""Structural regression tests for the location seed data.

Run the real seed SQL (via an in-memory SQLite stand-in) and assert the
arrival-flow topology the rest of the bot depends on:

  * each state seeds the arrival trio (arrival-terminal, refugee-camp,
    immigration-office),
  * refugee-camp is a SUBLOCATION of immigration-office -- not a sibling,
  * immigration-office is a top-level location (no parent).

These are the structural preconditions for "immigration-office is the only
walkable/drivable pre-immigration top-level location".
"""

import config

from permissions import is_travel_target
from sqlite_harness import location_by_name, load_locations_db

# Seed files store uppercase state names; config.STATES keys are title-case.
SEED_STATES = {state: state.upper() for state in config.STATES}


def _parent_map(conn) -> dict:
    conn.row_factory = None
    return {
        row[0]: row[1]
        for row in conn.execute("SELECT id, parent_location_id FROM locations")
    }


def test_arrival_trio_seeded_for_every_state():
    conn = load_locations_db()
    try:
        for state, seed_state in SEED_STATES.items():
            for channel_name in ("arrival-terminal", "refugee-camp", "immigration-office"):
                location_by_name(conn, seed_state, channel_name)
    finally:
        conn.close()


def test_refugee_camp_is_sublocation_of_immigration_office():
    conn = load_locations_db()
    for seed_state in SEED_STATES.values():
        camp = location_by_name(conn, seed_state, "refugee-camp")
        office = location_by_name(conn, seed_state, "immigration-office")
        assert camp["parent_location_id"] == office["id"], (
            f"{seed_state}: refugee-camp must be a sublocation of "
            f"immigration-office, got parent {camp['parent_location_id']} "
            f"(office id {office['id']})"
        )
    conn.close()


def test_immigration_office_is_top_level():
    conn = load_locations_db()
    for seed_state in SEED_STATES.values():
        office = location_by_name(conn, seed_state, "immigration-office")
        assert office["parent_location_id"] is None, (
            f"{seed_state}: immigration-office must not have a parent"
        )
    conn.close()


def test_refugee_camp_is_not_a_travel_target():
    # Normal travel (walk/drive/bus) only targets top-level locations.
    conn = load_locations_db()
    parent_map = _parent_map(conn)
    for seed_state in SEED_STATES.values():
        camp = location_by_name(conn, seed_state, "refugee-camp")
        office = location_by_name(conn, seed_state, "immigration-office")
        assert is_travel_target(office["id"], parent_map) is True
        assert is_travel_target(camp["id"], parent_map) is False
    conn.close()


def test_sublocation_inheritance_from_seeded_topology():
    # End-to-end shape of the headline spec case: standing at the seeded
    # immigration-office location makes the seeded refugee-camp writable.
    import permissions

    conn = load_locations_db()
    for seed_state in SEED_STATES.values():
        camp = location_by_name(conn, seed_state, "refugee-camp")
        office = location_by_name(conn, seed_state, "immigration-office")
        sublocations_of = {
            parent: [
                child
                for child, parent_id in _parent_map(conn).items()
                if parent_id == parent
            ]
            for parent in {row[0] for row in _parent_map(conn).items()}
        }
        writable = permissions.writable_location_ids(office["id"], sublocations_of)
        assert office["id"] in writable
        assert camp["id"] in writable
    conn.close()