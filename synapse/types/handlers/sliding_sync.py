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

import logging
import typing
from collections import ChainMap
from enum import Enum
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Any,
    Callable,
    Final,
    Generic,
    Mapping,
    MutableMapping,
    Sequence,
    TypeVar,
    cast,
)

import attr
from pydantic import ConfigDict

from synapse.api.constants import EventTypes
from synapse.events import EventBase
from synapse.types import (
    DeviceListUpdates,
    JsonDict,
    JsonMapping,
    MultiWriterStreamToken,
    Requester,
    RoomStreamToken,
    SlidingSyncStreamToken,
    StrCollection,
    StreamToken,
    ThreadSubscriptionsToken,
    UserID,
)
from synapse.types.rest.client import SlidingSyncBody

if TYPE_CHECKING:
    from synapse.handlers.relations import BundledAggregations

logger = logging.getLogger(__name__)


class SlidingSyncConfig(SlidingSyncBody):
    """
    Inherit from `SlidingSyncBody` since we need all of the same fields and add a few
    extra fields that we need in the handler
    """

    user: UserID
    requester: Requester
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        # Allow custom types like `UserID` to be used in the model.
        arbitrary_types_allowed=True,
    )


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
            unstable_expanded_timeline: Flag which is set if we're returning more historic
                events due to the timeline limit having increased. See "XXX: Odd behavior"
                comment ing `synapse.handlers.sliding_sync`.
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
                This may not be included if there hasn't been a change.
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
            display_name: str | None
            avatar_url: str | None

        name: str | None
        avatar: str | None
        heroes: list[StrippedHero] | None
        is_dm: bool
        initial: bool
        unstable_expanded_timeline: bool
        # Should be empty for invite/knock rooms with `stripped_state`
        required_state: list[EventBase]
        # Should be empty for invite/knock rooms with `stripped_state`
        timeline_events: list[EventBase]
        bundled_aggregations: dict[str, "BundledAggregations"] | None
        # Optional because it's only relevant to invite/knock rooms
        stripped_state: list[JsonDict]
        # Only optional because it won't be included for invite/knock rooms with `stripped_state`
        prev_batch: StreamToken | None
        # Only optional because it won't be included for invite/knock rooms with `stripped_state`
        limited: bool | None
        # Only optional because it won't be included for invite/knock rooms with `stripped_state`
        num_live: int | None
        bump_stamp: int | None
        joined_count: int | None
        invited_count: int | None
        notification_count: int
        highlight_count: int

        def __bool__(self) -> bool:
            return (
                # If this is the first time the client is seeing the room, we should not filter it out
                # under any circumstance.
                self.initial
                # We need to let the client know if any of the info has changed
                or self.name is not None
                or self.avatar is not None
                or bool(self.heroes)
                or self.joined_count is not None
                or self.invited_count is not None
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
            range: tuple[int, int]
            room_ids: list[str]

        count: int
        ops: list[Operation]

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
            device_list_updates: DeviceListUpdates | None
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
                global_account_data_map: Mapping from `type` to `content` of global
                    account data events.
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

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class ThreadSubscriptionsExtension:
            """The Thread Subscriptions extension (MSC4308)

            Attributes:
                subscribed: map (room_id -> thread_root_id -> info) of new or changed subscriptions
                unsubscribed: map (room_id -> thread_root_id -> info) of new unsubscriptions
                prev_batch: if present, there is a gap and the client can use this token to backpaginate
            """

            @attr.s(slots=True, frozen=True, auto_attribs=True)
            class ThreadSubscription:
                # always present when `subscribed`
                automatic: bool | None

                # the same as our stream_id; useful for clients to resolve
                # race conditions locally
                bump_stamp: int

            @attr.s(slots=True, frozen=True, auto_attribs=True)
            class ThreadUnsubscription:
                # the same as our stream_id; useful for clients to resolve
                # race conditions locally
                bump_stamp: int

            # room_id -> event_id (of thread root) -> the subscription change
            subscribed: Mapping[str, Mapping[str, ThreadSubscription]] | None
            # room_id -> event_id (of thread root) -> the unsubscription
            unsubscribed: Mapping[str, Mapping[str, ThreadUnsubscription]] | None
            prev_batch: ThreadSubscriptionsToken | None

            def __bool__(self) -> bool:
                return (
                    bool(self.subscribed)
                    or bool(self.unsubscribed)
                    or bool(self.prev_batch)
                )

        to_device: ToDeviceExtension | None = None
        e2ee: E2eeExtension | None = None
        account_data: AccountDataExtension | None = None
        receipts: ReceiptsExtension | None = None
        typing: TypingExtension | None = None
        thread_subscriptions: ThreadSubscriptionsExtension | None = None

        def __bool__(self) -> bool:
            return bool(
                self.to_device
                or self.e2ee
                or self.account_data
                or self.receipts
                or self.typing
                or self.thread_subscriptions
            )

    next_pos: SlidingSyncStreamToken
    lists: Mapping[str, SlidingWindowList]
    rooms: dict[str, RoomResult]
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


class StateValues:
    """
    Understood values of the (type, state_key) tuple in `required_state`.
    """

    # Include all state events of the given type
    WILDCARD: Final = "*"
    # Lazy-load room membership events (include room membership events for any event
    # `sender` or membership change target in the timeline). We only give special
    # meaning to this value when it's a `state_key`.
    LAZY: Final = "$LAZY"
    # Subsitute with the requester's user ID. Typically used by clients to get
    # the user's membership.
    ME: Final = "$ME"


# We can't freeze this class because we want to update it in place with the
# de-duplicated data.
@attr.s(slots=True, auto_attribs=True, frozen=True)
class RoomSyncConfig:
    """
    Holds the config for what data we should fetch for a room in the sync response.

    Attributes:
        timeline_limit: The maximum number of events to return in the timeline.

        required_state_map: Map from state event type to state_keys requested for the
            room. The values are close to `StateKey` but actually use a syntax where you
            can provide `*` wildcard and `$LAZY` for lazy-loading room members.
    """

    timeline_limit: int
    required_state_map: Mapping[str, AbstractSet[str]]

    @classmethod
    def from_room_config(
        cls,
        room_params: SlidingSyncConfig.CommonRoomParameters,
    ) -> "RoomSyncConfig":
        """
        Create a `RoomSyncConfig` from a `SlidingSyncList`/`RoomSubscription` config.

        Args:
            room_params: `SlidingSyncConfig.SlidingSyncList` or `SlidingSyncConfig.RoomSubscription`
        """
        required_state_map: dict[str, set[str]] = {}
        for (
            state_type,
            state_key,
        ) in room_params.required_state:
            # If we already have a wildcard for this specific `state_key`, we don't need
            # to add it since the wildcard already covers it.
            if state_key in required_state_map.get(StateValues.WILDCARD, set()):
                continue

            # If we already have a wildcard `state_key` for this `state_type`, we don't need
            # to add anything else
            if StateValues.WILDCARD in required_state_map.get(state_type, set()):
                continue

            # If we're getting wildcards for the `state_type` and `state_key`, that's
            # all that matters so get rid of any other entries
            if state_type == StateValues.WILDCARD and state_key == StateValues.WILDCARD:
                required_state_map = {StateValues.WILDCARD: {StateValues.WILDCARD}}
                # We can break, since we don't need to add anything else
                break

            # If we're getting a wildcard for the `state_type`, get rid of any other
            # entries with the same `state_key`, since the wildcard will cover it already.
            elif state_type == StateValues.WILDCARD:
                # Get rid of any entries that match the `state_key`
                #
                # Make a copy so we don't run into an error: `dictionary changed size
                # during iteration`, when we remove items
                for (
                    existing_state_type,
                    existing_state_key_set,
                ) in list(required_state_map.items()):
                    # Make a copy so we don't run into an error: `Set changed size during
                    # iteration`, when we filter out and remove items
                    for existing_state_key in existing_state_key_set.copy():
                        if existing_state_key == state_key:
                            existing_state_key_set.remove(state_key)

                    # If we've the left the `set()` empty, remove it from the map
                    if existing_state_key_set == set():
                        required_state_map.pop(existing_state_type, None)

            # If we're getting a wildcard `state_key`, get rid of any other state_keys
            # for this `state_type` since the wildcard will cover it already.
            if state_key == StateValues.WILDCARD:
                required_state_map[state_type] = {state_key}
            # Otherwise, just add it to the set
            else:
                if required_state_map.get(state_type) is None:
                    required_state_map[state_type] = {state_key}
                else:
                    required_state_map[state_type].add(state_key)

        return cls(
            timeline_limit=room_params.timeline_limit,
            required_state_map=required_state_map,
        )

    def combine_room_sync_config(
        self, other_room_sync_config: "RoomSyncConfig"
    ) -> "RoomSyncConfig":
        """
        Combine this `RoomSyncConfig` with another `RoomSyncConfig` and return the
        superset union of the two.
        """
        timeline_limit = self.timeline_limit
        required_state_map = {
            event_type: set(state_keys)
            for event_type, state_keys in self.required_state_map.items()
        }

        # Take the highest timeline limit
        if timeline_limit < other_room_sync_config.timeline_limit:
            timeline_limit = other_room_sync_config.timeline_limit

        # Union the required state
        for (
            state_type,
            state_key_set,
        ) in other_room_sync_config.required_state_map.items():
            # If we already have a wildcard for everything, we don't need to add
            # anything else
            if StateValues.WILDCARD in required_state_map.get(
                StateValues.WILDCARD, set()
            ):
                break

            # If we already have a wildcard `state_key` for this `state_type`, we don't need
            # to add anything else
            if StateValues.WILDCARD in required_state_map.get(state_type, set()):
                continue

            # If we're getting wildcards for the `state_type` and `state_key`, that's
            # all that matters so get rid of any other entries
            if (
                state_type == StateValues.WILDCARD
                and StateValues.WILDCARD in state_key_set
            ):
                required_state_map = {state_type: {StateValues.WILDCARD}}
                # We can break, since we don't need to add anything else
                break

            for state_key in state_key_set:
                # If we already have a wildcard for this specific `state_key`, we don't need
                # to add it since the wildcard already covers it.
                if state_key in required_state_map.get(StateValues.WILDCARD, set()):
                    continue

                # If we're getting a wildcard for the `state_type`, get rid of any other
                # entries with the same `state_key`, since the wildcard will cover it already.
                if state_type == StateValues.WILDCARD:
                    # Get rid of any entries that match the `state_key`
                    #
                    # Make a copy so we don't run into an error: `dictionary changed size
                    # during iteration`, when we remove items
                    for existing_state_type, existing_state_key_set in list(
                        required_state_map.items()
                    ):
                        # Make a copy so we don't run into an error: `Set changed size during
                        # iteration`, when we filter out and remove items
                        for existing_state_key in existing_state_key_set.copy():
                            if existing_state_key == state_key:
                                existing_state_key_set.remove(state_key)

                        # If we've the left the `set()` empty, remove it from the map
                        if existing_state_key_set == set():
                            required_state_map.pop(existing_state_type, None)

                # If we're getting a wildcard `state_key`, get rid of any other state_keys
                # for this `state_type` since the wildcard will cover it already.
                if state_key == StateValues.WILDCARD:
                    required_state_map[state_type] = {state_key}
                    break
                # Otherwise, just add it to the set
                else:
                    if required_state_map.get(state_type) is None:
                        required_state_map[state_type] = {state_key}
                    else:
                        required_state_map[state_type].add(state_key)

        return RoomSyncConfig(timeline_limit, required_state_map)

    def must_await_full_state(
        self,
        is_mine_id: Callable[[str], bool],
    ) -> bool:
        """
        Check if we have a we're only requesting `required_state` which is completely
        satisfied even with partial state, then we don't need to `await_full_state` before
        we can return it.

        Also see `StateFilter.must_await_full_state(...)` for comparison

        Partially-stated rooms should have all state events except for remote membership
        events so if we require a remote membership event anywhere, then we need to
        return `True` (requires full state).

        Args:
            is_mine_id: a callable which confirms if a given state_key matches a mxid
               of a local user
        """
        wildcard_state_keys = self.required_state_map.get(StateValues.WILDCARD)
        # Requesting *all* state in the room so we have to wait
        if (
            wildcard_state_keys is not None
            and StateValues.WILDCARD in wildcard_state_keys
        ):
            return True

        # If the wildcards don't refer to remote user IDs, then we don't need to wait
        # for full state.
        if wildcard_state_keys is not None:
            for possible_user_id in wildcard_state_keys:
                if not possible_user_id[0].startswith(UserID.SIGIL):
                    # Not a user ID
                    continue

                localpart_hostname = possible_user_id.split(":", 1)
                if len(localpart_hostname) < 2:
                    # Not a user ID
                    continue

                if not is_mine_id(possible_user_id):
                    return True

        membership_state_keys = self.required_state_map.get(EventTypes.Member)
        # We aren't requesting any membership events at all so the partial state will
        # cover us.
        if membership_state_keys is None:
            return False

        # If we're requesting entirely local users, the partial state will cover us.
        for user_id in membership_state_keys:
            if user_id == StateValues.ME:
                continue
            # We're lazy-loading membership so we can just return the state we have.
            # Lazy-loading means we include membership for any event `sender` or
            # membership change target in the timeline but since we had to auth those
            # timeline events, we will have the membership state for them (including
            # from remote senders).
            elif user_id == StateValues.LAZY:
                continue
            elif user_id == StateValues.WILDCARD:
                return False
            elif not is_mine_id(user_id):
                return True

        # Local users only so the partial state will cover us.
        return False


class HaveSentRoomFlag(Enum):
    """Flag for whether we have sent the room down a sliding sync connection.

    The valid state changes here are:
        NEVER -> LIVE
        LIVE -> PREVIOUSLY
        PREVIOUSLY -> LIVE
    """

    # The room has never been sent down (or we have forgotten we have sent it
    # down).
    NEVER = "never"

    # We have previously sent the room down, but there are updates that we
    # haven't sent down.
    PREVIOUSLY = "previously"

    # We have sent the room down and the client has received all updates.
    LIVE = "live"


T = TypeVar("T", str, RoomStreamToken, MultiWriterStreamToken, int)


@attr.s(auto_attribs=True, slots=True, frozen=True)
class HaveSentRoom(Generic[T]):
    """Whether we have sent the room data down a sliding sync connection.

    We are generic over the type of token used, e.g. `RoomStreamToken` or
    `MultiWriterStreamToken`.

    Attributes:
        status: Flag of if we have or haven't sent down the room
        last_token: If the flag is `PREVIOUSLY` then this is non-null and
            contains the last stream token of the last updates we sent down
            the room, i.e. we still need to send everything since then to the
            client.
    """

    status: HaveSentRoomFlag
    last_token: T | None

    @staticmethod
    def live() -> "HaveSentRoom[T]":
        return HaveSentRoom(HaveSentRoomFlag.LIVE, None)

    @staticmethod
    def previously(last_token: T) -> "HaveSentRoom[T]":
        """Constructor for `PREVIOUSLY` flag."""
        return HaveSentRoom(HaveSentRoomFlag.PREVIOUSLY, last_token)

    @staticmethod
    def never() -> "HaveSentRoom[T]":
        # We use a singleton to avoid repeatedly instantiating new `never`
        # values.
        return _HAVE_SENT_ROOM_NEVER


_HAVE_SENT_ROOM_NEVER: HaveSentRoom[Any] = HaveSentRoom(HaveSentRoomFlag.NEVER, None)


@attr.s(auto_attribs=True, slots=True, frozen=True)
class RoomStatusMap(Generic[T]):
    """For a given stream, e.g. events, records what we have or have not sent
    down for that stream in a given room."""

    # `room_id` -> `HaveSentRoom`
    _statuses: Mapping[str, HaveSentRoom[T]] = attr.Factory(dict)

    def have_sent_room(self, room_id: str) -> HaveSentRoom[T]:
        """Return whether we have previously sent the room down"""
        return self._statuses.get(room_id, HaveSentRoom.never())

    def get_mutable(self) -> "MutableRoomStatusMap[T]":
        """Get a mutable copy of this state."""
        return MutableRoomStatusMap(
            statuses=self._statuses,
        )

    def copy(self) -> "RoomStatusMap[T]":
        """Make a copy of the class. Useful for converting from a mutable to
        immutable version."""

        return RoomStatusMap(statuses=dict(self._statuses))

    def __len__(self) -> int:
        return len(self._statuses)


class MutableRoomStatusMap(RoomStatusMap[T]):
    """A mutable version of `RoomStatusMap`"""

    # We use a ChainMap here so that we can easily track what has been updated
    # and what hasn't. Note that when we persist the per connection state this
    # will get flattened to a normal dict (via calling `.copy()`)
    _statuses: typing.ChainMap[str, HaveSentRoom[T]]

    def __init__(
        self,
        statuses: Mapping[str, HaveSentRoom[T]],
    ) -> None:
        # ChainMap requires a mutable mapping, but we're not actually going to
        # mutate it.
        statuses = cast(MutableMapping, statuses)

        super().__init__(
            statuses=ChainMap({}, statuses),
        )

    def get_updates(self) -> Mapping[str, HaveSentRoom[T]]:
        """Return only the changes that were made"""
        return self._statuses.maps[0]

    def record_sent_rooms(self, room_ids: StrCollection) -> None:
        """Record that we have sent these rooms in the response"""
        for room_id in room_ids:
            current_status = self._statuses.get(room_id, HaveSentRoom.never())
            if current_status.status == HaveSentRoomFlag.LIVE:
                continue

            self._statuses[room_id] = HaveSentRoom.live()

    def record_unsent_rooms(self, room_ids: StrCollection, from_token: T) -> None:
        """Record that we have not sent these rooms in the response, but there
        have been updates.
        """
        # Whether we add/update the entries for unsent rooms depends on the
        # existing entry:
        #   - LIVE: We have previously sent down everything up to
        #     `last_room_token, so we update the entry to be `PREVIOUSLY` with
        #     `last_room_token`.
        #   - PREVIOUSLY: We have previously sent down everything up to *a*
        #     given token, so we don't need to update the entry.
        #   - NEVER: We have never previously sent down the room, and we haven't
        #     sent anything down this time either so we leave it as NEVER.

        for room_id in room_ids:
            current_status = self._statuses.get(room_id, HaveSentRoom.never())
            if current_status.status != HaveSentRoomFlag.LIVE:
                continue

            self._statuses[room_id] = HaveSentRoom.previously(from_token)


@attr.s(auto_attribs=True, frozen=True)
class PerConnectionState:
    """The per-connection state. A snapshot of what we've sent down the
    connection before.

    Currently, we track whether we've sent down various aspects of a given room
    before.

    We use the `rooms` field to store the position in the events stream for each
    room that we've previously sent to the client before. On the next request
    that includes the room, we can then send only what's changed since that
    recorded position.

    Same goes for the `receipts` field so we only need to send the new receipts
    since the last time you made a sync request.

    Attributes:
        rooms: The status of each room for the events stream.
        receipts: The status of each room for the receipts stream.
        room_configs: Map from room_id to the `RoomSyncConfig` of all
            rooms that we have previously sent down.
    """

    rooms: RoomStatusMap[RoomStreamToken] = attr.Factory(RoomStatusMap)
    receipts: RoomStatusMap[MultiWriterStreamToken] = attr.Factory(RoomStatusMap)
    account_data: RoomStatusMap[int] = attr.Factory(RoomStatusMap)

    room_configs: Mapping[str, RoomSyncConfig] = attr.Factory(dict)

    def get_mutable(self) -> "MutablePerConnectionState":
        """Get a mutable copy of this state."""
        room_configs = cast(MutableMapping[str, RoomSyncConfig], self.room_configs)

        return MutablePerConnectionState(
            rooms=self.rooms.get_mutable(),
            receipts=self.receipts.get_mutable(),
            account_data=self.account_data.get_mutable(),
            room_configs=ChainMap({}, room_configs),
        )

    def copy(self) -> "PerConnectionState":
        return PerConnectionState(
            rooms=self.rooms.copy(),
            receipts=self.receipts.copy(),
            account_data=self.account_data.copy(),
            room_configs=dict(self.room_configs),
        )

    def __len__(self) -> int:
        return len(self.rooms) + len(self.receipts) + len(self.room_configs)


@attr.s(auto_attribs=True)
class MutablePerConnectionState(PerConnectionState):
    """A mutable version of `PerConnectionState`"""

    rooms: MutableRoomStatusMap[RoomStreamToken]
    receipts: MutableRoomStatusMap[MultiWriterStreamToken]
    account_data: MutableRoomStatusMap[int]

    room_configs: typing.ChainMap[str, RoomSyncConfig]

    def has_updates(self) -> bool:
        return (
            bool(self.rooms.get_updates())
            or bool(self.receipts.get_updates())
            or bool(self.account_data.get_updates())
            or bool(self.get_room_config_updates())
        )

    def get_room_config_updates(self) -> Mapping[str, RoomSyncConfig]:
        """Get updates to the room sync config"""
        return self.room_configs.maps[0]
