import itertools
import logging
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    Optional,
)

import attr

from synapse.events import EventBase
from synapse.types import (
    JsonMapping,
    StreamToken,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer


@attr.s(slots=True, frozen=True, auto_attribs=True)
class RoomResult:
    """

    Attributes:
        name: Room name or calculated room name.
        avatar: Room avatar
        heroes: List of stripped membership events (containing `user_id` and optionally
            `avatar_url` and `displayname`) for the users used to calculate the room name.
        initial: Flag which is set when this is the first time the server is sending this
            data on this connection. Clients can use this flag to replace or update
            their local state. When there is an update, servers MUST omit this flag
            entirely and NOT send "initial":false as this is wasteful on bandwidth. The
            absence of this flag means 'false'.
        required_state: The current state of the room
        timeline: Latest events in the room. The last event is the most recent
        is_dm: Flag to specify whether the room is a direct-message room (most likely
            between two people).
        invite_state: Stripped state events. Same as `rooms.invite.$room_id.invite_state`
            in sync v2, absent on joined/left rooms
        prev_batch: A token that can be passed as a start parameter to the
            `/rooms/<room_id>/messages` API to retrieve earlier messages.
        limited: True if their are more events than fit between the given position and now.
            Sync again to get more.
        joined_count: The number of users with membership of join, including the client's
            own user ID. (same as sync `v2 m.joined_member_count`)
        invited_count: The number of users with membership of invite. (same as sync v2
            `m.invited_member_count`)
        notification_count: The total number of unread notifications for this room. (same
            as sync v2)
        highlight_count: The number of unread notifications for this room with the highlight
            flag set. (same as sync v2)
        num_live: The number of timeline events which have just occurred and are not historical.
            The last N events are 'live' and should be treated as such. This is mostly
            useful to determine whether a given @mention event should make a noise or not.
            Clients cannot rely solely on the absence of `initial: true` to determine live
            events because if a room not in the sliding window bumps into the window because
            of an @mention it will have `initial: true` yet contain a single live event
            (with potentially other old events in the timeline).
    """

    name: str
    avatar: Optional[str]
    heroes: Optional[List[EventBase]]
    initial: bool
    required_state: List[EventBase]
    timeline: List[EventBase]
    is_dm: bool
    invite_state: List[EventBase]
    prev_batch: StreamToken
    limited: bool
    joined_count: int
    invited_count: int
    notification_count: int
    highlight_count: int
    num_live: int


@attr.s(slots=True, frozen=True, auto_attribs=True)
class SlidingWindowList:
    # TODO
    pass


@attr.s(slots=True, frozen=True, auto_attribs=True)
class SlidingSyncResult:
    """
    Attributes:
        pos: The next position in the sliding window to request (next_pos, next_batch).
        lists: Sliding window API. A map of list key to list results.
        rooms: Room subscription API. A map of room ID to room subscription to room results.
        extensions: TODO
    """

    pos: str
    lists: Dict[str, SlidingWindowList]
    rooms: List[RoomResult]
    extensions: JsonMapping


class SlidingSyncHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs_config = hs.config
        self.store = hs.get_datastores().main

    async def wait_for_sync_for_user():
        """Get the sync for a client if we have new data for it now. Otherwise
        wait for new data to arrive on the server. If the timeout expires, then
        return an empty sync result.
        """
        # If the user is not part of the mau group, then check that limits have
        # not been exceeded (if not part of the group by this point, almost certain
        # auth_blocking will occur)
        user_id = sync_config.user.to_string()
        await self.auth_blocking.check_auth_blocking(requester=requester)

        # if we have a since token, delete any to-device messages before that token
        # (since we now know that the device has received them)
        if since_token is not None:
            since_stream_id = since_token.to_device_key
            deleted = await self.store.delete_messages_for_device(
                sync_config.user.to_string(),
                sync_config.device_id,
                since_stream_id,
            )
            logger.debug(
                "Deleted %d to-device messages up to %d", deleted, since_stream_id
            )

        if timeout == 0 or since_token is None:
            return await self.current_sync_for_user(
                sync_config, sync_version, since_token
            )
        else:
            # Otherwise, we wait for something to happen and report it to the user.
            async def current_sync_callback(
                before_token: StreamToken, after_token: StreamToken
            ) -> Union[SyncResult, E2eeSyncResult]:
                return await self.current_sync_for_user(
                    sync_config, sync_version, since_token
                )

            result = await self.notifier.wait_for_events(
                sync_config.user.to_string(),
                timeout,
                current_sync_callback,
                from_token=since_token,
            )


        pass

            
    def assemble_response():
        # ...
        pass
