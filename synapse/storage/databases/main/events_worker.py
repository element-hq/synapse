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
import json
import logging
import threading
import weakref
from enum import Enum, auto
from itertools import chain
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Iterable,
    Literal,
    Mapping,
    MutableMapping,
    cast,
    overload,
)

import attr
from prometheus_client import Gauge

from twisted.internet import defer

from synapse.api.constants import Direction, EventTypes
from synapse.api.errors import NotFoundError, SynapseError
from synapse.api.room_versions import (
    KNOWN_ROOM_VERSIONS,
    EventFormatVersions,
    RoomVersion,
    RoomVersions,
)
from synapse.events import EventBase, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.events.utils import prune_event, strip_event
from synapse.logging.context import (
    PreserveLoggingContext,
    current_context,
    make_deferred_yieldable,
)
from synapse.logging.opentracing import (
    SynapseTags,
    set_tag,
    start_active_span,
    tag_args,
    trace,
)
from synapse.metrics import SERVER_NAME_LABEL
from synapse.metrics.background_process_metrics import (
    wrap_as_background_process,
)
from synapse.replication.tcp.streams import BackfillStream, UnPartialStatedEventStream
from synapse.replication.tcp.streams.events import EventsStream
from synapse.replication.tcp.streams.partial_state import UnPartialStatedEventStreamRow
from synapse.storage._base import SQLBaseStore, db_to_json, make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_tuple_in_list_sql_clause,
)
from synapse.storage.types import Cursor
from synapse.storage.util.id_generators import (
    AbstractStreamIdGenerator,
    MultiWriterIdGenerator,
)
from synapse.storage.util.sequence import build_sequence_generator
from synapse.types import JsonDict, get_domain_from_id
from synapse.types.state import StateFilter
from synapse.types.storage import _BackgroundUpdates
from synapse.util import unwrapFirstError
from synapse.util.async_helpers import ObservableDeferred, delay_cancellation
from synapse.util.caches.descriptors import cached, cachedList
from synapse.util.caches.lrucache import AsyncLruCache
from synapse.util.caches.stream_change_cache import StreamChangeCache
from synapse.util.cancellation import cancellable
from synapse.util.iterutils import batch_iter
from synapse.util.metrics import Measure

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class DatabaseCorruptionError(RuntimeError):
    """We found an event in the DB that has a persisted event ID that doesn't
    match its computed event ID."""

    def __init__(
        self, room_id: str, persisted_event_id: str, computed_event_id: str
    ) -> None:
        self.room_id = room_id
        self.persisted_event_id = persisted_event_id
        self.computed_event_id = computed_event_id

        message = (
            f"Database corruption: Event {persisted_event_id} in room {room_id} "
            f"from the database appears to have been modified (calculated "
            f"event id {computed_event_id})"
        )

        super().__init__(message)


# These values are used in the `enqueue_event` and `_fetch_loop` methods to
# control how we batch/bulk fetch events from the database.
# The values are plucked out of thing air to make initial sync run faster
# on jki.re
# TODO: Make these configurable.
EVENT_QUEUE_THREADS = 3  # Max number of threads that will fetch events
EVENT_QUEUE_ITERATIONS = 3  # No. times we block waiting for requests for events
EVENT_QUEUE_TIMEOUT_S = 0.1  # Timeout when waiting for requests for events


event_fetch_ongoing_gauge = Gauge(
    "synapse_event_fetch_ongoing",
    "The number of event fetchers that are running",
    labelnames=[SERVER_NAME_LABEL],
)


class InvalidEventError(Exception):
    """The event retrieved from the database is invalid and cannot be used."""


@attr.s(slots=True, auto_attribs=True)
class EventCacheEntry:
    event: EventBase
    redacted_event: EventBase | None


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _EventRow:
    """
    An event, as pulled from the database.

    Properties:
        event_id: The event ID of the event.

        stream_ordering: stream ordering for this event

        json: json-encoded event structure

        internal_metadata: json-encoded internal metadata dict

        format_version: The format of the event. Hopefully one of EventFormatVersions.
            'None' means the event predates EventFormatVersions (so the event is format V1).

        room_version_id: The version of the room which contains the event. Hopefully
            one of RoomVersions.

           Due to historical reasons, there may be a few events in the database which
           do not have an associated room; in this case None will be returned here.

        rejected_reason: if the event was rejected, the reason why.

        redactions: a list of event-ids which (claim to) redact this event.

        outlier: True if this event is an outlier.
    """

    event_id: str
    stream_ordering: int
    instance_name: str
    json: str
    internal_metadata: str
    format_version: int | None
    room_version_id: str | None
    rejected_reason: str | None
    redactions: list[str]
    outlier: bool


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventMetadata:
    """Event metadata returned by `get_metadata_for_event(..)`"""

    sender: str
    received_ts: int


class EventRedactBehaviour(Enum):
    """
    What to do when retrieving a redacted event from the database.
    """

    as_is = auto()
    redact = auto()
    block = auto()


class EventsWorkerStore(SQLBaseStore):
    # Whether to use dedicated DB threads for event fetching. This is only used
    # if there are multiple DB threads available. When used will lock the DB
    # thread for periods of time (so unit tests want to disable this when they
    # run DB transactions on the main thread). See EVENT_QUEUE_* for more
    # options controlling this.
    USE_DEDICATED_DB_THREADS_FOR_EVENT_FETCHING = True

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._stream_id_gen: MultiWriterIdGenerator
        self._backfill_id_gen: MultiWriterIdGenerator

        self._stream_id_gen = MultiWriterIdGenerator(
            db_conn=db_conn,
            db=database,
            notifier=hs.get_replication_notifier(),
            stream_name="events",
            server_name=self.server_name,
            instance_name=hs.get_instance_name(),
            tables=[
                ("events", "instance_name", "stream_ordering"),
                ("current_state_delta_stream", "instance_name", "stream_id"),
                ("ex_outlier_stream", "instance_name", "event_stream_ordering"),
            ],
            sequence_name="events_stream_seq",
            writers=hs.config.worker.writers.events,
        )
        self._backfill_id_gen = MultiWriterIdGenerator(
            db_conn=db_conn,
            db=database,
            notifier=hs.get_replication_notifier(),
            stream_name="backfill",
            server_name=self.server_name,
            instance_name=hs.get_instance_name(),
            tables=[
                ("events", "instance_name", "stream_ordering"),
                ("ex_outlier_stream", "instance_name", "event_stream_ordering"),
            ],
            sequence_name="events_backfill_stream_seq",
            positive=False,
            writers=hs.config.worker.writers.events,
        )

        events_max = self._stream_id_gen.get_current_token()
        curr_state_delta_prefill, min_curr_state_delta_id = self.db_pool.get_cache_dict(
            db_conn,
            "current_state_delta_stream",
            entity_column="room_id",
            stream_column="stream_id",
            max_value=events_max,  # As we share the stream id with events token
            limit=1000,
        )
        self._curr_state_delta_stream_cache: StreamChangeCache = StreamChangeCache(
            name="_curr_state_delta_stream_cache",
            server_name=self.server_name,
            current_stream_pos=min_curr_state_delta_id,
            prefilled_cache=curr_state_delta_prefill,
        )

        if hs.config.worker.run_background_tasks:
            # We periodically clean out old transaction ID mappings
            self.clock.looping_call(
                self._cleanup_old_transaction_ids,
                5 * 60 * 1000,
            )

        self._get_event_cache: AsyncLruCache[tuple[str], EventCacheEntry] = (
            AsyncLruCache(
                clock=hs.get_clock(),
                server_name=self.server_name,
                cache_name="*getEvent*",
                max_size=hs.config.caches.event_cache_size,
                # `extra_index_cb` Returns a tuple as that is the key type
                extra_index_cb=lambda _, v: (v.event.room_id,),
            )
        )

        # Map from event ID to a deferred that will result in a map from event
        # ID to cache entry. Note that the returned dict may not have the
        # requested event in it if the event isn't in the DB.
        self._current_event_fetches: dict[
            str, ObservableDeferred[dict[str, EventCacheEntry]]
        ] = {}

        # We keep track of the events we have currently loaded in memory so that
        # we can reuse them even if they've been evicted from the cache. We only
        # track events that don't need redacting in here (as then we don't need
        # to track redaction status).
        self._event_ref: MutableMapping[str, EventBase] = weakref.WeakValueDictionary()

        self._event_fetch_lock = threading.Condition()
        self._event_fetch_list: list[
            tuple[Iterable[str], "defer.Deferred[dict[str, _EventRow]]"]
        ] = []
        self._event_fetch_ongoing = 0
        event_fetch_ongoing_gauge.labels(**{SERVER_NAME_LABEL: self.server_name}).set(
            self._event_fetch_ongoing
        )

        # We define this sequence here so that it can be referenced from both
        # the DataStore and PersistEventStore.
        def get_chain_id_txn(txn: Cursor) -> int:
            txn.execute("SELECT COALESCE(max(chain_id), 0) FROM event_auth_chains")
            return cast(tuple[int], txn.fetchone())[0]

        self.event_chain_id_gen = build_sequence_generator(
            db_conn,
            database.engine,
            get_chain_id_txn,
            "event_auth_chain_id",
            table="event_auth_chains",
            id_column="chain_id",
        )

        self._un_partial_stated_events_stream_id_gen: AbstractStreamIdGenerator

        self._un_partial_stated_events_stream_id_gen = MultiWriterIdGenerator(
            db_conn=db_conn,
            db=database,
            notifier=hs.get_replication_notifier(),
            stream_name="un_partial_stated_event_stream",
            server_name=self.server_name,
            instance_name=hs.get_instance_name(),
            tables=[("un_partial_stated_event_stream", "instance_name", "stream_id")],
            sequence_name="un_partial_stated_event_stream_sequence",
            # TODO(faster_joins, multiple writers) Support multiple writers.
            writers=["master"],
        )

        # Added to accommodate some queries for the admin API in order to fetch/filter
        # membership events by when it was received
        self.db_pool.updates.register_background_index_update(
            update_name="events_received_ts_index",
            index_name="received_ts_idx",
            table="events",
            columns=("received_ts",),
            where_clause="type = 'm.room.member'",
        )

        # Added to support efficient reverse lookups on the foreign key
        # (user_id, device_id) when deleting devices.
        # We already had a UNIQUE index on these 4 columns but out-of-order
        # so replace that one.
        self.db_pool.updates.register_background_index_update(
            update_name="event_txn_id_device_id_txn_id2",
            index_name="event_txn_id_device_id_txn_id2",
            table="event_txn_id_device_id",
            columns=("user_id", "device_id", "room_id", "txn_id"),
            unique=True,
            replaces_index="event_txn_id_device_id_txn_id",
        )

        self._has_finished_sliding_sync_background_jobs = False
        """
        Flag to track when the sliding sync background jobs have
        finished (so we don't have to keep querying it every time)
        """

    def get_un_partial_stated_events_token(self, instance_name: str) -> int:
        return (
            self._un_partial_stated_events_stream_id_gen.get_current_token_for_writer(
                instance_name
            )
        )

    async def get_un_partial_stated_events_from_stream(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> tuple[list[tuple[int, tuple[str, bool]]], int, bool]:
        """Get updates for the un-partial-stated events replication stream.

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
            function to get further updatees.

            The updates are a list of 2-tuples of stream ID and the row data
        """

        if last_id == current_id:
            return [], current_id, False

        def get_un_partial_stated_events_from_stream_txn(
            txn: LoggingTransaction,
        ) -> tuple[list[tuple[int, tuple[str, bool]]], int, bool]:
            sql = """
                SELECT stream_id, event_id, rejection_status_changed
                FROM un_partial_stated_event_stream
                WHERE ? < stream_id AND stream_id <= ? AND instance_name = ?
                ORDER BY stream_id ASC
                LIMIT ?
            """
            txn.execute(sql, (last_id, current_id, instance_name, limit))
            updates = [
                (
                    row[0],
                    (
                        row[1],
                        bool(row[2]),
                    ),
                )
                for row in txn
            ]
            limited = False
            upto_token = current_id
            if len(updates) >= limit:
                upto_token = updates[-1][0]
                limited = True

            return updates, upto_token, limited

        return await self.db_pool.runInteraction(
            "get_un_partial_stated_events_from_stream",
            get_un_partial_stated_events_from_stream_txn,
        )

    def process_replication_rows(
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[Any],
    ) -> None:
        if stream_name == UnPartialStatedEventStream.NAME:
            for row in rows:
                assert isinstance(row, UnPartialStatedEventStreamRow)

                self.is_partial_state_event.invalidate((row.event_id,))

                if row.rejection_status_changed:
                    # If the partial-stated event became rejected or unrejected
                    # when it wasn't before, we need to invalidate this cache.
                    self._invalidate_local_get_event_cache(row.event_id)

        super().process_replication_rows(stream_name, instance_name, token, rows)

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == EventsStream.NAME:
            self._stream_id_gen.advance(instance_name, token)
        elif stream_name == BackfillStream.NAME:
            self._backfill_id_gen.advance(instance_name, -token)
        elif stream_name == UnPartialStatedEventStream.NAME:
            self._un_partial_stated_events_stream_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

    async def have_censored_event(self, event_id: str) -> bool:
        """Check if an event has been censored, i.e. if the content of the event has been erased
        from the database due to a redaction.

        Args:
            event_id: The event ID that was redacted.

        Returns:
            True if the event has been censored, False otherwise.
        """
        censored_redactions_list = await self.db_pool.simple_select_onecol(
            table="redactions",
            keyvalues={"redacts": event_id},
            retcol="have_censored",
            desc="get_have_censored",
        )
        return any(censored_redactions_list)

    # Inform mypy that if allow_none is False (the default) then get_event
    # always returns an EventBase.
    @overload
    async def get_event(
        self,
        event_id: str,
        redact_behaviour: EventRedactBehaviour = EventRedactBehaviour.redact,
        get_prev_content: bool = ...,
        allow_rejected: bool = ...,
        allow_none: Literal[False] = ...,
        check_room_id: str | None = ...,
    ) -> EventBase: ...

    @overload
    async def get_event(
        self,
        event_id: str,
        redact_behaviour: EventRedactBehaviour = EventRedactBehaviour.redact,
        get_prev_content: bool = ...,
        allow_rejected: bool = ...,
        allow_none: Literal[True] = ...,
        check_room_id: str | None = ...,
    ) -> EventBase | None: ...

    @cancellable
    async def get_event(
        self,
        event_id: str,
        redact_behaviour: EventRedactBehaviour = EventRedactBehaviour.redact,
        get_prev_content: bool = False,
        allow_rejected: bool = False,
        allow_none: bool = False,
        check_room_id: str | None = None,
    ) -> EventBase | None:
        """Get an event from the database by event_id.

        Events for unknown room versions will also be filtered out.

        Args:
            event_id: The event_id of the event to fetch

            redact_behaviour: Determine what to do with a redacted event. Possible values:
                * as_is - Return the full event body with no redacted content
                * redact - Return the event but with a redacted body
                * block - Do not return redacted events (behave as per allow_none
                    if the event is redacted)

            get_prev_content: If True and event is a state event,
                include the previous states content in the unsigned field.

            allow_rejected: If True, return rejected events. Otherwise,
                behave as per allow_none.

            allow_none: If True, return None if no event found, if
                False throw a NotFoundError

            check_room_id: if not None, check the room of the found event.
                If there is a mismatch, behave as per allow_none.

        Returns:
            The event, or None if the event was not found and allow_none is `True`.
        """
        if not isinstance(event_id, str):
            raise TypeError("Invalid event event_id %r" % (event_id,))

        events = await self.get_events_as_list(
            [event_id],
            redact_behaviour=redact_behaviour,
            get_prev_content=get_prev_content,
            allow_rejected=allow_rejected,
        )

        event = events[0] if events else None

        if event is not None and check_room_id is not None:
            if event.room_id != check_room_id:
                event = None

        if event is None and not allow_none:
            raise NotFoundError("Could not find event %s" % (event_id,))

        return event

    @trace
    async def get_events(
        self,
        event_ids: Collection[str],
        redact_behaviour: EventRedactBehaviour = EventRedactBehaviour.redact,
        get_prev_content: bool = False,
        allow_rejected: bool = False,
    ) -> dict[str, EventBase]:
        """Get events from the database

        Unknown events will be omitted from the response.

        Events for unknown room versions will also be filtered out.

        Args:
            event_ids: The event_ids of the events to fetch

            redact_behaviour: Determine what to do with a redacted event. Possible
                values:
                * as_is - Return the full event body with no redacted content
                * redact - Return the event but with a redacted body
                * block - Do not return redacted events (omit them from the response)

            get_prev_content: If True and event is a state event,
                include the previous states content in the unsigned field.

            allow_rejected: If True, return rejected events. Otherwise,
                omits rejected events from the response.

        Returns:
            A mapping from event_id to event.
        """
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "event_ids.length",
            str(len(event_ids)),
        )

        events = await self.get_events_as_list(
            event_ids,
            redact_behaviour=redact_behaviour,
            get_prev_content=get_prev_content,
            allow_rejected=allow_rejected,
        )

        return {e.event_id: e for e in events}

    @trace
    @tag_args
    @cancellable
    async def get_events_as_list(
        self,
        event_ids: Collection[str],
        redact_behaviour: EventRedactBehaviour = EventRedactBehaviour.redact,
        get_prev_content: bool = False,
        allow_rejected: bool = False,
    ) -> list[EventBase]:
        """Get events from the database and return in a list in the same order
        as given by `event_ids` arg.

        Unknown events will be omitted from the response.

        Events for unknown room versions will also be filtered out.

        Args:
            event_ids: The event_ids of the events to fetch

            redact_behaviour: Determine what to do with a redacted event. Possible values:
                * as_is - Return the full event body with no redacted content
                * redact - Return the event but with a redacted body
                * block - Do not return redacted events (omit them from the response)

            get_prev_content: If True and event is a state event,
                include the previous states content in the unsigned field.

            allow_rejected: If True, return rejected events. Otherwise,
                omits rejected events from the response.

        Returns:
            List of events fetched from the database. The events are in the same
            order as `event_ids` arg.

            Note that the returned list may be smaller than the list of event
            IDs if not all events could be fetched.
        """
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "event_ids.length",
            str(len(event_ids)),
        )

        if not event_ids:
            return []

        # there may be duplicates so we cast the list to a set
        event_entry_map = await self.get_unredacted_events_from_cache_or_db(
            set(event_ids), allow_rejected=allow_rejected
        )

        events = []
        for event_id in event_ids:
            entry = event_entry_map.get(event_id, None)
            if not entry:
                continue

            if not allow_rejected:
                assert not entry.event.rejected_reason, (
                    "rejected event returned from _get_events_from_cache_or_db despite "
                    "allow_rejected=False"
                )

            # We may not have had the original event when we received a redaction, so
            # we have to recheck auth now.

            if not allow_rejected and entry.event.type == EventTypes.Redaction:
                if entry.event.redacts is None:
                    # A redacted redaction doesn't have a `redacts` key, in
                    # which case lets just withhold the event.
                    #
                    # Note: Most of the time if the redactions has been
                    # redacted we still have the un-redacted event in the DB
                    # and so we'll still see the `redacts` key. However, this
                    # isn't always true e.g. if we have censored the event.
                    logger.debug(
                        "Withholding redaction event %s as we don't have redacts key",
                        event_id,
                    )
                    continue

                redacted_event_id = entry.event.redacts
                event_map = await self.get_unredacted_events_from_cache_or_db(
                    [redacted_event_id]
                )
                original_event_entry = event_map.get(redacted_event_id)
                if not original_event_entry:
                    # we don't have the redacted event (or it was rejected).
                    #
                    # We assume that the redaction isn't authorized for now; if the
                    # redacted event later turns up, the redaction will be re-checked,
                    # and if it is found valid, the original will get redacted before it
                    # is served to the client.
                    logger.debug(
                        "Withholding redaction event %s since we don't (yet) have the "
                        "original %s",
                        event_id,
                        redacted_event_id,
                    )
                    continue

                original_event = original_event_entry.event
                if original_event.type == EventTypes.Create:
                    # we never serve redactions of Creates to clients.
                    logger.info(
                        "Withholding redaction %s of create event %s",
                        event_id,
                        redacted_event_id,
                    )
                    continue

                if original_event.room_id != entry.event.room_id:
                    logger.info(
                        "Withholding redaction %s of event %s from a different room",
                        event_id,
                        redacted_event_id,
                    )
                    continue

                if entry.event.internal_metadata.need_to_check_redaction():
                    original_domain = get_domain_from_id(original_event.sender)
                    redaction_domain = get_domain_from_id(entry.event.sender)
                    if original_domain != redaction_domain:
                        # the senders don't match, so this is forbidden
                        logger.info(
                            "Withholding redaction %s whose sender domain %s doesn't "
                            "match that of redacted event %s %s",
                            event_id,
                            redaction_domain,
                            redacted_event_id,
                            original_domain,
                        )
                        continue

                    # Update the cache to save doing the checks again.
                    entry.event.internal_metadata.recheck_redaction = False

            event = entry.event

            if entry.redacted_event:
                if redact_behaviour == EventRedactBehaviour.block:
                    # Skip this event
                    continue
                elif redact_behaviour == EventRedactBehaviour.redact:
                    event = entry.redacted_event

            events.append(event)

            if get_prev_content:
                if "replaces_state" in event.unsigned:
                    prev = await self.get_event(
                        event.unsigned["replaces_state"],
                        get_prev_content=False,
                        allow_none=True,
                    )
                    if prev:
                        event.unsigned = dict(event.unsigned)
                        event.unsigned["prev_content"] = prev.content
                        event.unsigned["prev_sender"] = prev.sender

        return events

    @trace
    @cancellable
    async def get_unredacted_events_from_cache_or_db(
        self,
        event_ids: Collection[str],
        allow_rejected: bool = False,
    ) -> dict[str, EventCacheEntry]:
        """Fetch a bunch of events from the cache or the database.

        Note that the events pulled by this function will not have any redactions
        applied, and no guarantee is made about the ordering of the events returned.

        If events are pulled from the database, they will be cached for future lookups.

        Unknown events are omitted from the response.

        Args:

            event_ids: The event_ids of the events to fetch

            allow_rejected: Whether to include rejected events. If False,
                rejected events are omitted from the response.

        Returns:
            map from event id to result
        """
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "event_ids.length",
            str(len(event_ids)),
        )

        # Shortcut: check if we have any events in the *in memory* cache - this function
        # may be called repeatedly for the same event so at this point we cannot reach
        # out to any external cache for performance reasons. The external cache is
        # checked later on in the `get_missing_events_from_cache_or_db` function below.
        event_entry_map = self._get_events_from_local_cache(
            event_ids,
        )

        missing_events_ids = {e for e in event_ids if e not in event_entry_map}

        # We now look up if we're already fetching some of the events in the DB,
        # if so we wait for those lookups to finish instead of pulling the same
        # events out of the DB multiple times.
        #
        # Note: we might get the same `ObservableDeferred` back for multiple
        # events we're already fetching, so we deduplicate the deferreds to
        # avoid extraneous work (if we don't do this we can end up in a n^2 mode
        # when we wait on the same Deferred N times, then try and merge the
        # same dict into itself N times).
        already_fetching_ids: set[str] = set()
        already_fetching_deferreds: set[
            ObservableDeferred[dict[str, EventCacheEntry]]
        ] = set()

        for event_id in missing_events_ids:
            deferred = self._current_event_fetches.get(event_id)
            if deferred is not None:
                # We're already pulling the event out of the DB. Add the deferred
                # to the collection of deferreds to wait on.
                already_fetching_ids.add(event_id)
                already_fetching_deferreds.add(deferred)

        missing_events_ids.difference_update(already_fetching_ids)

        if missing_events_ids:

            async def get_missing_events_from_cache_or_db() -> dict[
                str, EventCacheEntry
            ]:
                """Fetches the events in `missing_event_ids` from the database.

                Also creates entries in `self._current_event_fetches` to allow
                concurrent `_get_events_from_cache_or_db` calls to reuse the same fetch.
                """
                log_ctx = current_context()
                log_ctx.record_event_fetch(len(missing_events_ids))

                # Add entries to `self._current_event_fetches` for each event we're
                # going to pull from the DB. We use a single deferred that resolves
                # to all the events we pulled from the DB (this will result in this
                # function returning more events than requested, but that can happen
                # already due to `_get_events_from_db`).
                fetching_deferred: ObservableDeferred[dict[str, EventCacheEntry]] = (
                    ObservableDeferred(defer.Deferred(), consumeErrors=True)
                )
                for event_id in missing_events_ids:
                    self._current_event_fetches[event_id] = fetching_deferred

                # Note that _get_events_from_db is also responsible for turning db rows
                # into FrozenEvents (via _get_event_from_row), which involves seeing if
                # the events have been redacted, and if so pulling the redaction event
                # out of the database to check it.
                #
                try:
                    # Try to fetch from any external cache. We already checked the
                    # in-memory cache above.
                    missing_events = await self._get_events_from_external_cache(
                        missing_events_ids,
                    )
                    # Now actually fetch any remaining events from the DB
                    db_missing_events = await self._get_events_from_db(
                        missing_events_ids - missing_events.keys(),
                    )
                    missing_events.update(db_missing_events)
                except Exception as e:
                    with PreserveLoggingContext():
                        fetching_deferred.errback(e)
                    raise e
                finally:
                    # Ensure that we mark these events as no longer being fetched.
                    for event_id in missing_events_ids:
                        self._current_event_fetches.pop(event_id, None)

                with PreserveLoggingContext():
                    fetching_deferred.callback(missing_events)

                return missing_events

            # We must allow the database fetch to complete in the presence of
            # cancellations, since multiple `_get_events_from_cache_or_db` calls can
            # reuse the same fetch.
            missing_events: dict[str, EventCacheEntry] = await delay_cancellation(
                get_missing_events_from_cache_or_db()
            )
            event_entry_map.update(missing_events)

        if already_fetching_deferreds:
            # Wait for the other event requests to finish and add their results
            # to ours.
            results = await make_deferred_yieldable(
                defer.gatherResults(
                    (d.observe() for d in already_fetching_deferreds),
                    consumeErrors=True,
                )
            ).addErrback(unwrapFirstError)

            for result in results:
                # We filter out events that we haven't asked for as we might get
                # a *lot* of superfluous events back, and there is no point
                # going through and inserting them all (which can take time).
                event_entry_map.update(
                    (event_id, entry)
                    for event_id, entry in result.items()
                    if event_id in already_fetching_ids
                )

        if not allow_rejected:
            event_entry_map = {
                event_id: entry
                for event_id, entry in event_entry_map.items()
                if not entry.event.rejected_reason
            }

        return event_entry_map

    def invalidate_get_event_cache_after_txn(
        self, txn: LoggingTransaction, event_id: str
    ) -> None:
        """
        Prepares a database transaction to invalidate the get event cache for a given
        event ID when executed successfully. This is achieved by attaching two callbacks
        to the transaction, one to invalidate the async cache and one for the in memory
        sync cache (importantly called in that order).

        Arguments:
            txn: the database transaction to attach the callbacks to
            event_id: the event ID to be invalidated from caches
        """

        txn.async_call_after(self._invalidate_async_get_event_cache, event_id)
        txn.call_after(self._invalidate_local_get_event_cache, event_id)

    async def _invalidate_async_get_event_cache(self, event_id: str) -> None:
        """
        Invalidates an event in the asynchronous get event cache, which may be remote.

        Arguments:
            event_id: the event ID to invalidate
        """

        await self._get_event_cache.invalidate((event_id,))

    def _invalidate_local_get_event_cache(self, event_id: str) -> None:
        """
        Invalidates an event in local in-memory get event caches.

        Arguments:
            event_id: the event ID to invalidate
        """

        self._get_event_cache.invalidate_local((event_id,))
        self._event_ref.pop(event_id, None)
        self._current_event_fetches.pop(event_id, None)

    def _invalidate_local_get_event_cache_room_id(self, room_id: str) -> None:
        """Clears the in-memory get event caches for a room.

        Used when we purge room history.
        """
        self._get_event_cache.invalidate_on_extra_index_local((room_id,))
        self._event_ref.clear()
        self._current_event_fetches.clear()

    def _invalidate_async_get_event_cache_room_id(self, room_id: str) -> None:
        """
        Clears the async get_event cache for a room. Currently a no-op until
        an async get_event cache is implemented - see https://github.com/matrix-org/synapse/pull/13242
        for preliminary work.
        """

    async def _get_events_from_cache(
        self, events: Iterable[str], update_metrics: bool = True
    ) -> dict[str, EventCacheEntry]:
        """Fetch events from the caches, both in memory and any external.

        May return rejected events.

        Args:
            events: list of event_ids to fetch
            update_metrics: Whether to update the cache hit ratio metrics
        """
        event_map = self._get_events_from_local_cache(
            events, update_metrics=update_metrics
        )

        missing_event_ids = [e for e in events if e not in event_map]
        event_map.update(
            await self._get_events_from_external_cache(
                events=missing_event_ids,
                update_metrics=update_metrics,
            )
        )

        return event_map

    @trace
    async def _get_events_from_external_cache(
        self, events: Collection[str], update_metrics: bool = True
    ) -> dict[str, EventCacheEntry]:
        """Fetch events from any configured external cache.

        May return rejected events.

        Args:
            events: list of event_ids to fetch
            update_metrics: Whether to update the cache hit ratio metrics
        """
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "events.length",
            str(len(events)),
        )
        event_map = {}

        for event_id in events:
            ret = await self._get_event_cache.get_external(
                (event_id,), None, update_metrics=update_metrics
            )
            if ret:
                event_map[event_id] = ret

        return event_map

    def _get_events_from_local_cache(
        self, events: Iterable[str], update_metrics: bool = True
    ) -> dict[str, EventCacheEntry]:
        """Fetch events from the local, in memory, caches.

        May return rejected events.

        Args:
            events: list of event_ids to fetch
            update_metrics: Whether to update the cache hit ratio metrics
        """
        event_map = {}

        for event_id in events:
            # First check if it's in the event cache
            ret = self._get_event_cache.get_local(
                (event_id,), None, update_metrics=update_metrics
            )
            if ret:
                event_map[event_id] = ret
                continue

            # Otherwise check if we still have the event in memory.
            event = self._event_ref.get(event_id)
            if event:
                # Reconstruct an event cache entry

                cache_entry = EventCacheEntry(
                    event=event,
                    # We don't cache weakrefs to redacted events, so we know
                    # this is None.
                    redacted_event=None,
                )
                event_map[event_id] = cache_entry

                # We add the entry back into the cache as we want to keep
                # recently queried events in the cache.
                self._get_event_cache.set_local((event_id,), cache_entry)

        return event_map

    async def get_stripped_room_state_from_event_context(
        self,
        context: EventContext,
        state_keys_to_include: StateFilter,
        membership_user_id: str | None = None,
    ) -> list[JsonDict]:
        """
        Retrieve the stripped state from a room, given an event context to retrieve state
        from as well as the state types to include. Optionally, include the membership
        events from a specific user.

        "Stripped" state means that only the `type`, `state_key`, `content` and `sender` keys
        are included from each state event.

        Args:
            context: The event context to retrieve state of the room from.
            state_keys_to_include: The state events to include, for each event type.
            membership_user_id: An optional user ID to include the stripped membership state
                events of. This is useful when generating the stripped state of a room for
                invites. We want to send membership events of the inviter, so that the
                invitee can display the inviter's profile information if the room lacks any.

        Returns:
            A list of dictionaries, each representing a stripped state event from the room.
        """
        if membership_user_id:
            types = chain(
                state_keys_to_include.to_types(),
                [(EventTypes.Member, membership_user_id)],
            )
            filter = StateFilter.from_types(types)
        else:
            filter = state_keys_to_include
        selected_state_ids = await context.get_current_state_ids(filter)

        # We know this event is not an outlier, so this must be
        # non-None.
        assert selected_state_ids is not None

        # Confusingly, get_current_state_events may return events that are discarded by
        # the filter, if they're in context._state_delta_due_to_event. Strip these away.
        selected_state_ids = filter.filter_state(selected_state_ids)

        state_to_include = await self.get_events(selected_state_ids.values())

        return [strip_event(e) for e in state_to_include.values()]

    def _maybe_start_fetch_thread(self) -> None:
        """Starts an event fetch thread if we are not yet at the maximum number."""
        with self._event_fetch_lock:
            if (
                self._event_fetch_list
                and self._event_fetch_ongoing < EVENT_QUEUE_THREADS
            ):
                self._event_fetch_ongoing += 1
                event_fetch_ongoing_gauge.labels(
                    **{SERVER_NAME_LABEL: self.server_name}
                ).set(self._event_fetch_ongoing)
                # `_event_fetch_ongoing` is decremented in `_fetch_thread`.
                should_start = True
            else:
                should_start = False

        if should_start:
            self.hs.run_as_background_process("fetch_events", self._fetch_thread)

    async def _fetch_thread(self) -> None:
        """Services requests for events from `_event_fetch_list`."""
        exc = None
        try:
            await self.db_pool.runWithConnection(self._fetch_loop)
        except BaseException as e:
            exc = e
            raise
        finally:
            should_restart = False
            event_fetches_to_fail = []
            with self._event_fetch_lock:
                self._event_fetch_ongoing -= 1
                event_fetch_ongoing_gauge.labels(
                    **{SERVER_NAME_LABEL: self.server_name}
                ).set(self._event_fetch_ongoing)

                # There may still be work remaining in `_event_fetch_list` if we
                # failed, or it was added in between us deciding to exit and
                # decrementing `_event_fetch_ongoing`.
                if self._event_fetch_list:
                    if exc is None:
                        # We decided to exit, but then some more work was added
                        # before `_event_fetch_ongoing` was decremented.
                        # If a new event fetch thread was not started, we should
                        # restart ourselves since the remaining event fetch threads
                        # may take a while to get around to the new work.
                        #
                        # Unfortunately it is not possible to tell whether a new
                        # event fetch thread was started, so we restart
                        # unconditionally. If we are unlucky, we will end up with
                        # an idle fetch thread, but it will time out after
                        # `EVENT_QUEUE_ITERATIONS * EVENT_QUEUE_TIMEOUT_S` seconds
                        # in any case.
                        #
                        # Note that multiple fetch threads may run down this path at
                        # the same time.
                        should_restart = True
                    elif isinstance(exc, Exception):
                        if self._event_fetch_ongoing == 0:
                            # We were the last remaining fetcher and failed.
                            # Fail any outstanding fetches since no one else will
                            # handle them.
                            event_fetches_to_fail = self._event_fetch_list
                            self._event_fetch_list = []
                        else:
                            # We weren't the last remaining fetcher, so another
                            # fetcher will pick up the work. This will either happen
                            # after their existing work, however long that takes,
                            # or after at most `EVENT_QUEUE_TIMEOUT_S` seconds if
                            # they are idle.
                            pass
                    else:
                        # The exception is a `SystemExit`, `KeyboardInterrupt` or
                        # `GeneratorExit`. Don't try to do anything clever here.
                        pass

            if should_restart:
                # We exited cleanly but noticed more work.
                self._maybe_start_fetch_thread()

            if event_fetches_to_fail:
                # We were the last remaining fetcher and failed.
                # Fail any outstanding fetches since no one else will handle them.
                assert exc is not None
                with PreserveLoggingContext():
                    for _, deferred in event_fetches_to_fail:
                        deferred.errback(exc)

    def _fetch_loop(self, conn: LoggingDatabaseConnection) -> None:
        """Takes a database connection and waits for requests for events from
        the _event_fetch_list queue.
        """
        i = 0
        while True:
            with self._event_fetch_lock:
                event_list = self._event_fetch_list
                self._event_fetch_list = []

                if not event_list:
                    # There are no requests waiting. If we haven't yet reached the
                    # maximum iteration limit, wait for some more requests to turn up.
                    # Otherwise, bail out.
                    single_threaded = self.database_engine.single_threaded
                    if (
                        not self.USE_DEDICATED_DB_THREADS_FOR_EVENT_FETCHING
                        or single_threaded
                        or i > EVENT_QUEUE_ITERATIONS
                    ):
                        return

                    self._event_fetch_lock.wait(EVENT_QUEUE_TIMEOUT_S)
                    i += 1
                    continue
                i = 0

            self._fetch_event_list(conn, event_list)

    def _fetch_event_list(
        self,
        conn: LoggingDatabaseConnection,
        event_list: list[tuple[Iterable[str], "defer.Deferred[dict[str, _EventRow]]"]],
    ) -> None:
        """Handle a load of requests from the _event_fetch_list queue

        Args:
            conn: database connection

            event_list:
                The fetch requests. Each entry consists of a list of event
                ids to be fetched, and a deferred to be completed once the
                events have been fetched.

                The deferreds are callbacked with a dictionary mapping from event id
                to event row. Note that it may well contain additional events that
                were not part of this request.
        """
        with Measure(
            self.clock, name="_fetch_event_list", server_name=self.server_name
        ):
            try:
                events_to_fetch = {
                    event_id for events, _ in event_list for event_id in events
                }

                row_dict = self.db_pool.new_transaction(
                    conn,
                    "do_fetch",
                    [],
                    [],
                    [],
                    self._fetch_event_rows,
                    events_to_fetch,
                )

                # We only want to resolve deferreds from the main thread
                def fire() -> None:
                    for _, d in event_list:
                        d.callback(row_dict)

                with PreserveLoggingContext():
                    self.hs.get_reactor().callFromThread(fire)
            except Exception as e:
                logger.exception("do_fetch")

                # We only want to resolve deferreds from the main thread
                def fire_errback(exc: Exception) -> None:
                    for _, d in event_list:
                        d.errback(exc)

                with PreserveLoggingContext():
                    self.hs.get_reactor().callFromThread(fire_errback, e)

    @trace
    async def _get_events_from_db(
        self, event_ids: Collection[str]
    ) -> dict[str, EventCacheEntry]:
        """Fetch a bunch of events from the database.

        May return rejected events.

        Returned events will be added to the cache for future lookups.

        Unknown events are omitted from the response.

        Args:
            event_ids: The event_ids of the events to fetch

        Returns:
            map from event id to result. May return extra events which
            weren't asked for.
        """
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "event_ids.length",
            str(len(event_ids)),
        )

        fetched_event_ids: set[str] = set()
        fetched_events: dict[str, _EventRow] = {}

        @trace
        async def _fetch_event_ids_and_get_outstanding_redactions(
            event_ids_to_fetch: Collection[str],
        ) -> Collection[str]:
            """
            Fetch all of the given event_ids and return any associated redaction event_ids
            that we still need to fetch in the next iteration.
            """
            set_tag(
                SynapseTags.FUNC_ARG_PREFIX + "event_ids_to_fetch.length",
                str(len(event_ids_to_fetch)),
            )
            row_map = await self._enqueue_events(event_ids_to_fetch)

            # we need to recursively fetch any redactions of those events
            redaction_ids: set[str] = set()
            for event_id in event_ids_to_fetch:
                row = row_map.get(event_id)
                fetched_event_ids.add(event_id)
                if row:
                    fetched_events[event_id] = row
                    redaction_ids.update(row.redactions)

            event_ids_to_fetch = redaction_ids.difference(fetched_event_ids)
            return event_ids_to_fetch

        # Grab the initial list of events requested
        event_ids_to_fetch = await _fetch_event_ids_and_get_outstanding_redactions(
            event_ids
        )
        # Then go and recursively find all of the associated redactions
        with start_active_span("recursively fetching redactions"):
            while event_ids_to_fetch:
                logger.debug("Also fetching redaction events %s", event_ids_to_fetch)

                event_ids_to_fetch = (
                    await _fetch_event_ids_and_get_outstanding_redactions(
                        event_ids_to_fetch
                    )
                )

        # build a map from event_id to EventBase
        event_map: dict[str, EventBase] = {}
        for event_id, row in fetched_events.items():
            assert row.event_id == event_id

            rejected_reason = row.rejected_reason

            # If the event or metadata cannot be parsed, log the error and act
            # as if the event is unknown.
            try:
                d = db_to_json(row.json)
            except ValueError:
                logger.error("Unable to parse json from event: %s", event_id)
                continue
            try:
                internal_metadata = db_to_json(row.internal_metadata)
            except ValueError:
                logger.error(
                    "Unable to parse internal_metadata from event: %s", event_id
                )
                continue

            format_version = row.format_version
            if format_version is None:
                # This means that we stored the event before we had the concept
                # of a event format version, so it must be a V1 event.
                format_version = EventFormatVersions.ROOM_V1_V2

            room_version_id = row.room_version_id

            room_version: RoomVersion | None
            if not room_version_id:
                # this should only happen for out-of-band membership events which
                # arrived before https://github.com/matrix-org/synapse/issues/6983
                # landed. For all other events, we should have
                # an entry in the 'rooms' table.
                #
                # However, the 'out_of_band_membership' flag is unreliable for older
                # invites, so just accept it for all membership events.
                #
                if d["type"] != EventTypes.Member:
                    raise InvalidEventError(
                        "Room %s for event %s is unknown" % (d["room_id"], event_id)
                    )

                # so, assuming this is an out-of-band-invite that arrived before
                # https://github.com/matrix-org/synapse/issues/6983
                # landed, we know that the room version must be v5 or earlier (because
                # v6 hadn't been invented at that point, so invites from such rooms
                # would have been rejected.)
                #
                # The main reason we need to know the room version here (other than
                # choosing the right python Event class) is in case the event later has
                # to be redacted - and all the room versions up to v5 used the same
                # redaction algorithm.
                #
                # So, the following approximations should be adequate.

                if format_version == EventFormatVersions.ROOM_V1_V2:
                    # if it's event format v1 then it must be room v1 or v2
                    room_version = RoomVersions.V1
                elif format_version == EventFormatVersions.ROOM_V3:
                    # if it's event format v2 then it must be room v3
                    room_version = RoomVersions.V3
                else:
                    # if it's event format v3 then it must be room v4 or v5
                    room_version = RoomVersions.V5
            else:
                room_version = KNOWN_ROOM_VERSIONS.get(room_version_id)
                if not room_version:
                    logger.warning(
                        "Event %s in room %s has unknown room version %s",
                        event_id,
                        d["room_id"],
                        room_version_id,
                    )
                    continue

                if room_version.event_format != format_version:
                    logger.error(
                        "Event %s in room %s with version %s has wrong format: "
                        "expected %s, was %s",
                        event_id,
                        d["room_id"],
                        room_version_id,
                        room_version.event_format,
                        format_version,
                    )
                    continue

            original_ev = make_event_from_dict(
                event_dict=d,
                room_version=room_version,
                internal_metadata_dict=internal_metadata,
                rejected_reason=rejected_reason,
            )
            original_ev.internal_metadata.stream_ordering = row.stream_ordering
            original_ev.internal_metadata.instance_name = row.instance_name
            original_ev.internal_metadata.outlier = row.outlier

            # Consistency check: if the content of the event has been modified in the
            # database, then the calculated event ID will not match the event id in the
            # database.
            if original_ev.event_id != event_id:
                # it's difficult to see what to do here. Pretty much all bets are off
                # if Synapse cannot rely on the consistency of its database.
                raise DatabaseCorruptionError(
                    d["room_id"], event_id, original_ev.event_id
                )

            event_map[event_id] = original_ev

        # finally, we can decide whether each one needs redacting, and build
        # the cache entries.
        result_map: dict[str, EventCacheEntry] = {}
        for event_id, original_ev in event_map.items():
            redactions = fetched_events[event_id].redactions
            redacted_event = self._maybe_redact_event_row(
                original_ev, redactions, event_map
            )

            cache_entry = EventCacheEntry(
                event=original_ev, redacted_event=redacted_event
            )

            await self._get_event_cache.set((event_id,), cache_entry)
            result_map[event_id] = cache_entry

            if not redacted_event:
                # We only cache references to unredacted events.
                self._event_ref[event_id] = original_ev

        return result_map

    async def _enqueue_events(self, events: Collection[str]) -> dict[str, _EventRow]:
        """Fetches events from the database using the _event_fetch_list. This
        allows batch and bulk fetching of events - it allows us to fetch events
        without having to create a new transaction for each request for events.

        Args:
            events: events to be fetched.

        Returns:
            A map from event id to row data from the database. May contain events
            that weren't requested.
        """

        events_d: "defer.Deferred[dict[str, _EventRow]]" = defer.Deferred()
        with self._event_fetch_lock:
            self._event_fetch_list.append((events, events_d))
            self._event_fetch_lock.notify()

        self._maybe_start_fetch_thread()

        logger.debug("Loading %d events: %s", len(events), events)
        with PreserveLoggingContext():
            row_map = await events_d
        logger.debug("Loaded %d events (%d rows)", len(events), len(row_map))

        return row_map

    def _fetch_event_rows(
        self, txn: LoggingTransaction, event_ids: Iterable[str]
    ) -> dict[str, _EventRow]:
        """Fetch event rows from the database

        Events which are not found are omitted from the result.

        Args:
            txn: The database transaction.
            event_ids: event IDs to fetch

        Returns:
            A map from event id to event info.
        """
        event_dict = {}
        for evs in batch_iter(event_ids, 200):
            sql = """\
                SELECT
                  e.event_id,
                  e.stream_ordering,
                  e.instance_name,
                  ej.internal_metadata,
                  ej.json,
                  ej.format_version,
                  r.room_version,
                  rej.reason,
                  e.outlier
                FROM events AS e
                  JOIN event_json AS ej USING (event_id)
                  LEFT JOIN rooms r ON r.room_id = e.room_id
                  LEFT JOIN rejections as rej USING (event_id)
                WHERE """

            clause, args = make_in_list_sql_clause(
                txn.database_engine, "e.event_id", evs
            )

            txn.execute(sql + clause, args)

            for row in txn:
                event_id = row[0]
                event_dict[event_id] = _EventRow(
                    event_id=event_id,
                    stream_ordering=row[1],
                    # If instance_name is null we default to "master"
                    instance_name=row[2] or "master",
                    internal_metadata=row[3],
                    json=row[4],
                    format_version=row[5],
                    room_version_id=row[6],
                    rejected_reason=row[7],
                    redactions=[],
                    outlier=bool(row[8]),  # This is an int in SQLite3
                )

            # check for redactions
            redactions_sql = "SELECT event_id, redacts FROM redactions WHERE "

            clause, args = make_in_list_sql_clause(txn.database_engine, "redacts", evs)

            txn.execute(redactions_sql + clause, args)

            for redacter, redacted in txn:
                d = event_dict.get(redacted)
                if d:
                    d.redactions.append(redacter)

            # check for MSC4293 redactions
            to_check = []
            events: list[_EventRow] = []
            for e in evs:
                try:
                    event = event_dict.get(e)
                    if not event:
                        continue
                    events.append(event)
                    event_json = json.loads(event.json)
                    room_id = event_json.get("room_id")
                    user_id = event_json.get("sender")
                    to_check.append((room_id, user_id))
                except Exception as exc:
                    raise InvalidEventError(f"Invalid event {event_id}") from exc

            # likely that some of these events may be for the same room/user combo, in
            # which case we don't need to do redundant queries
            to_check_set = set(to_check)
            room_redaction_sql = "SELECT room_id, user_id, redacting_event_id, redact_end_ordering FROM room_ban_redactions WHERE "
            (
                in_list_clause,
                room_redaction_args,
            ) = make_tuple_in_list_sql_clause(
                self.database_engine, ("room_id", "user_id"), to_check_set
            )
            txn.execute(room_redaction_sql + in_list_clause, room_redaction_args)
            for (
                returned_room_id,
                returned_user_id,
                redacting_event_id,
                redact_end_ordering,
            ) in txn:
                for e_row in events:
                    e_json = json.loads(e_row.json)
                    room_id = e_json.get("room_id")
                    user_id = e_json.get("sender")
                    room_and_user = (returned_room_id, returned_user_id)
                    # check if we have a redaction match for this room, user combination
                    if room_and_user != (room_id, user_id):
                        continue
                    if redact_end_ordering:
                        # Avoid redacting any events arriving *after* the membership event which
                        # ends an active redaction - note that this will always redact
                        # backfilled events, as they have a negative stream ordering
                        if e_row.stream_ordering >= redact_end_ordering:
                            continue
                    e_row.redactions.append(redacting_event_id)
        return event_dict

    def _maybe_redact_event_row(
        self,
        original_ev: EventBase,
        redactions: Iterable[str],
        event_map: dict[str, EventBase],
    ) -> EventBase | None:
        """Given an event object and a list of possible redacting event ids,
        determine whether to honour any of those redactions and if so return a redacted
        event.

        Args:
             original_ev: The original event.
             redactions: list of event ids of potential redaction events
             event_map: other events which have been fetched, in which we can
                look up the redaaction events. Map from event id to event.

        Returns:
            If the event should be redacted, a pruned event object. Otherwise, None.
        """
        if original_ev.type == "m.room.create":
            # we choose to ignore redactions of m.room.create events.
            return None

        for redaction_id in redactions:
            redaction_event = event_map.get(redaction_id)
            if not redaction_event or redaction_event.rejected_reason:
                # we don't have the redaction event, or the redaction event was not
                # authorized.
                logger.debug(
                    "%s was redacted by %s but redaction not found/authed",
                    original_ev.event_id,
                    redaction_id,
                )
                continue

            if redaction_event.room_id != original_ev.room_id:
                logger.debug(
                    "%s was redacted by %s but redaction was in a different room!",
                    original_ev.event_id,
                    redaction_id,
                )
                continue

            # Starting in room version v3, some redactions need to be
            # rechecked if we didn't have the redacted event at the
            # time, so we recheck on read instead.
            if redaction_event.internal_metadata.need_to_check_redaction():
                expected_domain = get_domain_from_id(original_ev.sender)
                if get_domain_from_id(redaction_event.sender) == expected_domain:
                    # This redaction event is allowed. Mark as not needing a recheck.
                    redaction_event.internal_metadata.recheck_redaction = False
                else:
                    # Senders don't match, so the event isn't actually redacted
                    logger.debug(
                        "%s was redacted by %s but the senders don't match",
                        original_ev.event_id,
                        redaction_id,
                    )
                    continue

            logger.debug("Redacting %s due to %s", original_ev.event_id, redaction_id)

            # we found a good redaction event. Redact!
            redacted_event = prune_event(original_ev)
            redacted_event.unsigned["redacted_by"] = redaction_id

            # It's fine to add the event directly, since get_pdu_json
            # will serialise this field correctly
            redacted_event.unsigned["redacted_because"] = redaction_event

            return redacted_event

        # no valid redaction found for this event
        return None

    async def have_events_in_timeline(self, event_ids: Iterable[str]) -> set[str]:
        """Given a list of event ids, check if we have already processed and
        stored them as non outliers.
        """
        rows = cast(
            list[tuple[str]],
            await self.db_pool.simple_select_many_batch(
                table="events",
                retcols=("event_id",),
                column="event_id",
                iterable=list(event_ids),
                keyvalues={"outlier": False},
                desc="have_events_in_timeline",
            ),
        )

        return {r[0] for r in rows}

    @trace
    @tag_args
    async def have_seen_events(
        self, room_id: str, event_ids: Iterable[str]
    ) -> set[str]:
        """Given a list of event ids, check if we have already processed them.

        The room_id is only used to structure the cache (so that it can later be
        invalidated by room_id) - there is no guarantee that the events are actually
        in the room in question.

        Args:
            room_id: Room we are polling
            event_ids: events we are looking for

        Returns:
            The set of events we have already seen.
        """

        # @cachedList chomps lots of memory if you call it with a big list, so
        # we break it down. However, each batch requires its own index scan, so we make
        # the batches as big as possible.

        results: set[str] = set()
        for event_ids_chunk in batch_iter(event_ids, 500):
            events_seen_dict = await self._have_seen_events_dict(
                room_id, event_ids_chunk
            )
            results.update(
                eid for (eid, have_event) in events_seen_dict.items() if have_event
            )

        return results

    @cachedList(cached_method_name="have_seen_event", list_name="event_ids")
    async def _have_seen_events_dict(
        self,
        room_id: str,
        event_ids: Collection[str],
    ) -> Mapping[str, bool]:
        """Helper for have_seen_events

        Returns:
             a dict {event_id -> bool}
        """
        # TODO: We used to query the _get_event_cache here as a fast-path before
        #  hitting the database. For if an event were in the cache, we've presumably
        #  seen it before.
        #
        #  But this is currently an invalid assumption due to the _get_event_cache
        #  not being invalidated when purging events from a room. The optimisation can
        #  be re-added after https://github.com/matrix-org/synapse/issues/13476

        def have_seen_events_txn(txn: LoggingTransaction) -> dict[str, bool]:
            # we deliberately do *not* query the database for room_id, to make the
            # query an index-only lookup on `events_event_id_key`.
            #
            # We therefore pull the events from the database into a set...

            sql = "SELECT event_id FROM events AS e WHERE "
            clause, args = make_in_list_sql_clause(
                txn.database_engine, "e.event_id", event_ids
            )
            txn.execute(sql + clause, args)
            found_events = {eid for (eid,) in txn}

            # ... and then we can update the results for each key
            return {eid: (eid in found_events) for eid in event_ids}

        return await self.db_pool.runInteraction(
            "have_seen_events", have_seen_events_txn
        )

    @cached(max_entries=100000, tree=True)
    async def have_seen_event(self, room_id: str, event_id: str) -> bool:
        res = await self._have_seen_events_dict(room_id, [event_id])
        return res[event_id]

    def _get_current_state_event_counts_txn(
        self, txn: LoggingTransaction, room_id: str
    ) -> int:
        """
        See get_current_state_event_counts.
        """
        sql = "SELECT COUNT(*) FROM current_state_events WHERE room_id=?"
        txn.execute(sql, (room_id,))
        row = txn.fetchone()
        return row[0] if row else 0

    async def get_current_state_event_counts(self, room_id: str) -> int:
        """
        Gets the current number of state events in a room.

        Args:
            room_id: The room ID to query.

        Returns:
            The current number of state events.
        """
        return await self.db_pool.runInteraction(
            "get_current_state_event_counts",
            self._get_current_state_event_counts_txn,
            room_id,
        )

    async def get_room_complexity(self, room_id: str) -> dict[str, float]:
        """
        Get a rough approximation of the complexity of the room. This is used by
        remote servers to decide whether they wish to join the room or not.
        Higher complexity value indicates that being in the room will consume
        more resources.

        Args:
            room_id: The room ID to query.

        Returns:
            Map of complexity version to complexity.
        """
        state_events = await self.get_current_state_event_counts(room_id)

        # Call this one "v1", so we can introduce new ones as we want to develop
        # it.
        complexity_v1 = round(state_events / 500, 2)

        return {"v1": complexity_v1}

    async def get_all_new_forward_event_rows(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> list[tuple[int, str, str, str, str, str, str, str, bool, bool]]:
        """Returns new events, for the Events replication stream

        Args:
            last_id: the last stream_id from the previous batch.
            current_id: the maximum stream_id to return up to
            limit: the maximum number of rows to return

        Returns:
            a list of events stream rows. Each tuple consists of a stream id as
            the first element, followed by fields suitable for casting into an
            EventsStreamRow.
        """

        def get_all_new_forward_event_rows(
            txn: LoggingTransaction,
        ) -> list[tuple[int, str, str, str, str, str, str, str, bool, bool]]:
            sql = (
                "SELECT e.stream_ordering, e.event_id, e.room_id, e.type,"
                " se.state_key, redacts, relates_to_id, membership, rejections.reason IS NOT NULL,"
                " e.outlier"
                " FROM events AS e"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events AS se USING (event_id)"
                " LEFT JOIN event_relations USING (event_id)"
                " LEFT JOIN room_memberships USING (event_id)"
                " LEFT JOIN rejections USING (event_id)"
                " WHERE ? < stream_ordering AND stream_ordering <= ?"
                " AND instance_name = ?"
                " ORDER BY stream_ordering ASC"
                " LIMIT ?"
            )
            txn.execute(sql, (last_id, current_id, instance_name, limit))
            return cast(
                list[tuple[int, str, str, str, str, str, str, str, bool, bool]],
                txn.fetchall(),
            )

        return await self.db_pool.runInteraction(
            "get_all_new_forward_event_rows", get_all_new_forward_event_rows
        )

    async def get_ex_outlier_stream_rows(
        self, instance_name: str, last_id: int, current_id: int
    ) -> list[tuple[int, str, str, str, str, str, str, str, bool, bool]]:
        """Returns de-outliered events, for the Events replication stream

        Args:
            last_id: the last stream_id from the previous batch.
            current_id: the maximum stream_id to return up to

        Returns:
            a list of events stream rows. Each tuple consists of a stream id as
            the first element, followed by fields suitable for casting into an
            EventsStreamRow.
        """

        def get_ex_outlier_stream_rows_txn(
            txn: LoggingTransaction,
        ) -> list[tuple[int, str, str, str, str, str, str, str, bool, bool]]:
            sql = (
                "SELECT out.event_stream_ordering, e.event_id, e.room_id, e.type,"
                " se.state_key, redacts, relates_to_id, membership, rejections.reason IS NOT NULL,"
                " e.outlier"
                " FROM events AS e"
                # NB: the next line (inner join) is what makes this query different from
                # get_all_new_forward_event_rows.
                " INNER JOIN ex_outlier_stream AS out USING (event_id)"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events AS se USING (event_id)"
                " LEFT JOIN event_relations USING (event_id)"
                " LEFT JOIN room_memberships USING (event_id)"
                " LEFT JOIN rejections USING (event_id)"
                " WHERE ? < out.event_stream_ordering"
                " AND out.event_stream_ordering <= ?"
                " AND out.instance_name = ?"
                " ORDER BY out.event_stream_ordering ASC"
            )

            txn.execute(sql, (last_id, current_id, instance_name))
            return cast(
                list[tuple[int, str, str, str, str, str, str, str, bool, bool]],
                txn.fetchall(),
            )

        return await self.db_pool.runInteraction(
            "get_ex_outlier_stream_rows", get_ex_outlier_stream_rows_txn
        )

    async def get_all_new_backfill_event_rows(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> tuple[list[tuple[int, tuple[str, str, str, str, str, str]]], int, bool]:
        """Get updates for backfill replication stream, including all new
        backfilled events and events that have gone from being outliers to not.

        NOTE: The IDs given here are from replication, and so should be
        *positive*.

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
            function to get further updatees.

            The updates are a list of 2-tuples of stream ID and the row data
        """
        if last_id == current_id:
            return [], current_id, False

        def get_all_new_backfill_event_rows(
            txn: LoggingTransaction,
        ) -> tuple[list[tuple[int, tuple[str, str, str, str, str, str]]], int, bool]:
            sql = (
                "SELECT -e.stream_ordering, e.event_id, e.room_id, e.type,"
                " se.state_key, redacts, relates_to_id"
                " FROM events AS e"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events AS se USING (event_id)"
                " LEFT JOIN event_relations USING (event_id)"
                " WHERE ? > stream_ordering AND stream_ordering >= ?"
                "  AND instance_name = ?"
                " ORDER BY stream_ordering ASC"
                " LIMIT ?"
            )
            txn.execute(sql, (-last_id, -current_id, instance_name, limit))
            new_event_updates: list[
                tuple[int, tuple[str, str, str, str, str, str]]
            ] = []
            row: tuple[int, str, str, str, str, str, str]
            # Type safety: iterating over `txn` yields `Tuple`, i.e.
            # `Tuple[Any, ...]` of arbitrary length. Mypy detects assigning a
            # variadic tuple to a fixed length tuple and flags it up as an error.
            for row in txn:
                new_event_updates.append((row[0], row[1:]))

            limited = False
            if len(new_event_updates) == limit:
                upper_bound = new_event_updates[-1][0]
                limited = True
            else:
                upper_bound = current_id

            sql = (
                "SELECT -event_stream_ordering, e.event_id, e.room_id, e.type,"
                " se.state_key, redacts, relates_to_id"
                " FROM events AS e"
                " INNER JOIN ex_outlier_stream AS out USING (event_id)"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events AS se USING (event_id)"
                " LEFT JOIN event_relations USING (event_id)"
                " WHERE ? > event_stream_ordering"
                " AND event_stream_ordering >= ?"
                " AND out.instance_name = ?"
                " ORDER BY event_stream_ordering DESC"
            )
            txn.execute(sql, (-last_id, -upper_bound, instance_name))
            # Type safety: iterating over `txn` yields `Tuple`, i.e.
            # `Tuple[Any, ...]` of arbitrary length. Mypy detects assigning a
            # variadic tuple to a fixed length tuple and flags it up as an error.
            for row in txn:
                new_event_updates.append((row[0], row[1:]))

            if len(new_event_updates) >= limit:
                upper_bound = new_event_updates[-1][0]
                limited = True

            return new_event_updates, upper_bound, limited

        return await self.db_pool.runInteraction(
            "get_all_new_backfill_event_rows", get_all_new_backfill_event_rows
        )

    async def get_all_updated_current_state_deltas(
        self, instance_name: str, from_token: int, to_token: int, target_row_count: int
    ) -> tuple[list[tuple[int, str, str, str, str]], int, bool]:
        """Fetch updates from current_state_delta_stream

        Args:
            from_token: The previous stream token. Updates from this stream id will
                be excluded.

            to_token: The current stream token (ie the upper limit). Updates up to this
                stream id will be included (modulo the 'limit' param)

            target_row_count: The number of rows to try to return. If more rows are
                available, we will set 'limited' in the result. In the event of a large
                batch, we may return more rows than this.
        Returns:
            A triplet `(updates, new_last_token, limited)`, where:
               * `updates` is a list of database tuples.
               * `new_last_token` is the new position in stream.
               * `limited` is whether there are more updates to fetch.
        """

        def get_all_updated_current_state_deltas_txn(
            txn: LoggingTransaction,
        ) -> list[tuple[int, str, str, str, str]]:
            sql = """
                SELECT stream_id, room_id, type, state_key, event_id
                FROM current_state_delta_stream
                WHERE ? < stream_id AND stream_id <= ?
                    AND instance_name = ?
                ORDER BY stream_id ASC LIMIT ?
            """
            txn.execute(sql, (from_token, to_token, instance_name, target_row_count))
            return cast(list[tuple[int, str, str, str, str]], txn.fetchall())

        def get_deltas_for_stream_id_txn(
            txn: LoggingTransaction, stream_id: int
        ) -> list[tuple[int, str, str, str, str]]:
            sql = """
                SELECT stream_id, room_id, type, state_key, event_id
                FROM current_state_delta_stream
                WHERE stream_id = ?
            """
            txn.execute(sql, [stream_id])
            return cast(list[tuple[int, str, str, str, str]], txn.fetchall())

        # we need to make sure that, for every stream id in the results, we get *all*
        # the rows with that stream id.

        rows: list[tuple[int, str, str, str, str]] = await self.db_pool.runInteraction(
            "get_all_updated_current_state_deltas",
            get_all_updated_current_state_deltas_txn,
        )

        # if we've got fewer rows than the limit, we're good
        if len(rows) < target_row_count:
            return rows, to_token, False

        # we hit the limit, so reduce the upper limit so that we exclude the stream id
        # of the last row in the result.
        assert rows[-1][0] <= to_token
        to_token = rows[-1][0] - 1

        # search backwards through the list for the point to truncate
        for idx in range(len(rows) - 1, 0, -1):
            if rows[idx - 1][0] <= to_token:
                return rows[:idx], to_token, True

        # bother. We didn't get a full set of changes for even a single
        # stream id. let's run the query again, without a row limit, but for
        # just one stream id.
        to_token += 1
        rows = await self.db_pool.runInteraction(
            "get_deltas_for_stream_id", get_deltas_for_stream_id_txn, to_token
        )

        return rows, to_token, True

    async def get_senders_for_event_ids(
        self, event_ids: Collection[str]
    ) -> dict[str, str | None]:
        """
        Given a sequence of event IDs, return the sender associated with each.

        Args:
            event_ids: A collection of event IDs as strings.

        Returns:
            A dict of event ID -> sender of the event.

        If a given event ID does not exist in the `events` table, then no entry
        for that event ID will be returned.
        """

        def _get_senders_for_event_ids(
            txn: LoggingTransaction,
        ) -> dict[str, str | None]:
            rows = self.db_pool.simple_select_many_txn(
                txn=txn,
                table="events",
                column="event_id",
                iterable=event_ids,
                keyvalues={},
                retcols=["event_id", "sender"],
            )
            return dict(rows)

        return await self.db_pool.runInteraction(
            "get_senders_for_event_ids", _get_senders_for_event_ids
        )

    @cached(max_entries=5000)
    async def get_event_ordering(self, event_id: str, room_id: str) -> tuple[int, int]:
        res = await self.db_pool.simple_select_one(
            table="events",
            retcols=["topological_ordering", "stream_ordering"],
            keyvalues={"event_id": event_id, "room_id": room_id},
            allow_none=True,
        )

        if not res:
            raise SynapseError(
                404, "Could not find event %s in room %s" % (event_id, room_id)
            )

        return int(res[0]), int(res[1])

    async def get_next_event_to_expire(self) -> tuple[str, int] | None:
        """Retrieve the entry with the lowest expiry timestamp in the event_expiry
        table, or None if there's no more event to expire.

        Returns:
            A tuple containing the event ID as its first element and an expiry timestamp
            as its second one, if there's at least one row in the event_expiry table.
            None otherwise.
        """

        def get_next_event_to_expire_txn(
            txn: LoggingTransaction,
        ) -> tuple[str, int] | None:
            txn.execute(
                """
                SELECT event_id, expiry_ts FROM event_expiry
                ORDER BY expiry_ts ASC LIMIT 1
                """
            )

            return cast(tuple[str, int] | None, txn.fetchone())

        return await self.db_pool.runInteraction(
            desc="get_next_event_to_expire", func=get_next_event_to_expire_txn
        )

    async def get_event_id_from_transaction_id_and_device_id(
        self, room_id: str, user_id: str, device_id: str, txn_id: str
    ) -> str | None:
        """Look up if we have already persisted an event for the transaction ID,
        returning the event ID if so.
        """
        return await self.db_pool.simple_select_one_onecol(
            table="event_txn_id_device_id",
            keyvalues={
                "room_id": room_id,
                "user_id": user_id,
                "device_id": device_id,
                "txn_id": txn_id,
            },
            retcol="event_id",
            allow_none=True,
            desc="get_event_id_from_transaction_id_and_device_id",
        )

    async def get_already_persisted_events(
        self, events: Iterable[EventBase]
    ) -> dict[str, str]:
        """Look up if we have already persisted an event for the transaction ID,
        returning a mapping from event ID in the given list to the event ID of
        an existing event.

        Also checks if there are duplicates in the given events, if there are
        will map duplicates to the *first* event.
        """

        mapping = {}
        txn_id_to_event: dict[tuple[str, str, str, str], str] = {}

        for event in events:
            device_id = getattr(event.internal_metadata, "device_id", None)
            txn_id = getattr(event.internal_metadata, "txn_id", None)

            if device_id and txn_id:
                # Check if this is a duplicate of an event in the given events.
                existing = txn_id_to_event.get(
                    (event.room_id, event.sender, device_id, txn_id)
                )
                if existing:
                    mapping[event.event_id] = existing
                    continue

                # Check if this is a duplicate of an event we've already
                # persisted.
                existing = await self.get_event_id_from_transaction_id_and_device_id(
                    event.room_id, event.sender, device_id, txn_id
                )
                if existing:
                    mapping[event.event_id] = existing
                    txn_id_to_event[
                        (event.room_id, event.sender, device_id, txn_id)
                    ] = existing
                else:
                    txn_id_to_event[
                        (event.room_id, event.sender, device_id, txn_id)
                    ] = event.event_id

        return mapping

    @wrap_as_background_process("_cleanup_old_transaction_ids")
    async def _cleanup_old_transaction_ids(self) -> None:
        """Cleans out transaction id mappings older than 24hrs."""

        def _cleanup_old_transaction_ids_txn(txn: LoggingTransaction) -> None:
            one_day_ago = self.clock.time_msec() - 24 * 60 * 60 * 1000
            sql = """
                DELETE FROM event_txn_id_device_id
                WHERE inserted_ts < ?
            """
            txn.execute(sql, (one_day_ago,))

        return await self.db_pool.runInteraction(
            "_cleanup_old_transaction_ids",
            _cleanup_old_transaction_ids_txn,
        )

    async def is_event_next_to_backward_gap(self, event: EventBase) -> bool:
        """Check if the given event is next to a backward gap of missing events.
        <latest messages> A(False)--->B(False)--->C(True)--->  <gap, unknown events> <oldest messages>

        Args:
            room_id: room where the event lives
            event: event to check (can't be an `outlier`)

        Returns:
            Boolean indicating whether it's an extremity
        """

        assert not event.internal_metadata.is_outlier(), (
            "is_event_next_to_backward_gap(...) can't be used with `outlier` events. "
            "This function relies on `event_backward_extremities` which won't be filled in for `outliers`."
        )

        def is_event_next_to_backward_gap_txn(txn: LoggingTransaction) -> bool:
            # If the event in question has any of its prev_events listed as a
            # backward extremity, it's next to a gap.
            #
            # We can't just check the backward edges in `event_edges` because
            # when we persist events, we will also record the prev_events as
            # edges to the event in question regardless of whether we have those
            # prev_events yet. We need to check whether those prev_events are
            # backward extremities, also known as gaps, that need to be
            # backfilled.
            backward_extremity_query = """
                SELECT 1 FROM event_backward_extremities
                WHERE
                    room_id = ?
                    AND %s
                LIMIT 1
            """

            # If the event in question is a backward extremity or has any of its
            # prev_events listed as a backward extremity, it's next to a
            # backward gap.
            clause, args = make_in_list_sql_clause(
                self.database_engine,
                "event_id",
                [event.event_id] + list(event.prev_event_ids()),
            )

            txn.execute(backward_extremity_query % (clause,), [event.room_id] + args)
            backward_extremities = txn.fetchall()

            # We consider any backward extremity as a backward gap
            if len(backward_extremities):
                return True

            return False

        return await self.db_pool.runInteraction(
            "is_event_next_to_backward_gap_txn",
            is_event_next_to_backward_gap_txn,
        )

    async def is_event_next_to_forward_gap(self, event: EventBase) -> bool:
        """Check if the given event is next to a forward gap of missing events.
        The gap in front of the latest events is not considered a gap.
        <latest messages> A(False)--->B(False)--->C(False)--->  <gap, unknown events> <oldest messages>
        <latest messages> A(False)--->B(False)--->  <gap, unknown events>  --->D(True)--->E(False) <oldest messages>

        Args:
            room_id: room where the event lives
            event: event to check (can't be an `outlier`)

        Returns:
            Boolean indicating whether it's an extremity
        """

        assert not event.internal_metadata.is_outlier(), (
            "is_event_next_to_forward_gap(...) can't be used with `outlier` events. "
            "This function relies on `event_edges` and `event_forward_extremities` which won't be filled in for `outliers`."
        )

        def is_event_next_to_gap_txn(txn: LoggingTransaction) -> bool:
            # If the event in question is a forward extremity, we will just
            # consider any potential forward gap as not a gap since it's one of
            # the latest events in the room.
            #
            # `event_forward_extremities` does not include backfilled or outlier
            # events so we can't rely on it to find forward gaps. We can only
            # use it to determine whether a message is the latest in the room.
            #
            # We can't combine this query with the `forward_edge_query` below
            # because if the event in question has no forward edges (isn't
            # referenced by any other event's prev_events) but is in
            # `event_forward_extremities`, we don't want to return 0 rows and
            # say it's next to a gap.
            forward_extremity_query = """
                SELECT 1 FROM event_forward_extremities
                WHERE
                    room_id = ?
                    AND event_id = ?
                LIMIT 1
            """

            # We consider any forward extremity as the latest in the room and
            # not a forward gap.
            #
            # To expand, even though there is technically a gap at the front of
            # the room where the forward extremities are, we consider those the
            # latest messages in the room so asking other homeservers for more
            # is useless. The new latest messages will just be federated as
            # usual.
            txn.execute(forward_extremity_query, (event.room_id, event.event_id))
            if txn.fetchone():
                return False

            # Check to see whether the event in question is already referenced
            # by another event. If we don't see any edges, we're next to a
            # forward gap.
            forward_edge_query = """
                SELECT 1 FROM event_edges
                /* Check to make sure the event referencing our event in question is not rejected */
                LEFT JOIN rejections ON event_edges.event_id = rejections.event_id
                WHERE
                    event_edges.prev_event_id = ?
                    /* It's not a valid edge if the event referencing our event in
                     * question is rejected.
                     */
                    AND rejections.event_id IS NULL
                LIMIT 1
            """

            # If there are no forward edges to the event in question (another
            # event hasn't referenced this event in their prev_events), then we
            # assume there is a forward gap in the history.
            txn.execute(forward_edge_query, (event.event_id,))
            if not txn.fetchone():
                return True

            return False

        return await self.db_pool.runInteraction(
            "is_event_next_to_gap_txn",
            is_event_next_to_gap_txn,
        )

    async def get_event_id_for_timestamp(
        self, room_id: str, timestamp: int, direction: Direction
    ) -> str | None:
        """Find the closest event to the given timestamp in the given direction.

        Args:
            room_id: Room to fetch the event from
            timestamp: The point in time (inclusive) we should navigate from in
                the given direction to find the closest event.
            direction: indicates whether we should navigate forward
                or backward from the given timestamp to find the closest event.

        Returns:
            The closest event_id otherwise None if we can't find any event in
            the given direction.
        """
        if direction == Direction.BACKWARDS:
            # Find closest event *before* a given timestamp. We use descending
            # (which gives values largest to smallest) because we want the
            # largest possible timestamp *before* the given timestamp.
            comparison_operator = "<="
            order = "DESC"
        else:
            # Find closest event *after* a given timestamp. We use ascending
            # (which gives values smallest to largest) because we want the
            # closest possible timestamp *after* the given timestamp.
            comparison_operator = ">="
            order = "ASC"

        sql_template = f"""
            SELECT event_id FROM events
            LEFT JOIN rejections USING (event_id)
            WHERE
                room_id = ?
                AND origin_server_ts {comparison_operator} ?
                /**
                 * Make sure the event isn't an `outlier` because we have no way
                 * to later check whether it's next to a gap. `outliers` do not
                 * have entries in the `event_edges`, `event_forward_extremeties`,
                 * and `event_backward_extremities` tables to check against
                 * (used by `is_event_next_to_backward_gap` and `is_event_next_to_forward_gap`).
                 */
                AND NOT outlier
                /* Make sure event is not rejected */
                AND rejections.event_id IS NULL
            /**
             * First sort by the message timestamp. If the message timestamps are the
             * same, we want the message that logically comes "next" (before/after
             * the given timestamp) based on the DAG and its topological order (`depth`).
             * Finally, we can tie-break based on when it was received on the server
             * (`stream_ordering`).
             */
            ORDER BY origin_server_ts {order}, depth {order}, stream_ordering {order}
            LIMIT 1;
        """

        def get_event_id_for_timestamp_txn(txn: LoggingTransaction) -> str | None:
            txn.execute(
                sql_template,
                (room_id, timestamp),
            )
            row = txn.fetchone()
            if row:
                (event_id,) = row
                return event_id

            return None

        return await self.db_pool.runInteraction(
            "get_event_id_for_timestamp_txn",
            get_event_id_for_timestamp_txn,
        )

    @cachedList(cached_method_name="is_partial_state_event", list_name="event_ids")
    async def get_partial_state_events(
        self, event_ids: Collection[str]
    ) -> Mapping[str, bool]:
        """Checks which of the given events have partial state

        Args:
            event_ids: the events we want to check for partial state.

        Returns:
            a dict mapping from event id to partial-stateness. We return True for
            any of the events which are unknown (or are outliers).
        """
        result = cast(
            list[tuple[str]],
            await self.db_pool.simple_select_many_batch(
                table="partial_state_events",
                column="event_id",
                iterable=event_ids,
                retcols=["event_id"],
                desc="get_partial_state_events",
            ),
        )
        # convert the result to a dict, to make @cachedList work
        partial = {r[0] for r in result}
        return {e_id: e_id in partial for e_id in event_ids}

    @cached()
    async def is_partial_state_event(self, event_id: str) -> bool:
        """Checks if the given event has partial state"""
        result = await self.db_pool.simple_select_one_onecol(
            table="partial_state_events",
            keyvalues={"event_id": event_id},
            retcol="1",
            allow_none=True,
            desc="is_partial_state_event",
        )
        return result is not None

    async def get_partial_state_events_batch(self, room_id: str) -> list[str]:
        """
        Get a list of events in the given room that:
        - have partial state; and
        - are ready to be resynced (because they have no prev_events that are
          partial-stated)

        See the docstring on `_get_partial_state_events_batch_txn` for more
        information.
        """
        return await self.db_pool.runInteraction(
            "get_partial_state_events_batch",
            self._get_partial_state_events_batch_txn,
            room_id,
        )

    @staticmethod
    def _get_partial_state_events_batch_txn(
        txn: LoggingTransaction, room_id: str
    ) -> list[str]:
        # we want to work through the events from oldest to newest, so
        # we only want events whose prev_events do *not* have partial state - hence
        # the 'NOT EXISTS' clause in the below.
        #
        # This is necessary because ordering by stream ordering isn't quite enough
        # to ensure that we work from oldest to newest event (in particular,
        # if an event is initially persisted as an outlier and later de-outliered,
        # it can end up with a lower stream_ordering than its prev_events).
        #
        # Typically this means we'll only return one event per batch, but that's
        # hard to do much about.
        #
        # See also: https://github.com/matrix-org/synapse/issues/13001
        txn.execute(
            """
            SELECT event_id FROM partial_state_events AS pse
                JOIN events USING (event_id)
            WHERE pse.room_id = ? AND
               NOT EXISTS(
                  SELECT 1 FROM event_edges AS ee
                     JOIN partial_state_events AS prev_pse ON (prev_pse.event_id=ee.prev_event_id)
                     WHERE ee.event_id=pse.event_id
               )
            ORDER BY events.stream_ordering
            LIMIT 100
            """,
            (room_id,),
        )
        return [row[0] for row in txn]

    def mark_event_rejected_txn(
        self,
        txn: LoggingTransaction,
        event_id: str,
        rejection_reason: str | None,
    ) -> None:
        """Mark an event that was previously accepted as rejected, or vice versa

        This can happen, for example, when resyncing state during a faster join.

        It is the caller's responsibility to ensure that other workers are
        sent a notification so that they call `_invalidate_local_get_event_cache()`.

        Args:
            txn:
            event_id: ID of event to update
            rejection_reason: reason it has been rejected, or None if it is now accepted
        """
        if rejection_reason is None:
            logger.info(
                "Marking previously-processed event %s as accepted",
                event_id,
            )
            self.db_pool.simple_delete_txn(
                txn,
                "rejections",
                keyvalues={"event_id": event_id},
            )
        else:
            logger.info(
                "Marking previously-processed event %s as rejected(%s)",
                event_id,
                rejection_reason,
            )
            self.db_pool.simple_upsert_txn(
                txn,
                table="rejections",
                keyvalues={"event_id": event_id},
                values={
                    "reason": rejection_reason,
                    "last_check": self.clock.time_msec(),
                },
            )
        self.db_pool.simple_update_txn(
            txn,
            table="events",
            keyvalues={"event_id": event_id},
            updatevalues={"rejection_reason": rejection_reason},
        )

        self.invalidate_get_event_cache_after_txn(txn, event_id)

    async def get_events_sent_by_user_in_room(
        self, user_id: str, room_id: str, limit: int, filter: list[str] | None = None
    ) -> list[str] | None:
        """
        Get a list of event ids of events sent by the user in the specified room

        Args:
            user_id: user ID to search against
            room_id: room ID of the room to search for events in
            filter: type of events to filter for
            limit: maximum number of event ids to return
        """

        def _get_events_by_user_in_room_txn(
            txn: LoggingTransaction,
            user_id: str,
            room_id: str,
            filter: list[str] | None,
            batch_size: int,
            offset: int,
        ) -> tuple[list[str] | None, int]:
            if filter:
                base_clause, args = make_in_list_sql_clause(
                    txn.database_engine, "type", filter
                )
                clause = f"AND {base_clause}"
                parameters = (user_id, room_id, *args, batch_size, offset)
            else:
                clause = ""
                parameters = (user_id, room_id, batch_size, offset)

            sql = f"""
                    SELECT event_id FROM events
                    WHERE sender = ? AND room_id = ?
                    {clause}
                    ORDER BY received_ts DESC
                    LIMIT ?
                    OFFSET ?
                  """
            txn.execute(sql, parameters)
            res = txn.fetchall()
            if res:
                events = [row[0] for row in res]
            else:
                events = None

            return events, offset + batch_size

        offset = 0
        batch_size = 100
        if batch_size > limit:
            batch_size = limit

        selected_ids: list[str] = []
        while offset < limit:
            res, offset = await self.db_pool.runInteraction(
                "get_events_by_user",
                _get_events_by_user_in_room_txn,
                user_id,
                room_id,
                filter,
                batch_size,
                offset,
            )
            if res:
                selected_ids = selected_ids + res
            else:
                break
        return selected_ids

    async def have_finished_sliding_sync_background_jobs(self) -> bool:
        """Return if it's safe to use the sliding sync membership tables."""

        if self._has_finished_sliding_sync_background_jobs:
            # as an optimisation, once the job finishes, don't issue another
            # database transaction to check it, since it won't 'un-finish'
            return True

        self._has_finished_sliding_sync_background_jobs = await self.db_pool.updates.have_completed_background_updates(
            (
                _BackgroundUpdates.SLIDING_SYNC_PREFILL_JOINED_ROOMS_TO_RECALCULATE_TABLE_BG_UPDATE,
                _BackgroundUpdates.SLIDING_SYNC_JOINED_ROOMS_BG_UPDATE,
                _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
            )
        )
        return self._has_finished_sliding_sync_background_jobs

    async def get_sent_invite_count_by_user(self, user_id: str, from_ts: int) -> int:
        """
        Get the number of invites sent by the given user at or after the provided timestamp.

        Args:
            user_id: user ID to search against
            from_ts: a timestamp in milliseconds from the unix epoch. Filters against
                `events.received_ts`

        """

        def _get_sent_invite_count_by_user_txn(
            txn: LoggingTransaction, user_id: str, from_ts: int
        ) -> int:
            sql = """
                  SELECT COUNT(rm.event_id)
                  FROM room_memberships AS rm
                  INNER JOIN events AS e USING(event_id)
                  WHERE rm.sender = ?
                    AND rm.membership = 'invite'
                    AND e.type = 'm.room.member'
                    AND e.received_ts >= ?
            """

            txn.execute(sql, (user_id, from_ts))
            res = txn.fetchone()

            if res is None:
                return 0
            return int(res[0])

        return await self.db_pool.runInteraction(
            "_get_sent_invite_count_by_user_txn",
            _get_sent_invite_count_by_user_txn,
            user_id,
            from_ts,
        )

    @cached(tree=True)
    async def get_metadata_for_event(
        self, room_id: str, event_id: str
    ) -> EventMetadata | None:
        row = await self.db_pool.simple_select_one(
            table="events",
            keyvalues={"room_id": room_id, "event_id": event_id},
            retcols=("sender", "received_ts"),
            allow_none=True,
            desc="get_metadata_for_event",
        )
        if row is None:
            return None

        return EventMetadata(
            sender=row[0],
            received_ts=row[1],
        )
