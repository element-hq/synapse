#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
import abc
import heapq
import logging
import threading
from collections import OrderedDict
from contextlib import contextmanager
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    AsyncContextManager,
    ContextManager,
    Dict,
    Generator,
    Generic,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

import attr
from sortedcontainers import SortedList, SortedSet

from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.types import Cursor
from synapse.storage.util.sequence import PostgresSequenceGenerator

if TYPE_CHECKING:
    from synapse.notifier import ReplicationNotifier

logger = logging.getLogger(__name__)


T = TypeVar("T")


class IdGenerator:
    def __init__(
        self,
        db_conn: LoggingDatabaseConnection,
        table: str,
        column: str,
    ):
        self._lock = threading.Lock()
        self._next_id = _load_current_id(db_conn, table, column)

    def get_next(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id


def _load_current_id(
    db_conn: LoggingDatabaseConnection, table: str, column: str, step: int = 1
) -> int:
    cur = db_conn.cursor(txn_name="_load_current_id")
    if step == 1:
        cur.execute("SELECT MAX(%s) FROM %s" % (column, table))
    else:
        cur.execute("SELECT MIN(%s) FROM %s" % (column, table))
    result = cur.fetchone()
    assert result is not None
    (val,) = result
    cur.close()
    current_id = int(val) if val else step
    res = (max if step > 0 else min)(current_id, step)
    logger.info("Initialising stream generator for %s(%s): %i", table, column, res)
    return res


class AbstractStreamIdGenerator(metaclass=abc.ABCMeta):
    """Generates or tracks stream IDs for a stream that may have multiple writers.

    Each stream ID represents a write transaction, whose completion is tracked
    so that the "current" stream ID of the stream can be determined.

    Stream IDs are monotonically increasing or decreasing integers representing write
    transactions. The "current" stream ID is the stream ID such that all transactions
    with equal or smaller stream IDs have completed. Since transactions may complete out
    of order, this is not the same as the stream ID of the last completed transaction.

    Completed transactions include both committed transactions and transactions that
    have been rolled back.
    """

    @abc.abstractmethod
    def advance(self, instance_name: str, new_id: int) -> None:
        """Advance the position of the named writer to the given ID, if greater
        than existing entry.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def get_current_token(self) -> int:
        """Returns the maximum stream id such that all stream ids less than or
        equal to it have been successfully persisted.

        Returns:
            The maximum stream id.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def get_current_token_for_writer(self, instance_name: str) -> int:
        """Returns the position of the given writer.

        For streams with single writers this is equivalent to `get_current_token`.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def get_minimal_local_current_token(self) -> int:
        """Tries to return a minimal current token for the local instance,
        i.e. for writers this would be the last successful write.

        If local instance is not a writer (or has written yet) then falls back
        to returning the normal "current token".
        """

    @abc.abstractmethod
    def get_next(self) -> AsyncContextManager[int]:
        """
        Usage:
            async with stream_id_gen.get_next() as stream_id:
                # ... persist event ...
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def get_next_mult(self, n: int) -> AsyncContextManager[Sequence[int]]:
        """
        Usage:
            async with stream_id_gen.get_next(n) as stream_ids:
                # ... persist events ...
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def get_next_txn(self, txn: LoggingTransaction) -> int:
        """
        Usage:
            stream_id_gen.get_next_txn(txn)
            # ... persist events ...
        """
        raise NotImplementedError()


class StreamIdGenerator(AbstractStreamIdGenerator):
    """Generates and tracks stream IDs for a stream with a single writer.

    This class must only be used when the current Synapse process is the sole
    writer for a stream.

    Args:
        db_conn(connection):  A database connection to use to fetch the
            initial value of the generator from.
        table(str): A database table to read the initial value of the id
            generator from.
        column(str): The column of the database table to read the initial
            value from the id generator from.
        extra_tables(list): List of pairs of database tables and columns to
            use to source the initial value of the generator from. The value
            with the largest magnitude is used.
        step(int): which direction the stream ids grow in. +1 to grow
            upwards, -1 to grow downwards.

    Usage:
        async with stream_id_gen.get_next() as stream_id:
            # ... persist event ...
    """

    def __init__(
        self,
        db_conn: LoggingDatabaseConnection,
        notifier: "ReplicationNotifier",
        table: str,
        column: str,
        extra_tables: Iterable[Tuple[str, str]] = (),
        step: int = 1,
        is_writer: bool = True,
    ) -> None:
        assert step != 0
        self._lock = threading.Lock()
        self._step: int = step
        self._current: int = _load_current_id(db_conn, table, column, step)
        self._is_writer = is_writer
        for table, column in extra_tables:
            self._current = (max if step > 0 else min)(
                self._current, _load_current_id(db_conn, table, column, step)
            )

        # We use this as an ordered set, as we want to efficiently append items,
        # remove items and get the first item. Since we insert IDs in order, the
        # insertion ordering will ensure its in the correct ordering.
        #
        # The key and values are the same, but we never look at the values.
        self._unfinished_ids: OrderedDict[int, int] = OrderedDict()

        self._notifier = notifier

    def advance(self, instance_name: str, new_id: int) -> None:
        # Advance should never be called on a writer instance, only over replication
        if self._is_writer:
            raise Exception("Replication is not supported by writer StreamIdGenerator")

        self._current = (max if self._step > 0 else min)(self._current, new_id)

    def get_next(self) -> AsyncContextManager[int]:
        with self._lock:
            self._current += self._step
            next_id = self._current

            self._unfinished_ids[next_id] = next_id

        @contextmanager
        def manager() -> Generator[int, None, None]:
            try:
                yield next_id
            finally:
                with self._lock:
                    self._unfinished_ids.pop(next_id)

                self._notifier.notify_replication()

        return _AsyncCtxManagerWrapper(manager())

    def get_next_mult(self, n: int) -> AsyncContextManager[Sequence[int]]:
        with self._lock:
            next_ids = range(
                self._current + self._step,
                self._current + self._step * (n + 1),
                self._step,
            )
            self._current += n * self._step

            for next_id in next_ids:
                self._unfinished_ids[next_id] = next_id

        @contextmanager
        def manager() -> Generator[Sequence[int], None, None]:
            try:
                yield next_ids
            finally:
                with self._lock:
                    for next_id in next_ids:
                        self._unfinished_ids.pop(next_id)

                self._notifier.notify_replication()

        return _AsyncCtxManagerWrapper(manager())

    def get_next_txn(self, txn: LoggingTransaction) -> int:
        """
        Retrieve the next stream ID from within a database transaction.

        Clean-up functions will be called when the transaction finishes.

        Args:
            txn: The database transaction object.

        Returns:
            The next stream ID.
        """
        if not self._is_writer:
            raise Exception("Tried to allocate stream ID on non-writer")

        # Get the next stream ID.
        with self._lock:
            self._current += self._step
            next_id = self._current

            self._unfinished_ids[next_id] = next_id

        def clear_unfinished_id(id_to_clear: int) -> None:
            """A function to mark processing this ID as finished"""
            with self._lock:
                self._unfinished_ids.pop(id_to_clear)

        # Mark this ID as finished once the database transaction itself finishes.
        txn.call_after(clear_unfinished_id, next_id)
        txn.call_on_exception(clear_unfinished_id, next_id)

        # Return the new ID.
        return next_id

    def get_current_token(self) -> int:
        if not self._is_writer:
            return self._current

        with self._lock:
            if self._unfinished_ids:
                return next(iter(self._unfinished_ids)) - self._step

            return self._current

    def get_current_token_for_writer(self, instance_name: str) -> int:
        return self.get_current_token()

    def get_minimal_local_current_token(self) -> int:
        return self.get_current_token()


class MultiWriterIdGenerator(AbstractStreamIdGenerator):
    """Generates and tracks stream IDs for a stream with multiple writers.

    Uses a Postgres sequence to coordinate ID assignment, but positions of other
    writers will only get updated when `advance` is called (by replication).

    Note: Only works with Postgres.

    Args:
        db_conn
        db
        instance_name: The name of this instance.
        tables: List of tables associated with the stream. Tuple of table
            name, column name that stores the writer's instance name, and
            column name that stores the stream ID.
        sequence_name: The name of the postgres sequence used to generate new
            IDs.
        writers: A list of known writers to use to populate current positions
            on startup. Can be empty if nothing uses `get_current_token` or
            `get_positions` (e.g. caches stream).
        positive: Whether the IDs are positive (true) or negative (false).
            When using negative IDs we go backwards from -1 to -2, -3, etc.
    """

    def __init__(
        self,
        db_conn: LoggingDatabaseConnection,
        db: DatabasePool,
        notifier: "ReplicationNotifier",
        instance_name: str,
        tables: List[Tuple[str, str, str]],
        sequence_name: str,
        writers: List[str],
        positive: bool = True,
    ) -> None:
        self._db = db
        self._notifier = notifier
        self._instance_name = instance_name
        self._positive = positive
        self._writers = writers
        self._return_factor = 1 if positive else -1

        # We lock as some functions may be called from DB threads.
        self._lock = threading.Lock()

        # Note: If we are a negative stream then we still store all the IDs as
        # positive to make life easier for us, and simply negate the IDs when we
        # return them.
        self._current_positions: Dict[str, int] = {}

        # Set of local IDs that we're still processing. The current position
        # should be less than the minimum of this set (if not empty).
        self._unfinished_ids: SortedSet[int] = SortedSet()

        # We also need to track when we've requested some new stream IDs but
        # they haven't yet been added to the `_unfinished_ids` set. Every time
        # we request a new stream ID we add the current max stream ID to the
        # list, and remove it once we've added the newly allocated IDs to the
        # `_unfinished_ids` set. This means that we *may* be allocated stream
        # IDs above those in the list, and so we can't advance the local current
        # position beyond the minimum stream ID in this list.
        self._in_flight_fetches: SortedList[int] = SortedList()

        # Set of local IDs that we've processed that are larger than the current
        # position, due to there being smaller unpersisted IDs.
        self._finished_ids: Set[int] = set()

        # We track the max position where we know everything before has been
        # persisted. This is done by a) looking at the min across all instances
        # and b) noting that if we have seen a run of persisted positions
        # without gaps (e.g. 5, 6, 7) then we can skip forward (e.g. to 7).
        #
        # Note: There is no guarantee that the IDs generated by the sequence
        # will be gapless; gaps can form when e.g. a transaction was rolled
        # back. This means that sometimes we won't be able to skip forward the
        # position even though everything has been persisted. However, since
        # gaps should be relatively rare it's still worth doing the bookkeeping
        # that allows us to skip forwards when there are gapless runs of
        # positions.
        #
        # We start at 1 here as a) the first generated stream ID will be 2, and
        # b) other parts of the code assume that stream IDs are strictly greater
        # than 0.
        self._persisted_upto_position = 1
        self._known_persisted_positions: List[int] = []

        # The maximum stream ID that we have seen been allocated across any writer.
        self._max_seen_allocated_stream_id = 1

        # The maximum position of the local instance. This can be higher than
        # the corresponding position in `current_positions` table when there are
        # no active writes in progress.
        self._max_position_of_local_instance = self._max_seen_allocated_stream_id

        self._sequence_gen = PostgresSequenceGenerator(sequence_name)

        # We check that the table and sequence haven't diverged.
        for table, _, id_column in tables:
            self._sequence_gen.check_consistency(
                db_conn,
                table=table,
                id_column=id_column,
                positive=positive,
            )

        # This goes and fills out the above state from the database.
        self._load_current_ids(db_conn, tables)

        self._max_seen_allocated_stream_id = max(
            self._current_positions.values(), default=1
        )

        # For the case where `stream_positions` is not up-to-date,
        # `_persisted_upto_position` may be higher.
        self._max_seen_allocated_stream_id = max(
            self._max_seen_allocated_stream_id, self._persisted_upto_position
        )

        # Bump our local maximum position now that we've loaded things from the
        # DB.
        self._max_position_of_local_instance = self._max_seen_allocated_stream_id

        if not writers:
            # If there have been no explicit writers given then any instance can
            # write to the stream. In which case, lets pre-seed our own
            # position with the current minimum.
            self._current_positions[self._instance_name] = self._persisted_upto_position

    def _load_current_ids(
        self,
        db_conn: LoggingDatabaseConnection,
        tables: List[Tuple[str, str, str]],
    ) -> None:
        cur = db_conn.cursor(txn_name="_load_current_ids")
        max_stream_id = 1

        # Load the current positions of all writers for the stream from its provided
        # table. Differentiate between(potentially) multiple workers for a stream by the
        # instance_name. For a multiple event writer example:
        # event_persister1 could be at 6
        # but event_persister2 could be at 3
        # For the case of no explicit writers, this will still pull the data for
        # populating max_stream_id.
        rows: List[Tuple[str, int]] = []
        for table, instance_column, id_column in tables:
            sql = """
                SELECT %(instance)s, %(agg)s(%(id)s) FROM %(table)s
                GROUP BY %(instance)s
            """ % {
                "id": id_column,
                "table": table,
                "instance": instance_column,
                "agg": "MAX" if self._positive else "MIN",
            }
            cur.execute(sql)

            # Cast safety: this corresponds to the types returned by the query above.
            rows.extend(cast(Iterable[Tuple[str, int]], cur))

        # This is filtered by writer...
        self._current_positions = {
            instance: stream_id * self._return_factor
            for instance, stream_id in rows
            if instance in self._writers
        }

        # ...where this is not
        for _, stream_id in rows:
            max_stream_id = max(max_stream_id, stream_id)

        # We set the `_persisted_upto_position` to be the minimum of all current
        # positions. If empty we use the max stream ID from the DB table.

        # Note some oddities around writer instances that disappear then reappear.
        # For example with an events stream writer:
        #  1. Using the main process to persist events
        #  2. Declaring a separate events worker for some time
        #  3. Resetting back to the main process
        # It should resolve itself the first time (3) writes a row, thereby advancing
        # its position.

        min_stream_id = min(self._current_positions.values(), default=None)

        if min_stream_id is None:
            # A min_stream_id being None means:
            # 1. This table has never been written to
            # 2. Whatever stream writer wrote to it is no longer available
            # 3. It doesn't matter if the writer position is tracked(see cache
            #    invalidation stream for example)
            # Use the max_stream_id as all of those stream ids will be guaranteed
            # to be persisted or will default to 1.
            # TODO: is this next comment still valid?
            # (This can be a problem for e.g. backfill streams where the server has
            # never backfilled).

            self._persisted_upto_position = max_stream_id
        else:
            # If we have a min_stream_id then we pull out everything greater
            # than it from the DB so that we can prefill
            # `_known_persisted_positions` and get a more accurate
            # `_persisted_upto_position`.

            self._persisted_upto_position = min_stream_id

            rows = []
            for table, instance_column, id_column in tables:
                sql = """
                    SELECT %(instance)s, %(id)s FROM %(table)s
                    WHERE ? %(cmp)s %(id)s
                """ % {
                    "id": id_column,
                    "table": table,
                    "instance": instance_column,
                    "cmp": "<=" if self._positive else ">=",
                }
                cur.execute(sql, (min_stream_id * self._return_factor,))

                # Cast safety: this corresponds to the types returned by the query above
                rows.extend(cast(Iterable[Tuple[str, int]], cur))

            with self._lock:
                for (
                    _,
                    stream_id,
                ) in rows:
                    stream_id = self._return_factor * stream_id
                    self._add_persisted_position(stream_id)

        if self._writers:
            # If we have explicit writers then make sure that each instance has
            # a position.
            for writer in self._writers:
                self._current_positions.setdefault(
                    writer, self._persisted_upto_position
                )

        cur.close()

    def _load_next_id_txn(self, txn: Cursor) -> int:
        stream_ids = self._load_next_mult_id_txn(txn, 1)
        return stream_ids[0]

    def _load_next_mult_id_txn(self, txn: Cursor, n: int) -> List[int]:
        # We need to track that we've requested some more stream IDs, and what
        # the current max allocated stream ID is. This is to prevent a race
        # where we've been allocated stream IDs but they have not yet been added
        # to the `_unfinished_ids` set, allowing the current position to advance
        # past them.
        with self._lock:
            current_max = self._max_seen_allocated_stream_id
            self._in_flight_fetches.add(current_max)

        try:
            stream_ids = self._sequence_gen.get_next_mult_txn(txn, n)

            with self._lock:
                self._unfinished_ids.update(stream_ids)
                self._max_seen_allocated_stream_id = max(
                    self._max_seen_allocated_stream_id, self._unfinished_ids[-1]
                )
        finally:
            with self._lock:
                self._in_flight_fetches.remove(current_max)

        return stream_ids

    def get_next(self) -> AsyncContextManager[int]:
        # If we have a list of instances that are allowed to write to this
        # stream, make sure we're in it.
        if self._writers and self._instance_name not in self._writers:
            raise Exception("Tried to allocate stream ID on non-writer")

        # Cast safety: the second argument to _MultiWriterCtxManager, multiple_ids,
        # controls the return type. If `None` or omitted, the context manager yields
        # a single integer stream_id; otherwise it yields a list of stream_ids.
        return cast(
            AsyncContextManager[int], _MultiWriterCtxManager(self, self._notifier)
        )

    def get_next_mult(self, n: int) -> AsyncContextManager[List[int]]:
        # If we have a list of instances that are allowed to write to this
        # stream, make sure we're in it.
        if self._writers and self._instance_name not in self._writers:
            raise Exception("Tried to allocate stream ID on non-writer")

        # Cast safety: see get_next.
        return cast(
            AsyncContextManager[List[int]],
            _MultiWriterCtxManager(self, self._notifier, n),
        )

    def get_next_txn(self, txn: LoggingTransaction) -> int:
        """
        Usage:

            stream_id = stream_id_gen.get_next_txn(txn)
            # ... persist event ...
        """

        # If we have a list of instances that are allowed to write to this
        # stream, make sure we're in it.
        if self._writers and self._instance_name not in self._writers:
            raise Exception("Tried to allocate stream ID on non-writer")

        next_id = self._load_next_id_txn(txn)

        txn.call_after(self._mark_ids_as_finished, [next_id])
        txn.call_on_exception(self._mark_ids_as_finished, [next_id])
        txn.call_after(self._notifier.notify_replication)

        return self._return_factor * next_id

    def get_next_mult_txn(self, txn: LoggingTransaction, n: int) -> List[int]:
        """
        Usage:

            stream_id = stream_id_gen.get_next_txn(txn)
            # ... persist event ...
        """

        # If we have a list of instances that are allowed to write to this
        # stream, make sure we're in it.
        if self._writers and self._instance_name not in self._writers:
            raise Exception("Tried to allocate stream ID on non-writer")

        next_ids = self._load_next_mult_id_txn(txn, n)

        txn.call_after(self._mark_ids_as_finished, next_ids)
        txn.call_on_exception(self._mark_ids_as_finished, next_ids)
        txn.call_after(self._notifier.notify_replication)

        return [self._return_factor * next_id for next_id in next_ids]

    def _mark_ids_as_finished(self, next_ids: List[int]) -> None:
        """These IDs have finished being processed so we should advance the
        current position if possible.
        """

        with self._lock:
            self._unfinished_ids.difference_update(next_ids)
            self._finished_ids.update(next_ids)

            new_cur: Optional[int] = None

            if self._unfinished_ids or self._in_flight_fetches:
                # If there are unfinished IDs then the new position will be the
                # largest finished ID strictly less than the minimum unfinished
                # ID.

                # The minimum unfinished ID needs to take account of both
                # `_unfinished_ids` and `_in_flight_fetches`.
                if self._unfinished_ids and self._in_flight_fetches:
                    # `_in_flight_fetches` stores the maximum safe stream ID, so
                    # we add one to make it equivalent to the minimum unsafe ID.
                    min_unfinished = min(
                        self._unfinished_ids[0], self._in_flight_fetches[0] + 1
                    )
                elif self._in_flight_fetches:
                    min_unfinished = self._in_flight_fetches[0] + 1
                else:
                    min_unfinished = self._unfinished_ids[0]

                finished = set()
                for s in self._finished_ids:
                    if s < min_unfinished:
                        if new_cur is None or new_cur < s:
                            new_cur = s
                    else:
                        finished.add(s)

                # We clear these out since they're now all less than the new
                # position.
                self._finished_ids = finished
            else:
                # There are no unfinished IDs so the new position is simply the
                # largest finished one.
                new_cur = max(self._finished_ids)

                # We clear these out since they're now all less than the new
                # position.
                self._finished_ids.clear()

            if new_cur:
                curr = self._current_positions.get(self._instance_name, 0)
                self._current_positions[self._instance_name] = max(curr, new_cur)
                self._max_position_of_local_instance = max(
                    curr, new_cur, self._max_position_of_local_instance
                )

            # TODO Can we call this for just the last position or somehow batch
            # _add_persisted_position.
            for next_id in next_ids:
                self._add_persisted_position(next_id)

    def get_current_token(self) -> int:
        return self.get_persisted_upto_position()

    def get_current_token_for_writer(self, instance_name: str) -> int:
        # If we don't have an entry for the given instance name, we assume it's a
        # new writer.
        #
        # For new writers we assume their initial position to be the current
        # persisted up to position. This stops Synapse from doing a full table
        # scan when a new writer announces itself over replication.
        with self._lock:
            if self._instance_name == instance_name:
                return self._return_factor * self._max_position_of_local_instance

            pos = self._current_positions.get(
                instance_name, self._persisted_upto_position
            )

            # We want to return the maximum "current token" that we can for a
            # writer, this helps ensure that streams progress as fast as
            # possible.
            pos = max(pos, self._persisted_upto_position)

            return self._return_factor * pos

    def get_minimal_local_current_token(self) -> int:
        with self._lock:
            return self._return_factor * self._current_positions.get(
                self._instance_name, self._persisted_upto_position
            )

    def get_positions(self) -> Dict[str, int]:
        """Get a copy of the current positon map.

        Note that this won't necessarily include all configured writers if some
        writers haven't written anything yet.
        """

        with self._lock:
            return {
                name: self._return_factor * i
                for name, i in self._current_positions.items()
            }

    def advance(self, instance_name: str, new_id: int) -> None:
        new_id *= self._return_factor

        with self._lock:
            self._current_positions[instance_name] = max(
                new_id, self._current_positions.get(instance_name, 0)
            )

            self._max_seen_allocated_stream_id = max(
                self._max_seen_allocated_stream_id, new_id
            )

            self._add_persisted_position(new_id)

    def get_persisted_upto_position(self) -> int:
        """Get the max position where all previous positions have been
        persisted.

        Note: In the worst case scenario this will be equal to the minimum
        position across writers. This means that the returned position here can
        lag if one writer doesn't write very often.
        """

        with self._lock:
            return self._return_factor * self._persisted_upto_position

    def _add_persisted_position(self, new_id: int) -> None:
        """Record that we have persisted a position.

        This is used to keep the `_current_positions` up to date.
        """

        # We require that the lock is locked by caller
        assert self._lock.locked()

        heapq.heappush(self._known_persisted_positions, new_id)

        # We move the current min position up if the minimum current positions
        # of all instances is higher (since by definition all positions less
        # that that have been persisted).
        our_current_position = self._current_positions.get(self._instance_name, 0)
        min_curr = min(
            (
                token
                for name, token in self._current_positions.items()
                if name != self._instance_name
            ),
            default=our_current_position,
        )

        if our_current_position and (self._unfinished_ids or self._in_flight_fetches):
            min_curr = min(min_curr, our_current_position)

        self._persisted_upto_position = max(min_curr, self._persisted_upto_position)

        # Advance our local max position.
        self._max_position_of_local_instance = max(
            self._max_position_of_local_instance, self._persisted_upto_position
        )

        if not self._unfinished_ids and not self._in_flight_fetches:
            # If we don't have anything in flight, it's safe to advance to the
            # max seen stream ID.
            self._max_position_of_local_instance = max(
                self._max_seen_allocated_stream_id, self._max_position_of_local_instance
            )

        # We now iterate through the seen positions, discarding those that are
        # less than the current min positions, and incrementing the min position
        # if its exactly one greater.
        #
        # This is also where we discard items from `_known_persisted_positions`
        # (to ensure the list doesn't infinitely grow).
        while self._known_persisted_positions:
            if self._known_persisted_positions[0] <= self._persisted_upto_position:
                heapq.heappop(self._known_persisted_positions)
            elif (
                self._known_persisted_positions[0] == self._persisted_upto_position + 1
            ):
                heapq.heappop(self._known_persisted_positions)
                self._persisted_upto_position += 1
            else:
                # There was a gap in seen positions, so there is nothing more to
                # do.
                break


@attr.s(frozen=True, auto_attribs=True)
class _AsyncCtxManagerWrapper(Generic[T]):
    """Helper class to convert a plain context manager to an async one.

    This is mainly useful if you have a plain context manager but the interface
    requires an async one.
    """

    inner: ContextManager[T]

    async def __aenter__(self) -> T:
        return self.inner.__enter__()

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> Optional[bool]:
        return self.inner.__exit__(exc_type, exc, tb)


@attr.s(slots=True, auto_attribs=True)
class _MultiWriterCtxManager:
    """Async context manager returned by MultiWriterIdGenerator"""

    id_gen: MultiWriterIdGenerator
    notifier: "ReplicationNotifier"
    multiple_ids: Optional[int] = None
    stream_ids: List[int] = attr.Factory(list)

    async def __aenter__(self) -> Union[int, List[int]]:
        # It's safe to run this in autocommit mode as fetching values from a
        # sequence ignores transaction semantics anyway.
        self.stream_ids = await self.id_gen._db.runInteraction(
            "_load_next_mult_id",
            self.id_gen._load_next_mult_id_txn,
            self.multiple_ids or 1,
            db_autocommit=True,
        )

        if self.multiple_ids is None:
            return self.stream_ids[0] * self.id_gen._return_factor
        else:
            return [i * self.id_gen._return_factor for i in self.stream_ids]

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        self.id_gen._mark_ids_as_finished(self.stream_ids)

        self.notifier.notify_replication()

        if exc_type is not None:
            return False

        return False
