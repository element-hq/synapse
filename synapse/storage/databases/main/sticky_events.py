#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
import logging
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    cast,
)

from synapse.api.constants import EventTypes, StickyEvent
from synapse.events import EventBase
from synapse.events.snapshot import EventPersistencePair
from synapse.replication.tcp.streams._base import StickyEventsStream
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.databases.main.cache import CacheInvalidationWorkerStore
from synapse.storage.databases.main.events import DeltaState
from synapse.storage.util.id_generators import MultiWriterIdGenerator

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class StickyEventsWorkerStore(CacheInvalidationWorkerStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._can_write_to_sticky_events = (
            self._instance_name in hs.config.worker.writers.events
        )

        self._sticky_events_id_gen: MultiWriterIdGenerator = MultiWriterIdGenerator(
            db_conn=db_conn,
            db=database,
            notifier=hs.get_replication_notifier(),
            stream_name="sticky_events",
            server_name=self.server_name,
            instance_name=self._instance_name,
            tables=[
                ("sticky_events", "instance_name", "stream_id"),
            ],
            sequence_name="sticky_events_sequence",
            writers=hs.config.worker.writers.events,
        )

    def process_replication_rows(
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[Any],
    ) -> None:
        super().process_replication_rows(stream_name, instance_name, token, rows)

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == StickyEventsStream.NAME:
            self._sticky_events_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

    def get_max_sticky_events_stream_id(self) -> int:
        """Get the current maximum stream_id for thread subscriptions.

        Returns:
            The maximum stream_id
        """
        return self._sticky_events_id_gen.get_current_token()

    def get_sticky_events_stream_id_generator(self) -> MultiWriterIdGenerator:
        return self._sticky_events_id_gen

    async def get_sticky_events_in_rooms(
        self,
        room_ids: Collection[str],
        from_id: int,
        now: int,
    ) -> Tuple[int, Dict[str, Set[str]]]:
        """
        Fetch all the sticky events in the given rooms, from the given sticky stream ID.

        Args:
            room_ids: The room IDs to return sticky events in.
            from_id: The sticky stream ID that sticky events should be returned from.
            now: The current time in unix millis, used for skipping expired events.
        Returns:
            A tuple of (to_id, map[room_id, event_ids])
        """
        sticky_events_rows = await self.db_pool.runInteraction(
            "get_sticky_events_in_rooms",
            self._get_sticky_events_in_rooms_txn,
            room_ids,
            from_id,
            now,
        )
        to_id = from_id
        room_to_events: Dict[str, Set[str]] = {}
        for stream_id, room_id, event_id in sticky_events_rows:
            to_id = max(to_id, stream_id)
            events = room_to_events.get(room_id, set())
            events.add(event_id)
            room_to_events[room_id] = events
        return (to_id, room_to_events)

    def _get_sticky_events_in_rooms_txn(
        self,
        txn: LoggingTransaction,
        room_ids: Collection[str],
        from_id: int,
        now: int,
    ) -> List[Tuple[int, str, str]]:
        if len(room_ids) == 0:
            return []
        clause, room_id_values = make_in_list_sql_clause(
            txn.database_engine, "room_id", room_ids
        )
        txn.execute(
            f"""
            SELECT stream_id, room_id, event_id FROM sticky_events WHERE soft_failed=FALSE AND expires_at > ?  AND stream_id > ? AND {clause}
            """,
            (now, from_id, room_id_values),
        )
        return cast(List[Tuple[int, str, str]], txn.fetchall())

    async def get_updated_sticky_events(
        self, from_id: int, to_id: int, limit: int
    ) -> List[Tuple[int, str, str]]:
        """Get updates to sticky events between two stream IDs.

        Args:
            from_id: The starting stream ID (exclusive)
            to_id: The ending stream ID (inclusive)
            limit: The maximum number of rows to return

        Returns:
            list of (stream_id, room_id, event_id) tuples
        """
        return await self.db_pool.runInteraction(
            "get_updated_sticky_events",
            self._get_updated_sticky_events_txn,
            from_id,
            to_id,
            limit,
        )

    def _get_updated_sticky_events_txn(
        self, txn: LoggingTransaction, from_id: int, to_id: int, limit: int
    ) -> List[Tuple[int, str, str]]:
        txn.execute(
            """
            SELECT stream_id, room_id, event_id FROM sticky_events WHERE stream_id > ? AND stream_id <= ? LIMIT ?
            """,
            (from_id, to_id, limit),
        )
        return cast(List[Tuple[int, str, str]], txn.fetchall())

    def handle_sticky_events_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        events_and_contexts: List[EventPersistencePair],
        state_delta_for_room: Optional[DeltaState],
    ) -> None:
        """Update the sticky events table, used in MSC4354. Intended to be called within the persist
        events transaction.

        This function assumes that `_store_event_txn()` (to persist the event) and
        `_update_current_state_txn(...)` (so the current state has taken the events into account)
        have already been run.

        "Handling" sticky events is broken into two phases:
         - for each sticky event in events_and_contexts, mark them as sticky in the sticky events table.
         - for each still-sticky soft-failed event in the room, re-evaluate soft-failedness.

        Args:
            txn
            room_id: The room that all of the events belong to
            events_and_contexts: The events being persisted.
            state_delta_for_room: The changes to the current state, used to detect if we need to
            re-evaluate soft-failed sticky events.
        """
        if len(events_and_contexts) == 0:
            return

        assert self._can_write_to_sticky_events

        # TODO: finish the impl
        # fetch soft failed sticky events to recheck now, before we insert new sticky events, else
        # we could incorrectly re-evaluate new sticky events
        # event_ids_to_check = self._get_soft_failed_sticky_events_to_recheck(txn, room_id, state_delta_for_room)
        # logger.info(f"_get_soft_failed_sticky_events_to_recheck => {event_ids_to_check}")
        # recheck them and update any that now pass soft-fail checks.
        # self._recheck_soft_failed_events(txn, room_id, event_ids_to_check)

        # insert brand new sticky events.
        self._insert_sticky_events_txn(txn, events_and_contexts)

    def _insert_sticky_events_txn(
        self,
        txn: LoggingTransaction,
        events_and_contexts: List[EventPersistencePair],
    ) -> None:
        now_ms = self._now()
        # event, expires_at, stream_id
        sticky_events: List[Tuple[EventBase, int, int]] = []
        for ev, _ in events_and_contexts:
            # MSC: Note: policy servers and other similar antispam techniques still apply to these events.
            if ev.internal_metadata.policy_server_spammy:
                continue
            # We shouldn't be passed rejected events, but if we do, we filter them out too.
            if ev.rejected_reason is not None:
                continue
            # MSC: The presence of sticky.duration_ms with a valid value makes the event “sticky”
            sticky_obj = ev.get_dict().get(StickyEvent.FIELD_NAME, None)
            if type(sticky_obj) is not dict:
                continue
            sticky_duration_ms = sticky_obj.get("duration_ms", None)
            # MSC: Valid values are the integer range 0-3600000 (1 hour).
            if (
                type(sticky_duration_ms) is int
                and sticky_duration_ms >= 0
                and sticky_duration_ms <= 3600000
            ):
                # MSC: The start time is min(now, origin_server_ts).
                # This ensures that malicious origin timestamps cannot specify start times in the future.
                # Calculate the end time as start_time + min(sticky.duration_ms, 3600000).
                expires_at = min(ev.origin_server_ts, now_ms) + min(
                    ev.get_dict()[StickyEvent.FIELD_NAME]["duration_ms"], 3600000
                )
                # filter out already expired sticky events
                if expires_at > now_ms:
                    sticky_events.append(
                        (ev, expires_at, self._sticky_events_id_gen.get_next_txn(txn))
                    )
        if len(sticky_events) == 0:
            return
        logger.info(
            "inserting %d sticky events in room %s",
            len(sticky_events),
            sticky_events[0][0].room_id,
        )
        self.db_pool.simple_insert_many_txn(
            txn,
            "sticky_events",
            keys=(
                "instance_name",
                "stream_id",
                "room_id",
                "event_id",
                "sender",
                "expires_at",
                "soft_failed",
            ),
            values=[
                (
                    self._instance_name,
                    stream_id,
                    ev.room_id,
                    ev.event_id,
                    ev.sender,
                    expires_at,
                    ev.internal_metadata.is_soft_failed(),
                )
                for (ev, expires_at, stream_id) in sticky_events
            ],
        )

    def _get_soft_failed_sticky_events_to_recheck(
        self,
        txn: LoggingTransaction,
        room_id: str,
        state_delta_for_room: Optional[DeltaState],
    ) -> List[str]:
        """Fetch soft-failed sticky events which should be rechecked against the current state.

        Soft-failed events are not rejected, so they pass auth at the state before
        the event and at the auth_events in the event. Instead, soft-failed events failed auth at
        the _current state of the room_. We only need to recheck soft failure if we have a reason to
        believe the event may pass that check now.

        Note that we don't bother rechecking accepted events that may now be soft-failed, because
        by that point it's too late as we've already sent the event to clients.

        Returns:
            A list of event IDs to recheck
        """

        if state_delta_for_room is None:
            # No change to current state => no way soft failure status could be different.
            return []

        # any change to critical auth events may change soft failure status. This means any changes
        # to join rules, power levels or member events. If the state has changed but it isn't one
        # of those events, we don't need to recheck.
        critical_auth_types = (
            EventTypes.JoinRules,
            EventTypes.PowerLevels,
            EventTypes.Member,
        )
        critical_auth_types_changed = set()
        critical_auth_types_changed.update(
            [
                typ
                for typ, _ in state_delta_for_room.to_delete
                if typ in critical_auth_types
            ]
        )
        critical_auth_types_changed.update(
            [
                typ
                for typ, _ in state_delta_for_room.to_insert
                if typ in critical_auth_types
            ]
        )
        if len(critical_auth_types_changed) == 0:
            # No change to critical auth events => no way soft failure status could be different.
            return []

        if critical_auth_types_changed == {EventTypes.Member}:
            # the final case we want to catch is when unprivileged users join/leave rooms. These users cause
            # changes in the critical auth types (the member event) but ultimately have no effect on soft
            # failure status for anyone but that user themselves.
            #
            # Grab the set of senders that have been modified and see if any of them sent a soft-failed
            # sticky event. If they did, then we need to re-evaluate. If they didn't, then we don't need to.
            new_membership_changes = set(
                [
                    skey
                    for typ, skey in state_delta_for_room.to_insert
                    if typ == EventTypes.Member
                ]
                + [
                    skey
                    for typ, skey in state_delta_for_room.to_delete
                    if typ == EventTypes.Member
                ]
            )
            # pull out senders of sticky events in this room
            events_to_recheck: List[Tuple[str]] = self.db_pool.simple_select_many_txn(
                txn,
                table="sticky_events",
                column="sender",
                iterable=new_membership_changes,
                keyvalues={
                    "room_id": room_id,
                    "soft_failed": True,
                },
                retcols=("event_id"),
            )
            return [event_id for (event_id,) in events_to_recheck]

        # otherwise one of the following must be true:
        # - there was a change in PL or join rules
        # - there was a change in the membership of a sender of a soft-failed sticky event.
        # In both of these cases we want to re-evaluate soft failure status.
        #
        # NB: event auth checks are NOT recursive. We don't need to specifically handle the case where
        # an admin user's membership changes which causes a PL event to be allowed, as when the PL event
        # gets allowed we will re-evaluate anyway. E.g:
        #
        #  PL(send_event=0, sender=Admin)
        #            ^              ^_____________________
        #            |                                   |
        # . PL(send_event=50, sender=Mod)              sticky event (sender=User)
        #
        # In this scenario, the sticky event is soft-failed due to the Mod updating the PL event to
        # set send_event=50, which User does not have. If we learn of an event which makes Mod's PL
        # event invalid (say, Mod was banned by Admin concurrently to Mod setting the PL event), then
        # the act of seeing the ban event will cause the old PL event to be in the state delta, meaning
        # we will re-evaluate the sticky event due to the PL changing. We don't need to specially handle case.a
        events_to_recheck = self.db_pool.simple_select_list_txn(
            txn,
            table="sticky_events",
            keyvalues={
                "room_id": room_id,
                "soft_failed": True,
            },
            retcols=("event_id"),
        )
        return [event_id for (event_id,) in events_to_recheck]

    def _recheck_soft_failed_events(
        self,
        txn: LoggingTransaction,
        room_id: str,
        event_ids: List[str],
    ) -> None:
        """
        Recheck authorised but soft-failed events. The provided event IDs must have already passed
        all auth checks (so the event isn't rejected) but soft-failure checks.

        Args:
            txn: The SQL transaction
            room_id: The room the event IDs are in.
            event_ids: The soft-failed events to re-evaluate.
        """
        # We know the events are otherwise authorised, so we only need to load the current state
        # and check if the events pass auth at the current state.

    def _now(self) -> int:
        return round(time.time() * 1000)
