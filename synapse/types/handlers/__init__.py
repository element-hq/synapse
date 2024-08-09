#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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
from enum import Enum
from typing import TYPE_CHECKING, Dict, Final, List, Mapping, Optional, Sequence, Tuple

import attr
from typing_extensions import TypedDict

from synapse._pydantic_compat import HAS_PYDANTIC_V2

if TYPE_CHECKING or HAS_PYDANTIC_V2:
    from pydantic.v1 import Extra
else:
    from pydantic import Extra

from synapse.events import EventBase
from synapse.types import (
    DeviceListUpdates,
    JsonDict,
    JsonMapping,
    Requester,
    SlidingSyncStreamToken,
    StreamToken,
    UserID,
)
from synapse.types.rest.client import SlidingSyncBody

if TYPE_CHECKING:
    from synapse.handlers.relations import BundledAggregations


class ShutdownRoomParams(TypedDict):
    """
    Attributes:
        requester_user_id:
            User who requested the action. Will be recorded as putting the room on the
            blocking list.
        new_room_user_id:
            If set, a new room will be created with this user ID
            as the creator and admin, and all users in the old room will be
            moved into that room. If not set, no new room will be created
            and the users will just be removed from the old room.
        new_room_name:
            A string representing the name of the room that new users will
            be invited to. Defaults to `Content Violation Notification`
        message:
            A string containing the first message that will be sent as
            `new_room_user_id` in the new room. Ideally this will clearly
            convey why the original room was shut down.
            Defaults to `Sharing illegal content on this server is not
            permitted and rooms in violation will be blocked.`
        block:
            If set to `true`, this room will be added to a blocking list,
            preventing future attempts to join the room. Defaults to `false`.
        purge:
            If set to `true`, purge the given room from the database.
        force_purge:
            If set to `true`, the room will be purged from database
            even if there are still users joined to the room.
    """

    requester_user_id: Optional[str]
    new_room_user_id: Optional[str]
    new_room_name: Optional[str]
    message: Optional[str]
    block: bool
    purge: bool
    force_purge: bool


class ShutdownRoomResponse(TypedDict):
    """
    Attributes:
        kicked_users: An array of users (`user_id`) that were kicked.
        failed_to_kick_users:
            An array of users (`user_id`) that that were not kicked.
        local_aliases:
            An array of strings representing the local aliases that were
            migrated from the old room to the new.
        new_room_id: A string representing the room ID of the new room.
    """

    kicked_users: List[str]
    failed_to_kick_users: List[str]
    local_aliases: List[str]
    new_room_id: Optional[str]


class SlidingSyncConfig(SlidingSyncBody):
    """
    Inherit from `SlidingSyncBody` since we need all of the same fields and add a few
    extra fields that we need in the handler
    """

    user: UserID
    requester: Requester

    # Pydantic config
    class Config:
        # By default, ignore fields that we don't recognise.
        extra = Extra.ignore
        # By default, don't allow fields to be reassigned after parsing.
        allow_mutation = False
        # Allow custom types like `UserID` to be used in the model
        arbitrary_types_allowed = True


class OperationType(Enum):
    """
    Represents the operation types in a Sliding Sync window.

    Attributes:
        SYNC: Sets a range of entries. Clients SHOULD discard what they previous knew about
            entries in this range.
        INSERT: Sets a single entry. If the position is not empty then clients MUST move
            entries to the left or the right depending on where the closest empty space is.
        DELETE: Remove a single entry. Often comes before an INSERT to allow entries to move
            places.
        INVALIDATE: Remove a range of entries. Clients MAY persist the invalidated range for
            offline support, but they should be treated as empty when additional operations
            which concern indexes in the range arrive from the server.
    """

    SYNC: Final = "SYNC"
    INSERT: Final = "INSERT"
    DELETE: Final = "DELETE"
    INVALIDATE: Final = "INVALIDATE"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class SlidingSyncResult:
    """
    The Sliding Sync result to be serialized to JSON for a response.

    Attributes:
        next_pos: The next position token in the sliding window to request (next_batch).
        lists: Sliding window API. A map of list key to list results.
        rooms: Room subscription API. A map of room ID to room results.
        extensions: Extensions API. A map of extension key to extension results.
    """

    @attr.s(slots=True, frozen=True, auto_attribs=True)
    class RoomResult:
        """
        Attributes:
            name: Room name or calculated room name.
            avatar: Room avatar
            heroes: List of stripped membership events (containing `user_id` and optionally
                `avatar_url` and `displayname`) for the users used to calculate the room name.
            is_dm: Flag to specify whether the room is a direct-message room (most likely
                between two people).
            initial: Flag which is set when this is the first time the server is sending this
                data on this connection. Clients can use this flag to replace or update
                their local state. When there is an update, servers MUST omit this flag
                entirely and NOT send "initial":false as this is wasteful on bandwidth. The
                absence of this flag means 'false'.
            required_state: The current state of the room
            timeline: Latest events in the room. The last event is the most recent.
            bundled_aggregations: A mapping of event ID to the bundled aggregations for
                the timeline events above. This allows clients to show accurate reaction
                counts (or edits, threads), even if some of the reaction events were skipped
                over in a gappy sync.
            stripped_state: Stripped state events (for rooms where the usre is
                invited/knocked). Same as `rooms.invite.$room_id.invite_state` in sync v2,
                absent on joined/left rooms
            prev_batch: A token that can be passed as a start parameter to the
                `/rooms/<room_id>/messages` API to retrieve earlier messages.
            limited: True if there are more events than `timeline_limit` looking
                backwards from the `response.pos` to the `request.pos`.
            num_live: The number of timeline events which have just occurred and are not historical.
                The last N events are 'live' and should be treated as such. This is mostly
                useful to determine whether a given @mention event should make a noise or not.
                Clients cannot rely solely on the absence of `initial: true` to determine live
                events because if a room not in the sliding window bumps into the window because
                of an @mention it will have `initial: true` yet contain a single live event
                (with potentially other old events in the timeline).
            bump_stamp: The `stream_ordering` of the last event according to the
                `bump_event_types`. This helps clients sort more readily without them
                needing to pull in a bunch of the timeline to determine the last activity.
                `bump_event_types` is a thing because for example, we don't want display
                name changes to mark the room as unread and bump it to the top. For
                encrypted rooms, we just have to consider any activity as a bump because we
                can't see the content and the client has to figure it out for themselves.
            joined_count: The number of users with membership of join, including the client's
                own user ID. (same as sync `v2 m.joined_member_count`)
            invited_count: The number of users with membership of invite. (same as sync v2
                `m.invited_member_count`)
            notification_count: The total number of unread notifications for this room. (same
                as sync v2)
            highlight_count: The number of unread notifications for this room with the highlight
                flag set. (same as sync v2)
        """

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class StrippedHero:
            user_id: str
            display_name: Optional[str]
            avatar_url: Optional[str]

        name: Optional[str]
        avatar: Optional[str]
        heroes: Optional[List[StrippedHero]]
        is_dm: bool
        initial: bool
        # Should be empty for invite/knock rooms with `stripped_state`
        required_state: List[EventBase]
        # Should be empty for invite/knock rooms with `stripped_state`
        timeline_events: List[EventBase]
        bundled_aggregations: Optional[Dict[str, "BundledAggregations"]]
        # Optional because it's only relevant to invite/knock rooms
        stripped_state: List[JsonDict]
        # Only optional because it won't be included for invite/knock rooms with `stripped_state`
        prev_batch: Optional[StreamToken]
        # Only optional because it won't be included for invite/knock rooms with `stripped_state`
        limited: Optional[bool]
        # Only optional because it won't be included for invite/knock rooms with `stripped_state`
        num_live: Optional[int]
        bump_stamp: int
        joined_count: int
        invited_count: int
        notification_count: int
        highlight_count: int

        def __bool__(self) -> bool:
            return (
                # If this is the first time the client is seeing the room, we should not filter it out
                # under any circumstance.
                self.initial
                # We need to let the client know if there are any new events
                or bool(self.required_state)
                or bool(self.timeline_events)
                or bool(self.stripped_state)
            )

    @attr.s(slots=True, frozen=True, auto_attribs=True)
    class SlidingWindowList:
        """
        Attributes:
            count: The total number of entries in the list. Always present if this list
                is.
            ops: The sliding list operations to perform.
        """

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class Operation:
            """
            Attributes:
                op: The operation type to perform.
                range: Which index positions are affected by this operation. These are
                    both inclusive.
                room_ids: Which room IDs are affected by this operation. These IDs match
                    up to the positions in the `range`, so the last room ID in this list
                    matches the 9th index. The room data is held in a separate object.
            """

            op: OperationType
            range: Tuple[int, int]
            room_ids: List[str]

        count: int
        ops: List[Operation]

    @attr.s(slots=True, frozen=True, auto_attribs=True)
    class Extensions:
        """Responses for extensions

        Attributes:
            to_device: The to-device extension (MSC3885)
            e2ee: The E2EE device extension (MSC3884)
        """

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class ToDeviceExtension:
            """The to-device extension (MSC3885)

            Attributes:
                next_batch: The to-device stream token the client should use
                    to get more results
                events: A list of to-device messages for the client
            """

            next_batch: str
            events: Sequence[JsonMapping]

            def __bool__(self) -> bool:
                return bool(self.events)

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class E2eeExtension:
            """The E2EE device extension (MSC3884)

            Attributes:
                device_list_updates: List of user_ids whose devices have changed or left (only
                    present on incremental syncs).
                device_one_time_keys_count: Map from key algorithm to the number of
                    unclaimed one-time keys currently held on the server for this device. If
                    an algorithm is unlisted, the count for that algorithm is assumed to be
                    zero. If this entire parameter is missing, the count for all algorithms
                    is assumed to be zero.
                device_unused_fallback_key_types: List of unused fallback key algorithms
                    for this device.
            """

            # Only present on incremental syncs
            device_list_updates: Optional[DeviceListUpdates]
            device_one_time_keys_count: Mapping[str, int]
            device_unused_fallback_key_types: Sequence[str]

            def __bool__(self) -> bool:
                # Note that "signed_curve25519" is always returned in key count responses
                # regardless of whether we uploaded any keys for it. This is necessary until
                # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
                #
                # Also related:
                # https://github.com/element-hq/element-android/issues/3725 and
                # https://github.com/matrix-org/synapse/issues/10456
                default_otk = self.device_one_time_keys_count.get("signed_curve25519")
                more_than_default_otk = len(self.device_one_time_keys_count) > 1 or (
                    default_otk is not None and default_otk > 0
                )

                return bool(
                    more_than_default_otk
                    or self.device_list_updates
                    or self.device_unused_fallback_key_types
                )

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class AccountDataExtension:
            """The Account Data extension (MSC3959)

            Attributes:
                global_account_data_map: Mapping from `type` to `content` of global account
                    data events.
                account_data_by_room_map: Mapping from room_id to mapping of `type` to
                    `content` of room account data events.
            """

            global_account_data_map: Mapping[str, JsonMapping]
            account_data_by_room_map: Mapping[str, Mapping[str, JsonMapping]]

            def __bool__(self) -> bool:
                return bool(
                    self.global_account_data_map or self.account_data_by_room_map
                )

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class ReceiptsExtension:
            """The Receipts extension (MSC3960)

            Attributes:
                room_id_to_receipt_map: Mapping from room_id to `m.receipt` ephemeral
                    event (type, content)
            """

            room_id_to_receipt_map: Mapping[str, JsonMapping]

            def __bool__(self) -> bool:
                return bool(self.room_id_to_receipt_map)

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class TypingExtension:
            """The Typing Notification extension (MSC3961)

            Attributes:
                room_id_to_typing_map: Mapping from room_id to `m.typing` ephemeral
                    event (type, content)
            """

            room_id_to_typing_map: Mapping[str, JsonMapping]

            def __bool__(self) -> bool:
                return bool(self.room_id_to_typing_map)

        to_device: Optional[ToDeviceExtension] = None
        e2ee: Optional[E2eeExtension] = None
        account_data: Optional[AccountDataExtension] = None
        receipts: Optional[ReceiptsExtension] = None
        typing: Optional[TypingExtension] = None

        def __bool__(self) -> bool:
            return bool(
                self.to_device
                or self.e2ee
                or self.account_data
                or self.receipts
                or self.typing
            )

    next_pos: SlidingSyncStreamToken
    lists: Dict[str, SlidingWindowList]
    rooms: Dict[str, RoomResult]
    extensions: Extensions

    def __bool__(self) -> bool:
        """Make the result appear empty if there are no updates. This is used
        to tell if the notifier needs to wait for more events when polling for
        events.
        """
        # We don't include `self.lists` here, as a) `lists` is always non-empty even if
        # there are no changes, and b) since we're sorting rooms by `stream_ordering` of
        # the latest activity, anything that would cause the order to change would end
        # up in `self.rooms` and cause us to send down the change.
        return bool(self.rooms or self.extensions)

    @staticmethod
    def empty(next_pos: SlidingSyncStreamToken) -> "SlidingSyncResult":
        "Return a new empty result"
        return SlidingSyncResult(
            next_pos=next_pos,
            lists={},
            rooms={},
            extensions=SlidingSyncResult.Extensions(),
        )
