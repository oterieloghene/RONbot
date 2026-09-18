"""Regression tests for the pure visibility/writability policy (permissions.py).

Two orthogonal layers:
  * Visibility -- role-gated, binary. No qualifying role -> the channel
    doesn't appear at all. Gaining a state role makes that state's channels
    visible in one shot.
  * Writability -- driven only by current_location_id (+ per-channel Discord
    overwrites). Sublocations inherit writability from their parent.
    Role membership alone NEVER grants writability.
"""

import permissions


# ---------------------------------------------------------------------------
# Visibility: binary, role-gated
# ---------------------------------------------------------------------------

def test_visibility_hidden_without_any_access_group():
    # A role_gated channel with no access groups configured is hidden from
    # everyone -- binary: no role, no channel.
    assert permissions.resolve_visibility(True, [], ["Delta"]) is False
    assert permissions.resolve_visibility(True, None, ["Delta"]) is False


def test_visibility_hidden_without_matching_role():
    groups = [["Delta Arrival"], ["Delta"]]
    assert permissions.resolve_visibility(True, groups, []) is False
    assert permissions.resolve_visibility(True, groups, ["Lagos"]) is False


def test_visibility_granted_by_any_one_group():
    groups = [["Delta Arrival"], ["Delta"]]
    assert permissions.resolve_visibility(True, groups, ["Delta Arrival"]) is True
    assert permissions.resolve_visibility(True, groups, ["Delta"]) is True


def test_visibility_group_is_all_roles_in_that_group():
    # A multi-role group requires every role in it.
    groups = [["Delta", "Immigration Officer"]]
    assert permissions.resolve_visibility(True, groups, ["Delta"]) is False
    assert permissions.resolve_visibility(True, groups, ["Immigration Officer"]) is False
    assert permissions.resolve_visibility(True, groups, ["Delta", "Immigration Officer"]) is True


def test_visibility_ungated_channels_visible():
    # role_gated=FALSE means the channel is open (degenerate, un-gated case).
    assert permissions.resolve_visibility(False, [], []) is True


def test_state_role_grant_makes_channels_visible():
    # Post-immigration: gaining the state role flips every state-gated
    # channel from invisible to visible -- and only those.
    state_channels = ["hotel-reception", "banking-hall"]
    other_channels = ["Lagos police-station"]

    after = {
        ch: permissions.resolve_visibility(True, [["Delta Arrival"], ["Delta"]], ["Delta"])
        for ch in state_channels + other_channels
    }

    assert after["hotel-reception"] is True
    assert after["banking-hall"] is True
    # Other states' groups never match just holding "Delta".
    assert permissions.resolve_visibility(True, [["Lagos"]], ["Delta"]) is False


# ---------------------------------------------------------------------------
# Writability: location-driven, sublocations inherit
# ---------------------------------------------------------------------------

def test_writable_only_current_location_without_sublocations():
    assert permissions.writable_location_ids(10, {}) == {10}
    assert permissions.writable_location_ids(10, {10: []}) == {10}


def test_writable_none_when_nowhere():
    assert permissions.writable_location_ids(None, {}) == set()


def test_sublocation_inherits_writability():
    # immigration-office = 5, refugee-camp = 6 (its sublocation).
    subs = {5: [6]}
    assert permissions.writable_location_ids(5, subs) == {5, 6}


def test_sublocation_inheritance_is_transitive():
    # 5 -> 6 -> 7: standing at 5 writes in all three.
    subs = {5: [6], 6: [7]}
    assert permissions.writable_location_ids(5, subs) == {5, 6, 7}


def test_being_in_sublocation_does_not_write_parent():
    # Inheritance flows downward only: at refugee-camp (6) you write in
    # 6 and its own children, not in the parent (5).
    subs = {5: [6], 6: [7]}
    assert permissions.writable_location_ids(6, subs) == {6, 7}


def test_state_role_alone_grants_no_writability():
    # Holding "Delta" (post-immigration) makes channels visible but changes
    # nothing about where the player can type -- location does that.
    visible = permissions.resolve_visibility(True, [["Delta"]], ["Delta"])
    assert visible is True
    assert permissions.writable_location_ids(None, {}) == set()
    # Even with the role, being at location 5 writes only 5 + sublocations.
    assert permissions.writable_location_ids(5, {5: [6]}) == {5, 6}


# ---------------------------------------------------------------------------
# Travel targets: only top-level locations are walk/drive/bus targets
# ---------------------------------------------------------------------------

def test_travel_targets_exclude_sublocations():
    # refugee-camp (6) is a sublocation of immigration-office (5), so it is
    # NOT a normal travel target; immigration-office is.
    parent_map = {5: None, 6: 5}
    assert permissions.is_travel_target(5, parent_map) is True
    assert permissions.is_travel_target(6, parent_map) is False


def test_travel_targets_unknown_location_is_top_level():
    # A location not present in the parent map has no known parent -> top level.
    assert permissions.is_travel_target(99, {5: None, 6: 5}) is True


def test_travel_targets_filter_helper():
    parent_map = {1: None, 2: 1, 3: None}
    assert permissions.travel_targets([1, 2, 3], parent_map) == {1, 3}


# ---------------------------------------------------------------------------
# Channel write decision: visibility AND location AND overwrite
# ---------------------------------------------------------------------------

def test_channel_write_requires_visibility():
    location = {"id": 6, "role_gated": True, "access_groups": [["Delta"]]}
    assert permissions.channel_write_allowed(
        member_role_names=[],
        location=location,
        current_location_id=6,
        parent_map={6: 5},
        overwrite_allows_send=True,
    ) is False


def test_channel_write_requires_location_match_or_sublocation():
    location = {"id": 10, "role_gated": True, "access_groups": [["Delta"]]}
    # Standing at 5 (a sibling) cannot write in 10's channel, even though it
    # is visible and the overwrite would allow send.
    assert permissions.channel_write_allowed(
        member_role_names=["Delta"],
        location=location,
        current_location_id=5,
        parent_map={5: None, 10: None},
        overwrite_allows_send=True,
    ) is False
    # Standing at the parent of 10 -> writable (sublocation inheritance).
    assert permissions.channel_write_allowed(
        member_role_names=["Delta"],
        location=location,
        current_location_id=5,
        parent_map={5: None, 10: 5},
        overwrite_allows_send=True,
    ) is True


def test_channel_write_requires_overwrite_to_allow_send():
    location = {"id": 5, "role_gated": True, "access_groups": [["Delta"]]}
    assert permissions.channel_write_allowed(
        member_role_names=["Delta"],
        location=location,
        current_location_id=5,
        parent_map={5: None},
        overwrite_allows_send=False,
    ) is False
    assert permissions.channel_write_allowed(
        member_role_names=["Delta"],
        location=location,
        current_location_id=5,
        parent_map={5: None},
        overwrite_allows_send=True,
    ) is True


def test_immigration_office_makes_refugee_camp_writable():
    # The headline spec case: at immigration-office, BOTH the office channel
    # and its refugee-camp subchannel are writable.
    office = {"id": 5, "role_gated": True, "access_groups": [["Delta Arrival"], ["Delta"]]}
    camp = {"id": 6, "role_gated": True, "access_groups": [["Delta Arrival"], ["Delta"]]}
    parent_map = {5: None, 6: 5}

    assert permissions.channel_write_allowed(
        ["Delta Arrival"], office, 5, parent_map, True
    ) is True
    assert permissions.channel_write_allowed(
        ["Delta Arrival"], camp, 5, parent_map, True
    ) is True