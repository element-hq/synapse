# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from collections.abc import Mapping
from typing import Iterator

class EventFormatVersions:
    """Internal enum for tracking the version of the event format,
    independently of the room version.

    To reduce confusion, the event format versions are named after the room
    versions that they were used or introduced in.
    The concept of an 'event format version' is specific to Synapse (the
    specification does not mention this term.)
    """

    ROOM_V1_V2: int
    """:server event id format: used for room v1 and v2"""
    ROOM_V3: int
    """MSC1659-style  event id format: used for room v3"""
    ROOM_V4_PLUS: int
    """MSC1884-style  format: introduced for room v4"""
    ROOM_V11_HYDRA_PLUS: int
    """MSC4291 room IDs as hashes: introduced for room HydraV11"""
    ROOM_VMSC4242: int
    """MSC4242 state DAGs: adds prev_state_events, removes auth_events"""

KNOWN_EVENT_FORMAT_VERSIONS: frozenset[int]

class StateResolutionVersions:
    """Enum to identify the state resolution algorithms."""

    V1: int
    """Room v1 state res"""
    V2: int
    """MSC1442 state res: room v2 and later"""
    V2_1: int
    """MSC4297 state res"""

class RoomDisposition:
    """Room disposition constants."""

    STABLE: str
    UNSTABLE: str

class PushRuleRoomFlag:
    """Enum for listing possible MSC3931 room version feature flags, for push rules."""

    EXTENSIBLE_EVENTS: str
    """MSC3932: Room version supports MSC1767 Extensible Events."""

class RoomVersion:
    """An object which describes the unique attributes of a room version."""

    identifier: str
    """The identifier for this version."""
    disposition: str
    """One of the RoomDisposition constants."""
    event_format: int
    """One of the EventFormatVersions constants."""
    state_res: int
    """One of the StateResolutionVersions constants."""
    enforce_key_validity: bool
    special_case_aliases_auth: bool
    """Before MSC2432, m.room.aliases had special auth rules and redaction rules."""
    strict_canonicaljson: bool
    """Strictly enforce canonicaljson, do not allow:
    * Integers outside the range of [-2^53 + 1, 2^53 - 1]
    * Floats
    * NaN, Infinity, -Infinity
    """
    limit_notifications_power_levels: bool
    """MSC2209: Check 'notifications' key while verifying
    m.room.power_levels auth rules."""
    implicit_room_creator: bool
    """MSC3820: No longer include the creator in m.room.create events (room version 11)."""
    updated_redaction_rules: bool
    """MSC3820: Apply updated redaction rules algorithm from room version 11."""
    restricted_join_rule: bool
    """Support the 'restricted' join rule."""
    restricted_join_rule_fix: bool
    """Support for the proper redaction rules for the restricted join rule.
    This requires restricted_join_rule to be enabled."""
    knock_join_rule: bool
    """Support the 'knock' join rule."""
    msc3389_relation_redactions: bool
    """MSC3389: Protect relation information from redaction."""
    knock_restricted_join_rule: bool
    """Support the 'knock_restricted' join rule."""
    enforce_int_power_levels: bool
    """Enforce integer power levels."""
    msc3931_push_features: list[str]
    """MSC3931: Adds a push rule condition for "room version feature flags", making
    some push rules room version dependent. Note that adding a flag to this list
    is not enough to mark it "supported": the push rule evaluator also needs to
    support the flag. Unknown flags are ignored by the evaluator, making conditions
    fail if used. Values from PushRuleRoomFlag."""
    msc3757_enabled: bool
    """MSC3757: Restricting who can overwrite a state event."""
    msc4289_creator_power_enabled: bool
    """MSC4289: Creator power enabled."""
    msc4291_room_ids_as_hashes: bool
    """MSC4291: Room IDs as hashes of the create event."""
    strict_event_byte_limits_room_versions: bool
    """Whether this room version strictly enforces event key size limits in bytes,
    rather than in codepoints.

    If true, this room version uses stricter event size validation."""
    msc4242_state_dags: bool
    """MSC4242: State DAGs. Creates events with prev_state_events instead of auth_events and derives
    state from it. Events are always processed in causal order without any gaps in the DAG
    (prev_state_events are always known), guaranteeing that processed events have a path to the
    create event. This is an emergent property of state DAGs as asserting that there is a path
    to the create event every time we insert an event would be prohibitively expensive.
    This is similar to how doubly-linked lists can potentially not refer to previous items correctly
    without verifying the list's integrity, but doing it on every insert is too expensive."""

class RoomVersions:
    V1: RoomVersion
    V2: RoomVersion
    V3: RoomVersion
    V4: RoomVersion
    V5: RoomVersion
    V6: RoomVersion
    V7: RoomVersion
    V8: RoomVersion
    V9: RoomVersion
    V10: RoomVersion
    MSC1767v10: RoomVersion
    MSC3389v10: RoomVersion
    MSC3757v10: RoomVersion
    V11: RoomVersion
    MSC3757v11: RoomVersion
    HydraV11: RoomVersion
    V12: RoomVersion
    MSC4242v12: RoomVersion

class KnownRoomVersionsMapping(Mapping[str, RoomVersion]):
    def add_room_version(self, room_version: RoomVersion) -> None: ...
    def __getitem__(self, key: str) -> RoomVersion: ...
    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator[str]: ...

KNOWN_ROOM_VERSIONS: KnownRoomVersionsMapping
