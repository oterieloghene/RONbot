"""Pure visibility/writability policy for location channels.

Two orthogonal layers, no Discord objects in sight:

* Visibility -- role-gated and binary. A channel is either visible or it
  isn't. role_gated=FALSE means the channel is open to everyone;
  role_gated=TRUE means the member must hold every role in at least one of
  the location's access groups, otherwise the channel doesn't appear at
  all. Gaining a state role flips that state's channels from invisible to
  visible in one shot -- and nothing else.

* Writability -- location-driven. Where the player can TYPE depends only on
  current_location_id (plus the per-channel Discord overwrite the caller
  applies on top). Inheritance flows DOWNWARD: sublocations inherit
  writability from their parent (refugee-camp under immigration-office).
  Role membership alone NEVER grants writability.
"""

from __future__ import annotations


def resolve_visibility(
    role_gated: bool,
    access_groups: list[list[str]] | None,
    member_role_names: list[str] | set[str] | tuple[str, ...],
) -> bool:
    """Binary, role-gated visibility decision.

    role_gated=False -> visible to everyone (open channel).
    role_gated=True  -> visible iff the member holds every role in at least
                        one access group. No groups configured -> hidden
                        from everyone (no role, no channel).
    """
    if not role_gated:
        return True
    if not access_groups:
        return False
    held = set(member_role_names or ())
    return any(set(group).issubset(held) for group in access_groups)


def sublocations_of(parent_map: dict[int, int | None]) -> dict[int, list[int]]:
    """Invert a {location_id: parent_id} map into {parent_id: [child_ids]}."""
    children: dict[int, list[int]] = {}
    for location_id, parent_id in parent_map.items():
        if parent_id is not None:
            children.setdefault(parent_id, []).append(location_id)
    return children


def writable_location_ids(
    current_location_id: int | None,
    sublocations_map: dict[int, list[int]],
) -> set[int]:
    """All location ids the player can currently type in.

    Exactly current_location_id plus every transitive sublocation of it.
    Inheritance flows downward only: being inside a sublocation does NOT
    open the parent. current_location_id=None -> no writable channels.
    """
    if current_location_id is None:
        return set()
    writable = {current_location_id}
    stack = [current_location_id]
    while stack:
        node = stack.pop()
        for child in sublocations_map.get(node, ()):
            if child not in writable:
                writable.add(child)
                stack.append(child)
    return writable


def is_travel_target(location_id: int, parent_map: dict[int, int | None]) -> bool:
    """Only top-level locations are walk/drive/bus destinations.

    Sublocations (e.g. refugee-camp under immigration-office) are reached
    by being present at the parent, not by travelling to them directly.
    A location absent from the parent map has no known parent -> top level.
    """
    return parent_map.get(location_id) is None


def travel_targets(
    location_ids: list[int] | set[int],
    parent_map: dict[int, int | None],
) -> set[int]:
    """Filter a set of locations down to the ones normal travel can reach."""
    return {lid for lid in location_ids if is_travel_target(lid, parent_map)}


def channel_write_allowed(
    member_role_names: list[str] | set[str] | tuple[str, ...],
    location: dict,
    current_location_id: int | None,
    parent_map: dict[int, int | None],
    overwrite_allows_send: bool,
) -> bool:
    """Full write decision for one channel.

    True only when ALL of the following hold:
      1. the channel is visible to the member (role-gated, binary),
      2. the channel's location is current_location_id or one of its
         sublocations (writability follows travel only),
      3. the channel's Discord overwrite allows sending.

    Note the deliberate asymmetry with visibility: a state role can make a
    channel visible, but it can never make it writable -- only standing at
    the right location can.
    """
    if not resolve_visibility(location["role_gated"], location.get("access_groups"), member_role_names):
        return False
    if not overwrite_allows_send:
        return False
    writable = writable_location_ids(
        current_location_id, sublocations_of(parent_map)
    )
    return location["id"] in writable