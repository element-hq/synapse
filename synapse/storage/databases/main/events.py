#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019-2021 The Matrix.org Foundation C.I.C.
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
import collections
import itertools
import logging
from collections import OrderedDict
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Generator,
    Iterable,
    Sequence,
    TypedDict,
    cast,
)

import attr
from prometheus_client import Counter

import synapse.metrics
from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    Membership,
    RelationTypes,
)
from synapse.api.errors import PartialStateConflictError
from synapse.api.room_versions import RoomVersions
from synapse.events import (
    EventBase,
    StrippedStateEvent,
    is_creator,
    relation_from_event,
)
from synapse.events.snapshot import EventPersistencePair
from synapse.events.utils import parse_stripped_state_event
from synapse.logging.opentracing import trace
from synapse.metrics import SERVER_NAME_LABEL
from synapse.storage._base import db_to_json, make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_tuple_in_list_sql_clause,
)
from synapse.storage.databases.main.event_federation import EventFederationStore
from synapse.storage.databases.main.events_worker import EventCacheEntry
from synapse.storage.databases.main.search import SearchEntry
from synapse.storage.engines import PostgresEngine
from synapse.storage.util.id_generators import AbstractStreamIdGenerator
from synapse.storage.util.sequence import SequenceGenerator
from synapse.types import (
    JsonDict,
    MutableStateMap,
    StateMap,
    StrCollection,
)
from synapse.types.handlers import SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
from synapse.types.state import StateFilter
from synapse.util.events import get_plain_text_topic_from_event_content
from synapse.util.iterutils import batch_iter, sorted_topologically
from synapse.util.json import json_encoder
from synapse.util.stringutils import non_null_str_or_none

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.storage.databases.main import DataStore


logger = logging.getLogger(__name__)

persist_event_counter = Counter(
    "synapse_storage_events_persisted_events", "", labelnames=[SERVER_NAME_LABEL]
)
event_counter = Counter(
    "synapse_storage_events_persisted_events_sep",
    "",
    labelnames=[
        "type",  # The event type or "*other*" for types we don't track
        "origin_type",
        SERVER_NAME_LABEL,
    ],
)

# Event types that we track in the `events_counter` metric above.
#
# This list is chosen to balance tracking the most common event types that are
# useful to monitor (and are likely to spike), while keeping the cardinality of
# the metric low enough to avoid wasted resources.
TRACKED_EVENT_TYPES = {
    EventTypes.Message,
    EventTypes.Encrypted,
    EventTypes.Member,
    EventTypes.ThirdPartyInvite,
    EventTypes.Redaction,
    EventTypes.Create,
    EventTypes.Tombstone,
}

# State event type/key pairs that we need to gather to fill in the
# `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` tables.
SLIDING_SYNC_RELEVANT_STATE_SET = (
    # So we can fill in the `room_type` column
    (EventTypes.Create, ""),
    # So we can fill in the `is_encrypted` column
    (EventTypes.RoomEncryption, ""),
    # So we can fill in the `room_name` column
    (EventTypes.Name, ""),
    # So we can fill in the `tombstone_successor_room_id` column
    (EventTypes.Tombstone, ""),
)


@attr.s(slots=True, auto_attribs=True)
class DeltaState:
    """Deltas to use to update the `current_state_events` table.

    Attributes:
        to_delete: List of type/state_keys to delete from current state
        to_insert: Map of state to upsert into current state
        no_longer_in_room: The server is no longer in the room, so the room
            should e.g. be removed from `current_state_events` table.
    """

    to_delete: list[tuple[str, str]]
    to_insert: StateMap[str]
    no_longer_in_room: bool = False

    def is_noop(self) -> bool:
        """Whether this state delta is actually empty"""
        return not self.to_delete and not self.to_insert and not self.no_longer_in_room


# We want `total=False` because we want to allow values to be unset.
class SlidingSyncStateInsertValues(TypedDict, total=False):
    """
    Insert values relevant for the `sliding_sync_joined_rooms` and
    `sliding_sync_membership_snapshots` database tables.
    """

    room_type: str | None
    is_encrypted: bool | None
    room_name: str | None
    tombstone_successor_room_id: str | None


class SlidingSyncMembershipSnapshotSharedInsertValues(
    SlidingSyncStateInsertValues, total=False
):
    """
    Insert values for `sliding_sync_membership_snapshots` that we can share across
    multiple memberships
    """

    has_known_state: bool | None


@attr.s(slots=True, auto_attribs=True)
class SlidingSyncMembershipInfo:
    """
    Values unique to each membership
    """

    user_id: str
    sender: str
    membership_event_id: str
    membership: str


@attr.s(slots=True, auto_attribs=True)
class SlidingSyncMembershipInfoWithEventPos(SlidingSyncMembershipInfo):
    """
    SlidingSyncMembershipInfo + `stream_ordering`/`instance_name` of the membership
    event
    """

    membership_event_stream_ordering: int
    membership_event_instance_name: str


@attr.s(slots=True, auto_attribs=True)
class SlidingSyncTableChanges:
    room_id: str
    # If the row doesn't exist in the `sliding_sync_joined_rooms` table, we need to
    # fully-insert it which means we also need to include a `bump_stamp` value to use
    # for the row. This should only be populated when we're trying to fully-insert a
    # row.
    #
    # FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
    # foreground update for
    # `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
    # https://github.com/element-hq/synapse/issues/17623)
    joined_room_bump_stamp_to_fully_insert: int | None
    # Values to upsert into `sliding_sync_joined_rooms`
    joined_room_updates: SlidingSyncStateInsertValues

    # Shared values to upsert into `sliding_sync_membership_snapshots` for each
    # `to_insert_membership_snapshots`
    membership_snapshot_shared_insert_values: (
        SlidingSyncMembershipSnapshotSharedInsertValues
    )
    # List of membership to insert into `sliding_sync_membership_snapshots`
    to_insert_membership_snapshots: list[SlidingSyncMembershipInfo]
    # List of user_id to delete from `sliding_sync_membership_snapshots`
    to_delete_membership_snapshots: list[str]


@attr.s(slots=True, auto_attribs=True)
class NewEventChainLinks:
    """Information about new auth chain links that need to be added to the DB.

    Attributes:
        chain_id, sequence_number: the IDs corresponding to the event being
            inserted, and the starting point of the links
        links: Lists the links that need to be added, 2-tuple of the chain
            ID/sequence number of the end point of the link.
    """

    chain_id: int
    sequence_number: int

    links: list[tuple[int, int]] = attr.Factory(list)


class PersistEventsStore:
    """Contains all the functions for writing events to the database.

    Should only be instantiated on one process (when using a worker mode setup).

    Note: This is not part of the `DataStore` mixin.
    """

    def __init__(
        self,
        hs: "HomeServer",
        db: DatabasePool,
        main_data_store: "DataStore",
        db_conn: LoggingDatabaseConnection,
    ):
        self.hs = hs
        self.server_name = hs.hostname
        self.db_pool = db
        self.store = main_data_store
        self.database_engine = db.engine
        self._clock = hs.get_clock()
        self._instance_name = hs.get_instance_name()

        self._ephemeral_messages_enabled = hs.config.server.enable_ephemeral_messages
        self.is_mine_id = hs.is_mine_id

        # This should only exist on instances that are configured to write
        assert hs.get_instance_name() in hs.config.worker.writers.events, (
            "Can only instantiate EventsStore on master"
        )

        # Since we have been configured to write, we ought to have id generators,
        # rather than id trackers.
        assert isinstance(self.store._backfill_id_gen, AbstractStreamIdGenerator)
        assert isinstance(self.store._stream_id_gen, AbstractStreamIdGenerator)

        # Ideally we'd move these ID gens here, unfortunately some other ID
        # generators are chained off them so doing so is a bit of a PITA.
        self._backfill_id_gen: AbstractStreamIdGenerator = self.store._backfill_id_gen
        self._stream_id_gen: AbstractStreamIdGenerator = self.store._stream_id_gen

    @trace
    async def _persist_events_and_state_updates(
        self,
        room_id: str,
        events_and_contexts: list[EventPersistencePair],
        *,
        state_delta_for_room: DeltaState | None,
        new_forward_extremities: set[str] | None,
        new_event_links: dict[str, NewEventChainLinks],
        use_negative_stream_ordering: bool = False,
        inhibit_local_membership_updates: bool = False,
    ) -> None:
        """Persist a set of events alongside updates to the current state and
                forward extremities tables.

        Assumes that we are only persisting events for one room at a time.

        Args:
            room_id:
            events_and_contexts:
            state_delta_for_room: The delta to apply to the room state
            new_forward_extremities: A set of event IDs that are the new forward
                extremities of the room.
            use_negative_stream_ordering: Whether to start stream_ordering on
                the negative side and decrement. This should be set as True
                for backfilled events because backfilled events get a negative
                stream ordering so they don't come down incremental `/sync`.
            inhibit_local_membership_updates: Stop the local_current_membership
                from being updated by these events. This should be set to True
                for backfilled events because backfilled events in the past do
                not affect the current local state.

        Returns:
            Resolves when the events have been persisted

        Raises:
            PartialStateConflictError: if attempting to persist a partial state event in
                a room that has been un-partial stated.
        """

        # We want to calculate the stream orderings as late as possible, as
        # we only notify after all events with a lesser stream ordering have
        # been persisted. I.e. if we spend 10s inside the with block then
        # that will delay all subsequent events from being notified about.
        # Hence why we do it down here rather than wrapping the entire
        # function.
        #
        # Its safe to do this after calculating the state deltas etc as we
        # only need to protect the *persistence* of the events. This is to
        # ensure that queries of the form "fetch events since X" don't
        # return events and stream positions after events that are still in
        # flight, as otherwise subsequent requests "fetch event since Y"
        # will not return those events.
        #
        # Note: Multiple instances of this function cannot be in flight at
        # the same time for the same room.
        if use_negative_stream_ordering:
            stream_ordering_manager = self._backfill_id_gen.get_next_mult(
                len(events_and_contexts)
            )
        else:
            stream_ordering_manager = self._stream_id_gen.get_next_mult(
                len(events_and_contexts)
            )

        async with stream_ordering_manager as stream_orderings:
            for (event, _), stream in zip(events_and_contexts, stream_orderings):
                # XXX: We can't rely on `stream_ordering`/`instance_name` being correct
                # at this point. We could be working with events that were previously
                # persisted as an `outlier` with one `stream_ordering` but are now being
                # persisted again and de-outliered and are being assigned a different
                # `stream_ordering` here that won't end up being used.
                # `_update_outliers_txn()` will fix this discrepancy (always use the
                # `stream_ordering` from the first time it was persisted).
                event.internal_metadata.stream_ordering = stream
                event.internal_metadata.instance_name = self._instance_name

            sliding_sync_table_changes = None
            if state_delta_for_room is not None:
                sliding_sync_table_changes = (
                    await self._calculate_sliding_sync_table_changes(
                        room_id, events_and_contexts, state_delta_for_room
                    )
                )

            await self.db_pool.runInteraction(
                "persist_events",
                self._persist_events_txn,
                room_id=room_id,
                events_and_contexts=events_and_contexts,
                inhibit_local_membership_updates=inhibit_local_membership_updates,
                state_delta_for_room=state_delta_for_room,
                new_forward_extremities=new_forward_extremities,
                new_event_links=new_event_links,
                sliding_sync_table_changes=sliding_sync_table_changes,
            )
            persist_event_counter.labels(**{SERVER_NAME_LABEL: self.server_name}).inc(
                len(events_and_contexts)
            )

            if not use_negative_stream_ordering:
                # we don't want to set the event_persisted_position to a negative
                # stream_ordering.
                synapse.metrics.event_persisted_position.labels(
                    **{SERVER_NAME_LABEL: self.server_name}
                ).set(stream)

            for event, context in events_and_contexts:
                if context.app_service:
                    origin_type = "application_service"
                elif self.hs.is_mine_id(event.sender):
                    origin_type = "local"
                else:
                    origin_type = "remote"

                # We only track a subset of event types, to avoid high
                # cardinality in the metrics.
                metrics_event_type = (
                    event.type if event.type in TRACKED_EVENT_TYPES else "*other*"
                )

                event_counter.labels(
                    type=metrics_event_type,
                    origin_type=origin_type,
                    **{SERVER_NAME_LABEL: self.server_name},
                ).inc()

                if (
                    not self.hs.config.experimental.msc4293_enabled
                    or event.type != EventTypes.Member
                    or event.state_key is None
                ):
                    continue

                # check if this is an unban/join that will undo a ban/kick redaction for
                # a user in the room
                if event.membership in [Membership.LEAVE, Membership.JOIN]:
                    if (
                        event.membership == Membership.LEAVE
                        and event.sender == event.state_key
                    ):
                        # self-leave, ignore
                        continue

                    # if there is an existing ban/leave causing redactions for
                    # this user/room combination update the entry with the stream
                    # ordering when the redactions should stop - in the case of a backfilled
                    # event where the stream ordering is negative, use the current max stream
                    # ordering
                    stream_ordering = event.internal_metadata.stream_ordering
                    assert stream_ordering is not None
                    if stream_ordering < 0:
                        stream_ordering = self._stream_id_gen.get_current_token()
                    await self.db_pool.simple_update(
                        "room_ban_redactions",
                        {"room_id": event.room_id, "user_id": event.state_key},
                        {"redact_end_ordering": stream_ordering},
                        desc="room_ban_redactions update redact_end_ordering",
                    )

                # check for msc4293 redact_events flag and apply if found
                if event.membership not in [Membership.LEAVE, Membership.BAN]:
                    continue
                redact = event.content.get("org.matrix.msc4293.redact_events", False)
                if not redact or not isinstance(redact, bool):
                    continue
                # self-bans currently are not authorized so we don't check for that
                # case
                if (
                    event.membership == Membership.BAN
                    and event.sender == event.state_key
                ):
                    continue

                # check that sender can redact
                redact_allowed = await self._can_sender_redact(event)

                # Signal that this user's past events in this room
                # should be redacted by adding an entry to
                # `room_ban_redactions`.
                if redact_allowed:
                    await self.db_pool.simple_upsert(
                        "room_ban_redactions",
                        {"room_id": event.room_id, "user_id": event.state_key},
                        {
                            "redacting_event_id": event.event_id,
                            "redact_end_ordering": None,
                        },
                        {
                            "room_id": event.room_id,
                            "user_id": event.state_key,
                            "redacting_event_id": event.event_id,
                            "redact_end_ordering": None,
                        },
                    )

                    # normally the cache entry for a redacted event would be invalidated
                    # by an arriving redaction event, but since we are not creating redaction
                    # events we invalidate manually
                    self.store._invalidate_local_get_event_cache_room_id(event.room_id)

                    self.store._invalidate_async_get_event_cache_room_id(event.room_id)

            if new_forward_extremities:
                self.store.get_latest_event_ids_in_room.prefill(
                    (room_id,), frozenset(new_forward_extremities)
                )

    async def _can_sender_redact(self, event: EventBase) -> bool:
        state_filter = StateFilter.from_types(
            [(EventTypes.PowerLevels, ""), (EventTypes.Create, "")]
        )
        state = await self.store.get_partial_filtered_current_state_ids(
            event.room_id, state_filter
        )
        pl_id = state[(EventTypes.PowerLevels, "")]
        pl_event = await self.store.get_event(pl_id, allow_none=True)

        create_id = state[(EventTypes.Create, "")]
        create_event = await self.store.get_event(create_id, allow_none=True)

        if create_event is None:
            # not sure how this would happen but if it does then just deny the redaction
            logger.warning("No create event found for room %s", event.room_id)
            return False

        if create_event.room_version.msc4289_creator_power_enabled:
            # per the spec, grant the creator infinite power level and all other users 0
            if is_creator(create_event, event.sender):
                return True
            if pl_event is None:
                # per the spec, users other than the room creator have power level
                # 0, which is less than the default to redact events (50).
                return False
        else:
            # per the spec, if a power level event isn't in the room, grant the creator
            # level 100 (the default redaction level is 50) and all other users 0
            if pl_event is None:
                return create_event.sender == event.sender

        assert pl_event is not None
        sender_level = pl_event.content.get("users", {}).get(event.sender)
        if sender_level is None:
            sender_level = pl_event.content.get("users_default", 0)

        redact_level = pl_event.content.get("redact")
        if redact_level is None:
            redact_level = pl_event.content.get("events_default", 0)

        room_redaction_level = pl_event.content.get("events", {}).get(
            "m.room.redaction"
        )
        if room_redaction_level is not None:
            if sender_level < room_redaction_level:
                return False

        if sender_level >= redact_level:
            return True

        return False

    async def _calculate_sliding_sync_table_changes(
        self,
        room_id: str,
        events_and_contexts: Sequence[EventPersistencePair],
        delta_state: DeltaState,
    ) -> SlidingSyncTableChanges:
        """
        Calculate the changes to the `sliding_sync_membership_snapshots` and
        `sliding_sync_joined_rooms` tables given the deltas that are going to be used to
        update the `current_state_events` table.

        Just a bunch of pre-processing so we so we don't need to spend time in the
        transaction itself gathering all of this info. It's also easier to deal with
        redactions outside of a transaction.

        Args:
            room_id: The room ID currently being processed.
            events_and_contexts: List of tuples of (event, context) being persisted.
                This is completely optional (you can pass an empty list) and will just
                save us from fetching the events from the database if we already have
                them. We assume the list is sorted ascending by `stream_ordering`. We
                don't care about the sort when the events are backfilled (with negative
                `stream_ordering`).
            delta_state: Deltas that are going to be used to update the
                `current_state_events` table. Changes to the current state of the room.

        Returns:
            SlidingSyncTableChanges
        """
        to_insert = delta_state.to_insert
        to_delete = delta_state.to_delete

        # If no state is changing, we don't need to do anything. This can happen when a
        # partial-stated room is re-syncing the current state.
        if not to_insert and not to_delete:
            return SlidingSyncTableChanges(
                room_id=room_id,
                joined_room_bump_stamp_to_fully_insert=None,
                joined_room_updates={},
                membership_snapshot_shared_insert_values={},
                to_insert_membership_snapshots=[],
                to_delete_membership_snapshots=[],
            )

        event_map = {event.event_id: event for event, _ in events_and_contexts}

        # Handle gathering info for the `sliding_sync_membership_snapshots` table
        #
        # This would only happen if someone was state reset out of the room
        user_ids_to_delete_membership_snapshots = [
            state_key
            for event_type, state_key in to_delete
            if event_type == EventTypes.Member and self.is_mine_id(state_key)
        ]

        membership_snapshot_shared_insert_values: SlidingSyncMembershipSnapshotSharedInsertValues = {}
        membership_infos_to_insert_membership_snapshots: list[
            SlidingSyncMembershipInfo
        ] = []
        if to_insert:
            membership_event_id_to_user_id_map: dict[str, str] = {}
            for state_key, event_id in to_insert.items():
                if state_key[0] == EventTypes.Member and self.is_mine_id(state_key[1]):
                    membership_event_id_to_user_id_map[event_id] = state_key[1]

            membership_event_map: dict[str, EventBase] = {}
            # In normal event persist scenarios, we should be able to find the
            # membership events in the `events_and_contexts` given to us but it's
            # possible a state reset happened which added us to the room without a
            # corresponding new membership event (reset back to a previous membership).
            missing_membership_event_ids: set[str] = set()
            for membership_event_id in membership_event_id_to_user_id_map.keys():
                membership_event = event_map.get(membership_event_id)
                if membership_event:
                    membership_event_map[membership_event_id] = membership_event
                else:
                    missing_membership_event_ids.add(membership_event_id)

            # Otherwise, we need to find a couple events that we were reset to.
            if missing_membership_event_ids:
                remaining_events = await self.store.get_events(
                    missing_membership_event_ids
                )
                # There shouldn't be any missing events
                assert remaining_events.keys() == missing_membership_event_ids, (
                    missing_membership_event_ids.difference(remaining_events.keys())
                )
                membership_event_map.update(remaining_events)

            for (
                membership_event_id,
                user_id,
            ) in membership_event_id_to_user_id_map.items():
                membership_infos_to_insert_membership_snapshots.append(
                    # XXX: We don't use `SlidingSyncMembershipInfoWithEventPos` here
                    # because we're sourcing the event from `events_and_contexts`, we
                    # can't rely on `stream_ordering`/`instance_name` being correct at
                    # this point. We could be working with events that were previously
                    # persisted as an `outlier` with one `stream_ordering` but are now
                    # being persisted again and de-outliered and assigned a different
                    # `stream_ordering` that won't end up being used. Since we call
                    # `_calculate_sliding_sync_table_changes()` before
                    # `_update_outliers_txn()` which fixes this discrepancy (always use
                    # the `stream_ordering` from the first time it was persisted), we're
                    # working with an unreliable `stream_ordering` value that will
                    # possibly be unused and not make it into the `events` table.
                    SlidingSyncMembershipInfo(
                        user_id=user_id,
                        sender=membership_event_map[membership_event_id].sender,
                        membership_event_id=membership_event_id,
                        membership=membership_event_map[membership_event_id].membership,
                    )
                )

            if membership_infos_to_insert_membership_snapshots:
                current_state_ids_map: MutableStateMap[str] = dict(
                    await self.store.get_partial_filtered_current_state_ids(
                        room_id,
                        state_filter=StateFilter.from_types(
                            SLIDING_SYNC_RELEVANT_STATE_SET
                        ),
                    )
                )
                # Since we fetched the current state before we took `to_insert`/`to_delete`
                # into account, we need to do a couple fixups.
                #
                # Update the current_state_map with what we have `to_delete`
                for state_key in to_delete:
                    current_state_ids_map.pop(state_key, None)
                # Update the current_state_map with what we have `to_insert`
                for state_key, event_id in to_insert.items():
                    if state_key in SLIDING_SYNC_RELEVANT_STATE_SET:
                        current_state_ids_map[state_key] = event_id

                current_state_map: MutableStateMap[EventBase] = {}
                # In normal event persist scenarios, we probably won't be able to find
                # these state events in `events_and_contexts` since we don't generally
                # batch up local membership changes with other events, but it can
                # happen.
                missing_state_event_ids: set[str] = set()
                for state_key, event_id in current_state_ids_map.items():
                    event = event_map.get(event_id)
                    if event:
                        current_state_map[state_key] = event
                    else:
                        missing_state_event_ids.add(event_id)

                # Otherwise, we need to find a couple events
                if missing_state_event_ids:
                    remaining_events = await self.store.get_events(
                        missing_state_event_ids
                    )
                    # There shouldn't be any missing events
                    assert remaining_events.keys() == missing_state_event_ids, (
                        missing_state_event_ids.difference(remaining_events.keys())
                    )
                    for event in remaining_events.values():
                        current_state_map[(event.type, event.state_key)] = event

                if current_state_map:
                    state_insert_values = PersistEventsStore._get_sliding_sync_insert_values_from_state_map(
                        current_state_map
                    )
                    membership_snapshot_shared_insert_values.update(state_insert_values)
                    # We have current state to work from
                    membership_snapshot_shared_insert_values["has_known_state"] = True
                else:
                    # We don't have any `current_state_events` anymore (previously
                    # cleared out because of `no_longer_in_room`). This can happen if
                    # one user is joined and another is invited (some non-join
                    # membership). If the joined user leaves, we are `no_longer_in_room`
                    # and `current_state_events` is cleared out. When the invited user
                    # rejects the invite (leaves the room), we will end up here.
                    #
                    # In these cases, we should inherit the meta data from the previous
                    # snapshot so we shouldn't update any of the state values. When
                    # using sliding sync filters, this will prevent the room from
                    # disappearing/appearing just because you left the room.
                    #
                    # Ideally, we could additionally assert that we're only here for
                    # valid non-join membership transitions.
                    assert delta_state.no_longer_in_room

        # Handle gathering info for the `sliding_sync_joined_rooms` table
        #
        # We only deal with
        # updating the state related columns. The
        # `event_stream_ordering`/`bump_stamp` are updated elsewhere in the event
        # persisting stack (see
        # `_update_sliding_sync_tables_with_new_persisted_events_txn()`)
        #
        joined_room_updates: SlidingSyncStateInsertValues = {}
        bump_stamp_to_fully_insert: int | None = None
        if not delta_state.no_longer_in_room:
            current_state_ids_map = {}

            # Always fully-insert rows if they don't already exist in the
            # `sliding_sync_joined_rooms` table. This way we can rely on a row if it
            # exists in the table.
            #
            # FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
            # foreground update for
            # `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
            # https://github.com/element-hq/synapse/issues/17623)
            existing_row_in_table = await self.store.db_pool.simple_select_one_onecol(
                table="sliding_sync_joined_rooms",
                keyvalues={"room_id": room_id},
                retcol="room_id",
                allow_none=True,
            )
            if not existing_row_in_table:
                most_recent_bump_event_pos_results = (
                    await self.store.get_last_event_pos_in_room(
                        room_id,
                        event_types=SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES,
                    )
                )
                if most_recent_bump_event_pos_results is not None:
                    _, new_bump_event_pos = most_recent_bump_event_pos_results

                    # If we've just joined a remote room, then the last bump event may
                    # have been backfilled (and so have a negative stream ordering).
                    # These negative stream orderings can't sensibly be compared, so
                    # instead just leave it as `None` in the table and we will use their
                    # membership event position as the bump event position in the
                    # Sliding Sync API.
                    if new_bump_event_pos.stream > 0:
                        bump_stamp_to_fully_insert = new_bump_event_pos.stream

                current_state_ids_map = dict(
                    await self.store.get_partial_filtered_current_state_ids(
                        room_id,
                        state_filter=StateFilter.from_types(
                            SLIDING_SYNC_RELEVANT_STATE_SET
                        ),
                    )
                )

            # Look through the items we're going to insert into the current state to see
            # if there is anything that we care about and should also update in the
            # `sliding_sync_joined_rooms` table.
            for state_key, event_id in to_insert.items():
                if state_key in SLIDING_SYNC_RELEVANT_STATE_SET:
                    current_state_ids_map[state_key] = event_id

            # Get the full event objects for the current state events
            #
            # In normal event persist scenarios, we should be able to find the state
            # events in the `events_and_contexts` given to us but it's possible a state
            # reset happened which that reset back to a previous state.
            current_state_map = {}
            missing_event_ids: set[str] = set()
            for state_key, event_id in current_state_ids_map.items():
                event = event_map.get(event_id)
                if event:
                    current_state_map[state_key] = event
                else:
                    missing_event_ids.add(event_id)

            # Otherwise, we need to find a couple events that we were reset to.
            if missing_event_ids:
                remaining_events = await self.store.get_events(missing_event_ids)
                # There shouldn't be any missing events
                assert remaining_events.keys() == missing_event_ids, (
                    missing_event_ids.difference(remaining_events.keys())
                )
                for event in remaining_events.values():
                    current_state_map[(event.type, event.state_key)] = event

            joined_room_updates = (
                PersistEventsStore._get_sliding_sync_insert_values_from_state_map(
                    current_state_map
                )
            )

            # If something is being deleted from the state, we need to clear it out
            for state_key in to_delete:
                if state_key == (EventTypes.Create, ""):
                    joined_room_updates["room_type"] = None
                elif state_key == (EventTypes.RoomEncryption, ""):
                    joined_room_updates["is_encrypted"] = False
                elif state_key == (EventTypes.Name, ""):
                    joined_room_updates["room_name"] = None

        return SlidingSyncTableChanges(
            room_id=room_id,
            # For `sliding_sync_joined_rooms`
            joined_room_bump_stamp_to_fully_insert=bump_stamp_to_fully_insert,
            joined_room_updates=joined_room_updates,
            # For `sliding_sync_membership_snapshots`
            membership_snapshot_shared_insert_values=membership_snapshot_shared_insert_values,
            to_insert_membership_snapshots=membership_infos_to_insert_membership_snapshots,
            to_delete_membership_snapshots=user_ids_to_delete_membership_snapshots,
        )

    async def calculate_chain_cover_index_for_events(
        self, room_id: str, events: Collection[EventBase]
    ) -> dict[str, NewEventChainLinks]:
        # Filter to state events, and ensure there are no duplicates.
        state_events = []
        seen_events = set()
        for event in events:
            if not event.is_state() or event.event_id in seen_events:
                continue

            state_events.append(event)
            seen_events.add(event.event_id)

        if not state_events:
            return {}

        return await self.db_pool.runInteraction(
            "_calculate_chain_cover_index_for_events",
            self.calculate_chain_cover_index_for_events_txn,
            room_id,
            state_events,
        )

    def calculate_chain_cover_index_for_events_txn(
        self, txn: LoggingTransaction, room_id: str, state_events: Collection[EventBase]
    ) -> dict[str, NewEventChainLinks]:
        # We now calculate chain ID/sequence numbers for any state events we're
        # persisting. We ignore out of band memberships as we're not in the room
        # and won't have their auth chain (we'll fix it up later if we join the
        # room).
        #
        # See: docs/auth_chain_difference_algorithm.md

        # We ignore legacy rooms that we aren't filling the chain cover index
        # for.
        row = self.db_pool.simple_select_one_txn(
            txn,
            table="rooms",
            keyvalues={"room_id": room_id},
            retcols=("room_id", "has_auth_chain_index"),
            allow_none=True,
        )
        if row is None or row[1] is False:
            return {}

        # Filter out events that we've already calculated.
        rows = self.db_pool.simple_select_many_txn(
            txn,
            table="event_auth_chains",
            column="event_id",
            iterable=[e.event_id for e in state_events],
            keyvalues={},
            retcols=("event_id",),
        )
        already_persisted_events = {event_id for (event_id,) in rows}
        state_events = [
            event
            for event in state_events
            if event.event_id not in already_persisted_events
        ]

        if not state_events:
            return {}

        # We need to know the type/state_key and auth events of the events we're
        # calculating chain IDs for. We don't rely on having the full Event
        # instances as we'll potentially be pulling more events from the DB and
        # we don't need the overhead of fetching/parsing the full event JSON.
        event_to_types = {e.event_id: (e.type, e.state_key) for e in state_events}
        event_to_auth_chain = {e.event_id: e.auth_event_ids() for e in state_events}
        event_to_room_id = {e.event_id: e.room_id for e in state_events}

        return self._calculate_chain_cover_index(
            txn,
            self.db_pool,
            self.store.event_chain_id_gen,
            event_to_room_id,
            event_to_types,
            event_to_auth_chain,
        )

    async def _get_events_which_are_prevs(self, event_ids: Iterable[str]) -> list[str]:
        """Filter the supplied list of event_ids to get those which are prev_events of
        existing (non-outlier/rejected) events.

        Args:
            event_ids: event ids to filter

        Returns:
            Filtered event ids
        """
        results: list[str] = []

        def _get_events_which_are_prevs_txn(
            txn: LoggingTransaction, batch: Collection[str]
        ) -> None:
            sql = """
            SELECT prev_event_id, internal_metadata
            FROM event_edges
                INNER JOIN events USING (event_id)
                LEFT JOIN rejections USING (event_id)
                LEFT JOIN event_json USING (event_id)
            WHERE
                NOT events.outlier
                AND rejections.event_id IS NULL
                AND
            """

            clause, args = make_in_list_sql_clause(
                self.database_engine, "prev_event_id", batch
            )

            txn.execute(sql + clause, args)
            results.extend(r[0] for r in txn if not db_to_json(r[1]).get("soft_failed"))

        for chunk in batch_iter(event_ids, 100):
            await self.db_pool.runInteraction(
                "_get_events_which_are_prevs", _get_events_which_are_prevs_txn, chunk
            )

        return results

    async def _get_prevs_before_rejected(self, event_ids: Iterable[str]) -> set[str]:
        """Get soft-failed ancestors to remove from the extremities.

        Given a set of events, find all those that have been soft-failed or
        rejected. Returns those soft failed/rejected events and their prev
        events (whether soft-failed/rejected or not), and recurses up the
        prev-event graph until it finds no more soft-failed/rejected events.

        This is used to find extremities that are ancestors of new events, but
        are separated by soft failed events.

        Args:
            event_ids: Events to find prev events for. Note that these must have
                already been persisted.

        Returns:
            The previous events.
        """

        # The set of event_ids to return. This includes all soft-failed events
        # and their prev events.
        existing_prevs: set[str] = set()

        def _get_prevs_before_rejected_txn(
            txn: LoggingTransaction, batch: Collection[str]
        ) -> None:
            to_recursively_check = batch

            while to_recursively_check:
                sql = """
                SELECT
                    event_id, prev_event_id, internal_metadata,
                    rejections.event_id IS NOT NULL
                FROM event_edges
                    INNER JOIN events USING (event_id)
                    LEFT JOIN rejections USING (event_id)
                    LEFT JOIN event_json USING (event_id)
                WHERE
                    NOT events.outlier
                    AND
                """

                clause, args = make_in_list_sql_clause(
                    self.database_engine, "event_id", to_recursively_check
                )

                txn.execute(sql + clause, args)
                to_recursively_check = []

                for _, prev_event_id, metadata, rejected in txn:
                    if prev_event_id in existing_prevs:
                        continue

                    soft_failed = db_to_json(metadata).get("soft_failed")
                    if soft_failed or rejected:
                        to_recursively_check.append(prev_event_id)
                        existing_prevs.add(prev_event_id)

        for chunk in batch_iter(event_ids, 100):
            await self.db_pool.runInteraction(
                "_get_prevs_before_rejected", _get_prevs_before_rejected_txn, chunk
            )

        return existing_prevs

    def _persist_events_txn(
        self,
        txn: LoggingTransaction,
        *,
        room_id: str,
        events_and_contexts: list[EventPersistencePair],
        inhibit_local_membership_updates: bool,
        state_delta_for_room: DeltaState | None,
        new_forward_extremities: set[str] | None,
        new_event_links: dict[str, NewEventChainLinks],
        sliding_sync_table_changes: SlidingSyncTableChanges | None,
    ) -> None:
        """Insert some number of room events into the necessary database tables.

        Rejected events are only inserted into the events table, the events_json table,
        and the rejections table. Things reading from those table will need to check
        whether the event was rejected.

        Assumes that we are only persisting events for one room at a time.

        Args:
            txn
            room_id: The room the events are from
            events_and_contexts: events to persist
            inhibit_local_membership_updates: Stop the local_current_membership
                from being updated by these events. This should be set to True
                for backfilled events because backfilled events in the past do
                not affect the current local state.
            delete_existing True to purge existing table rows for the events
                from the database. This is useful when retrying due to
                IntegrityError.
            state_delta_for_room: Deltas that are going to be used to update the
                `current_state_events` table. Changes to the current state of the room.
            new_forward_extremities: The new forward extremities for the room:
                a set of the event ids which are the forward extremities.
            sliding_sync_table_changes: Changes to the
                `sliding_sync_membership_snapshots` and `sliding_sync_joined_rooms` tables
                derived from the given `delta_state` (see
                `_calculate_sliding_sync_table_changes(...)`)

        Raises:
            PartialStateConflictError: if attempting to persist a partial state event in
                a room that has been un-partial stated.
        """
        all_events_and_contexts = events_and_contexts

        min_stream_order = events_and_contexts[0][0].internal_metadata.stream_ordering
        max_stream_order = events_and_contexts[-1][0].internal_metadata.stream_ordering

        # We check that the room still exists for events we're trying to
        # persist. This is to protect against races with deleting a room.
        #
        # Annoyingly SQLite doesn't support row level locking.
        if isinstance(self.database_engine, PostgresEngine):
            txn.execute(
                "SELECT room_version FROM rooms WHERE room_id = ? FOR SHARE",
                (room_id,),
            )
            row = txn.fetchone()
            if row is None:
                raise Exception(f"Room does not exist {room_id}")

        # stream orderings should have been assigned by now
        assert min_stream_order
        assert max_stream_order

        # Once the txn completes, invalidate all of the relevant caches. Note that we do this
        # up here because it captures all the events_and_contexts before any are removed.
        for event, _ in events_and_contexts:
            self.store.invalidate_get_event_cache_after_txn(txn, event.event_id)
            if event.redacts:
                self.store.invalidate_get_event_cache_after_txn(txn, event.redacts)

            relates_to = None
            relation = relation_from_event(event)
            if relation:
                relates_to = relation.parent_id

            assert event.internal_metadata.stream_ordering is not None
            txn.call_after(
                self.store._invalidate_caches_for_event,
                event.internal_metadata.stream_ordering,
                event.event_id,
                event.room_id,
                event.type,
                getattr(event, "state_key", None),
                event.redacts,
                relates_to,
                backfilled=False,
            )

        # Ensure that we don't have the same event twice.
        events_and_contexts = self._filter_events_and_contexts_for_duplicates(
            events_and_contexts
        )

        self._update_room_depths_txn(
            txn, room_id, events_and_contexts=events_and_contexts
        )

        # _update_outliers_txn filters out any events which have already been
        # persisted, and returns the filtered list.
        events_and_contexts = self._update_outliers_txn(
            txn, events_and_contexts=events_and_contexts
        )

        # From this point onwards the events are only events that we haven't
        # seen before.

        self._store_event_txn(txn, events_and_contexts=events_and_contexts)

        if new_forward_extremities:
            self._update_forward_extremities_txn(
                txn,
                room_id,
                new_forward_extremities=new_forward_extremities,
                max_stream_order=max_stream_order,
            )

        self._persist_transaction_ids_txn(txn, events_and_contexts)

        # Insert into event_to_state_groups.
        self._store_event_state_mappings_txn(txn, events_and_contexts)

        self._persist_event_auth_chain_txn(
            txn, [e for e, _ in events_and_contexts], new_event_links
        )

        # _store_rejected_events_txn filters out any events which were
        # rejected, and returns the filtered list.
        events_and_contexts = self._store_rejected_events_txn(
            txn, events_and_contexts=events_and_contexts
        )

        # From this point onwards the events are only ones that weren't
        # rejected.

        self._update_metadata_tables_txn(
            txn,
            events_and_contexts=events_and_contexts,
            all_events_and_contexts=all_events_and_contexts,
            inhibit_local_membership_updates=inhibit_local_membership_updates,
        )

        # We call this last as it assumes we've inserted the events into
        # room_memberships, where applicable.
        # NB: This function invalidates all state related caches
        if state_delta_for_room:
            # If the state delta exists, the sliding sync table changes should also exist
            assert sliding_sync_table_changes is not None

            self._update_current_state_txn(
                txn,
                room_id,
                state_delta_for_room,
                min_stream_order,
                sliding_sync_table_changes,
            )

        # We only update the sliding sync tables for non-backfilled events.
        self._update_sliding_sync_tables_with_new_persisted_events_txn(
            txn, room_id, events_and_contexts
        )

    def _persist_event_auth_chain_txn(
        self,
        txn: LoggingTransaction,
        events: list[EventBase],
        new_event_links: dict[str, NewEventChainLinks],
    ) -> None:
        if new_event_links:
            self._persist_chain_cover_index(txn, self.db_pool, new_event_links)

        # We only care about state events, so this if there are no state events.
        if not any(e.is_state() for e in events):
            return

        # We want to store event_auth mappings for rejected events, as they're
        # used in state res v2.
        # This is only necessary if the rejected event appears in an accepted
        # event's auth chain, but its easier for now just to store them (and
        # it doesn't take much storage compared to storing the entire event
        # anyway).
        self.db_pool.simple_insert_many_txn(
            txn,
            table="event_auth",
            keys=("event_id", "room_id", "auth_id"),
            values=[
                (event.event_id, event.room_id, auth_id)
                for event in events
                for auth_id in event.auth_event_ids()
                if event.is_state()
            ],
        )

    @classmethod
    def _add_chain_cover_index(
        cls,
        txn: LoggingTransaction,
        db_pool: DatabasePool,
        event_chain_id_gen: SequenceGenerator,
        event_to_room_id: dict[str, str],
        event_to_types: dict[str, tuple[str, str]],
        event_to_auth_chain: dict[str, StrCollection],
    ) -> None:
        """Calculate and persist the chain cover index for the given events.

        Args:
            event_to_room_id: Event ID to the room ID of the event
            event_to_types: Event ID to type and state_key of the event
            event_to_auth_chain: Event ID to list of auth event IDs of the
                event (events with no auth events can be excluded).
        """

        new_event_links = cls._calculate_chain_cover_index(
            txn,
            db_pool,
            event_chain_id_gen,
            event_to_room_id,
            event_to_types,
            event_to_auth_chain,
        )
        cls._persist_chain_cover_index(txn, db_pool, new_event_links)

    @classmethod
    def _calculate_chain_cover_index(
        cls,
        txn: LoggingTransaction,
        db_pool: DatabasePool,
        event_chain_id_gen: SequenceGenerator,
        event_to_room_id: dict[str, str],
        event_to_types: dict[str, tuple[str, str]],
        event_to_auth_chain: dict[str, StrCollection],
    ) -> dict[str, NewEventChainLinks]:
        """Calculate the chain cover index for the given events.

        Args:
            event_to_room_id: Event ID to the room ID of the event
            event_to_types: Event ID to type and state_key of the event
            event_to_auth_chain: Event ID to list of auth event IDs of the
                event (events with no auth events can be excluded).

        Returns:
            A mapping with any new auth chain links we need to add, keyed by
            event ID.
        """

        # Map from event ID to chain ID/sequence number.
        chain_map: dict[str, tuple[int, int]] = {}

        # Set of event IDs to calculate chain ID/seq numbers for.
        events_to_calc_chain_id_for = set(event_to_room_id)

        # We check if there are any events that need to be handled in the rooms
        # we're looking at. These should just be out of band memberships, where
        # we didn't have the auth chain when we first persisted.
        auth_chain_to_calc_rows = cast(
            list[tuple[str, str, str]],
            db_pool.simple_select_many_txn(
                txn,
                table="event_auth_chain_to_calculate",
                keyvalues={},
                column="room_id",
                iterable=set(event_to_room_id.values()),
                retcols=("event_id", "type", "state_key"),
            ),
        )
        for event_id, event_type, state_key in auth_chain_to_calc_rows:
            # (We could pull out the auth events for all rows at once using
            # simple_select_many, but this case happens rarely and almost always
            # with a single row.)
            auth_events = db_pool.simple_select_onecol_txn(
                txn,
                "event_auth",
                keyvalues={"event_id": event_id},
                retcol="auth_id",
            )

            events_to_calc_chain_id_for.add(event_id)
            event_to_types[event_id] = (event_type, state_key)
            event_to_auth_chain[event_id] = auth_events

        # First we get the chain ID and sequence numbers for the events'
        # auth events (that aren't also currently being persisted).
        #
        # Note that there there is an edge case here where we might not have
        # calculated chains and sequence numbers for events that were "out
        # of band". We handle this case by fetching the necessary info and
        # adding it to the set of events to calculate chain IDs for.

        missing_auth_chains = {
            a_id
            for auth_events in event_to_auth_chain.values()
            for a_id in auth_events
            if a_id not in events_to_calc_chain_id_for
        }

        # We loop here in case we find an out of band membership and need to
        # fetch their auth event info.
        while missing_auth_chains:
            sql = """
                SELECT event_id, events.type, se.state_key, chain_id, sequence_number
                FROM events
                INNER JOIN state_events AS se USING (event_id)
                LEFT JOIN event_auth_chains USING (event_id)
                WHERE
            """
            clause, args = make_in_list_sql_clause(
                txn.database_engine,
                "event_id",
                missing_auth_chains,
            )
            txn.execute(sql + clause, args)

            missing_auth_chains.clear()

            for (
                auth_id,
                event_type,
                state_key,
                chain_id,
                sequence_number,
            ) in txn.fetchall():
                event_to_types[auth_id] = (event_type, state_key)

                if chain_id is None:
                    # No chain ID, so the event was persisted out of band.
                    # We add to list of events to calculate auth chains for.

                    events_to_calc_chain_id_for.add(auth_id)

                    event_to_auth_chain[auth_id] = db_pool.simple_select_onecol_txn(
                        txn,
                        "event_auth",
                        keyvalues={"event_id": auth_id},
                        retcol="auth_id",
                    )

                    missing_auth_chains.update(
                        e
                        for e in event_to_auth_chain[auth_id]
                        if e not in event_to_types
                    )
                else:
                    chain_map[auth_id] = (chain_id, sequence_number)

        # Now we check if we have any events where we don't have auth chain,
        # this should only be out of band memberships.
        for event_id in sorted_topologically(event_to_auth_chain, event_to_auth_chain):
            for auth_id in event_to_auth_chain[event_id]:
                if (
                    auth_id not in chain_map
                    and auth_id not in events_to_calc_chain_id_for
                ):
                    events_to_calc_chain_id_for.discard(event_id)

                    # If this is an event we're trying to persist we add it to
                    # the list of events to calculate chain IDs for next time
                    # around. (Otherwise we will have already added it to the
                    # table).
                    room_id = event_to_room_id.get(event_id)
                    if room_id:
                        e_type, state_key = event_to_types[event_id]
                        db_pool.simple_upsert_txn(
                            txn,
                            table="event_auth_chain_to_calculate",
                            keyvalues={"event_id": event_id},
                            values={
                                "room_id": room_id,
                                "type": e_type,
                                "state_key": state_key,
                            },
                        )

                    # We stop checking the event's auth events since we've
                    # discarded it.
                    break

        if not events_to_calc_chain_id_for:
            return {}

        # Allocate chain ID/sequence numbers to each new event.
        new_chain_tuples = cls._allocate_chain_ids(
            txn,
            db_pool,
            event_chain_id_gen,
            event_to_room_id,
            event_to_types,
            event_to_auth_chain,
            events_to_calc_chain_id_for,
            chain_map,
        )
        chain_map.update(new_chain_tuples)

        to_return = {
            event_id: NewEventChainLinks(chain_id, sequence_number)
            for event_id, (chain_id, sequence_number) in new_chain_tuples.items()
        }

        # Now we need to calculate any new links between chains caused by
        # the new events.
        #
        # Links are pairs of chain ID/sequence numbers such that for any
        # event A (CA, SA) and any event B (CB, SB), B is in A's auth chain
        # if and only if there is at least one link (CA, S1) -> (CB, S2)
        # where SA >= S1 and S2 >= SB.
        #
        # We try and avoid adding redundant links to the table, e.g. if we
        # have two links between two chains which both start/end at the
        # sequence number event (or cross) then one can be safely dropped.
        #
        # To calculate new links we look at every new event and:
        #   1. Fetch the chain ID/sequence numbers of its auth events,
        #      discarding any that are reachable by other auth events, or
        #      that have the same chain ID as the event.
        #   2. For each retained auth event we:
        #       a. Add a link from the event's to the auth event's chain
        #          ID/sequence number

        # Step 1, fetch all existing links from all the chains we've seen
        # referenced.
        chain_links = _LinkMap()

        for links in EventFederationStore._get_chain_links(
            txn, {chain_id for chain_id, _ in chain_map.values()}
        ):
            for origin_chain_id, inner_links in links.items():
                for (
                    origin_sequence_number,
                    target_chain_id,
                    target_sequence_number,
                ) in inner_links:
                    chain_links.add_link(
                        (origin_chain_id, origin_sequence_number),
                        (target_chain_id, target_sequence_number),
                        new=False,
                    )

        # We do this in toplogical order to avoid adding redundant links.
        for event_id in sorted_topologically(
            events_to_calc_chain_id_for, event_to_auth_chain
        ):
            chain_id, sequence_number = chain_map[event_id]

            # Filter out auth events that are reachable by other auth
            # events. We do this by looking at every permutation of pairs of
            # auth events (A, B) to check if B is reachable from A.
            reduction = {
                a_id
                for a_id in event_to_auth_chain.get(event_id, [])
                if chain_map[a_id][0] != chain_id
            }
            for start_auth_id, end_auth_id in itertools.permutations(
                event_to_auth_chain.get(event_id, []),
                r=2,
            ):
                if chain_links.exists_path_from(
                    chain_map[start_auth_id], chain_map[end_auth_id]
                ):
                    reduction.discard(end_auth_id)

            # Step 2, figure out what the new links are from the reduced
            # list of auth events.
            for auth_id in reduction:
                auth_chain_id, auth_sequence_number = chain_map[auth_id]

                # Step 2a, add link between the event and auth event
                to_return[event_id].links.append((auth_chain_id, auth_sequence_number))
                chain_links.add_link(
                    (chain_id, sequence_number), (auth_chain_id, auth_sequence_number)
                )

        return to_return

    @classmethod
    def _persist_chain_cover_index(
        cls,
        txn: LoggingTransaction,
        db_pool: DatabasePool,
        new_event_links: dict[str, NewEventChainLinks],
    ) -> None:
        db_pool.simple_insert_many_txn(
            txn,
            table="event_auth_chains",
            keys=("event_id", "chain_id", "sequence_number"),
            values=[
                (event_id, new_links.chain_id, new_links.sequence_number)
                for event_id, new_links in new_event_links.items()
            ],
        )

        db_pool.simple_delete_many_txn(
            txn,
            table="event_auth_chain_to_calculate",
            keyvalues={},
            column="event_id",
            values=new_event_links,
        )

        db_pool.simple_insert_many_txn(
            txn,
            table="event_auth_chain_links",
            keys=(
                "origin_chain_id",
                "origin_sequence_number",
                "target_chain_id",
                "target_sequence_number",
            ),
            values=[
                (
                    new_links.chain_id,
                    new_links.sequence_number,
                    target_chain_id,
                    target_sequence_number,
                )
                for new_links in new_event_links.values()
                for (target_chain_id, target_sequence_number) in new_links.links
            ],
        )

    @staticmethod
    def _allocate_chain_ids(
        txn: LoggingTransaction,
        db_pool: DatabasePool,
        event_chain_id_gen: SequenceGenerator,
        event_to_room_id: dict[str, str],
        event_to_types: dict[str, tuple[str, str]],
        event_to_auth_chain: dict[str, StrCollection],
        events_to_calc_chain_id_for: set[str],
        chain_map: dict[str, tuple[int, int]],
    ) -> dict[str, tuple[int, int]]:
        """Allocates, but does not persist, chain ID/sequence numbers for the
        events in `events_to_calc_chain_id_for`. (c.f. _add_chain_cover_index
        for info on args)
        """

        # We now calculate the chain IDs/sequence numbers for the events. We do
        # this by looking at the chain ID and sequence number of any auth event
        # with the same type/state_key and incrementing the sequence number by
        # one. If there was no match or the chain ID/sequence number is already
        # taken we generate a new chain.
        #
        # We try to reduce the number of times that we hit the database by
        # batching up calls, to make this more efficient when persisting large
        # numbers of state events (e.g. during joins).
        #
        # We do this by:
        #   1. Calculating for each event which auth event will be used to
        #      inherit the chain ID, i.e. converting the auth chain graph to a
        #      tree that we can allocate chains on. We also keep track of which
        #      existing chain IDs have been referenced.
        #   2. Fetching the max allocated sequence number for each referenced
        #      existing chain ID, generating a map from chain ID to the max
        #      allocated sequence number.
        #   3. Iterating over the tree and allocating a chain ID/seq no. to the
        #      new event, by incrementing the sequence number from the
        #      referenced event's chain ID/seq no. and checking that the
        #      incremented sequence number hasn't already been allocated (by
        #      looking in the map generated in the previous step). We generate a
        #      new chain if the sequence number has already been allocated.
        #

        existing_chains: set[int] = set()
        tree: list[tuple[str, str | None]] = []

        # We need to do this in a topologically sorted order as we want to
        # generate chain IDs/sequence numbers of an event's auth events before
        # the event itself.
        for event_id in sorted_topologically(
            events_to_calc_chain_id_for, event_to_auth_chain
        ):
            for auth_id in event_to_auth_chain.get(event_id, []):
                if event_to_types.get(event_id) == event_to_types.get(auth_id):
                    existing_chain_id = chain_map.get(auth_id)
                    if existing_chain_id:
                        existing_chains.add(existing_chain_id[0])

                    tree.append((event_id, auth_id))
                    break
            else:
                tree.append((event_id, None))

        # Fetch the current max sequence number for each existing referenced chain.
        sql = """
            SELECT chain_id, MAX(sequence_number) FROM event_auth_chains
            WHERE %s
            GROUP BY chain_id
        """
        clause, args = make_in_list_sql_clause(
            db_pool.engine, "chain_id", existing_chains
        )
        txn.execute(sql % (clause,), args)

        chain_to_max_seq_no: dict[Any, int] = {row[0]: row[1] for row in txn}

        # Allocate the new events chain ID/sequence numbers.
        #
        # To reduce the number of calls to the database we don't allocate a
        # chain ID number in the loop, instead we use a temporary `object()` for
        # each new chain ID. Once we've done the loop we generate the necessary
        # number of new chain IDs in one call, replacing all temporary
        # objects with real allocated chain IDs.

        unallocated_chain_ids: set[object] = set()
        new_chain_tuples: dict[str, tuple[Any, int]] = {}
        for event_id, auth_event_id in tree:
            # If we reference an auth_event_id we fetch the allocated chain ID,
            # either from the existing `chain_map` or the newly generated
            # `new_chain_tuples` map.
            existing_chain_id = None
            if auth_event_id:
                existing_chain_id = new_chain_tuples.get(auth_event_id)
                if not existing_chain_id:
                    existing_chain_id = chain_map[auth_event_id]

            new_chain_tuple: tuple[Any, int] | None = None
            if existing_chain_id:
                # We found a chain ID/sequence number candidate, check its
                # not already taken.
                proposed_new_id = existing_chain_id[0]
                proposed_new_seq = existing_chain_id[1] + 1

                if chain_to_max_seq_no[proposed_new_id] < proposed_new_seq:
                    new_chain_tuple = (
                        proposed_new_id,
                        proposed_new_seq,
                    )

            # If we need to start a new chain we allocate a temporary chain ID.
            if not new_chain_tuple:
                new_chain_tuple = (object(), 1)
                unallocated_chain_ids.add(new_chain_tuple[0])

            new_chain_tuples[event_id] = new_chain_tuple
            chain_to_max_seq_no[new_chain_tuple[0]] = new_chain_tuple[1]

        # Generate new chain IDs for all unallocated chain IDs.
        newly_allocated_chain_ids = event_chain_id_gen.get_next_mult_txn(
            txn, len(unallocated_chain_ids)
        )

        # Map from potentially temporary chain ID to real chain ID
        chain_id_to_allocated_map: dict[Any, int] = dict(
            zip(unallocated_chain_ids, newly_allocated_chain_ids)
        )
        chain_id_to_allocated_map.update((c, c) for c in existing_chains)

        return {
            event_id: (chain_id_to_allocated_map[chain_id], seq)
            for event_id, (chain_id, seq) in new_chain_tuples.items()
        }

    def _persist_transaction_ids_txn(
        self,
        txn: LoggingTransaction,
        events_and_contexts: list[EventPersistencePair],
    ) -> None:
        """Persist the mapping from transaction IDs to event IDs (if defined)."""

        inserted_ts = self._clock.time_msec()
        to_insert_device_id: list[tuple[str, str, str, str, str, int]] = []
        for event, _ in events_and_contexts:
            txn_id = getattr(event.internal_metadata, "txn_id", None)
            device_id = getattr(event.internal_metadata, "device_id", None)

            if txn_id is not None:
                if device_id is not None:
                    to_insert_device_id.append(
                        (
                            event.event_id,
                            event.room_id,
                            event.sender,
                            device_id,
                            txn_id,
                            inserted_ts,
                        )
                    )

        # Synapse relies on the device_id to scope transactions for events..
        if to_insert_device_id:
            self.db_pool.simple_insert_many_txn(
                txn,
                table="event_txn_id_device_id",
                keys=(
                    "event_id",
                    "room_id",
                    "user_id",
                    "device_id",
                    "txn_id",
                    "inserted_ts",
                ),
                values=to_insert_device_id,
            )

    async def update_current_state(
        self,
        room_id: str,
        state_delta: DeltaState,
        sliding_sync_table_changes: SlidingSyncTableChanges,
    ) -> None:
        """
        Update the current state stored in the datatabase for the given room

        Args:
            room_id
            state_delta: Deltas that are going to be used to update the
                `current_state_events` table. Changes to the current state of the room.
            sliding_sync_table_changes: Changes to the
                `sliding_sync_membership_snapshots` and `sliding_sync_joined_rooms` tables
                derived from the given `delta_state` (see
                `_calculate_sliding_sync_table_changes(...)`)
        """

        if state_delta.is_noop():
            return

        async with self._stream_id_gen.get_next() as stream_ordering:
            await self.db_pool.runInteraction(
                "update_current_state",
                self._update_current_state_txn,
                room_id,
                delta_state=state_delta,
                stream_id=stream_ordering,
                sliding_sync_table_changes=sliding_sync_table_changes,
            )

    def _update_current_state_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        delta_state: DeltaState,
        stream_id: int,
        sliding_sync_table_changes: SlidingSyncTableChanges,
    ) -> None:
        """
        Handles updating tables that track the current state of a room.

        Args:
            txn
            room_id
            delta_state: Deltas that are going to be used to update the
                `current_state_events` table. Changes to the current state of the room.
            stream_id: This is expected to be the minimum `stream_ordering` for the
                batch of events that we are persisting; which means we do not end up in a
                situation where workers see events before the `current_state_delta` updates.
                FIXME: However, this function also gets called with next upcoming
                `stream_ordering` when we re-sync the state of a partial stated room (see
                `update_current_state(...)`) which may be "correct" but it would be good to
                nail down what exactly is the expected value here.
            sliding_sync_table_changes: Changes to the
                `sliding_sync_membership_snapshots` and `sliding_sync_joined_rooms` tables
                derived from the given `delta_state` (see
                `_calculate_sliding_sync_table_changes(...)`)
        """
        to_delete = delta_state.to_delete
        to_insert = delta_state.to_insert

        # Sanity check we're processing the same thing
        assert room_id == sliding_sync_table_changes.room_id

        # Figure out the changes of membership to invalidate the
        # `get_rooms_for_user` cache.
        # We find out which membership events we may have deleted
        # and which we have added, then we invalidate the caches for all
        # those users.
        members_to_cache_bust = {
            state_key
            for ev_type, state_key in itertools.chain(to_delete, to_insert)
            if ev_type == EventTypes.Member
        }

        if delta_state.no_longer_in_room:
            # Server is no longer in the room so we delete the room from
            # current_state_events, being careful we've already updated the
            # rooms.room_version column (which gets populated in a
            # background task).
            self._upsert_room_version_txn(txn, room_id)

            # Before deleting we populate the current_state_delta_stream
            # so that async background tasks get told what happened.
            sql = """
                    INSERT INTO current_state_delta_stream
                        (stream_id, instance_name, room_id, type, state_key, event_id, prev_event_id)
                    SELECT ?, ?, room_id, type, state_key, null, event_id
                        FROM current_state_events
                        WHERE room_id = ?
                """
            txn.execute(sql, (stream_id, self._instance_name, room_id))

            # Grab the list of users before we clear out the current state
            users_in_room = self.store.get_users_in_room_txn(txn, room_id)
            # We also want to invalidate the membership caches for users
            # that were in the room.
            members_to_cache_bust.update(users_in_room)

            self.db_pool.simple_delete_txn(
                txn,
                table="current_state_events",
                keyvalues={"room_id": room_id},
            )
            self.db_pool.simple_delete_txn(
                txn,
                table="sliding_sync_joined_rooms",
                keyvalues={"room_id": room_id},
            )
        else:
            # We're still in the room, so we update the current state as normal.

            # First we add entries to the current_state_delta_stream. We
            # do this before updating the current_state_events table so
            # that we can use it to calculate the `prev_event_id`. (This
            # allows us to not have to pull out the existing state
            # unnecessarily).
            #
            # The stream_id for the update is chosen to be the minimum of the stream_ids
            # for the batch of the events that we are persisting; that means we do not
            # end up in a situation where workers see events before the
            # current_state_delta updates.
            #
            sql = """
                    INSERT INTO current_state_delta_stream
                    (stream_id, instance_name, room_id, type, state_key, event_id, prev_event_id)
                    SELECT ?, ?, ?, ?, ?, ?, (
                        SELECT event_id FROM current_state_events
                        WHERE room_id = ? AND type = ? AND state_key = ?
                    )
                """
            txn.execute_batch(
                sql,
                [
                    (
                        stream_id,
                        self._instance_name,
                        room_id,
                        etype,
                        state_key,
                        to_insert.get((etype, state_key)),
                        room_id,
                        etype,
                        state_key,
                    )
                    for etype, state_key in itertools.chain(to_delete, to_insert)
                ],
            )
            # Now we actually update the current_state_events table

            txn.execute_batch(
                "DELETE FROM current_state_events"
                " WHERE room_id = ? AND type = ? AND state_key = ?",
                [
                    (room_id, etype, state_key)
                    for etype, state_key in itertools.chain(to_delete, to_insert)
                ],
            )

            # We include the membership in the current state table, hence we do
            # a lookup when we insert. This assumes that all events have already
            # been inserted into room_memberships.
            txn.execute_batch(
                """INSERT INTO current_state_events
                        (room_id, type, state_key, event_id, membership, event_stream_ordering)
                    VALUES (
                        ?, ?, ?, ?,
                        (SELECT membership FROM room_memberships WHERE event_id = ?),
                        (SELECT stream_ordering FROM events WHERE event_id = ?)
                    )
                    """,
                [
                    (room_id, key[0], key[1], ev_id, ev_id, ev_id)
                    for key, ev_id in to_insert.items()
                ],
            )

            # Handle updating the `sliding_sync_joined_rooms` table. We only deal with
            # updating the state related columns. The
            # `event_stream_ordering`/`bump_stamp` are updated elsewhere in the event
            # persisting stack (see
            # `_update_sliding_sync_tables_with_new_persisted_events_txn()`)
            #
            # We only need to update when one of the relevant state values has changed
            if sliding_sync_table_changes.joined_room_updates:
                sliding_sync_updates_keys = (
                    sliding_sync_table_changes.joined_room_updates.keys()
                )
                sliding_sync_updates_values = (
                    sliding_sync_table_changes.joined_room_updates.values()
                )

                args: list[Any] = [
                    room_id,
                    room_id,
                    sliding_sync_table_changes.joined_room_bump_stamp_to_fully_insert,
                ]
                args.extend(iter(sliding_sync_updates_values))

                # XXX: We use a sub-query for `stream_ordering` because it's unreliable to
                # pre-calculate from `events_and_contexts` at the time when
                # `_calculate_sliding_sync_table_changes()` is ran. We could be working
                # with events that were previously persisted as an `outlier` with one
                # `stream_ordering` but are now being persisted again and de-outliered
                # and assigned a different `stream_ordering`. Since we call
                # `_calculate_sliding_sync_table_changes()` before
                # `_update_outliers_txn()` which fixes this discrepancy (always use the
                # `stream_ordering` from the first time it was persisted), we're working
                # with an unreliable `stream_ordering` value that will possibly be
                # unused and not make it into the `events` table.
                #
                # We don't update `event_stream_ordering` `ON CONFLICT` because it's
                # simpler and we can just rely on
                # `_update_sliding_sync_tables_with_new_persisted_events_txn()` to do
                # the right thing (same for `bump_stamp`). The only reason we're
                # inserting `event_stream_ordering` here is because the column has a
                # `NON NULL` constraint and we need some answer.
                txn.execute(
                    f"""
                    INSERT INTO sliding_sync_joined_rooms
                        (room_id, event_stream_ordering, bump_stamp, {", ".join(sliding_sync_updates_keys)})
                    VALUES (
                        ?,
                        (SELECT stream_ordering FROM events WHERE room_id = ? ORDER BY stream_ordering DESC LIMIT 1),
                        ?,
                        {", ".join("?" for _ in sliding_sync_updates_values)}
                    )
                    ON CONFLICT (room_id)
                    DO UPDATE SET
                        {", ".join(f"{key} = EXCLUDED.{key}" for key in sliding_sync_updates_keys)}
                    """,
                    args,
                )

        # We now update `local_current_membership`. We do this regardless
        # of whether we're still in the room or not to handle the case where
        # e.g. we just got banned (where we need to record that fact here).

        # Note: Do we really want to delete rows here (that we do not
        # subsequently reinsert below)? While technically correct it means
        # we have no record of the fact the user *was* a member of the
        # room but got, say, state reset out of it.
        if to_delete or to_insert:
            txn.execute_batch(
                "DELETE FROM local_current_membership"
                " WHERE room_id = ? AND user_id = ?",
                [
                    (room_id, state_key)
                    for etype, state_key in itertools.chain(to_delete, to_insert)
                    if etype == EventTypes.Member and self.is_mine_id(state_key)
                ],
            )

        if to_insert:
            txn.execute_batch(
                """INSERT INTO local_current_membership
                        (room_id, user_id, event_id, membership, event_stream_ordering)
                    VALUES (
                        ?, ?, ?,
                        (SELECT membership FROM room_memberships WHERE event_id = ?),
                        (SELECT stream_ordering FROM events WHERE event_id = ?)
                    )
                    """,
                [
                    (room_id, key[1], ev_id, ev_id, ev_id)
                    for key, ev_id in to_insert.items()
                    if key[0] == EventTypes.Member and self.is_mine_id(key[1])
                ],
            )

        # Handle updating the `sliding_sync_membership_snapshots` table
        #
        # This would only happen if someone was state reset out of the room
        if sliding_sync_table_changes.to_delete_membership_snapshots:
            self.db_pool.simple_delete_many_txn(
                txn,
                table="sliding_sync_membership_snapshots",
                column="user_id",
                values=sliding_sync_table_changes.to_delete_membership_snapshots,
                keyvalues={"room_id": room_id},
            )

        # We do this regardless of whether the server is `no_longer_in_room` or not
        # because we still want a row if a local user was just left/kicked or got banned
        # from the room.
        if sliding_sync_table_changes.to_insert_membership_snapshots:
            # Update the `sliding_sync_membership_snapshots` table
            #
            sliding_sync_snapshot_keys = sliding_sync_table_changes.membership_snapshot_shared_insert_values.keys()
            sliding_sync_snapshot_values = sliding_sync_table_changes.membership_snapshot_shared_insert_values.values()
            # We need to insert/update regardless of whether we have
            # `sliding_sync_snapshot_keys` because there are other fields in the `ON
            # CONFLICT` upsert to run (see inherit case (explained in
            # `_calculate_sliding_sync_table_changes()`) for more context when this
            # happens).
            #
            # XXX: We use a sub-query for `stream_ordering` because it's unreliable to
            # pre-calculate from `events_and_contexts` at the time when
            # `_calculate_sliding_sync_table_changes()` is ran. We could be working with
            # events that were previously persisted as an `outlier` with one
            # `stream_ordering` but are now being persisted again and de-outliered and
            # assigned a different `stream_ordering` that won't end up being used. Since
            # we call `_calculate_sliding_sync_table_changes()` before
            # `_update_outliers_txn()` which fixes this discrepancy (always use the
            # `stream_ordering` from the first time it was persisted), we're working
            # with an unreliable `stream_ordering` value that will possibly be unused
            # and not make it into the `events` table.
            txn.execute_batch(
                f"""
                INSERT INTO sliding_sync_membership_snapshots
                    (room_id, user_id, sender, membership_event_id, membership, forgotten, event_stream_ordering, event_instance_name
                    {("," + ", ".join(sliding_sync_snapshot_keys)) if sliding_sync_snapshot_keys else ""})
                VALUES (
                    ?, ?, ?, ?, ?, ?,
                    (SELECT stream_ordering FROM events WHERE event_id = ?),
                    (SELECT COALESCE(instance_name, 'master') FROM events WHERE event_id = ?)
                    {("," + ", ".join("?" for _ in sliding_sync_snapshot_values)) if sliding_sync_snapshot_values else ""}
                )
                ON CONFLICT (room_id, user_id)
                DO UPDATE SET
                    sender = EXCLUDED.sender,
                    membership_event_id = EXCLUDED.membership_event_id,
                    membership = EXCLUDED.membership,
                    forgotten = EXCLUDED.forgotten,
                    event_stream_ordering = EXCLUDED.event_stream_ordering
                    {("," + ", ".join(f"{key} = EXCLUDED.{key}" for key in sliding_sync_snapshot_keys)) if sliding_sync_snapshot_keys else ""}
                """,
                [
                    [
                        room_id,
                        membership_info.user_id,
                        membership_info.sender,
                        membership_info.membership_event_id,
                        membership_info.membership,
                        # Since this is a new membership, it isn't forgotten anymore (which
                        # matches how Synapse currently thinks about the forgotten status)
                        0,
                        # XXX: We do not use `membership_info.membership_event_stream_ordering` here
                        # because it is an unreliable value. See XXX note above.
                        membership_info.membership_event_id,
                        # XXX: We do not use `membership_info.membership_event_instance_name` here
                        # because it is an unreliable value. See XXX note above.
                        membership_info.membership_event_id,
                    ]
                    + list(sliding_sync_snapshot_values)
                    for membership_info in sliding_sync_table_changes.to_insert_membership_snapshots
                ],
            )

        txn.call_after(
            self.store._curr_state_delta_stream_cache.entity_has_changed,
            room_id,
            stream_id,
        )

        for user_id in members_to_cache_bust:
            txn.call_after(
                self.store._membership_stream_cache.entity_has_changed,
                user_id,
                stream_id,
            )

        # Invalidate the various caches
        self.store._invalidate_state_caches_and_stream(
            txn, room_id, members_to_cache_bust
        )

        # Check if any of the remote membership changes requires us to
        # unsubscribe from their device lists.
        self.store.handle_potentially_left_users_txn(
            txn, {m for m in members_to_cache_bust if not self.hs.is_mine_id(m)}
        )

    @classmethod
    def _get_relevant_sliding_sync_current_state_event_ids_txn(
        cls, txn: LoggingTransaction, room_id: str
    ) -> MutableStateMap[str]:
        """
        Fetch the current state event IDs for the relevant (to the
        `sliding_sync_joined_rooms` table) state types for the given room.

        Returns:
            A tuple of:
                1. StateMap of event IDs necessary to to fetch the relevant state values
                   needed to insert into the
                   `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots`.
                2. The corresponding latest `stream_id` in the
                   `current_state_delta_stream` table. This is useful to compare against
                   the `current_state_delta_stream` table later so you can check whether
                   the current state has changed since you last fetched the current
                   state.
        """
        # Fetch the current state event IDs from the database
        (
            event_type_and_state_key_in_list_clause,
            event_type_and_state_key_args,
        ) = make_tuple_in_list_sql_clause(
            txn.database_engine,
            ("type", "state_key"),
            SLIDING_SYNC_RELEVANT_STATE_SET,
        )
        txn.execute(
            f"""
            SELECT c.event_id, c.type, c.state_key
            FROM current_state_events AS c
            WHERE
                c.room_id = ?
                AND {event_type_and_state_key_in_list_clause}
            """,
            [room_id] + event_type_and_state_key_args,
        )
        current_state_map: MutableStateMap[str] = {
            (event_type, state_key): event_id for event_id, event_type, state_key in txn
        }

        return current_state_map

    @classmethod
    def _get_sliding_sync_insert_values_from_state_map(
        cls, state_map: StateMap[EventBase]
    ) -> SlidingSyncStateInsertValues:
        """
        Extract the relevant state values from the `state_map` needed to insert into the
        `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` tables.

        Returns:
            Map from column names (`room_type`, `is_encrypted`, `room_name`) to relevant
            state values needed to insert into
            the `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` tables.
        """
        # Map of values to insert/update in the `sliding_sync_membership_snapshots` table
        sliding_sync_insert_map: SlidingSyncStateInsertValues = {}

        # Parse the raw event JSON
        for state_key, event in state_map.items():
            if state_key == (EventTypes.Create, ""):
                room_type = event.content.get(EventContentFields.ROOM_TYPE)
                # Scrutinize JSON values
                if room_type is None or (
                    isinstance(room_type, str)
                    # We ignore values with null bytes as Postgres doesn't allow them in
                    # text columns.
                    and "\0" not in room_type
                ):
                    sliding_sync_insert_map["room_type"] = room_type
            elif state_key == (EventTypes.RoomEncryption, ""):
                encryption_algorithm = event.content.get(
                    EventContentFields.ENCRYPTION_ALGORITHM
                )
                is_encrypted = encryption_algorithm is not None
                sliding_sync_insert_map["is_encrypted"] = is_encrypted
            elif state_key == (EventTypes.Name, ""):
                room_name = event.content.get(EventContentFields.ROOM_NAME)
                # Scrutinize JSON values. We ignore values with nulls as
                # postgres doesn't allow null bytes in text columns.
                if room_name is None or (
                    isinstance(room_name, str)
                    # We ignore values with null bytes as Postgres doesn't allow them in
                    # text columns.
                    and "\0" not in room_name
                ):
                    sliding_sync_insert_map["room_name"] = room_name
            elif state_key == (EventTypes.Tombstone, ""):
                successor_room_id = event.content.get(
                    EventContentFields.TOMBSTONE_SUCCESSOR_ROOM
                )
                # Scrutinize JSON values
                if successor_room_id is None or (
                    isinstance(successor_room_id, str)
                    # We ignore values with null bytes as Postgres doesn't allow them in
                    # text columns.
                    and "\0" not in successor_room_id
                ):
                    sliding_sync_insert_map["tombstone_successor_room_id"] = (
                        successor_room_id
                    )
            else:
                # We only expect to see events according to the
                # `SLIDING_SYNC_RELEVANT_STATE_SET`.
                raise AssertionError(
                    "Unexpected event (we should not be fetching extra events or this "
                    + "piece of code needs to be updated to handle a new event type added "
                    + "to `SLIDING_SYNC_RELEVANT_STATE_SET`): {state_key} {event.event_id}"
                )

        return sliding_sync_insert_map

    @classmethod
    def _get_sliding_sync_insert_values_from_stripped_state(
        cls, unsigned_stripped_state_events: Any
    ) -> SlidingSyncMembershipSnapshotSharedInsertValues:
        """
        Pull out the relevant state values from the stripped state on an invite or knock
        membership event needed to insert into the `sliding_sync_membership_snapshots`
        tables.

        Returns:
            Map from column names (`room_type`, `is_encrypted`, `room_name`) to relevant
            state values needed to insert into the `sliding_sync_membership_snapshots` tables.
        """
        # Map of values to insert/update in the `sliding_sync_membership_snapshots` table
        sliding_sync_insert_map: SlidingSyncMembershipSnapshotSharedInsertValues = {}

        if unsigned_stripped_state_events is not None:
            stripped_state_map: MutableStateMap[StrippedStateEvent] = {}
            if isinstance(unsigned_stripped_state_events, list):
                for raw_stripped_event in unsigned_stripped_state_events:
                    stripped_state_event = parse_stripped_state_event(
                        raw_stripped_event
                    )
                    if stripped_state_event is not None:
                        stripped_state_map[
                            (
                                stripped_state_event.type,
                                stripped_state_event.state_key,
                            )
                        ] = stripped_state_event

            # If there is some stripped state, we assume the remote server passed *all*
            # of the potential stripped state events for the room.
            create_stripped_event = stripped_state_map.get((EventTypes.Create, ""))
            # Sanity check that we at-least have the create event
            if create_stripped_event is not None:
                sliding_sync_insert_map["has_known_state"] = True

                # XXX: Keep this up-to-date with `SLIDING_SYNC_RELEVANT_STATE_SET`

                # Find the room_type
                sliding_sync_insert_map["room_type"] = (
                    create_stripped_event.content.get(EventContentFields.ROOM_TYPE)
                    if create_stripped_event is not None
                    else None
                )

                # Find whether the room is_encrypted
                encryption_stripped_event = stripped_state_map.get(
                    (EventTypes.RoomEncryption, "")
                )
                encryption = (
                    encryption_stripped_event.content.get(
                        EventContentFields.ENCRYPTION_ALGORITHM
                    )
                    if encryption_stripped_event is not None
                    else None
                )
                sliding_sync_insert_map["is_encrypted"] = encryption is not None

                # Find the room_name
                room_name_stripped_event = stripped_state_map.get((EventTypes.Name, ""))
                sliding_sync_insert_map["room_name"] = (
                    room_name_stripped_event.content.get(EventContentFields.ROOM_NAME)
                    if room_name_stripped_event is not None
                    else None
                )

                # Check for null bytes in the room name and type. We have to
                # ignore values with null bytes as Postgres doesn't allow them
                # in text columns.
                if (
                    sliding_sync_insert_map["room_name"] is not None
                    and "\0" in sliding_sync_insert_map["room_name"]
                ):
                    sliding_sync_insert_map.pop("room_name")

                if (
                    sliding_sync_insert_map["room_type"] is not None
                    and "\0" in sliding_sync_insert_map["room_type"]
                ):
                    sliding_sync_insert_map.pop("room_type")

                # Find the tombstone_successor_room_id
                # Note: This isn't one of the stripped state events according to the spec
                # but seems like there is no reason not to support this kind of thing.
                tombstone_stripped_event = stripped_state_map.get(
                    (EventTypes.Tombstone, "")
                )
                sliding_sync_insert_map["tombstone_successor_room_id"] = (
                    tombstone_stripped_event.content.get(
                        EventContentFields.TOMBSTONE_SUCCESSOR_ROOM
                    )
                    if tombstone_stripped_event is not None
                    else None
                )

                if (
                    sliding_sync_insert_map["tombstone_successor_room_id"] is not None
                    and "\0" in sliding_sync_insert_map["tombstone_successor_room_id"]
                ):
                    sliding_sync_insert_map.pop("tombstone_successor_room_id")

            else:
                # No stripped state provided
                sliding_sync_insert_map["has_known_state"] = False
                sliding_sync_insert_map["room_type"] = None
                sliding_sync_insert_map["room_name"] = None
                sliding_sync_insert_map["is_encrypted"] = False
        else:
            # No stripped state provided
            sliding_sync_insert_map["has_known_state"] = False
            sliding_sync_insert_map["room_type"] = None
            sliding_sync_insert_map["room_name"] = None
            sliding_sync_insert_map["is_encrypted"] = False

        return sliding_sync_insert_map

    def _update_sliding_sync_tables_with_new_persisted_events_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        events_and_contexts: list[EventPersistencePair],
    ) -> None:
        """
        Update the latest `event_stream_ordering`/`bump_stamp` columns in the
        `sliding_sync_joined_rooms` table for the room with new events.

        This function assumes that `_store_event_txn()` (to persist the event) and
        `_update_current_state_txn(...)` (so that `sliding_sync_joined_rooms` table has
        been updated with rooms that were joined) have already been run.

        Args:
            txn
            room_id: The room that all of the events belong to
            events_and_contexts: The events being persisted. We assume the list is
                sorted ascending by `stream_ordering`. We don't care about the sort when the
                events are backfilled (with negative `stream_ordering`).
        """

        # Nothing to do if there are no events
        if len(events_and_contexts) == 0:
            return

        # Since the list is sorted ascending by `stream_ordering`, the last event should
        # have the highest `stream_ordering`.
        max_stream_ordering = events_and_contexts[-1][
            0
        ].internal_metadata.stream_ordering
        # `stream_ordering` should be assigned for persisted events
        assert max_stream_ordering is not None
        # Check if the event is a backfilled event (with a negative `stream_ordering`).
        # If one event is backfilled, we assume this whole batch was backfilled.
        if max_stream_ordering < 0:
            # We only update the sliding sync tables for non-backfilled events.
            return

        max_bump_stamp = None
        for event, _ in reversed(events_and_contexts):
            # Sanity check that all events belong to the same room
            assert event.room_id == room_id

            if event.type in SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES:
                # `stream_ordering` should be assigned for persisted events
                assert event.internal_metadata.stream_ordering is not None

                max_bump_stamp = event.internal_metadata.stream_ordering

                # Since we're iterating in reverse, we can break as soon as we find a
                # matching bump event which should have the highest `stream_ordering`.
                break

        # Handle updating the `sliding_sync_joined_rooms` table.
        #
        txn.execute(
            """
            UPDATE sliding_sync_joined_rooms
            SET
                event_stream_ordering = CASE
                    WHEN event_stream_ordering IS NULL OR event_stream_ordering < ?
                        THEN ?
                    ELSE event_stream_ordering
                END,
                bump_stamp = CASE
                    WHEN bump_stamp IS NULL OR bump_stamp < ?
                        THEN ?
                    ELSE bump_stamp
                END
            WHERE room_id = ?
            """,
            (
                max_stream_ordering,
                max_stream_ordering,
                max_bump_stamp,
                max_bump_stamp,
                room_id,
            ),
        )
        # This may or may not update any rows depending if we are `no_longer_in_room`

    def _upsert_room_version_txn(self, txn: LoggingTransaction, room_id: str) -> None:
        """Update the room version in the database based off current state
        events.

        This is used when we're about to delete current state and we want to
        ensure that the `rooms.room_version` column is up to date.
        """

        sql = """
            SELECT json FROM event_json
            INNER JOIN current_state_events USING (room_id, event_id)
            WHERE room_id = ? AND type = ? AND state_key = ?
        """
        txn.execute(sql, (room_id, EventTypes.Create, ""))
        row = txn.fetchone()
        if row:
            event_json = db_to_json(row[0])
            content = event_json.get("content", {})
            creator = content.get("creator")
            room_version_id = content.get("room_version", RoomVersions.V1.identifier)

            self.db_pool.simple_upsert_txn(
                txn,
                table="rooms",
                keyvalues={"room_id": room_id},
                values={"room_version": room_version_id},
                insertion_values={"is_public": False, "creator": creator},
            )

    def _update_forward_extremities_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        new_forward_extremities: set[str],
        max_stream_order: int,
    ) -> None:
        self.db_pool.simple_delete_txn(
            txn, table="event_forward_extremities", keyvalues={"room_id": room_id}
        )

        self.db_pool.simple_insert_many_txn(
            txn,
            table="event_forward_extremities",
            keys=("event_id", "room_id"),
            values=[(ev_id, room_id) for ev_id in new_forward_extremities],
        )
        # We now insert into stream_ordering_to_exterm a mapping from room_id,
        # new stream_ordering to new forward extremeties in the room.
        # This allows us to later efficiently look up the forward extremeties
        # for a room before a given stream_ordering
        self.db_pool.simple_insert_many_txn(
            txn,
            table="stream_ordering_to_exterm",
            keys=("room_id", "event_id", "stream_ordering"),
            values=[
                (room_id, event_id, max_stream_order)
                for event_id in new_forward_extremities
            ],
        )

    @classmethod
    def _filter_events_and_contexts_for_duplicates(
        cls, events_and_contexts: list[EventPersistencePair]
    ) -> list[EventPersistencePair]:
        """Ensure that we don't have the same event twice.

        Pick the earliest non-outlier if there is one, else the earliest one.

        Args:
            events_and_contexts:

        Returns:
            filtered list
        """
        new_events_and_contexts: OrderedDict[str, EventPersistencePair] = OrderedDict()
        for event, context in events_and_contexts:
            prev_event_context = new_events_and_contexts.get(event.event_id)
            if prev_event_context:
                if not event.internal_metadata.is_outlier():
                    if prev_event_context[0].internal_metadata.is_outlier():
                        # To ensure correct ordering we pop, as OrderedDict is
                        # ordered by first insertion.
                        new_events_and_contexts.pop(event.event_id, None)
                        new_events_and_contexts[event.event_id] = (event, context)
            else:
                new_events_and_contexts[event.event_id] = (event, context)
        return list(new_events_and_contexts.values())

    def _update_room_depths_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        events_and_contexts: list[EventPersistencePair],
    ) -> None:
        """Update min_depth for each room

        Args:
            txn: db connection
            room_id: The room ID
            events_and_contexts: events we are persisting
        """
        stream_ordering: int | None = None
        depth_update = 0
        for event, context in events_and_contexts:
            # Don't update the stream ordering for backfilled events because
            # backfilled events have negative stream_ordering and happened in the
            # past, so we know that we don't need to update the stream_ordering
            # tip/front for the room.
            assert event.internal_metadata.stream_ordering is not None
            if event.internal_metadata.stream_ordering >= 0:
                if stream_ordering is None:
                    stream_ordering = event.internal_metadata.stream_ordering
                else:
                    stream_ordering = max(
                        stream_ordering, event.internal_metadata.stream_ordering
                    )

            if not event.internal_metadata.is_outlier() and not context.rejected:
                depth_update = max(event.depth, depth_update)

        # Then update the `stream_ordering` position to mark the latest event as
        # the front of the room.
        if stream_ordering is not None:
            txn.call_after(
                self.store._events_stream_cache.entity_has_changed,
                room_id,
                stream_ordering,
            )

        self._update_min_depth_for_room_txn(txn, room_id, depth_update)

    def _update_outliers_txn(
        self,
        txn: LoggingTransaction,
        events_and_contexts: list[EventPersistencePair],
    ) -> list[EventPersistencePair]:
        """Update any outliers with new event info.

        This turns outliers into ex-outliers (unless the new event was rejected), and
        also removes any other events we have already seen from the list.

        Args:
            txn: db connection
            events_and_contexts: events we are persisting

        Returns:
            new list, without events which are already in the events table.

        Raises:
            PartialStateConflictError: if attempting to persist a partial state event in
                a room that has been un-partial stated.
        """
        rows = cast(
            list[tuple[str, bool]],
            self.db_pool.simple_select_many_txn(
                txn,
                "events",
                "event_id",
                [event.event_id for event, _ in events_and_contexts],
                keyvalues={},
                retcols=("event_id", "outlier"),
            ),
        )

        have_persisted = dict(rows)

        logger.debug(
            "_update_outliers_txn: events=%s have_persisted=%s",
            [ev.event_id for ev, _ in events_and_contexts],
            have_persisted,
        )

        to_remove = set()
        for event, context in events_and_contexts:
            outlier_persisted = have_persisted.get(event.event_id)
            logger.debug(
                "_update_outliers_txn: event=%s outlier=%s outlier_persisted=%s",
                event.event_id,
                event.internal_metadata.is_outlier(),
                outlier_persisted,
            )

            # Ignore events which we haven't persisted at all
            if outlier_persisted is None:
                continue

            to_remove.add(event)

            if context.rejected:
                # If the incoming event is rejected then we don't care if the event
                # was an outlier or not - what we have is at least as good.
                continue

            if not event.internal_metadata.is_outlier() and outlier_persisted:
                # We received a copy of an event that we had already stored as
                # an outlier in the database. We now have some state at that event
                # so we need to update the state_groups table with that state.
                #
                # Note that we do not update the stream_ordering of the event in this
                # scenario. XXX: does this cause bugs? It will mean we won't send such
                # events down /sync. In general they will be historical events, so that
                # doesn't matter too much, but that is not always the case.

                logger.info(
                    "_update_outliers_txn: Updating state for ex-outlier event %s",
                    event.event_id,
                )

                # insert into event_to_state_groups.
                try:
                    self._store_event_state_mappings_txn(txn, ((event, context),))
                except Exception:
                    logger.exception("")
                    raise

                # Add an entry to the ex_outlier_stream table to replicate the
                # change in outlier status to our workers.
                stream_order = event.internal_metadata.stream_ordering
                state_group_id = context.state_group
                self.db_pool.simple_insert_txn(
                    txn,
                    table="ex_outlier_stream",
                    values={
                        "event_stream_ordering": stream_order,
                        "event_id": event.event_id,
                        "state_group": state_group_id,
                        "instance_name": self._instance_name,
                    },
                )

                sql = "UPDATE events SET outlier = FALSE WHERE event_id = ?"
                txn.execute(sql, (event.event_id,))

                # Update the event_backward_extremities table now that this
                # event isn't an outlier any more.
                self._update_backward_extremeties(txn, [event])

        return [ec for ec in events_and_contexts if ec[0] not in to_remove]

    def _store_event_txn(
        self,
        txn: LoggingTransaction,
        events_and_contexts: Collection[EventPersistencePair],
    ) -> None:
        """Insert new events into the event, event_json, redaction and
        state_events tables.
        """

        if not events_and_contexts:
            # nothing to do here
            return

        def event_dict(event: EventBase) -> JsonDict:
            d = event.get_dict()
            d.pop("redacted", None)
            d.pop("redacted_because", None)
            return d

        self.db_pool.simple_insert_many_txn(
            txn,
            table="event_json",
            keys=("event_id", "room_id", "internal_metadata", "json", "format_version"),
            values=[
                (
                    event.event_id,
                    event.room_id,
                    json_encoder.encode(event.internal_metadata.get_dict()),
                    json_encoder.encode(event_dict(event)),
                    event.format_version,
                )
                for event, _ in events_and_contexts
            ],
        )

        self.db_pool.simple_insert_many_txn(
            txn,
            table="events",
            keys=(
                "instance_name",
                "stream_ordering",
                "topological_ordering",
                "depth",
                "event_id",
                "room_id",
                "type",
                "processed",
                "outlier",
                "origin_server_ts",
                "received_ts",
                "sender",
                "contains_url",
                "state_key",
                "rejection_reason",
            ),
            values=[
                (
                    self._instance_name,
                    event.internal_metadata.stream_ordering,
                    event.depth,  # topological_ordering
                    event.depth,  # depth
                    event.event_id,
                    event.room_id,
                    event.type,
                    True,  # processed
                    event.internal_metadata.is_outlier(),
                    int(event.origin_server_ts),
                    self._clock.time_msec(),
                    event.sender,
                    "url" in event.content and isinstance(event.content["url"], str),
                    event.get_state_key(),
                    context.rejected,
                )
                for event, context in events_and_contexts
            ],
        )

        # If we're persisting an unredacted event we go and ensure
        # that we mark any redactions that reference this event as
        # requiring censoring.
        unredacted_events = [
            event.event_id
            for event, _ in events_and_contexts
            if not event.internal_metadata.is_redacted()
        ]
        sql = "UPDATE redactions SET have_censored = FALSE WHERE "
        clause, args = make_in_list_sql_clause(
            self.database_engine,
            "redacts",
            unredacted_events,
        )
        txn.execute(sql + clause, args)

        self.db_pool.simple_insert_many_txn(
            txn,
            table="state_events",
            keys=("event_id", "room_id", "type", "state_key"),
            values=[
                (event.event_id, event.room_id, event.type, event.state_key)
                for event, _ in events_and_contexts
                if event.is_state()
            ],
        )

    def _store_rejected_events_txn(
        self,
        txn: LoggingTransaction,
        events_and_contexts: list[EventPersistencePair],
    ) -> list[EventPersistencePair]:
        """Add rows to the 'rejections' table for received events which were
        rejected

        Args:
            txn: db connection
            events_and_contexts: events we are persisting

        Returns:
            new list, without the rejected events.
        """
        # Remove the rejected events from the list now that we've added them
        # to the events table and the events_json table.
        to_remove = set()
        for event, context in events_and_contexts:
            if context.rejected:
                # Insert the event_id into the rejections table
                # (events.rejection_reason has already been done)
                self._store_rejections_txn(txn, event.event_id, context.rejected)
                to_remove.add(event)

        return [ec for ec in events_and_contexts if ec[0] not in to_remove]

    def _update_metadata_tables_txn(
        self,
        txn: LoggingTransaction,
        *,
        events_and_contexts: list[EventPersistencePair],
        all_events_and_contexts: list[EventPersistencePair],
        inhibit_local_membership_updates: bool = False,
    ) -> None:
        """Update all the miscellaneous tables for new events

        Args:
            txn: db connection
            events_and_contexts: events we are persisting
            all_events_and_contexts: all events that we were going to persist.
                This includes events we've already persisted, etc, that wouldn't
                appear in events_and_context.
            inhibit_local_membership_updates: Stop the local_current_membership
                from being updated by these events. This should be set to True
                for backfilled events because backfilled events in the past do
                not affect the current local state.
        """

        # Insert all the push actions into the event_push_actions table.
        self._set_push_actions_for_event_and_users_txn(
            txn,
            events_and_contexts=events_and_contexts,
            all_events_and_contexts=all_events_and_contexts,
        )

        if not events_and_contexts:
            # nothing to do here
            return

        for event, _ in events_and_contexts:
            if event.type == EventTypes.Redaction and event.redacts is not None:
                # Remove the entries in the event_push_actions table for the
                # redacted event.
                self._remove_push_actions_for_event_id_txn(
                    txn, event.room_id, event.redacts
                )

                # Remove from relations table.
                self._handle_redact_relations(txn, event.room_id, event.redacts)

        # Update the event_forward_extremities, event_backward_extremities and
        # event_edges tables.
        self._handle_mult_prev_events(
            txn, events=[event for event, _ in events_and_contexts]
        )

        for event, _ in events_and_contexts:
            if event.type == EventTypes.Name:
                # Insert into the event_search table.
                self._store_room_name_txn(txn, event)
            elif event.type == EventTypes.Topic:
                # Insert into the event_search table.
                self._store_room_topic_txn(txn, event)
            elif event.type == EventTypes.Message:
                # Insert into the event_search table.
                self._store_room_message_txn(txn, event)
            elif event.type == EventTypes.Redaction and event.redacts is not None:
                # Insert into the redactions table.
                self._store_redaction(txn, event)
            elif event.type == EventTypes.Retention:
                # Update the room_retention table.
                self._store_retention_policy_for_room_txn(txn, event)

            self._handle_event_relations(txn, event)

            # Store the labels for this event.
            labels = event.content.get(EventContentFields.LABELS)
            if labels:
                self.insert_labels_for_event_txn(
                    txn, event.event_id, labels, event.room_id, event.depth
                )

            if self._ephemeral_messages_enabled:
                # If there's an expiry timestamp on the event, store it.
                expiry_ts = event.content.get(EventContentFields.SELF_DESTRUCT_AFTER)
                if type(expiry_ts) is int and not event.is_state():  # noqa: E721
                    self._insert_event_expiry_txn(txn, event.event_id, expiry_ts)

        # Insert into the room_memberships table.
        self._store_room_members_txn(
            txn,
            [
                event
                for event, _ in events_and_contexts
                if event.type == EventTypes.Member
            ],
            inhibit_local_membership_updates=inhibit_local_membership_updates,
        )

        # Prefill the event cache
        self._add_to_cache(txn, events_and_contexts)

    def _add_to_cache(
        self,
        txn: LoggingTransaction,
        events_and_contexts: list[EventPersistencePair],
    ) -> None:
        to_prefill: list[EventCacheEntry] = []

        ev_map = {e.event_id: e for e, _ in events_and_contexts}
        if not ev_map:
            return

        sql = (
            "SELECT "
            " e.event_id as event_id, "
            " r.redacts as redacts,"
            " rej.event_id as rejects "
            " FROM events as e"
            " LEFT JOIN rejections as rej USING (event_id)"
            " LEFT JOIN redactions as r ON e.event_id = r.redacts"
            " WHERE "
        )

        clause, args = make_in_list_sql_clause(
            self.database_engine, "e.event_id", list(ev_map)
        )

        txn.execute(sql + clause, args)
        for event_id, redacts, rejects in txn:
            event = ev_map[event_id]
            if not rejects and not redacts:
                to_prefill.append(EventCacheEntry(event=event, redacted_event=None))

        async def external_prefill() -> None:
            for cache_entry in to_prefill:
                await self.store._get_event_cache.set_external(
                    (cache_entry.event.event_id,), cache_entry
                )

        def local_prefill() -> None:
            for cache_entry in to_prefill:
                self.store._get_event_cache.set_local(
                    (cache_entry.event.event_id,), cache_entry
                )

        # The order these are called here is not as important as knowing that after the
        # transaction is finished, the async_call_after will run before the call_after.
        txn.async_call_after(external_prefill)
        txn.call_after(local_prefill)

    def _store_redaction(self, txn: LoggingTransaction, event: EventBase) -> None:
        assert event.redacts is not None
        self.db_pool.simple_upsert_txn(
            txn,
            table="redactions",
            keyvalues={"event_id": event.event_id},
            values={
                "redacts": event.redacts,
                "received_ts": self._clock.time_msec(),
            },
        )

    def insert_labels_for_event_txn(
        self,
        txn: LoggingTransaction,
        event_id: str,
        labels: list[str],
        room_id: str,
        topological_ordering: int,
    ) -> None:
        """Store the mapping between an event's ID and its labels, with one row per
        (event_id, label) tuple.

        Args:
            txn: The transaction to execute.
            event_id: The event's ID.
            labels: A list of text labels.
            room_id: The ID of the room the event was sent to.
            topological_ordering: The position of the event in the room's topology.
        """
        self.db_pool.simple_insert_many_txn(
            txn=txn,
            table="event_labels",
            keys=("event_id", "label", "room_id", "topological_ordering"),
            values=[
                (event_id, label, room_id, topological_ordering) for label in labels
            ],
        )

    def _insert_event_expiry_txn(
        self, txn: LoggingTransaction, event_id: str, expiry_ts: int
    ) -> None:
        """Save the expiry timestamp associated with a given event ID.

        Args:
            txn: The database transaction to use.
            event_id: The event ID the expiry timestamp is associated with.
            expiry_ts: The timestamp at which to expire (delete) the event.
        """
        self.db_pool.simple_insert_txn(
            txn=txn,
            table="event_expiry",
            values={"event_id": event_id, "expiry_ts": expiry_ts},
        )

    def _store_room_members_txn(
        self,
        txn: LoggingTransaction,
        events: list[EventBase],
        *,
        inhibit_local_membership_updates: bool = False,
    ) -> None:
        """
        Store a room member in the database.

        Args:
            txn: The transaction to use.
            events: List of events to store.
            inhibit_local_membership_updates: Stop the local_current_membership
                from being updated by these events. This should be set to True
                for backfilled events because backfilled events in the past do
                not affect the current local state.
        """

        self.db_pool.simple_insert_many_txn(
            txn,
            table="room_memberships",
            keys=(
                "event_id",
                "event_stream_ordering",
                "user_id",
                "sender",
                "room_id",
                "membership",
                "display_name",
                "avatar_url",
            ),
            values=[
                (
                    event.event_id,
                    event.internal_metadata.stream_ordering,
                    event.state_key,
                    event.user_id,
                    event.room_id,
                    event.membership,
                    non_null_str_or_none(event.content.get("displayname")),
                    non_null_str_or_none(event.content.get("avatar_url")),
                )
                for event in events
            ],
        )

        for event in events:
            # Sanity check that we're working with persisted events
            assert event.internal_metadata.stream_ordering is not None
            assert event.internal_metadata.instance_name is not None

            # We update the local_current_membership table only if the event is
            # "current", i.e., its something that has just happened.
            #
            # This will usually get updated by the `current_state_events` handling,
            # unless its an outlier, and an outlier is only "current" if it's an "out of
            # band membership", like a remote invite or a rejection of a remote invite.
            if (
                self.is_mine_id(event.state_key)
                and not inhibit_local_membership_updates
                and event.internal_metadata.is_outlier()
                and event.internal_metadata.is_out_of_band_membership()
            ):
                # The only sort of out-of-band-membership events we expect to see here
                # are remote invites/knocks and LEAVE events corresponding to
                # rejected/retracted invites and rescinded knocks.
                assert event.type == EventTypes.Member
                assert event.membership in (
                    Membership.INVITE,
                    Membership.KNOCK,
                    Membership.LEAVE,
                )

                self.db_pool.simple_upsert_txn(
                    txn,
                    table="local_current_membership",
                    keyvalues={"room_id": event.room_id, "user_id": event.state_key},
                    values={
                        "event_id": event.event_id,
                        "event_stream_ordering": event.internal_metadata.stream_ordering,
                        "membership": event.membership,
                    },
                )

                # Handle updating the `sliding_sync_membership_snapshots` table
                # (out-of-band membership events only)
                #
                raw_stripped_state_events = None
                if event.membership == Membership.INVITE:
                    invite_room_state = event.unsigned.get("invite_room_state")
                    raw_stripped_state_events = invite_room_state
                elif event.membership == Membership.KNOCK:
                    knock_room_state = event.unsigned.get("knock_room_state")
                    raw_stripped_state_events = knock_room_state

                insert_values = {
                    "sender": event.sender,
                    "membership_event_id": event.event_id,
                    "membership": event.membership,
                    # Since this is a new membership, it isn't forgotten anymore (which
                    # matches how Synapse currently thinks about the forgotten status)
                    "forgotten": 0,
                    "event_stream_ordering": event.internal_metadata.stream_ordering,
                    "event_instance_name": event.internal_metadata.instance_name,
                }
                if event.membership == Membership.LEAVE:
                    # Inherit the meta data from the remote invite/knock. When using
                    # sliding sync filters, this will prevent the room from
                    # disappearing/appearing just because you left the room.
                    pass
                elif event.membership in (Membership.INVITE, Membership.KNOCK):
                    extra_insert_values = (
                        self._get_sliding_sync_insert_values_from_stripped_state(
                            raw_stripped_state_events
                        )
                    )
                    insert_values.update(extra_insert_values)
                else:
                    # We don't know how to handle this type of membership yet
                    #
                    # FIXME: We should use `assert_never` here but for some reason
                    # the exhaustive matching doesn't recognize the `Never` here.
                    # assert_never(event.membership)
                    raise AssertionError(
                        f"Unexpected out-of-band membership {event.membership} ({event.event_id}) that we don't know how to handle yet"
                    )

                self.db_pool.simple_upsert_txn(
                    txn,
                    table="sliding_sync_membership_snapshots",
                    keyvalues={
                        "room_id": event.room_id,
                        "user_id": event.state_key,
                    },
                    values=insert_values,
                )

    def _handle_event_relations(
        self, txn: LoggingTransaction, event: EventBase
    ) -> None:
        """Handles inserting relation data during persistence of events

        Args:
            txn: The current database transaction.
            event: The event which might have relations.
        """
        relation = relation_from_event(event)
        if not relation:
            # No relation, nothing to do.
            return

        self.db_pool.simple_insert_txn(
            txn,
            table="event_relations",
            values={
                "event_id": event.event_id,
                "relates_to_id": relation.parent_id,
                "relation_type": relation.rel_type,
                "aggregation_key": relation.aggregation_key,
            },
        )

        if relation.rel_type == RelationTypes.THREAD:
            # Upsert into the threads table, but only overwrite the value if the
            # new event is of a later topological order OR if the topological
            # ordering is equal, but the stream ordering is later.
            # (Note by definition that the stream ordering will always be later
            # unless this is a backfilled event [= negative stream ordering]
            # because we are only persisting this event now and stream_orderings
            # are strictly monotonically increasing)
            sql = """
            INSERT INTO threads (room_id, thread_id, latest_event_id, topological_ordering, stream_ordering)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (room_id, thread_id)
            DO UPDATE SET
                latest_event_id = excluded.latest_event_id,
                topological_ordering = excluded.topological_ordering,
                stream_ordering = excluded.stream_ordering
            WHERE
                threads.topological_ordering <= excluded.topological_ordering AND
                threads.stream_ordering < excluded.stream_ordering
            """

            txn.execute(
                sql,
                (
                    event.room_id,
                    relation.parent_id,
                    event.event_id,
                    event.depth,
                    event.internal_metadata.stream_ordering,
                ),
            )

    def _handle_redact_relations(
        self, txn: LoggingTransaction, room_id: str, redacted_event_id: str
    ) -> None:
        """Handles receiving a redaction and checking whether the redacted event
        has any relations which must be removed from the database.

        Args:
            txn
            room_id: The room ID of the event that was redacted.
            redacted_event_id: The event that was redacted.
        """

        # Fetch the relation of the event being redacted.
        row = self.db_pool.simple_select_one_txn(
            txn,
            table="event_relations",
            keyvalues={"event_id": redacted_event_id},
            retcols=("relates_to_id", "relation_type"),
            allow_none=True,
        )
        # Nothing to do if no relation is found.
        if row is None:
            return

        redacted_relates_to, rel_type = row
        self.db_pool.simple_delete_txn(
            txn, table="event_relations", keyvalues={"event_id": redacted_event_id}
        )

        # Any relation information for the related event must be cleared.
        self.store._invalidate_cache_and_stream(
            txn,
            self.store.get_relations_for_event,
            (
                room_id,
                redacted_relates_to,
            ),
        )
        if rel_type == RelationTypes.REFERENCE:
            self.store._invalidate_cache_and_stream(
                txn, self.store.get_references_for_event, (redacted_relates_to,)
            )
        if rel_type == RelationTypes.REPLACE:
            self.store._invalidate_cache_and_stream(
                txn, self.store.get_applicable_edit, (redacted_relates_to,)
            )
        if rel_type == RelationTypes.THREAD:
            self.store._invalidate_cache_and_stream(
                txn, self.store.get_thread_summary, (redacted_relates_to,)
            )
            self.store._invalidate_cache_and_stream(
                txn, self.store.get_thread_participated, (redacted_relates_to,)
            )
            self.store._invalidate_cache_and_stream(
                txn, self.store.get_threads, (room_id,)
            )

            # Find the new latest event in the thread.
            sql = """
            SELECT event_id, topological_ordering, stream_ordering
            FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE relates_to_id = ? AND relation_type = ?
            ORDER BY topological_ordering DESC, stream_ordering DESC
            LIMIT 1
            """
            txn.execute(sql, (redacted_relates_to, RelationTypes.THREAD))

            # If a latest event is found, update the threads table, this might
            # be the same current latest event (if an earlier event in the thread
            # was redacted).
            latest_event_row = txn.fetchone()
            if latest_event_row:
                self.db_pool.simple_upsert_txn(
                    txn,
                    table="threads",
                    keyvalues={"room_id": room_id, "thread_id": redacted_relates_to},
                    values={
                        "latest_event_id": latest_event_row[0],
                        "topological_ordering": latest_event_row[1],
                        "stream_ordering": latest_event_row[2],
                    },
                )

            # Otherwise, delete the thread: it no longer exists.
            else:
                self.db_pool.simple_delete_one_txn(
                    txn, table="threads", keyvalues={"thread_id": redacted_relates_to}
                )

    def _store_room_topic_txn(self, txn: LoggingTransaction, event: EventBase) -> None:
        if isinstance(event.content.get("topic"), str):
            self.store_event_search_txn(
                txn,
                event,
                "content.topic",
                get_plain_text_topic_from_event_content(event.content) or "",
            )

    def _store_room_name_txn(self, txn: LoggingTransaction, event: EventBase) -> None:
        if isinstance(event.content.get("name"), str):
            self.store_event_search_txn(
                txn, event, "content.name", event.content["name"]
            )

    def _store_room_message_txn(
        self, txn: LoggingTransaction, event: EventBase
    ) -> None:
        if isinstance(event.content.get("body"), str):
            self.store_event_search_txn(
                txn, event, "content.body", event.content["body"]
            )

    def _store_retention_policy_for_room_txn(
        self, txn: LoggingTransaction, event: EventBase
    ) -> None:
        if not event.is_state():
            logger.debug("Ignoring non-state m.room.retention event")
            return

        if hasattr(event, "content") and (
            "min_lifetime" in event.content or "max_lifetime" in event.content
        ):
            if (
                "min_lifetime" in event.content
                and type(event.content["min_lifetime"]) is not int  # noqa: E721
            ) or (
                "max_lifetime" in event.content
                and type(event.content["max_lifetime"]) is not int  # noqa: E721
            ):
                # Ignore the event if one of the value isn't an integer.
                return

            self.db_pool.simple_insert_txn(
                txn=txn,
                table="room_retention",
                values={
                    "room_id": event.room_id,
                    "event_id": event.event_id,
                    "min_lifetime": event.content.get("min_lifetime"),
                    "max_lifetime": event.content.get("max_lifetime"),
                },
            )

            self.store._invalidate_cache_and_stream(
                txn, self.store.get_retention_policy_for_room, (event.room_id,)
            )

    def store_event_search_txn(
        self, txn: LoggingTransaction, event: EventBase, key: str, value: str
    ) -> None:
        """Add event to the search table

        Args:
            txn: The database transaction.
            event: The event being added to the search table.
            key: A key describing the search value (one of "content.name",
                "content.topic", or "content.body")
            value: The value from the event's content.
        """
        self.store.store_search_entries_txn(
            txn,
            (
                SearchEntry(
                    key=key,
                    value=value,
                    event_id=event.event_id,
                    room_id=event.room_id,
                    stream_ordering=event.internal_metadata.stream_ordering,
                    origin_server_ts=event.origin_server_ts,
                ),
            ),
        )

    def _set_push_actions_for_event_and_users_txn(
        self,
        txn: LoggingTransaction,
        events_and_contexts: list[EventPersistencePair],
        all_events_and_contexts: list[EventPersistencePair],
    ) -> None:
        """Handles moving push actions from staging table to main
        event_push_actions table for all events in `events_and_contexts`.

        Also ensures that all events in `all_events_and_contexts` are removed
        from the push action staging area.

        Args:
            events_and_contexts: events we are persisting
            all_events_and_contexts: all events that we were going to persist.
                This includes events we've already persisted, etc, that wouldn't
                appear in events_and_context.
        """

        # Only notifiable events will have push actions associated with them,
        # so let's filter them out. (This makes joining large rooms faster, as
        # these queries took seconds to process all the state events).
        notifiable_events = [
            event
            for event, _ in events_and_contexts
            if event.internal_metadata.is_notifiable()
        ]

        sql = """
            INSERT INTO event_push_actions (
                room_id, event_id, user_id, actions, stream_ordering,
                topological_ordering, notif, highlight, unread, thread_id
            )
            SELECT ?, event_id, user_id, actions, ?, ?, notif, highlight, unread, thread_id
            FROM event_push_actions_staging
            WHERE event_id = ?
        """

        if notifiable_events:
            txn.execute_batch(
                sql,
                [
                    (
                        event.room_id,
                        event.internal_metadata.stream_ordering,
                        event.depth,
                        event.event_id,
                    )
                    for event in notifiable_events
                ],
            )

        # Now we delete the staging area for *all* events that were being
        # persisted.
        txn.execute_batch(
            "DELETE FROM event_push_actions_staging WHERE event_id = ?",
            [
                (event.event_id,)
                for event, _ in all_events_and_contexts
                if event.internal_metadata.is_notifiable()
            ],
        )

    def _remove_push_actions_for_event_id_txn(
        self, txn: LoggingTransaction, room_id: str, event_id: str
    ) -> None:
        txn.execute(
            "DELETE FROM event_push_actions WHERE room_id = ? AND event_id = ?",
            (room_id, event_id),
        )

    def _store_rejections_txn(
        self, txn: LoggingTransaction, event_id: str, reason: str
    ) -> None:
        self.db_pool.simple_insert_txn(
            txn,
            table="rejections",
            values={
                "event_id": event_id,
                "reason": reason,
                "last_check": self._clock.time_msec(),
            },
        )

    def _store_event_state_mappings_txn(
        self,
        txn: LoggingTransaction,
        events_and_contexts: Collection[EventPersistencePair],
    ) -> None:
        """
        Raises:
            PartialStateConflictError: if attempting to persist a partial state event in
                a room that has been un-partial stated.
        """
        state_groups = {}
        for event, context in events_and_contexts:
            if event.internal_metadata.is_outlier():
                # double-check that we don't have any events that claim to be outliers
                # *and* have partial state (which is meaningless: we should have no
                # state at all for an outlier)
                if context.partial_state:
                    raise ValueError(
                        "Outlier event %s claims to have partial state", event.event_id
                    )

                continue

            # if the event was rejected, just give it the same state as its
            # predecessor.
            if context.rejected:
                state_groups[event.event_id] = context.state_group_before_event
                continue

            state_groups[event.event_id] = context.state_group

        # if we have partial state for these events, record the fact. (This happens
        # here rather than in _store_event_txn because it also needs to happen when
        # we de-outlier an event.)
        try:
            self.db_pool.simple_insert_many_txn(
                txn,
                table="partial_state_events",
                keys=("room_id", "event_id"),
                values=[
                    (
                        event.room_id,
                        event.event_id,
                    )
                    for event, ctx in events_and_contexts
                    if ctx.partial_state
                ],
            )
        except self.db_pool.engine.module.IntegrityError:
            logger.info(
                "Cannot persist events %s in rooms %s: room has been un-partial stated",
                [
                    event.event_id
                    for event, ctx in events_and_contexts
                    if ctx.partial_state
                ],
                list(
                    {
                        event.room_id
                        for event, ctx in events_and_contexts
                        if ctx.partial_state
                    }
                ),
            )
            raise PartialStateConflictError()

        self.db_pool.simple_upsert_many_txn(
            txn,
            table="event_to_state_groups",
            key_names=["event_id"],
            key_values=[[event_id] for event_id, _ in state_groups.items()],
            value_names=["state_group"],
            value_values=[
                [state_group_id] for _, state_group_id in state_groups.items()
            ],
        )

        for event_id, state_group_id in state_groups.items():
            txn.call_after(
                self.store._get_state_group_for_event.prefill,
                (event_id,),
                state_group_id,
            )

    def _update_min_depth_for_room_txn(
        self, txn: LoggingTransaction, room_id: str, depth: int
    ) -> None:
        min_depth = self.store._get_min_depth_interaction(txn, room_id)

        if min_depth is not None and depth >= min_depth:
            return

        self.db_pool.simple_upsert_txn(
            txn,
            table="room_depth",
            keyvalues={"room_id": room_id},
            values={"min_depth": depth},
        )

    def _handle_mult_prev_events(
        self, txn: LoggingTransaction, events: list[EventBase]
    ) -> None:
        """
        For the given event, update the event edges table and forward and
        backward extremities tables.
        """
        self.db_pool.simple_insert_many_txn(
            txn,
            table="event_edges",
            keys=("event_id", "prev_event_id"),
            values=[
                (ev.event_id, e_id) for ev in events for e_id in ev.prev_event_ids()
            ],
        )

        self._update_backward_extremeties(txn, events)

    def _update_backward_extremeties(
        self, txn: LoggingTransaction, events: list[EventBase]
    ) -> None:
        """Updates the event_backward_extremities tables based on the new/updated
        events being persisted.

        This is called for new events *and* for events that were outliers, but
        are now being persisted as non-outliers.

        Forward extremities are handled when we first start persisting the events.
        """

        room_id = events[0].room_id

        potential_backwards_extremities = {
            e_id
            for ev in events
            for e_id in ev.prev_event_ids()
            if not ev.internal_metadata.is_outlier()
        }

        if not potential_backwards_extremities:
            return

        existing_events_outliers = self.db_pool.simple_select_many_txn(
            txn,
            table="events",
            column="event_id",
            iterable=potential_backwards_extremities,
            keyvalues={"outlier": False},
            retcols=("event_id",),
        )

        potential_backwards_extremities.difference_update(
            e for (e,) in existing_events_outliers
        )

        if potential_backwards_extremities:
            self.db_pool.simple_upsert_many_txn(
                txn,
                table="event_backward_extremities",
                key_names=("room_id", "event_id"),
                key_values=[(room_id, ev) for ev in potential_backwards_extremities],
                value_names=(),
                value_values=(),
            )

            # Record the stream orderings where we have new gaps.
            gap_events = [
                (room_id, self._instance_name, ev.internal_metadata.stream_ordering)
                for ev in events
                if any(
                    e_id in potential_backwards_extremities
                    for e_id in ev.prev_event_ids()
                )
            ]

            self.db_pool.simple_insert_many_txn(
                txn,
                table="timeline_gaps",
                keys=("room_id", "instance_name", "stream_ordering"),
                values=gap_events,
            )

        # Delete all these events that we've already fetched and now know that their
        # prev events are the new backwards extremeties.
        query = (
            "DELETE FROM event_backward_extremities WHERE event_id = ? AND room_id = ?"
        )
        backward_extremity_tuples_to_remove = [
            (ev.event_id, ev.room_id)
            for ev in events
            if not ev.internal_metadata.is_outlier()
            # If we encountered an event with no prev_events, then we might
            # as well remove it now because it won't ever have anything else
            # to backfill from.
            or len(ev.prev_event_ids()) == 0
        ]
        txn.execute_batch(
            query,
            backward_extremity_tuples_to_remove,
        )

        # Clear out the failed backfill attempts after we successfully pulled
        # the event. Since we no longer need these events as backward
        # extremities, it also means that they won't be backfilled from again so
        # we no longer need to store the backfill attempts around it.
        query = """
            DELETE FROM event_failed_pull_attempts
            WHERE event_id = ? and room_id = ?
        """
        txn.execute_batch(
            query,
            backward_extremity_tuples_to_remove,
        )


@attr.s(slots=True, auto_attribs=True)
class _LinkMap:
    """A helper type for tracking links between chains."""

    # Stores the set of links as nested maps: source chain ID -> target chain ID
    # -> source sequence number -> target sequence number.
    maps: dict[int, dict[int, dict[int, int]]] = attr.Factory(dict)

    # Stores the links that have been added (with new set to true), as tuples of
    # `(source chain ID, source sequence no, target chain ID, target sequence no.)`
    additions: set[tuple[int, int, int, int]] = attr.Factory(set)

    def add_link(
        self,
        src_tuple: tuple[int, int],
        target_tuple: tuple[int, int],
        new: bool = True,
    ) -> bool:
        """Add a new link between two chains, ensuring no redundant links are added.

        New links should be added in topological order.

        Args:
            src_tuple: The chain ID/sequence number of the source of the link.
            target_tuple: The chain ID/sequence number of the target of the link.
            new: Whether this is a "new" link, i.e. should it be returned
                by `get_additions`.

        Returns:
            True if a link was added, false if the given link was dropped as redundant
        """
        src_chain, src_seq = src_tuple
        target_chain, target_seq = target_tuple

        current_links = self.maps.setdefault(src_chain, {}).setdefault(target_chain, {})

        assert src_chain != target_chain

        if new:
            # Check if the new link is redundant
            for current_seq_src, current_seq_target in current_links.items():
                # If a link "crosses" another link then its redundant. For example
                # in the following link 1 (L1) is redundant, as any event reachable
                # via L1 is *also* reachable via L2.
                #
                #   Chain A     Chain B
                #      |          |
                #   L1 |------    |
                #      |     |    |
                #   L2 |---- | -->|
                #      |     |    |
                #      |     |--->|
                #      |          |
                #      |          |
                #
                # So we only need to keep links which *do not* cross, i.e. links
                # that both start and end above or below an existing link.
                #
                # Note, since we add links in topological ordering we should never
                # see `src_seq` less than `current_seq_src`.

                if current_seq_src <= src_seq and target_seq <= current_seq_target:
                    # This new link is redundant, nothing to do.
                    return False

            self.additions.add((src_chain, src_seq, target_chain, target_seq))

        current_links[src_seq] = target_seq
        return True

    def get_additions(self) -> Generator[tuple[int, int, int, int], None, None]:
        """Gets any newly added links.

        Yields:
            The source chain ID/sequence number and target chain ID/sequence number
        """

        for src_chain, src_seq, target_chain, _ in self.additions:
            target_seq = self.maps.get(src_chain, {}).get(target_chain, {}).get(src_seq)
            if target_seq is not None:
                yield (src_chain, src_seq, target_chain, target_seq)

    def exists_path_from(
        self,
        src_tuple: tuple[int, int],
        target_tuple: tuple[int, int],
    ) -> bool:
        """Checks if there is a path between the source chain ID/sequence and
        target chain ID/sequence.
        """
        src_chain, src_seq = src_tuple
        target_chain, target_seq = target_tuple

        if src_chain == target_chain:
            return target_seq <= src_seq

        # We have to graph traverse the links to check for indirect paths.
        visited_chains: dict[int, int] = collections.Counter()
        search = [(src_chain, src_seq)]
        while search:
            chain, seq = search.pop()
            visited_chains[chain] = max(seq, visited_chains[chain])
            for tc, links in self.maps.get(chain, {}).items():
                for ss, ts in links.items():
                    # Don't revisit chains we've already seen, unless the target
                    # sequence number is higher than last time.
                    if ts <= visited_chains.get(tc, 0):
                        continue

                    if ss <= seq:
                        if tc == target_chain:
                            if target_seq <= ts:
                                return True
                        else:
                            search.append((tc, ts))

        return False
