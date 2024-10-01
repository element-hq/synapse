#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

from typing import Callable, Dict, Optional, Tuple

import attr


class EventFormatVersions:
    """This is an internal enum for tracking the version of the event format,
    independently of the room version.

    To reduce confusion, the event format versions are named after the room
    versions that they were used or introduced in.
    The concept of an 'event format version' is specific to Synapse (the
    specification does not mention this term.)
    """

    ROOM_V1_V2 = 1  # $id:server event id format: used for room v1 and v2
    ROOM_V3 = 2  # MSC1659-style $hash event id format: used for room v3
    ROOM_V4_PLUS = 3  # MSC1884-style $hash format: introduced for room v4


KNOWN_EVENT_FORMAT_VERSIONS = {
    EventFormatVersions.ROOM_V1_V2,
    EventFormatVersions.ROOM_V3,
    EventFormatVersions.ROOM_V4_PLUS,
}


class StateResolutionVersions:
    """Enum to identify the state resolution algorithms"""

    V1 = 1  # room v1 state res
    V2 = 2  # MSC1442 state res: room v2 and later


class RoomDisposition:
    STABLE = "stable"
    UNSTABLE = "unstable"


class PushRuleRoomFlag:
    """Enum for listing possible MSC3931 room version feature flags, for push rules"""

    # MSC3932: Room version supports MSC1767 Extensible Events.
    EXTENSIBLE_EVENTS = "org.matrix.msc3932.extensible_events"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class RoomVersion:
    """An object which describes the unique attributes of a room version."""

    identifier: str  # the identifier for this version
    disposition: str  # one of the RoomDispositions
    event_format: int  # one of the EventFormatVersions
    state_res: int  # one of the StateResolutionVersions
    enforce_key_validity: bool

    # Before MSC2432, m.room.aliases had special auth rules and redaction rules
    special_case_aliases_auth: bool
    # Strictly enforce canonicaljson, do not allow:
    # * Integers outside the range of [-2 ^ 53 + 1, 2 ^ 53 - 1]
    # * Floats
    # * NaN, Infinity, -Infinity
    strict_canonicaljson: bool
    # MSC2209: Check 'notifications' key while verifying
    # m.room.power_levels auth rules.
    limit_notifications_power_levels: bool
    # No longer include the creator in m.room.create events.
    implicit_room_creator: bool
    # Apply updated redaction rules algorithm from room version 11.
    updated_redaction_rules: bool
    # Support the 'restricted' join rule.
    restricted_join_rule: bool
    # Support for the proper redaction rules for the restricted join rule. This requires
    # restricted_join_rule to be enabled.
    restricted_join_rule_fix: bool
    # Support the 'knock' join rule.
    knock_join_rule: bool
    # MSC3389: Protect relation information from redaction.
    msc3389_relation_redactions: bool
    # Support the 'knock_restricted' join rule.
    knock_restricted_join_rule: bool
    # Enforce integer power levels
    enforce_int_power_levels: bool
    # MSC3931: Adds a push rule condition for "room version feature flags", making
    # some push rules room version dependent. Note that adding a flag to this list
    # is not enough to mark it "supported": the push rule evaluator also needs to
    # support the flag. Unknown flags are ignored by the evaluator, making conditions
    # fail if used.
    msc3931_push_features: Tuple[str, ...]  # values from PushRuleRoomFlag
    # MSC3757: Restricting who can overwrite a state event
    msc3757_enabled: bool


class RoomVersions:
    V1 = RoomVersion(
        "1",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V1_V2,
        StateResolutionVersions.V1,
        enforce_key_validity=False,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=False,
        restricted_join_rule_fix=False,
        knock_join_rule=False,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=False,
        enforce_int_power_levels=False,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    V2 = RoomVersion(
        "2",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V1_V2,
        StateResolutionVersions.V2,
        enforce_key_validity=False,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=False,
        restricted_join_rule_fix=False,
        knock_join_rule=False,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=False,
        enforce_int_power_levels=False,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    V3 = RoomVersion(
        "3",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V3,
        StateResolutionVersions.V2,
        enforce_key_validity=False,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=False,
        restricted_join_rule_fix=False,
        knock_join_rule=False,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=False,
        enforce_int_power_levels=False,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    V4 = RoomVersion(
        "4",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=False,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=False,
        restricted_join_rule_fix=False,
        knock_join_rule=False,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=False,
        enforce_int_power_levels=False,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    V5 = RoomVersion(
        "5",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=False,
        restricted_join_rule_fix=False,
        knock_join_rule=False,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=False,
        enforce_int_power_levels=False,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    V6 = RoomVersion(
        "6",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=False,
        restricted_join_rule_fix=False,
        knock_join_rule=False,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=False,
        enforce_int_power_levels=False,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    V7 = RoomVersion(
        "7",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=False,
        restricted_join_rule_fix=False,
        knock_join_rule=True,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=False,
        enforce_int_power_levels=False,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    V8 = RoomVersion(
        "8",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=True,
        restricted_join_rule_fix=False,
        knock_join_rule=True,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=False,
        enforce_int_power_levels=False,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    V9 = RoomVersion(
        "9",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=True,
        restricted_join_rule_fix=True,
        knock_join_rule=True,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=False,
        enforce_int_power_levels=False,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    V10 = RoomVersion(
        "10",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=True,
        restricted_join_rule_fix=True,
        knock_join_rule=True,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=True,
        enforce_int_power_levels=True,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    MSC1767v10 = RoomVersion(
        # MSC1767 (Extensible Events) based on room version "10"
        "org.matrix.msc1767.10",
        RoomDisposition.UNSTABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=True,
        restricted_join_rule_fix=True,
        knock_join_rule=True,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=True,
        enforce_int_power_levels=True,
        msc3931_push_features=(PushRuleRoomFlag.EXTENSIBLE_EVENTS,),
        msc3757_enabled=False,
    )
    MSC3757v10 = RoomVersion(
        # MSC3757 (Restricting who can overwrite a state event) based on room version "10"
        "org.matrix.msc3757.10",
        RoomDisposition.UNSTABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        implicit_room_creator=False,
        updated_redaction_rules=False,
        restricted_join_rule=True,
        restricted_join_rule_fix=True,
        knock_join_rule=True,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=True,
        enforce_int_power_levels=True,
        msc3931_push_features=(),
        msc3757_enabled=True,
    )
    V11 = RoomVersion(
        "11",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        implicit_room_creator=True,  # Used by MSC3820
        updated_redaction_rules=True,  # Used by MSC3820
        restricted_join_rule=True,
        restricted_join_rule_fix=True,
        knock_join_rule=True,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=True,
        enforce_int_power_levels=True,
        msc3931_push_features=(),
        msc3757_enabled=False,
    )
    MSC3757v11 = RoomVersion(
        # MSC3757 (Restricting who can overwrite a state event) based on room version "11"
        "org.matrix.msc3757.11",
        RoomDisposition.UNSTABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        implicit_room_creator=True,  # Used by MSC3820
        updated_redaction_rules=True,  # Used by MSC3820
        restricted_join_rule=True,
        restricted_join_rule_fix=True,
        knock_join_rule=True,
        msc3389_relation_redactions=False,
        knock_restricted_join_rule=True,
        enforce_int_power_levels=True,
        msc3931_push_features=(),
        msc3757_enabled=True,
    )


KNOWN_ROOM_VERSIONS: Dict[str, RoomVersion] = {
    v.identifier: v
    for v in (
        RoomVersions.V1,
        RoomVersions.V2,
        RoomVersions.V3,
        RoomVersions.V4,
        RoomVersions.V5,
        RoomVersions.V6,
        RoomVersions.V7,
        RoomVersions.V8,
        RoomVersions.V9,
        RoomVersions.V10,
        RoomVersions.V11,
        RoomVersions.MSC3757v10,
        RoomVersions.MSC3757v11,
    )
}


@attr.s(slots=True, frozen=True, auto_attribs=True)
class RoomVersionCapability:
    """An object which describes the unique attributes of a room version."""

    identifier: str  # the identifier for this capability
    preferred_version: Optional[RoomVersion]
    support_check_lambda: Callable[[RoomVersion], bool]


MSC3244_CAPABILITIES = {
    cap.identifier: {
        "preferred": (
            cap.preferred_version.identifier
            if cap.preferred_version is not None
            else None
        ),
        "support": [
            v.identifier
            for v in KNOWN_ROOM_VERSIONS.values()
            if cap.support_check_lambda(v)
        ],
    }
    for cap in (
        RoomVersionCapability(
            "knock",
            RoomVersions.V7,
            lambda room_version: room_version.knock_join_rule,
        ),
        RoomVersionCapability(
            "restricted",
            RoomVersions.V9,
            lambda room_version: room_version.restricted_join_rule,
        ),
    )
}
