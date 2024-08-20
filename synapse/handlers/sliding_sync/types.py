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
    Callable,
    Dict,
    Final,
    Generic,
    Mapping,
    MutableMapping,
    Optional,
    Set,
    TypeVar,
    cast,
)

import attr

from synapse.api.constants import EventTypes
from synapse.types import MultiWriterStreamToken, RoomStreamToken, StrCollection, UserID
from synapse.types.handlers import SlidingSyncConfig

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class StateValues:
    """
    Understood values of the (type, state_key) tuple in `required_state`.
    """

    # Include all state events of the given type
    WILDCARD: Final = "*"
    # Lazy-load room membership events (include room membership events for any event
    # `sender` in the timeline). We only give special meaning to this value when it's a
    # `state_key`.
    LAZY: Final = "$LAZY"
    # Subsitute with the requester's user ID. Typically used by clients to get
    # the user's membership.
    ME: Final = "$ME"


# We can't freeze this class because we want to update it in place with the
# de-duplicated data.
@attr.s(slots=True, auto_attribs=True)
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
    required_state_map: Dict[str, Set[str]]

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
        required_state_map: Dict[str, Set[str]] = {}
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

    def deep_copy(self) -> "RoomSyncConfig":
        required_state_map: Dict[str, Set[str]] = {
            state_type: state_key_set.copy()
            for state_type, state_key_set in self.required_state_map.items()
        }

        return RoomSyncConfig(
            timeline_limit=self.timeline_limit,
            required_state_map=required_state_map,
        )

    def combine_room_sync_config(
        self, other_room_sync_config: "RoomSyncConfig"
    ) -> None:
        """
        Combine this `RoomSyncConfig` with another `RoomSyncConfig` and take the
        superset union of the two.
        """
        # Take the highest timeline limit
        if self.timeline_limit < other_room_sync_config.timeline_limit:
            self.timeline_limit = other_room_sync_config.timeline_limit

        # Union the required state
        for (
            state_type,
            state_key_set,
        ) in other_room_sync_config.required_state_map.items():
            # If we already have a wildcard for everything, we don't need to add
            # anything else
            if StateValues.WILDCARD in self.required_state_map.get(
                StateValues.WILDCARD, set()
            ):
                break

            # If we already have a wildcard `state_key` for this `state_type`, we don't need
            # to add anything else
            if StateValues.WILDCARD in self.required_state_map.get(state_type, set()):
                continue

            # If we're getting wildcards for the `state_type` and `state_key`, that's
            # all that matters so get rid of any other entries
            if (
                state_type == StateValues.WILDCARD
                and StateValues.WILDCARD in state_key_set
            ):
                self.required_state_map = {state_type: {StateValues.WILDCARD}}
                # We can break, since we don't need to add anything else
                break

            for state_key in state_key_set:
                # If we already have a wildcard for this specific `state_key`, we don't need
                # to add it since the wildcard already covers it.
                if state_key in self.required_state_map.get(
                    StateValues.WILDCARD, set()
                ):
                    continue

                # If we're getting a wildcard for the `state_type`, get rid of any other
                # entries with the same `state_key`, since the wildcard will cover it already.
                if state_type == StateValues.WILDCARD:
                    # Get rid of any entries that match the `state_key`
                    #
                    # Make a copy so we don't run into an error: `dictionary changed size
                    # during iteration`, when we remove items
                    for existing_state_type, existing_state_key_set in list(
                        self.required_state_map.items()
                    ):
                        # Make a copy so we don't run into an error: `Set changed size during
                        # iteration`, when we filter out and remove items
                        for existing_state_key in existing_state_key_set.copy():
                            if existing_state_key == state_key:
                                existing_state_key_set.remove(state_key)

                        # If we've the left the `set()` empty, remove it from the map
                        if existing_state_key_set == set():
                            self.required_state_map.pop(existing_state_type, None)

                # If we're getting a wildcard `state_key`, get rid of any other state_keys
                # for this `state_type` since the wildcard will cover it already.
                if state_key == StateValues.WILDCARD:
                    self.required_state_map[state_type] = {state_key}
                    break
                # Otherwise, just add it to the set
                else:
                    if self.required_state_map.get(state_type) is None:
                        self.required_state_map[state_type] = {state_key}
                    else:
                        self.required_state_map[state_type].add(state_key)

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
            # Lazy-loading means we include membership for any event `sender` in the
            # timeline but since we had to auth those timeline events, we will have the
            # membership state for them (including from remote senders).
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


T = TypeVar("T")


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
    last_token: Optional[T]

    @staticmethod
    def live() -> "HaveSentRoom[T]":
        return HaveSentRoom(HaveSentRoomFlag.LIVE, None)

    @staticmethod
    def previously(last_token: T) -> "HaveSentRoom[T]":
        """Constructor for `PREVIOUSLY` flag."""
        return HaveSentRoom(HaveSentRoomFlag.PREVIOUSLY, last_token)

    @staticmethod
    def never() -> "HaveSentRoom[T]":
        return HaveSentRoom(HaveSentRoomFlag.NEVER, None)


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


@attr.s(auto_attribs=True)
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

    room_configs: Mapping[str, RoomSyncConfig] = attr.Factory(dict)

    def get_mutable(self) -> "MutablePerConnectionState":
        """Get a mutable copy of this state."""
        room_configs = cast(MutableMapping[str, RoomSyncConfig], self.room_configs)

        return MutablePerConnectionState(
            rooms=self.rooms.get_mutable(),
            receipts=self.receipts.get_mutable(),
            room_configs=ChainMap({}, room_configs),
        )

    def copy(self) -> "PerConnectionState":
        return PerConnectionState(
            rooms=self.rooms.copy(),
            receipts=self.receipts.copy(),
            room_configs=dict(self.room_configs),
        )


@attr.s(auto_attribs=True)
class MutablePerConnectionState(PerConnectionState):
    """A mutable version of `PerConnectionState`"""

    rooms: MutableRoomStatusMap[RoomStreamToken]
    receipts: MutableRoomStatusMap[MultiWriterStreamToken]

    room_configs: typing.ChainMap[str, RoomSyncConfig]

    def has_updates(self) -> bool:
        return (
            bool(self.rooms.get_updates())
            or bool(self.receipts.get_updates())
            or bool(self.get_room_config_updates())
        )

    def get_room_config_updates(self) -> Mapping[str, RoomSyncConfig]:
        """Get updates to the room sync config"""
        return self.room_configs.maps[0]
