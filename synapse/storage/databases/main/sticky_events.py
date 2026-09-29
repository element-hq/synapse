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
import random
from collections.abc import Set
from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING, Collection, cast

from twisted.internet.defer import Deferred

from synapse import event_auth
from synapse.api.constants import EventTypes
from synapse.api.errors import AuthError
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
from synapse.storage.databases.main.state import StateGroupWorkerStore
from synapse.storage.engines import PostgresEngine, Sqlite3Engine
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.types import StateKey
from synapse.types.state import StateFilter
from synapse.util.duration import Duration
from synapse.util.stringutils import shortstr

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

DELETE_EXPIRED_STICKY_EVENTS_INTERVAL = Duration(hours=1)
"""
Remove entries from the sticky_events table at this frequency.
Note: don't be misled, we still honour shorter expiration timeouts,
because readers of the sticky_events table filter out expired sticky events
themselves, even if they aren't deleted from the table yet.

Currently just an arbitrary choice.
Frequent enough to clean up expired sticky events promptly,
especially given the short cap on the lifetime of sticky events.
"""


@dataclass(frozen=True)
class StickyEventUpdate:
    stream_id: int
    room_id: str
    event_id: str
    soft_failed: bool


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
            # Start a looping call to clean up the `sticky_events` table
            #
            # Because this will run once per event persister (for now),
            # randomly stagger the initial time so that they don't all
            # coincide with each other if the workers are deployed at the
            # same time. This allows each cleanup to be somewhat more effective
            # than if they all started at the same time, as they would all be
            # cleaning up the same thing whereas each worker gets to clean up a little
            # throughout the hour when they're staggered.
            #
            # Concurrent execution of the same deletions could also lead to
            # repeatable serialisation violations in the database transaction,
            # meaning we'd have to retry the transaction several times.
            #
            # This staggering is not critical, it's just best-effort.
            self.clock.call_later(
                # random() is 0.0 to 1.0
                DELETE_EXPIRED_STICKY_EVENTS_INTERVAL * random.random(),
                self.clock.looping_call,
                self._run_background_cleanup,
                DELETE_EXPIRED_STICKY_EVENTS_INTERVAL,
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

        if hs.config.experimental.msc4354_enabled and isinstance(
            self.database_engine, Sqlite3Engine
        ):
            import sqlite3

            if sqlite3.sqlite_version_info < (3, 40, 0):
                raise RuntimeError(
                    f"Experimental MSC4354 Sticky Events enabled but SQLite3 version is too old: {sqlite3.sqlite_version_info}, must be at least 3.40. Disable MSC4354 Sticky Events, switch to Postgres, or upgrade SQLite. See https://github.com/element-hq/synapse/issues/19428"
                )

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == StickyEventsStream.NAME:
            self._sticky_events_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

    def get_max_sticky_events_stream_id(self) -> int:
        """Get the current maximum stream_id for sticky events.

        Returns:
            The maximum stream_id
        """
        return self._sticky_events_id_gen.get_current_token()

    def get_sticky_events_stream_id_generator(self) -> MultiWriterIdGenerator:
        return self._sticky_events_id_gen

    async def get_sticky_events_in_rooms(
        self,
        room_ids: Collection[str],
        *,
        from_id: int,
        to_id: int,
        now: int,
        limit: int | None,
    ) -> tuple[int, dict[str, list[str]]]:
        """
        Fetch all the sticky events' IDs in the given rooms, with sticky stream IDs satisfying
        from_id < sticky stream ID <= to_id.

        The events are returned ordered by the sticky events stream.

        Args:
            room_ids: The room IDs to return sticky events in.
            from_id: The sticky stream ID that sticky events should be returned from (exclusive).
            to_id: The sticky stream ID that sticky events should end at (inclusive).
            now: The current time in unix millis, used for skipping expired events.
            limit: Max sticky events to return, or None to apply no limit.
        Returns:
            to_id, dict[room_id, list[event_ids]]
        """
        sticky_events_rows = await self.db_pool.runInteraction(
            "get_sticky_events_in_rooms",
            self._get_sticky_events_in_rooms_txn,
            room_ids,
            from_id=from_id,
            to_id=to_id,
            now=now,
            limit=limit,
        )

        if not sticky_events_rows:
            return to_id, {}

        # Get stream_id of the last row, which is the highest
        new_to_id, _, _ = sticky_events_rows[-1]

        # room ID -> event IDs
        room_id_to_event_ids: dict[str, list[str]] = {}
        for _, room_id, event_id in sticky_events_rows:
            events = room_id_to_event_ids.setdefault(room_id, [])
            events.append(event_id)

        return (new_to_id, room_id_to_event_ids)

    def _get_sticky_events_in_rooms_txn(
        self,
        txn: LoggingTransaction,
        room_ids: Collection[str],
        *,
        from_id: int,
        to_id: int,
        now: int,
        limit: int | None,
    ) -> list[tuple[int, str, str]]:
        if len(room_ids) == 0:
            return []
        room_id_in_list_clause, room_id_in_list_values = make_in_list_sql_clause(
            txn.database_engine, "se.room_id", room_ids
        )
        limit_clause = ""
        limit_params: tuple[int, ...] = ()
        if limit is not None:
            limit_clause = "LIMIT ?"
            limit_params = (limit,)

        if isinstance(self.database_engine, PostgresEngine):
            expr_soft_failed = "COALESCE(((ej.internal_metadata::jsonb)->>'soft_failed')::boolean, FALSE)"
        else:
            expr_soft_failed = "COALESCE(ej.internal_metadata->>'soft_failed', FALSE)"

        txn.execute(
            f"""
            SELECT se.stream_id, se.room_id, event_id
            FROM sticky_events se
            INNER JOIN event_json ej USING (event_id)
            WHERE
                NOT {expr_soft_failed}
                AND ? < expires_at
                AND ? < stream_id
                AND stream_id <= ?
                AND {room_id_in_list_clause}
            ORDER BY stream_id ASC
            {limit_clause}
            """,
            (now, from_id, to_id, *room_id_in_list_values, *limit_params),
        )
        return cast(list[tuple[int, str, str]], txn.fetchall())

    async def get_updated_sticky_events(
        self, *, from_id: int, to_id: int, limit: int
    ) -> list[StickyEventUpdate]:
        """Get updates to sticky events between two stream IDs.

        Bounds: from_id < ... <= to_id

        Args:
            from_id: The starting stream ID (exclusive)
            to_id: The ending stream ID (inclusive)
            limit: The maximum number of rows to return

        Returns:
            list of StickyEventUpdate update rows
        """

        if not self.hs.config.experimental.msc4354_enabled:
            # We need to prevent `_get_updated_sticky_events_txn`
            # from running when MSC4354 is turned off, because the query used
            # for SQLite is not compatible with Ubuntu 22.04 (as used in our CI olddeps run).
            # It's technically out of support.
            # See: https://github.com/element-hq/synapse/issues/19428
            return []

        return await self.db_pool.runInteraction(
            "get_updated_sticky_events",
            self._get_updated_sticky_events_txn,
            from_id,
            to_id,
            limit,
        )

    def _get_updated_sticky_events_txn(
        self, txn: LoggingTransaction, from_id: int, to_id: int, limit: int
    ) -> list[StickyEventUpdate]:
        if isinstance(self.database_engine, PostgresEngine):
            expr_soft_failed = "COALESCE(((ej.internal_metadata::jsonb)->>'soft_failed')::boolean, FALSE)"
        else:
            expr_soft_failed = "COALESCE(ej.internal_metadata->>'soft_failed', FALSE)"

        txn.execute(
            f"""
            SELECT se.stream_id, se.room_id, se.event_id,
            {expr_soft_failed} AS "soft_failed"
            FROM sticky_events se
            INNER JOIN event_json ej USING (event_id)
            WHERE ? < stream_id AND stream_id <= ?
            LIMIT ?
            """,
            (from_id, to_id, limit),
        )

        return [
            StickyEventUpdate(
                stream_id=stream_id,
                room_id=room_id,
                event_id=event_id,
                soft_failed=bool(soft_failed),
            )
            for stream_id, room_id, event_id, soft_failed in txn
        ]

    def insert_sticky_events_txn(
        self,
        txn: LoggingTransaction,
        events: list[EventBase],
    ) -> None:
        """
        Insert events into the sticky_events table.

        Skips inserting events:
            - if they are considered spammy by the policy server;
              (unsure if correct, track: https://github.com/matrix-org/matrix-spec-proposals/pull/4354#discussion_r2727593350)
            - if they are considered spammy by a Synapse spam checker module;
            - if they are rejected;
            - if they are outliers (they should be reconsidered for insertion when de-outliered); or
            - if they are not sticky (e.g. if the stickiness expired).

        Note: Soft-failed sticky events ARE inserted, as their soft-failed status
            could be re-evaluated later.

        Skipping the insertion of these types of 'invalid' events is useful for performance reasons because
        they would fill up the table yet we wouldn't show them to clients anyway.

        Since syncing clients can't (easily?) 'skip over' sticky events (due to being in-order, reliably delivered),
        tracking loads of invalid events in the table could make it expensive for servers to retrieve the sticky events that are actually valid.

        For instance, someone spamming 1000s of rejected or 'policy_server_spammy' events could clog up this table in a way that means we either
        have to deliver empty payloads to syncing clients, or consider substantially more than 100 events in order to gather a 100-sized batch to send down.
        """

        now_ms = self.clock.time_msec()
        # event, expires_at
        sticky_events: list[tuple[EventBase, int]] = []
        for ev in events:
            # MSC: Note: policy servers and other similar antispam techniques still apply to these events.
            # We don't filter out soft-failed events altogether (in case they get re-evaluated later),
            # so filter out `spam_checker_spammy` events specifically as we don't want to re-evaluate _those_ later.
            if (
                ev.internal_metadata.policy_server_spammy
                or ev.internal_metadata.spam_checker_spammy
            ):
                continue
            # We shouldn't be passed rejected events, but if we do, we filter them out too.
            if ev.rejected_reason is not None:
                continue
            # We can't persist outlier sticky events as we don't know the room state at that event
            if ev.internal_metadata.is_outlier():
                continue
            sticky_duration = ev.sticky_duration()
            if sticky_duration is None:
                continue
            # Calculate the end time as start_time + effective sticky duration
            expires_at = min(ev.origin_server_ts, now_ms) + sticky_duration.as_millis()
            # Filter out already expired sticky events
            if expires_at <= now_ms:
                continue

            sticky_events.append((ev, expires_at))

        if len(sticky_events) == 0:
            return

        logger.info(
            "inserting %d sticky events in room %s",
            len(sticky_events),
            sticky_events[0][0].room_id,
        )

        # Generate stream_ids in one go
        sticky_events_with_ids = zip(
            sticky_events,
            self._sticky_events_id_gen.get_next_mult_txn(txn, len(sticky_events)),
            strict=True,
        )

        self.db_pool.simple_insert_many_txn(
            txn,
            "sticky_events",
            keys=(
                "instance_name",
                "stream_id",
                "room_id",
                "event_id",
                "event_stream_ordering",
                "sender",
                "expires_at",
            ),
            values=[
                (
                    self._instance_name,
                    stream_id,
                    ev.room_id,
                    ev.event_id,
                    ev.internal_metadata.stream_ordering,
                    ev.sender,
                    expires_at,
                )
                for (ev, expires_at), stream_id in sticky_events_with_ids
            ],
        )

    async def compute_sticky_events_to_un_soft_fail(
        self,
        room_id: str,
        events_and_contexts: list[EventPersistencePair],
        state_delta_for_room: DeltaState,
    ) -> set[str]:
        """
        Determine which soft-failed sticky events in the given room will become
        un-soft-failed once `state_delta_for_room` has been applied to the current state.

        As per MSC4354:
        > **Re-evaluate soft-failure** of soft-failed unexpired sticky events when the membership state of the sender changes.[^softfail]
        >
        > [^softfail]: Not all servers will agree on soft-failure status due to the check considering the “current state” of the room.
        > To ensure all servers agree on which events are sticky, we need to re-evaluate soft-failed status when the current room state changes.
        > This becomes particularly important when room state is rolled back. For example, if Charlie sends some sticky event E and
        > then Bob kicks Charlie, but concurrently Alice kicks Bob then whether or not a receiving server would accept E would depend
        > on whether they saw “Alice kicks Bob” or “Bob kicks Charlie”. If they saw “Alice kicks Bob” then E would be accepted. If they
        > saw “Bob kicks Charlie” then E would be rejected, and would need to be rolled back when they see “Alice kicks Bob”.
        >
        > — https://github.com/matrix-org/matrix-spec-proposals/blob/4ad14b0cd3b09205dcba59e45cbf1cab1e75edf7/proposals/4354-sticky-events.md#L95

        Must be called from within the per-room event persistence critical section (see
        `_EventPeristenceQueue`) and immediately before the persist transaction, so that
        nothing else can change the room's current state in the meantime.

        Args:
            room_id: The room that all of the events belong to
            events_and_contexts: The events about to be persisted. These are not eligible
                for re-evaluation.
            state_delta_for_room: The changes about to be made to the current state, used
                to detect if we need to re-evaluate soft-failed sticky events.

        Returns:
            The event IDs of sticky events which are currently recorded as soft-failed
            but which pass auth against the new current state.
        """
        assert self._can_write_to_sticky_events

        # Fetch the soft-failed sticky events to recheck
        event_ids_to_check = await self._get_soft_failed_sticky_events_to_recheck(
            room_id, state_delta_for_room
        )
        # Defensively filter out soft-failed events in events_and_contexts: they haven't been
        # inserted into `sticky_events` yet, but be defensive in case we are asked to
        # re-persist an event which is already there (e.g. de-outliering), as their
        # soft failure status won't have changed for them.
        persisting_event_ids = {ev.event_id for ev, _ in events_and_contexts}
        event_ids_to_check = [
            event_id
            for event_id in event_ids_to_check
            if event_id not in persisting_event_ids
        ]
        if not event_ids_to_check:
            return set()

        events_to_check = await self.get_events(
            event_ids_to_check, allow_rejected=False
        )

        # Calculate what (state event type, state key) tuples are needed as auth events for the
        # soft-failed events we are reconsidering?
        # e.g. [('m.room.member', '@user:example.org'), ('m.room.power_levels', ''), ...]
        needed_state_tuples_for_auth: set[StateKey] = set()
        for soft_failed_event in events_to_check.values():
            needed_state_tuples_for_auth.update(
                event_auth.auth_types_for_event(
                    soft_failed_event.room_version, soft_failed_event
                )
            )

        # Load the needed auth state from the current state
        # (type, state key) -> event_id
        current_auth_state_ids_map = dict(
            await self.get_partial_filtered_current_state_ids(
                room_id, StateFilter.from_types(needed_state_tuples_for_auth)
            )
        )
        # `state_delta_for_room` hasn't yet been applied to the room's persisted current state,
        # so we need to apply it here to the auth state we are using for the re-evaluation
        for deleted_key in state_delta_for_room.to_delete:
            current_auth_state_ids_map.pop(deleted_key, None)
        for inserted_key, inserted_event_id in state_delta_for_room.to_insert.items():
            if inserted_key in needed_state_tuples_for_auth:
                current_auth_state_ids_map[inserted_key] = inserted_event_id

        # Now load in the auth events
        persisting_events_by_id = {ev.event_id: ev for ev, _ in events_and_contexts}
        current_auth_events: list[EventBase] = []
        current_auth_state_event_ids_to_fetch: list[str] = []
        for event_id in current_auth_state_ids_map.values():
            persisting_event = persisting_events_by_id.get(event_id)
            if persisting_event is not None:
                # This event is one we are about to persist, so just use it
                current_auth_events.append(persisting_event)
            else:
                # This event needs to be loaded from the database
                current_auth_state_event_ids_to_fetch.append(event_id)
        current_auth_events.extend(
            await self.get_events_as_list(current_auth_state_event_ids_to_fetch)
        )

        passing_event_ids: set[str] = set()
        for soft_failed_event in events_to_check.values():
            try:
                # We don't need to check_state_independent_auth_rules as that doesn't depend on room state,
                # so if it passed once it'll pass again.
                event_auth.check_state_dependent_auth_rules(
                    soft_failed_event, current_auth_events
                )

                # Ready to be un-soft-failed
                passing_event_ids.add(soft_failed_event.event_id)
            except AuthError:
                # state-dependent auth rules still unsatisfied: remain soft-failed
                pass

        if passing_event_ids:
            logger.info(
                "%s soft-failed events now pass current state checks in room %s : %s",
                len(passing_event_ids),
                room_id,
                shortstr(passing_event_ids),
            )

        return passing_event_ids

    async def _get_soft_failed_sticky_events_to_recheck(
        self,
        room_id: str,
        state_delta_for_room: DeltaState,
    ) -> list[str]:
        """
        Fetch soft-failed sticky events which should be rechecked against the current state.

        Returns:
            A list of event IDs to recheck
        """

        if state_delta_for_room.no_longer_in_room:
            # We're leaving the room, so the current state is about to be wiped and
            # nothing can pass auth against it.
            return []

        # Only a change to critical auth state may change soft failure status.
        # This means any changes to join rules, power levels or member events.
        # If the state has changed but these types are unchanged, we don't need to recheck.
        CRITICAL_AUTH_TYPES = (
            EventTypes.JoinRules,
            EventTypes.PowerLevels,
            EventTypes.Member,
        )

        critical_auth_types_changed = {
            typ
            for typ, _ in chain(
                state_delta_for_room.to_insert, state_delta_for_room.to_delete
            )
            if typ in CRITICAL_AUTH_TYPES
        }
        if len(critical_auth_types_changed) == 0:
            # No change to critical auth events.
            # No way soft failure status could be different.
            return []

        if critical_auth_types_changed == {EventTypes.Member}:
            # If the only critical auth state that changed is user memberships,
            # then we can restrict our re-evaluation to only reconsider soft-failed sticky events sent
            # by the users who have their membership changed.
            # Events sent by any other user can not be affected,
            # with the pedantic yet possible exception of sticky invite/kick/ban `m.room.member`
            # state events (where state key ≠ sender).
            # That said: we don't expect to use those and it is not possible to create one with
            # the Client-Server API.
            changed_members = {
                membership_user_id
                for event_type, membership_user_id in chain(
                    state_delta_for_room.to_insert, state_delta_for_room.to_delete
                )
                if event_type == EventTypes.Member
            }

            return await self.db_pool.runInteraction(
                "_get_soft_failed_sticky_events_to_recheck_members",
                self._get_soft_failed_sticky_events_txn,
                room_id,
                # Only reconsider events from changed members
                senders=changed_members,
            )

        # If we reach here, then it must be the case that there have been changes in
        # power level or join rules.
        # In both of these cases we want to re-evaluate soft failure status of all the
        # soft-failed events in the room.
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
        return await self.db_pool.runInteraction(
            "_get_soft_failed_sticky_events_to_recheck",
            self._get_soft_failed_sticky_events_txn,
            room_id,
            # Consider everyone
            senders=None,
        )

    def _get_soft_failed_sticky_events_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        *,
        senders: Collection[str] | None,
    ) -> list[str]:
        """
        Fetch the event IDs of (unexpired) soft-failed sticky events in a room.

        Args:
            room_id: the room to look in.
            senders:
                If present, only return sticky events sent by one of these users.
                If None, do not restrict by sender.
        """
        sender_clause = ""
        sender_args: Collection[str] = ()
        if senders is not None:
            if not senders:
                return []
            sender_sql, sender_args = make_in_list_sql_clause(
                txn.database_engine, "se.sender", senders
            )
            sender_clause = f"AND {sender_sql}"

        if isinstance(self.database_engine, PostgresEngine):
            expr_soft_failed = "COALESCE(((ej.internal_metadata::jsonb)->>'soft_failed')::boolean, FALSE)"
        else:
            expr_soft_failed = "COALESCE(ej.internal_metadata->>'soft_failed', FALSE)"

        # Note that we are relying on the 1h stickiness limit to make this
        # tractable, as we can't realistically apply any LIMIT here.
        txn.execute(
            f"""
            SELECT se.event_id
            FROM sticky_events se
            INNER JOIN event_json ej USING (event_id)
            WHERE
                se.room_id = ?
                AND ? < se.expires_at
                AND {expr_soft_failed}
                {sender_clause}
            """,
            (room_id, self.clock.time_msec(), *sender_args),
        )
        return [event_id for (event_id,) in txn]

    def un_soft_fail_sticky_events_txn(
        self, txn: LoggingTransaction, sticky_event_ids: Set[str]
    ) -> None:
        """
        For the given soft-failed sticky events:

        - removes their soft-failed status
        - moves them to the end of the `sticky_events` stream so that clients get told about them
        """
        if not sticky_event_ids:
            return

        # Update the internal metadata on the event itself.
        event_id_in_list_clause, event_id_in_list_args = make_in_list_sql_clause(
            txn.database_engine,
            "event_id",
            sticky_event_ids,
        )
        if isinstance(txn.database_engine, PostgresEngine):
            # It's a bit sad that internal_metadata is TEXT and not JSONB...
            txn.execute(
                f"""
                UPDATE event_json
                SET internal_metadata = (
                    jsonb_set(internal_metadata::jsonb, '{{soft_failed}}', 'false'::jsonb)
                )::text
                WHERE {event_id_in_list_clause}
                """,
                event_id_in_list_args,
            )
        else:
            assert isinstance(txn.database_engine, Sqlite3Engine)
            txn.execute(
                f"""
                UPDATE event_json
                SET internal_metadata = json_set(internal_metadata, '$.soft_failed', json('false'))
                WHERE {event_id_in_list_clause}
                """,
                event_id_in_list_args,
            )

        # Invalidate caches as a result
        for event_id in sticky_event_ids:
            self.invalidate_get_event_cache_after_txn(txn, event_id)

        # Move the events to the end of the sticky events stream
        new_stream_ids = self._sticky_events_id_gen.get_next_mult_txn(
            txn, len(sticky_event_ids)
        )
        self.db_pool.simple_update_many_txn(
            txn,
            table="sticky_events",
            key_names=("event_id",),
            key_values=[(event_id,) for event_id in sticky_event_ids],
            value_names=(
                "stream_id",
                "instance_name",
            ),
            value_values=[
                (stream_id, self._instance_name) for stream_id in new_stream_ids
            ],
        )

    async def _delete_expired_sticky_events(self) -> None:
        await self.db_pool.runInteraction(
            "_delete_expired_sticky_events",
            self._delete_expired_sticky_events_txn,
            self.clock.time_msec(),
        )

    def _delete_expired_sticky_events_txn(
        self, txn: LoggingTransaction, now: int
    ) -> None:
        """
        From the `sticky_events` table, deletes all entries whose expiry is in the past
        (older than `now`).

        This is fine because we don't consider the events as sticky anymore when that's
        happened.
        """
        txn.execute(
            """
            DELETE FROM sticky_events WHERE expires_at < ?
            """,
            (now,),
        )

    def _run_background_cleanup(self) -> Deferred:
        return self.hs.run_as_background_process(
            "delete_expired_sticky_events",
            self._delete_expired_sticky_events,
        )
