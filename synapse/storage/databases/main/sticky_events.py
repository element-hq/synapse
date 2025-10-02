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

from twisted.internet.defer import Deferred

from synapse import event_auth
from synapse.api.constants import EventTypes, StickyEvent
from synapse.api.errors import AuthError
from synapse.events import EventBase
from synapse.events.snapshot import EventPersistencePair
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.replication.tcp.streams._base import StickyEventsStream
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.databases.main.cache import CacheInvalidationWorkerStore
from synapse.storage.databases.main.events import DeltaState
from synapse.storage.databases.main.state import StateGroupWorkerStore
from synapse.storage.engines import PostgresEngine
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.types.state import StateFilter
from synapse.util.stringutils import shortstr

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# Remove entries from the sticky_events table at this frequency.
# Note: this does NOT mean we don't honour shorter expiration timeouts.
# Consumers call 'get_sticky_events_in_rooms' which has `WHERE expires_at > ?`
# to filter out expired sticky events that have yet to be deleted.
DELETE_EXPIRED_STICKY_EVENTS_MS = 60 * 1000 * 60  # 1 hour


class StickyEventsWorkerStore(StateGroupWorkerStore, CacheInvalidationWorkerStore):
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

        # Technically this means we will cleanup N times, once per event persister, maybe put on master?
        if self._can_write_to_sticky_events:
            self._clock.looping_call(
                self._run_background_cleanup, DELETE_EXPIRED_STICKY_EVENTS_MS
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
        to_id: int,
        now: int,
    ) -> Tuple[int, Dict[str, Set[str]]]:
        """
        Fetch all the sticky events in the given rooms, from the given sticky stream ID.

        Args:
            room_ids: The room IDs to return sticky events in.
            from_id: The sticky stream ID that sticky events should be returned from (exclusive).
            to_id: The sticky stream ID that sticky events should end at (inclusive).
            now: The current time in unix millis, used for skipping expired events.
        Returns:
            A tuple of (to_id, map[room_id, event_ids])
        """
        sticky_events_rows = await self.db_pool.runInteraction(
            "get_sticky_events_in_rooms",
            self._get_sticky_events_in_rooms_txn,
            room_ids,
            from_id,
            to_id,
            now,
        )
        new_to_id = from_id
        room_to_events: Dict[str, Set[str]] = {}
        for stream_id, room_id, event_id in sticky_events_rows:
            new_to_id = max(new_to_id, stream_id)
            events = room_to_events.get(room_id, set())
            events.add(event_id)
            room_to_events[room_id] = events
        return (new_to_id, room_to_events)

    def _get_sticky_events_in_rooms_txn(
        self,
        txn: LoggingTransaction,
        room_ids: Collection[str],
        from_id: int,
        to_id: int,
        now: int,
    ) -> List[Tuple[int, str, str]]:
        if len(room_ids) == 0:
            return []
        clause, room_id_values = make_in_list_sql_clause(
            txn.database_engine, "room_id", room_ids
        )
        txn.execute(
            f"""
            SELECT stream_id, room_id, event_id FROM sticky_events
            WHERE soft_failed=FALSE AND expires_at > ? AND stream_id > ? AND stream_id <= ? AND {clause}
            """,
            (now, from_id, to_id, *room_id_values),
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

    async def get_sticky_event_ids_sent_by_self(
        self, room_id: str, from_stream_pos: int
    ) -> List[str]:
        """Get sticky event IDs which have been sent by users on this homeserver.

        Used when sending sticky events eagerly to newly joined servers, or when catching up over federation.

        Args:
            room_id: The room to fetch sticky events in.
            from_stream_pos: The stream position to return events from. May be 0 for newly joined servers.
        Returns:
            A list of event IDs, which may be empty.
        """
        return await self.db_pool.runInteraction(
            "get_sticky_event_ids_sent_by_self",
            self._get_sticky_event_ids_sent_by_self_txn,
            room_id,
            from_stream_pos,
        )

    def _get_sticky_event_ids_sent_by_self_txn(
        self, txn: LoggingTransaction, room_id: str, from_stream_pos: int
    ) -> List[str]:
        now_ms = self._now()
        txn.execute(
            """
            SELECT sticky_events.event_id, sticky_events.sender, events.stream_ordering FROM sticky_events
            INNER JOIN events ON events.event_id = sticky_events.event_id
            WHERE soft_failed=FALSE AND expires_at > ? AND sticky_events.room_id = ?
            """,
            (now_ms, room_id),
        )
        rows = cast(List[Tuple[str, str, int]], txn.fetchall())
        return [
            row[0]
            for row in rows
            if row[2] > from_stream_pos and self.hs.is_mine_id(row[1])
        ]

    async def reevaluate_soft_failed_sticky_events(
        self,
        room_id: str,
        events_and_contexts: List[EventPersistencePair],
        state_delta_for_room: Optional[DeltaState],
    ) -> None:
        """Re-evaluate soft failed events in the room provided.

        Args:
            room_id: The room that all of the events belong to
            events_and_contexts: The events just persisted. These are not eligible for re-evaluation.
            state_delta_for_room: The changes to the current state, used to detect if we need to
            re-evaluate soft-failed sticky events.
        """
        assert self._can_write_to_sticky_events

        # fetch soft failed sticky events to recheck
        event_ids_to_check = await self._get_soft_failed_sticky_events_to_recheck(
            room_id, state_delta_for_room
        )
        # filter out soft-failed events in events_and_contexts as we just inserted them, so the
        # soft failure status won't have changed for them.
        persisting_event_ids = {ev.event_id for ev, _ in events_and_contexts}
        event_ids_to_check = [
            item for item in event_ids_to_check if item not in persisting_event_ids
        ]
        if event_ids_to_check:
            logger.info(
                "_get_soft_failed_sticky_events_to_recheck => %s", event_ids_to_check
            )
            # recheck them and update any that now pass soft-fail checks.
            await self._recheck_soft_failed_events(room_id, event_ids_to_check)

    def insert_sticky_events_txn(
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
            # We can't persist outlier sticky events as we don't know the room state at that event
            if ev.internal_metadata.is_outlier():
                continue
            # MSC: The presence of sticky.duration_ms with a valid value makes the event “sticky”
            sticky_duration = ev.sticky_duration()
            if sticky_duration:
                # MSC: The start time is min(now, origin_server_ts).
                # This ensures that malicious origin timestamps cannot specify start times in the future.
                # Calculate the end time as start_time + min(sticky.duration_ms, MAX_DURATION_MS).
                expires_at = min(ev.origin_server_ts, now_ms) + min(
                    ev.get_dict()[StickyEvent.FIELD_NAME]["duration_ms"],
                    StickyEvent.MAX_DURATION_MS,
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

    async def _get_soft_failed_sticky_events_to_recheck(
        self,
        room_id: str,
        state_delta_for_room: Optional[DeltaState],
    ) -> List[str]:
        """Fetch soft-failed sticky events which should be rechecked against the current state.

        Soft-failed events are not rejected, so they pass auth at the state before
        the event and at the auth_events in the event. Instead, soft-failed events failed auth at
        the *current* state of the room. We only need to recheck soft failure if we have a reason to
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
            events_to_recheck: List[
                Tuple[str]
            ] = await self.db_pool.simple_select_many_batch(
                table="sticky_events",
                column="sender",
                iterable=new_membership_changes,
                keyvalues={
                    "room_id": room_id,
                    "soft_failed": True,
                },
                retcols=("event_id",),
                desc="_get_soft_failed_sticky_events_to_recheck_members",
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
        #  PL(send_event=0, sender=Admin) #1
        #            ^              ^_____________________
        #            |                                   |
        # . PL(send_event=50, sender=Mod) #2            sticky event (sender=User) #3
        #
        # In this scenario, the sticky event is soft-failed due to the Mod updating the PL event to
        # set send_event=50, which User does not have. If we learn of an event which makes Mod's PL
        # event invalid (say, Mod was banned by Admin concurrently to Mod setting the PL event), then
        # the act of seeing the ban event will cause the old PL event to be in the state delta, meaning
        # we will re-evaluate the sticky event due to the PL changing. We don't need to specially handle
        # this case.
        events_to_recheck = await self.db_pool.simple_select_list(
            table="sticky_events",
            keyvalues={
                "room_id": room_id,
                "soft_failed": True,
            },
            retcols=("event_id",),
            desc="_get_soft_failed_sticky_events_to_recheck",
        )
        return [event_id for (event_id,) in events_to_recheck]

    async def _recheck_soft_failed_events(
        self,
        room_id: str,
        soft_failed_event_ids: Collection[str],
    ) -> None:
        """
        Recheck authorised but soft-failed events. The provided event IDs must have already passed
        all auth checks (so the event isn't rejected) but soft-failure checks.

        Args:
            txn: The SQL transaction
            room_id: The room the event IDs are in.
            soft_failed_event_ids: The soft-failed events to re-evaluate.
        """
        # Load all the soft-failed events to recheck, and pull out the precise state tuples we need
        soft_failed_event_map = await self.get_events(
            soft_failed_event_ids, allow_rejected=False
        )
        needed_tuples: Set[Tuple[str, str]] = set()
        for ev in soft_failed_event_map.values():
            needed_tuples.update(event_auth.auth_types_for_event(ev.room_version, ev))

        # We know the events are otherwise authorised, so we only need to load the needed tuples from
        # the current state to check if the events pass auth.
        current_state_map = await self.get_partial_filtered_current_state_ids(
            room_id, StateFilter.from_types(needed_tuples)
        )
        current_state_ids_list = [e for _, e in current_state_map.items()]
        current_auth_events = await self.get_events_as_list(current_state_ids_list)
        passing_event_ids: Set[str] = set()
        for soft_failed_event in soft_failed_event_map.values():
            if soft_failed_event.internal_metadata.policy_server_spammy:
                # don't re-evaluate spam.
                continue
            try:
                # We don't need to check_state_independent_auth_rules as that doesn't depend on room state,
                # so if it passed once it'll pass again.
                event_auth.check_state_dependent_auth_rules(
                    soft_failed_event, current_auth_events
                )
                passing_event_ids.add(soft_failed_event.event_id)
            except AuthError:
                pass

        if not passing_event_ids:
            return

        logger.info(
            "%s soft-failed events now pass current state checks in room %s : %s",
            len(passing_event_ids),
            room_id,
            shortstr(passing_event_ids),
        )
        # Update the DB with the new soft-failure status
        await self.db_pool.runInteraction(
            "_recheck_soft_failed_events",
            self._update_soft_failure_status_txn,
            passing_event_ids,
        )

    def _update_soft_failure_status_txn(
        self, txn: LoggingTransaction, passing_event_ids: Set[str]
    ) -> None:
        # Update the sticky events table so we notify downstream of the change in soft-failure status
        new_stream_ids: List[Tuple[str, int]] = [
            (event_id, self._sticky_events_id_gen.get_next_txn(txn))
            for event_id in passing_event_ids
        ]
        # [event_id, stream_pos, event_id, stream_pos, ...]
        params = [p for pair in new_stream_ids for p in pair]
        if isinstance(txn.database_engine, PostgresEngine):
            values_placeholders = ", ".join(["(?, ?)"] * len(new_stream_ids))
            txn.execute(
                f"""
                UPDATE sticky_events AS se
                SET
                    soft_failed = FALSE,
                    stream_id   = v.stream_id
                FROM (VALUES
                    {values_placeholders}
                ) AS v(event_id, stream_id)
                WHERE se.event_id = v.event_id;
                """,
                params,
            )
            # Also update the internal metadata on the event itself, so when we filter_events_for_client
            # we don't filter them out. It's a bit sad internal_metadata is TEXT and not JSONB...
            clause, args = make_in_list_sql_clause(
                txn.database_engine,
                "event_id",
                passing_event_ids,
            )
            txn.execute(
                """
                UPDATE event_json
                SET internal_metadata = (
                    jsonb_set(internal_metadata::jsonb, '{soft_failed}', 'false'::jsonb)
                )::text
                WHERE %s
                """
                % clause,
                args,
            )
        else:
            # Use a CASE expression to update in bulk for sqlite
            case_expr = " ".join(["WHEN ? THEN ? " for _ in new_stream_ids])
            txn.execute(
                f"""
                UPDATE sticky_events
                SET
                    soft_failed = FALSE,
                    stream_id = CASE event_id
                        {case_expr}
                        ELSE stream_id
                    END
                WHERE event_id IN ({",".join("?" * len(new_stream_ids))});
                """,
                params + [eid for eid, _ in new_stream_ids],
            )
            clause, args = make_in_list_sql_clause(
                txn.database_engine,
                "event_id",
                passing_event_ids,
            )
            txn.execute(
                f"""
                UPDATE event_json
                SET internal_metadata = json_set(internal_metadata, '$.soft_failed', json('false'))
                WHERE {clause}
                """,
                args,
            )
        # finally, invalidate caches
        for event_id in passing_event_ids:
            self.invalidate_get_event_cache_after_txn(txn, event_id)

    async def _delete_expired_sticky_events(self) -> None:
        logger.info("delete_expired_sticky_events")
        await self.db_pool.runInteraction(
            "_delete_expired_sticky_events",
            self._delete_expired_sticky_events_txn,
            self._now(),
        )

    def _delete_expired_sticky_events_txn(
        self, txn: LoggingTransaction, now: int
    ) -> None:
        txn.execute(
            """
            DELETE FROM sticky_events WHERE expires_at < ?
            """,
            (now,),
        )

    def _now(self) -> int:
        return round(time.time() * 1000)

    def _run_background_cleanup(self) -> Deferred:
        return run_as_background_process(
            "delete_expired_sticky_events",
            self.server_name,
            self._delete_expired_sticky_events,
        )
