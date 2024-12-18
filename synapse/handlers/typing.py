#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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
import logging
import random
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Set, Tuple

import attr

from synapse.api.constants import EduTypes
from synapse.api.errors import AuthError, ShadowBanError, SynapseError
from synapse.appservice import ApplicationService
from synapse.metrics.background_process_metrics import (
    run_as_background_process,
    wrap_as_background_process,
)
from synapse.replication.tcp.streams import TypingStream
from synapse.streams import EventSource
from synapse.types import (
    JsonDict,
    JsonMapping,
    Requester,
    StrCollection,
    StreamKeyType,
    UserID,
)
from synapse.util.caches.stream_change_cache import StreamChangeCache
from synapse.util.metrics import Measure
from synapse.util.retryutils import filter_destinations_by_retry_limiter
from synapse.util.wheel_timer import WheelTimer

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# A tiny object useful for storing a user's membership in a room, as a mapping
# key
@attr.s(slots=True, frozen=True, auto_attribs=True)
class RoomMember:
    room_id: str
    user_id: str


# How often we expect remote servers to resend us presence.
FEDERATION_TIMEOUT = 60 * 1000

# How often to resend typing across federation.
FEDERATION_PING_INTERVAL = 40 * 1000


# How long to remember a typing notification happened in a room before
# forgetting about it.
FORGET_TIMEOUT = 10 * 60 * 1000


class FollowerTypingHandler:
    """A typing handler on a different process than the writer that is updated
    via replication.
    """

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self.server_name = hs.config.server.server_name
        self.clock = hs.get_clock()
        self.is_mine_id = hs.is_mine_id
        self.is_mine_server_name = hs.is_mine_server_name

        self.federation = None
        if hs.should_send_federation():
            self.federation = hs.get_federation_sender()

        if hs.get_instance_name() not in hs.config.worker.writers.typing:
            hs.get_federation_registry().register_instances_for_edu(
                EduTypes.TYPING,
                hs.config.worker.writers.typing,
            )

        # map room IDs to serial numbers
        self._room_serials: Dict[str, int] = {}
        # map room IDs to sets of users currently typing
        self._room_typing: Dict[str, Set[str]] = {}

        self._member_last_federation_poke: Dict[RoomMember, int] = {}
        self.wheel_timer: WheelTimer[RoomMember] = WheelTimer(bucket_size=5000)
        self._latest_room_serial = 0

        self._rooms_updated: Set[str] = set()

        self.clock.looping_call(self._handle_timeouts, 5000)
        self.clock.looping_call(self._prune_old_typing, FORGET_TIMEOUT)

    def _reset(self) -> None:
        """Reset the typing handler's data caches."""
        # map room IDs to serial numbers
        self._room_serials = {}
        # map room IDs to sets of users currently typing
        self._room_typing = {}

        self._rooms_updated = set()

        self._member_last_federation_poke = {}
        self.wheel_timer = WheelTimer(bucket_size=5000)

    @wrap_as_background_process("typing._handle_timeouts")
    async def _handle_timeouts(self) -> None:
        logger.debug("Checking for typing timeouts")

        now = self.clock.time_msec()

        members = set(self.wheel_timer.fetch(now))

        for member in members:
            self._handle_timeout_for_member(now, member)

    def _handle_timeout_for_member(self, now: int, member: RoomMember) -> None:
        if not self.is_typing(member):
            # Nothing to do if they're no longer typing
            return

        # Check if we need to resend a keep alive over federation for this
        # user.
        if self.federation and self.is_mine_id(member.user_id):
            last_fed_poke = self._member_last_federation_poke.get(member, None)
            if not last_fed_poke or last_fed_poke + FEDERATION_PING_INTERVAL <= now:
                run_as_background_process(
                    "typing._push_remote", self._push_remote, member=member, typing=True
                )

        # Add a paranoia timer to ensure that we always have a timer for
        # each person typing.
        self.wheel_timer.insert(now=now, obj=member, then=now + 60 * 1000)

    def is_typing(self, member: RoomMember) -> bool:
        return member.user_id in self._room_typing.get(member.room_id, set())

    async def _push_remote(self, member: RoomMember, typing: bool) -> None:
        if not self.federation:
            return

        try:
            self._member_last_federation_poke[member] = self.clock.time_msec()

            now = self.clock.time_msec()
            self.wheel_timer.insert(
                now=now, obj=member, then=now + FEDERATION_PING_INTERVAL
            )

            hosts: StrCollection = (
                await self._storage_controllers.state.get_current_hosts_in_room(
                    member.room_id
                )
            )
            hosts = await filter_destinations_by_retry_limiter(
                hosts,
                clock=self.clock,
                store=self.store,
            )
            for domain in hosts:
                if not self.is_mine_server_name(domain):
                    logger.debug("sending typing update to %s", domain)
                    self.federation.build_and_send_edu(
                        destination=domain,
                        edu_type=EduTypes.TYPING,
                        content={
                            "room_id": member.room_id,
                            "user_id": member.user_id,
                            "typing": typing,
                        },
                        key=member,
                    )
        except Exception:
            logger.exception("Error pushing typing notif to remotes")

    def process_replication_rows(
        self, token: int, rows: List[TypingStream.TypingStreamRow]
    ) -> None:
        """Should be called whenever we receive updates for typing stream."""

        if self._latest_room_serial > token:
            # The typing worker has gone backwards (e.g. it may have restarted).
            # To prevent inconsistent data, just clear everything.
            logger.info("Typing handler stream went backwards; resetting")
            self._reset()

        # Set the latest serial token to whatever the server gave us.
        self._latest_room_serial = token

        for row in rows:
            self._room_serials[row.room_id] = token

            prev_typing = self._room_typing.get(row.room_id, set())
            now_typing = set(row.user_ids)
            self._room_typing[row.room_id] = now_typing
            self._rooms_updated.add(row.room_id)

            if self.federation:
                run_as_background_process(
                    "_send_changes_in_typing_to_remotes",
                    self._send_changes_in_typing_to_remotes,
                    row.room_id,
                    prev_typing,
                    now_typing,
                )

    async def _send_changes_in_typing_to_remotes(
        self, room_id: str, prev_typing: Set[str], now_typing: Set[str]
    ) -> None:
        """Process a change in typing of a room from replication, sending EDUs
        for any local users.
        """

        if not self.federation:
            return

        for user_id in now_typing - prev_typing:
            if self.is_mine_id(user_id):
                await self._push_remote(RoomMember(room_id, user_id), True)

        for user_id in prev_typing - now_typing:
            if self.is_mine_id(user_id):
                await self._push_remote(RoomMember(room_id, user_id), False)

    def get_current_token(self) -> int:
        return self._latest_room_serial

    def _prune_old_typing(self) -> None:
        """Prune rooms that haven't seen typing updates since last time.

        This is safe to do as clients should time out old typing notifications.
        """
        stale_rooms = self._room_serials.keys() - self._rooms_updated

        for room_id in stale_rooms:
            self._room_serials.pop(room_id, None)
            self._room_typing.pop(room_id, None)

        self._rooms_updated = set()


class TypingWriterHandler(FollowerTypingHandler):
    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        assert hs.get_instance_name() in hs.config.worker.writers.typing

        self.auth = hs.get_auth()
        self.notifier = hs.get_notifier()
        self.event_auth_handler = hs.get_event_auth_handler()

        self.hs = hs

        hs.get_federation_registry().register_edu_handler(
            EduTypes.TYPING, self._recv_edu
        )

        hs.get_distributor().observe("user_left_room", self.user_left_room)

        # clock time we expect to stop
        self._member_typing_until: Dict[RoomMember, int] = {}

        # caches which room_ids changed at which serials
        self._typing_stream_change_cache = StreamChangeCache(
            "TypingStreamChangeCache", self._latest_room_serial
        )

    def _handle_timeout_for_member(self, now: int, member: RoomMember) -> None:
        super()._handle_timeout_for_member(now, member)

        if not self.is_typing(member):
            # Nothing to do if they're no longer typing
            return

        until = self._member_typing_until.get(member, None)
        if not until or until <= now:
            logger.info("Timing out typing for: %s", member.user_id)
            self._stopped_typing(member)
            return

    async def started_typing(
        self, target_user: UserID, requester: Requester, room_id: str, timeout: int
    ) -> None:
        target_user_id = target_user.to_string()

        if not self.is_mine_id(target_user_id):
            raise SynapseError(400, "User is not hosted on this homeserver")

        if target_user != requester.user:
            raise AuthError(400, "Cannot set another user's typing state")

        if requester.shadow_banned:
            # We randomly sleep a bit just to annoy the requester.
            await self.clock.sleep(random.randint(1, 10))
            raise ShadowBanError()

        await self.auth.check_user_in_room(room_id, requester)

        logger.debug("%s has started typing in %s", target_user_id, room_id)

        member = RoomMember(room_id=room_id, user_id=target_user_id)

        was_present = member.user_id in self._room_typing.get(room_id, set())

        now = self.clock.time_msec()
        self._member_typing_until[member] = now + timeout

        self.wheel_timer.insert(now=now, obj=member, then=now + timeout)

        if was_present:
            # No point sending another notification
            return

        self._push_update(member=member, typing=True)

    async def stopped_typing(
        self, target_user: UserID, requester: Requester, room_id: str
    ) -> None:
        target_user_id = target_user.to_string()

        if not self.is_mine_id(target_user_id):
            raise SynapseError(400, "User is not hosted on this homeserver")

        if target_user != requester.user:
            raise AuthError(400, "Cannot set another user's typing state")

        if requester.shadow_banned:
            # We randomly sleep a bit just to annoy the requester.
            await self.clock.sleep(random.randint(1, 10))
            raise ShadowBanError()

        await self.auth.check_user_in_room(room_id, requester)

        logger.debug("%s has stopped typing in %s", target_user_id, room_id)

        member = RoomMember(room_id=room_id, user_id=target_user_id)

        self._stopped_typing(member)

    def user_left_room(self, user: UserID, room_id: str) -> None:
        user_id = user.to_string()
        if self.is_mine_id(user_id):
            member = RoomMember(room_id=room_id, user_id=user_id)
            self._stopped_typing(member)

    def _stopped_typing(self, member: RoomMember) -> None:
        if member.user_id not in self._room_typing.get(member.room_id, set()):
            # No point
            return

        self._member_typing_until.pop(member, None)
        self._member_last_federation_poke.pop(member, None)

        self._push_update(member=member, typing=False)

    def _push_update(self, member: RoomMember, typing: bool) -> None:
        if self.hs.is_mine_id(member.user_id):
            # Only send updates for changes to our own users.
            run_as_background_process(
                "typing._push_remote", self._push_remote, member, typing
            )

        self._push_update_local(member=member, typing=typing)

    async def _recv_edu(self, origin: str, content: JsonDict) -> None:
        room_id = content["room_id"]
        user_id = content["user_id"]

        # If we're not in the room just ditch the event entirely. This is
        # probably an old server that has come back and thinks we're still in
        # the room (or we've been rejoined to the room by a state reset).
        is_in_room = await self.event_auth_handler.is_host_in_room(
            room_id, self.server_name
        )
        if not is_in_room:
            logger.info(
                "Ignoring typing update for room %r from server %s as we're not in the room",
                room_id,
                origin,
            )
            return

        member = RoomMember(user_id=user_id, room_id=room_id)

        # Check that the string is a valid user id
        user = UserID.from_string(user_id)

        if user.domain != origin:
            logger.info(
                "Got typing update from %r with bad 'user_id': %r", origin, user_id
            )
            return

        # Let's check that the origin server is in the room before accepting the typing
        # event. We don't want to block waiting on a partial state so take an
        # approximation if needed.
        domains = await self._storage_controllers.state.get_current_hosts_in_room_or_partial_state_approximation(
            room_id
        )

        if user.domain in domains:
            logger.info("Got typing update from %s: %r", user_id, content)
            now = self.clock.time_msec()
            self._member_typing_until[member] = now + FEDERATION_TIMEOUT
            self.wheel_timer.insert(now=now, obj=member, then=now + FEDERATION_TIMEOUT)
            self._push_update_local(member=member, typing=content["typing"])

    def _push_update_local(self, member: RoomMember, typing: bool) -> None:
        room_set = self._room_typing.setdefault(member.room_id, set())
        if typing:
            room_set.add(member.user_id)
        else:
            room_set.discard(member.user_id)

        self._latest_room_serial += 1
        self._room_serials[member.room_id] = self._latest_room_serial
        self._typing_stream_change_cache.entity_has_changed(
            member.room_id, self._latest_room_serial
        )
        self._rooms_updated.add(member.room_id)

        self.notifier.on_new_event(
            StreamKeyType.TYPING, self._latest_room_serial, rooms=[member.room_id]
        )

    async def get_all_typing_updates(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> Tuple[List[Tuple[int, list]], int, bool]:
        """Get updates for typing replication stream.

        Args:
            instance_name: The writer we want to fetch updates from. Unused
                here since there is only ever one writer.
            last_id: The token to fetch updates from. Exclusive.
            current_id: The token to fetch updates up to. Inclusive.
            limit: The requested limit for the number of rows to return. The
                function may return more or fewer rows.

        Returns:
            A tuple consisting of: the updates, a token to use to fetch
            subsequent updates, and whether we returned fewer rows than exists
            between the requested tokens due to the limit.

            The token returned can be used in a subsequent call to this
            function to get further updates.

            The updates are a list of 2-tuples of stream ID and the row data
        """

        if last_id == current_id:
            return [], current_id, False

        result = self._typing_stream_change_cache.get_all_entities_changed(last_id)

        if result.hit:
            changed_rooms: Iterable[str] = result.entities
        else:
            changed_rooms = self._room_serials

        rows = []
        for room_id in changed_rooms:
            serial = self._room_serials.get(room_id)
            if serial and last_id < serial <= current_id:
                typing = self._room_typing.get(room_id, set())
                rows.append((serial, [room_id, list(typing)]))
        rows.sort()

        limited = False
        # We, unusually, use a strict limit here as we have all the rows in
        # memory rather than pulling them out of the database with a `LIMIT ?`
        # clause.
        if len(rows) > limit:
            rows = rows[:limit]
            current_id = rows[-1][0]
            limited = True

        return rows, current_id, limited

    def process_replication_rows(
        self, token: int, rows: List[TypingStream.TypingStreamRow]
    ) -> None:
        # The writing process should never get updates from replication.
        raise Exception("Typing writer instance got typing info over replication")


class TypingNotificationEventSource(EventSource[int, JsonMapping]):
    def __init__(self, hs: "HomeServer"):
        self._main_store = hs.get_datastores().main
        self.clock = hs.get_clock()
        # We can't call get_typing_handler here because there's a cycle:
        #
        #   Typing -> Notifier -> TypingNotificationEventSource -> Typing
        #
        self.get_typing_handler = hs.get_typing_handler

    def _make_event_for(self, room_id: str) -> JsonMapping:
        typing = self.get_typing_handler()._room_typing[room_id]
        return {
            "type": EduTypes.TYPING,
            "room_id": room_id,
            "content": {"user_ids": list(typing)},
        }

    async def get_new_events_as(
        self, from_key: int, service: ApplicationService
    ) -> Tuple[List[JsonMapping], int]:
        """Returns a set of new typing events that an appservice
        may be interested in.

        Args:
            from_key: the stream position at which events should be fetched from.
            service: The appservice which may be interested.

        Returns:
            A two-tuple containing the following:
                * A list of json dictionaries derived from typing events that the
                  appservice may be interested in.
                * The latest known room serial.
        """
        with Measure(self.clock, "typing.get_new_events_as"):
            handler = self.get_typing_handler()

            events = []

            # Work on a copy of things here as these may change in the handler while
            # waiting for the AS `is_interested_in_room` call to complete.
            # Shallow copy is safe as no nested data is present.
            latest_room_serial = handler._latest_room_serial
            room_serials = handler._room_serials.copy()

            for room_id, serial in room_serials.items():
                if serial <= from_key:
                    continue

                if not await service.is_interested_in_room(room_id, self._main_store):
                    continue

                events.append(self._make_event_for(room_id))

            return events, latest_room_serial

    async def get_new_events(
        self,
        user: UserID,
        from_key: int,
        limit: int,
        room_ids: Iterable[str],
        is_guest: bool,
        explicit_room_id: Optional[str] = None,
        to_key: Optional[int] = None,
    ) -> Tuple[List[JsonMapping], int]:
        """
        Find typing notifications for given rooms (> `from_token` and <= `to_token`)
        """

        with Measure(self.clock, "typing.get_new_events"):
            from_key = int(from_key)
            handler = self.get_typing_handler()

            events = []
            for room_id in room_ids:
                if room_id not in handler._room_serials:
                    continue
                if handler._room_serials[room_id] <= from_key or (
                    to_key is not None and handler._room_serials[room_id] > to_key
                ):
                    continue

                events.append(self._make_event_for(room_id))

            return events, handler._latest_room_serial

    def get_current_key(self) -> int:
        return self.get_typing_handler()._latest_room_serial
