#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014 - 2016 OpenMarket Ltd
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
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Literal,
    Mapping,
    TypeVar,
    overload,
)

import attr
from prometheus_client import Counter

from twisted.internet import defer
from twisted.internet.defer import Deferred

from synapse.api.constants import EduTypes, EventTypes, HistoryVisibility, Membership
from synapse.api.errors import AuthError
from synapse.events import EventBase
from synapse.handlers.presence import format_user_presence_state
from synapse.logging import issue9533_logger
from synapse.logging.context import PreserveLoggingContext
from synapse.logging.opentracing import log_kv, start_active_span
from synapse.metrics import SERVER_NAME_LABEL, LaterGauge
from synapse.streams.config import PaginationConfig
from synapse.types import (
    ISynapseReactor,
    JsonDict,
    MultiWriterStreamToken,
    PersistedEventPosition,
    RoomStreamToken,
    StrCollection,
    StreamKeyType,
    StreamToken,
    UserID,
)
from synapse.util.async_helpers import (
    timeout_deferred,
)
from synapse.util.stringutils import shortstr
from synapse.visibility import filter_events_for_client

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# FIXME: Unused metric, remove if not needed.
notified_events_counter = Counter(
    "synapse_notifier_notified_events", "", labelnames=[SERVER_NAME_LABEL]
)

users_woken_by_stream_counter = Counter(
    "synapse_notifier_users_woken_by_stream",
    "",
    labelnames=["stream", SERVER_NAME_LABEL],
)


notifier_listeners_gauge = LaterGauge(
    name="synapse_notifier_listeners",
    desc="",
    labelnames=[SERVER_NAME_LABEL],
)

notifier_rooms_gauge = LaterGauge(
    name="synapse_notifier_rooms",
    desc="",
    labelnames=[SERVER_NAME_LABEL],
)
notifier_users_gauge = LaterGauge(
    name="synapse_notifier_users",
    desc="",
    labelnames=[SERVER_NAME_LABEL],
)

T = TypeVar("T")


# TODO(paul): Should be shared somewhere
def count(func: Callable[[T], bool], it: Iterable[T]) -> int:
    """Return the number of items in it for which func returns true."""
    n = 0
    for x in it:
        if func(x):
            n += 1
    return n


class _NotifierUserStream:
    """This represents a user connected to the event stream.
    It tracks the most recent stream token for that user.
    At a given point a user may have a number of streams listening for
    events.

    This listener will also keep track of which rooms it is listening in
    so that it can remove itself from the indexes in the Notifier class.
    """

    def __init__(
        self,
        reactor: ISynapseReactor,
        user_id: str,
        rooms: StrCollection,
        current_token: StreamToken,
        time_now_ms: int,
    ):
        self.reactor = reactor
        self.user_id = user_id
        self.rooms = set(rooms)

        # The last token for which we should wake up any streams that have a
        # token that comes before it. This gets updated every time we get poked.
        # We start it at the current token since if we get any streams
        # that have a token from before we have no idea whether they should be
        # woken up or not, so lets just wake them up.
        self.current_token = current_token
        self.last_notified_ms = time_now_ms

        # Set of listeners that we need to wake up when there has been a change.
        self.listeners: set[Deferred[StreamToken]] = set()

    def update_and_fetch_deferreds(
        self,
        current_token: StreamToken,
        time_now_ms: int,
    ) -> Collection["Deferred[StreamToken]"]:
        """Update the stream for this user because of a new event from an
        event source, and return the set of deferreds to wake up.

        Args:
            current_token: The new current token.
            time_now_ms: The current time in milliseconds.

        Returns:
            The set of deferreds that need to be called.
        """
        self.current_token = current_token
        self.last_notified_ms = time_now_ms

        listeners = self.listeners
        self.listeners = set()

        return listeners

    def remove(self, notifier: "Notifier") -> None:
        """Remove this listener from all the indexes in the Notifier
        it knows about.
        """

        for room in self.rooms:
            lst = notifier.room_to_user_streams.get(room, set())
            lst.discard(self)

            if not lst:
                notifier.room_to_user_streams.pop(room, None)

        notifier.user_to_user_stream.pop(self.user_id)

    def count_listeners(self) -> int:
        return len(self.listeners)

    def new_listener(self, token: StreamToken) -> "Deferred[StreamToken]":
        """Returns a deferred that is resolved when there is a new token
        greater than the given token.

        Args:
            token: The token from which we are streaming from, i.e. we shouldn't
                notify for things that happened before this.
        """
        # Immediately wake up stream if something has already since happened
        # since their last token.
        if token != self.current_token:
            return defer.succeed(self.current_token)

        # Create a new deferred and add it to the set of listeners. We add a
        # cancel handler to remove it from the set again, to handle timeouts.
        deferred: "Deferred[StreamToken]" = Deferred(
            canceller=lambda d: self.listeners.discard(d)
        )
        self.listeners.add(deferred)

        return deferred


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventStreamResult:
    events: list[JsonDict | EventBase]
    start_token: StreamToken
    end_token: StreamToken

    def __bool__(self) -> bool:
        return bool(self.events)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _PendingRoomEventEntry:
    event_pos: PersistedEventPosition
    extra_users: Collection[UserID]

    room_id: str
    type: str
    state_key: str | None
    membership: str | None


class Notifier:
    """This class is responsible for notifying any listeners when there are
    new events available for it.

    Primarily used from the /events stream.
    """

    UNUSED_STREAM_EXPIRY_MS = 10 * 60 * 1000

    def __init__(self, hs: "HomeServer"):
        self.user_to_user_stream: dict[str, _NotifierUserStream] = {}
        self.room_to_user_streams: dict[str, set[_NotifierUserStream]] = {}

        self.hs = hs
        self.server_name = hs.hostname
        self._storage_controllers = hs.get_storage_controllers()
        self.event_sources = hs.get_event_sources()
        self.store = hs.get_datastores().main
        self.pending_new_room_events: list[_PendingRoomEventEntry] = []

        self._replication_notifier = hs.get_replication_notifier()
        self._new_join_in_room_callbacks: list[Callable[[str, str], None]] = []

        self._federation_client = hs.get_federation_http_client()

        self._third_party_rules = hs.get_module_api_callbacks().third_party_event_rules

        # List of callbacks to be notified when a lock is released
        self._lock_released_callback: list[Callable[[str, str, str], None]] = []

        self.reactor = hs.get_reactor()
        self.clock = hs.get_clock()
        self.appservice_handler = hs.get_application_service_handler()
        self._pusher_pool = hs.get_pusherpool()

        self.federation_sender = None
        if hs.should_send_federation():
            self.federation_sender = hs.get_federation_sender()

        self.state_handler = hs.get_state_handler()

        self.clock.looping_call(
            self.remove_expired_streams, self.UNUSED_STREAM_EXPIRY_MS
        )

        # This is not a very cheap test to perform, but it's only executed
        # when rendering the metrics page, which is likely once per minute at
        # most when scraping it.
        #
        # Ideally, we'd use `Mapping[tuple[str], int]` here but mypy doesn't like it.
        # This is close enough and better than a type ignore.
        def count_listeners() -> Mapping[tuple[str, ...], int]:
            all_user_streams: set[_NotifierUserStream] = set()

            for streams in list(self.room_to_user_streams.values()):
                all_user_streams |= streams
            for stream in list(self.user_to_user_stream.values()):
                all_user_streams.add(stream)

            return {
                (self.server_name,): sum(
                    stream.count_listeners() for stream in all_user_streams
                )
            }

        notifier_listeners_gauge.register_hook(
            homeserver_instance_id=hs.get_instance_id(), hook=count_listeners
        )
        notifier_rooms_gauge.register_hook(
            homeserver_instance_id=hs.get_instance_id(),
            hook=lambda: {
                (self.server_name,): count(
                    bool, list(self.room_to_user_streams.values())
                )
            },
        )
        notifier_users_gauge.register_hook(
            homeserver_instance_id=hs.get_instance_id(),
            hook=lambda: {(self.server_name,): len(self.user_to_user_stream)},
        )

    def add_replication_callback(self, cb: Callable[[], None]) -> None:
        """Add a callback that will be called when some new data is available.
        Callback is not given any arguments. It should *not* return a Deferred - if
        it needs to do any asynchronous work, a background thread should be started and
        wrapped with run_as_background_process.
        """
        self._replication_notifier.add_replication_callback(cb)

    def add_new_join_in_room_callback(self, cb: Callable[[str, str], None]) -> None:
        """Add a callback that will be called when a user joins a room.

        This only fires on genuine membership changes, e.g. "invite" -> "join".
        Membership transitions like "join" -> "join" (for e.g. displayname changes) do
        not trigger the callback.

        When called, the callback receives two arguments: the event ID and the room ID.
        It should *not* return a Deferred - if it needs to do any asynchronous work, a
        background thread should be started and wrapped with run_as_background_process.
        """
        self._new_join_in_room_callbacks.append(cb)

    async def on_new_room_events(
        self,
        events_and_pos: list[tuple[EventBase, PersistedEventPosition]],
        max_room_stream_token: RoomStreamToken,
        extra_users: Collection[UserID] | None = None,
    ) -> None:
        """Creates a _PendingRoomEventEntry for each of the listed events and calls
        notify_new_room_events with the results."""
        event_entries = []
        for event, pos in events_and_pos:
            entry = self.create_pending_room_event_entry(
                pos,
                extra_users,
                event.room_id,
                event.type,
                event.get("state_key"),
                event.content.get("membership"),
            )
            event_entries.append((entry, event.event_id))
        await self.notify_new_room_events(event_entries, max_room_stream_token)

    async def on_un_partial_stated_room(
        self,
        room_id: str,
        new_token: int,
    ) -> None:
        """Used by the resync background processes to wake up all listeners
        of this room when it is un-partial-stated.

        It will also notify replication listeners of the change in stream.
        """

        # Wake up all related user stream notifiers
        user_streams = self.room_to_user_streams.get(room_id, set())
        time_now_ms = self.clock.time_msec()
        current_token = self.event_sources.get_current_token()

        listeners: list["Deferred[StreamToken]"] = []
        for user_stream in user_streams:
            try:
                listeners.extend(
                    user_stream.update_and_fetch_deferreds(current_token, time_now_ms)
                )
            except Exception:
                logger.exception("Failed to notify listener")

        with PreserveLoggingContext():
            for listener in listeners:
                listener.callback(current_token)

        users_woken_by_stream_counter.labels(
            stream=StreamKeyType.UN_PARTIAL_STATED_ROOMS,
            **{SERVER_NAME_LABEL: self.server_name},
        ).inc(len(user_streams))

        # Poke the replication so that other workers also see the write to
        # the un-partial-stated rooms stream.
        self.notify_replication()

    async def notify_new_room_events(
        self,
        event_entries: list[tuple[_PendingRoomEventEntry, str]],
        max_room_stream_token: RoomStreamToken,
    ) -> None:
        """Used by handlers to inform the notifier something has happened
        in the room, room event wise.

        This triggers the notifier to wake up any listeners that are
        listening to the room, and any listeners for the users in the
        `extra_users` param.

        This also notifies modules listening on new events via the
        `on_new_event` callback.

        The events can be persisted out of order. The notifier will wait
        until all previous events have been persisted before notifying
        the client streams.
        """
        for event_entry, event_id in event_entries:
            self.pending_new_room_events.append(event_entry)
            await self._third_party_rules.on_new_event(event_id)

        self._notify_pending_new_room_events(max_room_stream_token)

        self.notify_replication()

    def create_pending_room_event_entry(
        self,
        event_pos: PersistedEventPosition,
        extra_users: Collection[UserID] | None,
        room_id: str,
        event_type: str,
        state_key: str | None,
        membership: str | None,
    ) -> _PendingRoomEventEntry:
        """Creates and returns a _PendingRoomEventEntry"""
        return _PendingRoomEventEntry(
            event_pos=event_pos,
            extra_users=extra_users or [],
            room_id=room_id,
            type=event_type,
            state_key=state_key,
            membership=membership,
        )

    def _notify_pending_new_room_events(
        self, max_room_stream_token: RoomStreamToken
    ) -> None:
        """Notify for the room events that were queued waiting for a previous
        event to be persisted.
        Args:
            max_room_stream_token: The highest stream_id below which all
                events have been persisted.
        """
        pending = self.pending_new_room_events
        self.pending_new_room_events = []

        users: set[UserID] = set()
        rooms: set[str] = set()

        for entry in pending:
            if entry.event_pos.persisted_after(max_room_stream_token):
                self.pending_new_room_events.append(entry)
            else:
                if (
                    entry.type == EventTypes.Member
                    and entry.membership == Membership.JOIN
                    and entry.state_key
                ):
                    self._user_joined_room(entry.state_key, entry.room_id)

                users.update(entry.extra_users)
                rooms.add(entry.room_id)

        if users or rooms:
            self.on_new_event(
                StreamKeyType.ROOM,
                max_room_stream_token,
                users=users,
                rooms=rooms,
            )
            self._on_updated_room_token(max_room_stream_token)

    def _on_updated_room_token(self, max_room_stream_token: RoomStreamToken) -> None:
        """Poke services that might care that the room position has been
        updated.
        """

        # poke any interested application service.
        self._notify_app_services(max_room_stream_token)
        self._notify_pusher_pool(max_room_stream_token)

        if self.federation_sender:
            self.federation_sender.notify_new_events(max_room_stream_token)

    def _notify_app_services(self, max_room_stream_token: RoomStreamToken) -> None:
        try:
            self.appservice_handler.notify_interested_services(max_room_stream_token)
        except Exception:
            logger.exception("Error notifying application services of event")

    def _notify_pusher_pool(self, max_room_stream_token: RoomStreamToken) -> None:
        try:
            self._pusher_pool.on_new_notifications(max_room_stream_token)
        except Exception:
            logger.exception("Error pusher pool of event")

    @overload
    def on_new_event(
        self,
        stream_key: Literal[StreamKeyType.ROOM],
        new_token: RoomStreamToken,
        users: Collection[str | UserID] | None = None,
        rooms: StrCollection | None = None,
    ) -> None: ...

    @overload
    def on_new_event(
        self,
        stream_key: Literal[StreamKeyType.RECEIPT],
        new_token: MultiWriterStreamToken,
        users: Collection[str | UserID] | None = None,
        rooms: StrCollection | None = None,
    ) -> None: ...

    @overload
    def on_new_event(
        self,
        stream_key: Literal[
            StreamKeyType.ACCOUNT_DATA,
            StreamKeyType.DEVICE_LIST,
            StreamKeyType.PRESENCE,
            StreamKeyType.PUSH_RULES,
            StreamKeyType.TO_DEVICE,
            StreamKeyType.TYPING,
            StreamKeyType.UN_PARTIAL_STATED_ROOMS,
            StreamKeyType.THREAD_SUBSCRIPTIONS,
        ],
        new_token: int,
        users: Collection[str | UserID] | None = None,
        rooms: StrCollection | None = None,
    ) -> None: ...

    def on_new_event(
        self,
        stream_key: StreamKeyType,
        new_token: int | RoomStreamToken | MultiWriterStreamToken,
        users: Collection[str | UserID] | None = None,
        rooms: StrCollection | None = None,
    ) -> None:
        """Used to inform listeners that something has happened event wise.

        Will wake up all listeners for the given users and rooms.

        Args:
            stream_key: The stream the event came from.
            new_token: The value of the new stream token.
            users: The users that should be informed of the new event.
            rooms: A collection of room IDs for which each joined member will be
                informed of the new event.
        """
        users = users or []
        rooms = rooms or []

        user_streams: set[_NotifierUserStream] = set()

        log_kv(
            {
                "waking_up_explicit_users": len(users),
                "waking_up_explicit_rooms": len(rooms),
                "users": shortstr(users),
                "rooms": shortstr(rooms),
                "stream": stream_key,
                "stream_id": new_token,
            }
        )

        # Only calculate which user streams to wake up if there are, in fact,
        # any user streams registered.
        if self.user_to_user_stream or self.room_to_user_streams:
            for user in users:
                user_stream = self.user_to_user_stream.get(str(user))
                if user_stream is not None:
                    user_streams.add(user_stream)

            for room in rooms:
                user_streams |= self.room_to_user_streams.get(room, set())

            if stream_key == StreamKeyType.TO_DEVICE:
                issue9533_logger.debug(
                    "to-device messages stream id %s, awaking streams for %s",
                    new_token,
                    users,
                )

            time_now_ms = self.clock.time_msec()
            current_token = self.event_sources.get_current_token()
            listeners: list["Deferred[StreamToken]"] = []
            for user_stream in user_streams:
                try:
                    listeners.extend(
                        user_stream.update_and_fetch_deferreds(
                            current_token, time_now_ms
                        )
                    )
                except Exception:
                    logger.exception("Failed to notify listener")

            # We resolve all these deferreds in one go so that we only need to
            # call `PreserveLoggingContext` once, as it has a bunch of overhead
            # (to calculate performance stats)
            if listeners:
                with PreserveLoggingContext():
                    for listener in listeners:
                        listener.callback(current_token)

            if user_streams:
                users_woken_by_stream_counter.labels(
                    stream=stream_key,
                    **{SERVER_NAME_LABEL: self.server_name},
                ).inc(len(user_streams))

        self.notify_replication()

        # Notify appservices.
        try:
            self.appservice_handler.notify_interested_services_ephemeral(
                stream_key,
                new_token,
                users,
            )
        except Exception:
            logger.exception("Error notifying application services of ephemeral events")

    def on_new_replication_data(self) -> None:
        """Used to inform replication listeners that something has happened
        without waking up any of the normal user event streams"""
        self.notify_replication()

    async def wait_for_events(
        self,
        user_id: str,
        timeout: int,
        callback: Callable[[StreamToken, StreamToken], Awaitable[T]],
        room_ids: StrCollection | None = None,
        from_token: StreamToken = StreamToken.START,
    ) -> T:
        """Wait until the callback returns a non empty response or the
        timeout fires.
        """
        user_stream = self.user_to_user_stream.get(user_id)
        if user_stream is None:
            current_token = self.event_sources.get_current_token()
            if room_ids is None:
                room_ids = await self.store.get_rooms_for_user(user_id)
            user_stream = _NotifierUserStream(
                reactor=self.reactor,
                user_id=user_id,
                rooms=room_ids,
                current_token=current_token,
                time_now_ms=self.clock.time_msec(),
            )
            self._register_with_keys(user_stream)

        result = None
        prev_token = from_token
        if timeout:
            end_time = self.clock.time_msec() + timeout

            while not result:
                with start_active_span("wait_for_events"):
                    try:
                        now = self.clock.time_msec()
                        if end_time <= now:
                            break

                        # Now we wait for the _NotifierUserStream to be told there
                        # is a new token.
                        listener = user_stream.new_listener(prev_token)
                        listener = timeout_deferred(
                            deferred=listener,
                            timeout=(end_time - now) / 1000.0,
                            # We don't track these calls since they are constantly being
                            # overridden by new calls to /sync and they don't hold the
                            # `HomeServer` in memory on shutdown. It is safe to let them
                            # timeout of their own accord after shutting down since it
                            # won't delay shutdown and there won't be any adverse
                            # behaviour.
                            cancel_on_shutdown=False,
                            clock=self.hs.get_clock(),
                        )

                        log_kv(
                            {
                                "wait_for_events": "sleep",
                                "token": prev_token,
                            }
                        )

                        with PreserveLoggingContext():
                            await listener

                        log_kv(
                            {
                                "wait_for_events": "woken",
                                "token": user_stream.current_token,
                            }
                        )

                        current_token = user_stream.current_token

                        result = await callback(prev_token, current_token)
                        log_kv(
                            {
                                "wait_for_events": "result",
                                "result": bool(result),
                            }
                        )
                        if result:
                            break

                        # Update the prev_token to the current_token since nothing
                        # has happened between the old prev_token and the current_token
                        prev_token = current_token
                    except defer.TimeoutError:
                        log_kv({"wait_for_events": "timeout"})
                        break
                    except defer.CancelledError:
                        log_kv({"wait_for_events": "cancelled"})
                        break

        if result is None:
            # This happened if there was no timeout or if the timeout had
            # already expired.
            current_token = user_stream.current_token
            result = await callback(prev_token, current_token)

        return result

    async def get_events_for(
        self,
        user: UserID,
        pagination_config: PaginationConfig,
        timeout: int,
        is_guest: bool = False,
        explicit_room_id: str | None = None,
    ) -> EventStreamResult:
        """For the given user and rooms, return any new events for them. If
        there are no new events wait for up to `timeout` milliseconds for any
        new events to happen before returning.

        If explicit_room_id is not set, the user's joined rooms will be polled
        for events.
        If explicit_room_id is set, that room will be polled for events only if
        it is world readable or the user has joined the room.
        """
        if pagination_config.from_token:
            from_token = pagination_config.from_token
        else:
            from_token = self.event_sources.get_current_token()

        limit = pagination_config.limit

        room_ids, is_joined = await self._get_room_ids(user, explicit_room_id)
        is_peeking = not is_joined

        async def check_for_updates(
            before_token: StreamToken, after_token: StreamToken
        ) -> EventStreamResult:
            if after_token == before_token:
                return EventStreamResult([], from_token, from_token)

            # The events fetched from each source are a JsonDict, EventBase, or
            # UserPresenceState, but see below for UserPresenceState being
            # converted to JsonDict.
            events: list[JsonDict | EventBase] = []
            end_token = from_token

            for keyname, source in self.event_sources.sources.get_sources():
                before_id = before_token.get_field(keyname)
                after_id = after_token.get_field(keyname)
                if before_id == after_id:
                    continue

                new_events, new_key = await source.get_new_events(
                    user=user,
                    from_key=from_token.get_field(keyname),
                    limit=limit,
                    is_guest=is_peeking,
                    room_ids=room_ids,
                    explicit_room_id=explicit_room_id,
                )

                if keyname == StreamKeyType.ROOM:
                    new_events = await filter_events_for_client(
                        self._storage_controllers,
                        user.to_string(),
                        new_events,
                        is_peeking=is_peeking,
                    )
                elif keyname == StreamKeyType.PRESENCE:
                    now = self.clock.time_msec()
                    new_events[:] = [
                        {
                            "type": EduTypes.PRESENCE,
                            "content": format_user_presence_state(event, now),
                        }
                        for event in new_events
                    ]

                events.extend(new_events)
                end_token = end_token.copy_and_replace(keyname, new_key)

            return EventStreamResult(events, from_token, end_token)

        user_id_for_stream = user.to_string()
        if is_peeking:
            # Internally, the notifier keeps an event stream per user_id.
            # This is used by both /sync and /events.
            # We want /events to be used for peeking independently of /sync,
            # without polluting its contents. So we invent an illegal user ID
            # (which thus cannot clash with any real users) for keying peeking
            # over /events.
            #
            # I am sorry for what I have done.
            user_id_for_stream = "_PEEKING_%s_%s" % (
                explicit_room_id,
                user_id_for_stream,
            )

        result = await self.wait_for_events(
            user_id_for_stream,
            timeout,
            check_for_updates,
            room_ids=room_ids,
            from_token=from_token,
        )

        return result

    async def wait_for_stream_token(self, stream_token: StreamToken) -> bool:
        """Wait for this worker to catch up with the given stream token."""
        current_token = self.event_sources.get_current_token()
        if stream_token.is_before_or_eq(current_token):
            return True

        # Work around a bug where older Synapse versions gave out tokens "from
        # the future", i.e. that are ahead of the tokens persisted in the DB.
        stream_token = await self.event_sources.bound_future_token(stream_token)

        start = self.clock.time_msec()
        logged = False
        while True:
            current_token = self.event_sources.get_current_token()
            if stream_token.is_before_or_eq(current_token):
                return True

            now = self.clock.time_msec()

            if now - start > 10_000:
                return False

            if not logged:
                logger.info(
                    "Waiting for current token to reach %s; currently at %s",
                    stream_token,
                    current_token,
                )
                logged = True

            # TODO: be better
            await self.clock.sleep(0.5)

    async def _get_room_ids(
        self, user: UserID, explicit_room_id: str | None
    ) -> tuple[StrCollection, bool]:
        joined_room_ids = await self.store.get_rooms_for_user(user.to_string())
        if explicit_room_id:
            if explicit_room_id in joined_room_ids:
                return [explicit_room_id], True
            if await self._is_world_readable(explicit_room_id):
                return [explicit_room_id], False
            raise AuthError(403, "Non-joined access not allowed")
        return joined_room_ids, True

    async def _is_world_readable(self, room_id: str) -> bool:
        state = await self._storage_controllers.state.get_current_state_event(
            room_id, EventTypes.RoomHistoryVisibility, ""
        )
        if state and "history_visibility" in state.content:
            return (
                state.content["history_visibility"] == HistoryVisibility.WORLD_READABLE
            )
        else:
            return False

    def remove_expired_streams(self) -> None:
        time_now_ms = self.clock.time_msec()
        expired_streams = []
        expire_before_ts = time_now_ms - self.UNUSED_STREAM_EXPIRY_MS
        for stream in self.user_to_user_stream.values():
            if stream.count_listeners():
                continue
            if stream.last_notified_ms < expire_before_ts:
                expired_streams.append(stream)

        for expired_stream in expired_streams:
            expired_stream.remove(self)

    def _register_with_keys(self, user_stream: _NotifierUserStream) -> None:
        self.user_to_user_stream[user_stream.user_id] = user_stream

        for room in user_stream.rooms:
            s = self.room_to_user_streams.setdefault(room, set())
            s.add(user_stream)

    def _user_joined_room(self, user_id: str, room_id: str) -> None:
        new_user_stream = self.user_to_user_stream.get(user_id)
        if new_user_stream is not None:
            room_streams = self.room_to_user_streams.setdefault(room_id, set())
            room_streams.add(new_user_stream)
            new_user_stream.rooms.add(room_id)

    def notify_replication(self) -> None:
        """Notify the any replication listeners that there's a new event"""
        self._replication_notifier.notify_replication()

    def notify_user_joined_room(self, event_id: str, room_id: str) -> None:
        for cb in self._new_join_in_room_callbacks:
            cb(event_id, room_id)

    def notify_remote_server_up(self, server: str) -> None:
        """Notify any replication that a remote server has come back up"""
        # We call federation_sender directly rather than registering as a
        # callback as a) we already have a reference to it and b) it introduces
        # circular dependencies.
        if self.federation_sender:
            self.federation_sender.wake_destination(server)

        # Tell the federation client about the fact the server is back up, so
        # that any in flight requests can be immediately retried.
        self._federation_client.wake_destination(server)

    def add_lock_released_callback(
        self, callback: Callable[[str, str, str], None]
    ) -> None:
        """Add a function to be called whenever we are notified about a released lock."""
        self._lock_released_callback.append(callback)

    def notify_lock_released(
        self, instance_name: str, lock_name: str, lock_key: str
    ) -> None:
        """Notify the callbacks that a lock has been released."""
        for cb in self._lock_released_callback:
            cb(instance_name, lock_name, lock_key)


@attr.s(auto_attribs=True)
class ReplicationNotifier:
    """Tracks callbacks for things that need to know about stream changes.

    This is separate from the notifier to avoid circular dependencies.
    """

    _replication_callbacks: list[Callable[[], None]] = attr.Factory(list)

    def add_replication_callback(self, cb: Callable[[], None]) -> None:
        """Add a callback that will be called when some new data is available.
        Callback is not given any arguments. It should *not* return a Deferred - if
        it needs to do any asynchronous work, a background thread should be started and
        wrapped with run_as_background_process.
        """
        self._replication_callbacks.append(cb)

    def notify_replication(self) -> None:
        """Notify the any replication listeners that there's a new event"""
        for cb in self._replication_callbacks:
            cb()
