#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
import inspect
import logging
import time
import types
from collections import defaultdict
from time import monotonic as monotonic_time
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    cast,
    overload,
)

import attr
from prometheus_client import Counter, Histogram
from typing_extensions import Concatenate, Literal, ParamSpec

from twisted.enterprise import adbapi
from twisted.internet.interfaces import IReactorCore

from synapse.api.errors import StoreError
from synapse.config.database import DatabaseConnectionConfig
from synapse.logging import opentracing
from synapse.logging.context import (
    LoggingContext,
    current_context,
    make_deferred_yieldable,
)
from synapse.metrics import LaterGauge, register_threadpool
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.storage.background_updates import BackgroundUpdater
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine, Sqlite3Engine
from synapse.storage.types import Connection, Cursor, SQLQueryParameters
from synapse.types import StrCollection
from synapse.util.async_helpers import delay_cancellation
from synapse.util.iterutils import batch_iter

if TYPE_CHECKING:
    from synapse.server import HomeServer

# python 3 does not have a maximum int value
MAX_TXN_ID = 2**63 - 1

logger = logging.getLogger(__name__)

sql_logger = logging.getLogger("synapse.storage.SQL")
transaction_logger = logging.getLogger("synapse.storage.txn")
perf_logger = logging.getLogger("synapse.storage.TIME")

sql_scheduling_timer = Histogram("synapse_storage_schedule_time", "sec")

sql_query_timer = Histogram("synapse_storage_query_time", "sec", ["verb"])
sql_txn_count = Counter("synapse_storage_transaction_time_count", "sec", ["desc"])
sql_txn_duration = Counter("synapse_storage_transaction_time_sum", "sec", ["desc"])


# Unique indexes which have been added in background updates. Maps from table name
# to the name of the background update which added the unique index to that table.
#
# This is used by the upsert logic to figure out which tables are safe to do a proper
# UPSERT on: until the relevant background update has completed, we
# have to emulate an upsert by locking the table.
#
UNIQUE_INDEX_BACKGROUND_UPDATES = {
    "user_ips": "user_ips_device_unique_index",
    "device_lists_remote_extremeties": "device_lists_remote_extremeties_unique_idx",
    "device_lists_remote_cache": "device_lists_remote_cache_unique_idx",
    "event_search": "event_search_event_id_idx",
    "local_media_repository_thumbnails": "local_media_repository_thumbnails_method_idx",
    "remote_media_cache_thumbnails": "remote_media_repository_thumbnails_method_idx",
    "event_push_summary": "event_push_summary_unique_index2",
    "receipts_linearized": "receipts_linearized_unique_index",
    "receipts_graph": "receipts_graph_unique_index",
}


class _PoolConnection(Connection):
    """
    A Connection from twisted.enterprise.adbapi.Connection.
    """

    def reconnect(self) -> None: ...


def make_pool(
    reactor: IReactorCore,
    db_config: DatabaseConnectionConfig,
    engine: BaseDatabaseEngine,
) -> adbapi.ConnectionPool:
    """Get the connection pool for the database."""

    # By default enable `cp_reconnect`. We need to fiddle with db_args in case
    # someone has explicitly set `cp_reconnect`.
    db_args = dict(db_config.config.get("args", {}))
    db_args.setdefault("cp_reconnect", True)

    def _on_new_connection(conn: Connection) -> None:
        # Ensure we have a logging context so we can correctly track queries,
        # etc.
        with LoggingContext("db.on_new_connection"):
            engine.on_new_connection(
                LoggingDatabaseConnection(conn, engine, "on_new_connection")
            )

    connection_pool = adbapi.ConnectionPool(
        db_config.config["name"],
        cp_reactor=reactor,
        cp_openfun=_on_new_connection,
        **db_args,
    )

    register_threadpool(f"database-{db_config.name}", connection_pool.threadpool)

    return connection_pool


def make_conn(
    db_config: DatabaseConnectionConfig,
    engine: BaseDatabaseEngine,
    default_txn_name: str,
) -> "LoggingDatabaseConnection":
    """Make a new connection to the database and return it.

    Returns:
        Connection
    """

    db_params = {
        k: v
        for k, v in db_config.config.get("args", {}).items()
        if not k.startswith("cp_")
    }
    native_db_conn = engine.module.connect(**db_params)
    db_conn = LoggingDatabaseConnection(native_db_conn, engine, default_txn_name)

    engine.on_new_connection(db_conn)
    return db_conn


@attr.s(slots=True, auto_attribs=True)
class LoggingDatabaseConnection:
    """A wrapper around a database connection that returns `LoggingTransaction`
    as its cursor class.

    This is mainly used on startup to ensure that queries get logged correctly
    """

    conn: Connection
    engine: BaseDatabaseEngine
    default_txn_name: str

    def cursor(
        self,
        *,
        txn_name: Optional[str] = None,
        after_callbacks: Optional[List["_CallbackListEntry"]] = None,
        async_after_callbacks: Optional[List["_AsyncCallbackListEntry"]] = None,
        exception_callbacks: Optional[List["_CallbackListEntry"]] = None,
    ) -> "LoggingTransaction":
        if not txn_name:
            txn_name = self.default_txn_name

        return LoggingTransaction(
            self.conn.cursor(),
            name=txn_name,
            database_engine=self.engine,
            after_callbacks=after_callbacks,
            async_after_callbacks=async_after_callbacks,
            exception_callbacks=exception_callbacks,
        )

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def __enter__(self) -> "LoggingDatabaseConnection":
        self.conn.__enter__()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[types.TracebackType],
    ) -> Optional[bool]:
        return self.conn.__exit__(exc_type, exc_value, traceback)

    # Proxy through any unknown lookups to the DB conn class.
    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)


# The type of entry which goes on our after_callbacks and exception_callbacks lists.
_CallbackListEntry = Tuple[Callable[..., object], Tuple[object, ...], Dict[str, object]]
_AsyncCallbackListEntry = Tuple[
    Callable[..., Awaitable], Tuple[object, ...], Dict[str, object]
]

P = ParamSpec("P")
R = TypeVar("R")


class LoggingTransaction:
    """An object that almost-transparently proxies for the 'txn' object
    passed to the constructor. Adds logging and metrics to the .execute()
    method.

    Args:
        txn: The database transaction object to wrap.
        name: The name of this transactions for logging.
        database_engine
        after_callbacks: A list that callbacks will be appended to
            that have been added by `call_after` which should be run on
            successful completion of the transaction. None indicates that no
            callbacks should be allowed to be scheduled to run.
        async_after_callbacks: A list that asynchronous callbacks will be appended
            to by `async_call_after` which should run, before after_callbacks, on
            successful completion of the transaction. None indicates that no
            callbacks should be allowed to be scheduled to run.
        exception_callbacks: A list that callbacks will be appended
            to that have been added by `call_on_exception` which should be run
            if transaction ends with an error. None indicates that no callbacks
            should be allowed to be scheduled to run.
    """

    __slots__ = [
        "txn",
        "name",
        "database_engine",
        "after_callbacks",
        "async_after_callbacks",
        "exception_callbacks",
    ]

    def __init__(
        self,
        txn: Cursor,
        name: str,
        database_engine: BaseDatabaseEngine,
        after_callbacks: Optional[List[_CallbackListEntry]] = None,
        async_after_callbacks: Optional[List[_AsyncCallbackListEntry]] = None,
        exception_callbacks: Optional[List[_CallbackListEntry]] = None,
    ):
        self.txn = txn
        self.name = name
        self.database_engine = database_engine
        self.after_callbacks = after_callbacks
        self.async_after_callbacks = async_after_callbacks
        self.exception_callbacks = exception_callbacks

    def call_after(
        self, callback: Callable[P, object], *args: P.args, **kwargs: P.kwargs
    ) -> None:
        """Call the given callback on the main twisted thread after the transaction has
        finished.

        Mostly used to invalidate the caches on the correct thread.

        Note that transactions may be retried a few times if they encounter database
        errors such as serialization failures. Callbacks given to `call_after`
        will accumulate across transaction attempts and will _all_ be called once a
        transaction attempt succeeds, regardless of whether previous transaction
        attempts failed. Otherwise, if all transaction attempts fail, all
        `call_on_exception` callbacks will be run instead.
        """
        # if self.after_callbacks is None, that means that whatever constructed the
        # LoggingTransaction isn't expecting there to be any callbacks; assert that
        # is not the case.
        assert self.after_callbacks is not None
        self.after_callbacks.append((callback, args, kwargs))

    def async_call_after(
        self, callback: Callable[P, Awaitable], *args: P.args, **kwargs: P.kwargs
    ) -> None:
        """Call the given asynchronous callback on the main twisted thread after
        the transaction has finished (but before those added in `call_after`).

        Mostly used to invalidate remote caches after transactions.

        Note that transactions may be retried a few times if they encounter database
        errors such as serialization failures. Callbacks given to `async_call_after`
        will accumulate across transaction attempts and will _all_ be called once a
        transaction attempt succeeds, regardless of whether previous transaction
        attempts failed. Otherwise, if all transaction attempts fail, all
        `call_on_exception` callbacks will be run instead.
        """
        # if self.async_after_callbacks is None, that means that whatever constructed the
        # LoggingTransaction isn't expecting there to be any callbacks; assert that
        # is not the case.
        assert self.async_after_callbacks is not None
        self.async_after_callbacks.append((callback, args, kwargs))

    def call_on_exception(
        self, callback: Callable[P, object], *args: P.args, **kwargs: P.kwargs
    ) -> None:
        """Call the given callback on the main twisted thread after the transaction has
        failed.

        Note that transactions may be retried a few times if they encounter database
        errors such as serialization failures. Callbacks given to `call_on_exception`
        will accumulate across transaction attempts and will _all_ be called once the
        final transaction attempt fails. No `call_on_exception` callbacks will be run
        if any transaction attempt succeeds.
        """
        # if self.exception_callbacks is None, that means that whatever constructed the
        # LoggingTransaction isn't expecting there to be any callbacks; assert that
        # is not the case.
        assert self.exception_callbacks is not None
        self.exception_callbacks.append((callback, args, kwargs))

    def fetchone(self) -> Optional[Tuple]:
        return self.txn.fetchone()

    def fetchmany(self, size: Optional[int] = None) -> List[Tuple]:
        return self.txn.fetchmany(size=size)

    def fetchall(self) -> List[Tuple]:
        return self.txn.fetchall()

    def __iter__(self) -> Iterator[Tuple]:
        return self.txn.__iter__()

    @property
    def rowcount(self) -> int:
        return self.txn.rowcount

    @property
    def description(
        self,
    ) -> Optional[Sequence[Any]]:
        return self.txn.description

    def execute_batch(self, sql: str, args: Iterable[Iterable[Any]]) -> None:
        """Similar to `executemany`, except `txn.rowcount` will not be correct
        afterwards.

        More efficient than `executemany` on PostgreSQL
        """

        if isinstance(self.database_engine, PostgresEngine):
            from psycopg2.extras import execute_batch

            # TODO: is it safe for values to be Iterable[Iterable[Any]] here?
            # https://www.psycopg.org/docs/extras.html?highlight=execute_batch#psycopg2.extras.execute_batch
            # suggests each arg in args should be a sequence or mapping
            self._do_execute(
                lambda the_sql: execute_batch(self.txn, the_sql, args), sql
            )
        else:
            # TODO: is it safe for values to be Iterable[Iterable[Any]] here?
            # https://docs.python.org/3/library/sqlite3.html?highlight=sqlite3#sqlite3.Cursor.executemany
            # suggests that the outer collection may be iterable, but
            # https://docs.python.org/3/library/sqlite3.html?highlight=sqlite3#how-to-use-placeholders-to-bind-values-in-sql-queries
            # suggests that the inner collection should be a sequence or dict.
            self.executemany(sql, args)

    def execute_values(
        self,
        sql: str,
        values: Iterable[Iterable[Any]],
        template: Optional[str] = None,
        fetch: bool = True,
    ) -> List[Tuple]:
        """Corresponds to psycopg2.extras.execute_values. Only available when
        using postgres.

        The `fetch` parameter must be set to False if the query does not return
        rows (e.g. INSERTs).

        The `template` is the snippet to merge to every item in argslist to
        compose the query.
        """
        assert isinstance(self.database_engine, PostgresEngine)
        from psycopg2.extras import execute_values

        return self._do_execute(
            # TODO: is it safe for values to be Iterable[Iterable[Any]] here?
            # https://www.psycopg.org/docs/extras.html?highlight=execute_batch#psycopg2.extras.execute_values says values should be Sequence[Sequence]
            lambda the_sql, the_values: execute_values(
                self.txn, the_sql, the_values, template=template, fetch=fetch
            ),
            sql,
            values,
        )

    def execute(self, sql: str, parameters: SQLQueryParameters = ()) -> None:
        self._do_execute(self.txn.execute, sql, parameters)

    def executemany(self, sql: str, *args: Any) -> None:
        """Repeatedly execute the same piece of SQL with different parameters.

        See https://peps.python.org/pep-0249/#executemany. Note in particular that

        > Use of this method for an operation which produces one or more result sets
        > constitutes undefined behavior

        so you can't use this for e.g. a SELECT, an UPDATE ... RETURNING, or a
        DELETE FROM... RETURNING.
        """
        # TODO: we should add a type for *args here. Looking at Cursor.executemany
        # and DBAPI2 it ought to be Sequence[_Parameter], but we pass in
        # Iterable[Iterable[Any]] in execute_batch and execute_values above, which mypy
        # complains about.
        self._do_execute(self.txn.executemany, sql, *args)

    def executescript(self, sql: str) -> None:
        if isinstance(self.database_engine, Sqlite3Engine):
            self._do_execute(self.txn.executescript, sql)  # type: ignore[attr-defined]
        else:
            raise NotImplementedError(
                f"executescript only exists for sqlite driver, not {type(self.database_engine)}"
            )

    def _make_sql_one_line(self, sql: str) -> str:
        "Strip newlines out of SQL so that the loggers in the DB are on one line"
        return " ".join(line.strip() for line in sql.splitlines() if line.strip())

    def _do_execute(
        self,
        func: Callable[Concatenate[str, P], R],
        sql: str,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        # Generate a one-line version of the SQL to better log it.
        one_line_sql = self._make_sql_one_line(sql)

        # TODO(paul): Maybe use 'info' and 'debug' for values?
        sql_logger.debug("[SQL] {%s} %s", self.name, one_line_sql)

        sql = self.database_engine.convert_param_style(sql)
        if args:
            try:
                sql_logger.debug("[SQL values] {%s} %r", self.name, args[0])
            except Exception:
                # Don't let logging failures stop SQL from working
                pass

        start = time.time()

        try:
            with opentracing.start_active_span(
                "db.query",
                tags={
                    opentracing.tags.DATABASE_TYPE: "sql",
                    opentracing.tags.DATABASE_STATEMENT: one_line_sql,
                },
            ):
                return func(sql, *args, **kwargs)
        except Exception as e:
            sql_logger.debug("[SQL FAIL] {%s} %s", self.name, e)
            raise
        finally:
            secs = time.time() - start
            sql_logger.debug("[SQL time] {%s} %f sec", self.name, secs)
            sql_query_timer.labels(sql.split()[0]).observe(secs)

    def close(self) -> None:
        self.txn.close()

    def __enter__(self) -> "LoggingTransaction":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[types.TracebackType],
    ) -> None:
        self.close()


class PerformanceCounters:
    def __init__(self) -> None:
        self.current_counters: Dict[str, Tuple[int, float]] = {}
        self.previous_counters: Dict[str, Tuple[int, float]] = {}

    def update(self, key: str, duration_secs: float) -> None:
        count, cum_time = self.current_counters.get(key, (0, 0.0))
        count += 1
        cum_time += duration_secs
        self.current_counters[key] = (count, cum_time)

    def interval(self, interval_duration_secs: float, limit: int = 3) -> str:
        counters = []
        for name, (count, cum_time) in self.current_counters.items():
            prev_count, prev_time = self.previous_counters.get(name, (0, 0))
            counters.append(
                (
                    (cum_time - prev_time) / interval_duration_secs,
                    count - prev_count,
                    name,
                )
            )

        self.previous_counters = dict(self.current_counters)

        counters.sort(reverse=True)

        top_n_counters = ", ".join(
            "%s(%d): %.3f%%" % (name, count, 100 * ratio)
            for ratio, count, name in counters[:limit]
        )

        return top_n_counters


class DatabasePool:
    """Wraps a single physical database and connection pool.

    A single database may be used by multiple data stores.
    """

    _TXN_ID = 0
    engine: BaseDatabaseEngine

    def __init__(
        self,
        hs: "HomeServer",
        database_config: DatabaseConnectionConfig,
        engine: BaseDatabaseEngine,
    ):
        self.hs = hs
        self._clock = hs.get_clock()
        self._txn_limit = database_config.config.get("txn_limit", 0)
        self._database_config = database_config
        self._db_pool = make_pool(hs.get_reactor(), database_config, engine)

        self.updates = BackgroundUpdater(hs, self)
        LaterGauge(
            "synapse_background_update_status",
            "Background update status",
            [],
            self.updates.get_status,
        )

        self._previous_txn_total_time = 0.0
        self._current_txn_total_time = 0.0
        self._previous_loop_ts = 0.0

        # Transaction counter: key is the twisted thread id, value is the current count
        self._txn_counters: Dict[int, int] = defaultdict(int)

        # TODO(paul): These can eventually be removed once the metrics code
        #   is running in mainline, and we have some nice monitoring frontends
        #   to watch it
        self._txn_perf_counters = PerformanceCounters()

        self.engine = engine

        # A set of tables that are not safe to use native upserts in.
        self._unsafe_to_upsert_tables = set(UNIQUE_INDEX_BACKGROUND_UPDATES.keys())

        # The user_directory_search table is unsafe to use native upserts
        # on SQLite because the existing search table does not have an index.
        if isinstance(self.engine, Sqlite3Engine):
            self._unsafe_to_upsert_tables.add("user_directory_search")

        # Check ASAP (and then later, every 1s) to see if we have finished
        # background updates of tables that aren't safe to update.
        self._clock.call_later(
            0.0,
            run_as_background_process,
            "upsert_safety_check",
            self._check_safe_to_upsert,
        )

    def name(self) -> str:
        "Return the name of this database"
        return self._database_config.name

    def is_running(self) -> bool:
        """Is the database pool currently running"""
        return self._db_pool.running

    async def _check_safe_to_upsert(self) -> None:
        """
        Is it safe to use native UPSERT?

        If there are background updates, we will need to wait, as they may be
        the addition of indexes that set the UNIQUE constraint that we require.

        If the background updates have not completed, wait 15 sec and check again.
        """
        updates = cast(
            List[Tuple[str]],
            await self.simple_select_list(
                "background_updates",
                keyvalues=None,
                retcols=["update_name"],
                desc="check_background_updates",
            ),
        )
        background_update_names = [x[0] for x in updates]

        for table, update_name in UNIQUE_INDEX_BACKGROUND_UPDATES.items():
            if update_name not in background_update_names:
                logger.debug("Now safe to upsert in %s", table)
                self._unsafe_to_upsert_tables.discard(table)

        # If there's any updates still running, reschedule to run.
        if background_update_names:
            self._clock.call_later(
                15.0,
                run_as_background_process,
                "upsert_safety_check",
                self._check_safe_to_upsert,
            )

    def start_profiling(self) -> None:
        self._previous_loop_ts = monotonic_time()

        def loop() -> None:
            curr = self._current_txn_total_time
            prev = self._previous_txn_total_time
            self._previous_txn_total_time = curr

            time_now = monotonic_time()
            time_then = self._previous_loop_ts
            self._previous_loop_ts = time_now

            duration = time_now - time_then
            ratio = (curr - prev) / duration

            top_three_counters = self._txn_perf_counters.interval(duration, limit=3)

            perf_logger.debug(
                "Total database time: %.3f%% {%s}", ratio * 100, top_three_counters
            )

        self._clock.looping_call(loop, 10000)

    def new_transaction(
        self,
        conn: LoggingDatabaseConnection,
        desc: str,
        after_callbacks: List[_CallbackListEntry],
        async_after_callbacks: List[_AsyncCallbackListEntry],
        exception_callbacks: List[_CallbackListEntry],
        func: Callable[Concatenate[LoggingTransaction, P], R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Start a new database transaction with the given connection.

        Note: The given func may be called multiple times under certain
        failure modes. This is normally fine when in a standard transaction,
        but care must be taken if the connection is in `autocommit` mode that
        the function will correctly handle being aborted and retried half way
        through its execution.

        Similarly, the arguments to `func` (`args`, `kwargs`) should not be generators,
        since they could be evaluated multiple times (which would produce an empty
        result on the second or subsequent evaluation). Likewise, the closure of `func`
        must not reference any generators.  This method attempts to detect such usage
        and will log an error.

        Args:
            conn
            desc
            after_callbacks
            async_after_callbacks
            exception_callbacks
            func
            *args
            **kwargs
        """

        # Robustness check: ensure that none of the arguments are generators, since that
        # will fail if we have to repeat the transaction.
        # For now, we just log an error, and hope that it works on the first attempt.
        # TODO: raise an exception.

        for i, arg in enumerate(args):
            if inspect.isgenerator(arg):
                logger.error(
                    "Programming error: generator passed to new_transaction as "
                    "argument %i to function %s",
                    i,
                    func,
                )
        for name, val in kwargs.items():
            if inspect.isgenerator(val):
                logger.error(
                    "Programming error: generator passed to new_transaction as "
                    "argument %s to function %s",
                    name,
                    func,
                )
        # also check variables referenced in func's closure
        if inspect.isfunction(func):
            # Keep the cast for now---it helps PyCharm to understand what `func` is.
            f = cast(types.FunctionType, func)  # type: ignore[redundant-cast]
            if f.__closure__:
                for i, cell in enumerate(f.__closure__):
                    try:
                        contents = cell.cell_contents
                    except ValueError:
                        # cell.cell_contents can raise if the "cell" is empty,
                        # which indicates that the variable is currently
                        # unbound.
                        continue

                    if inspect.isgenerator(contents):
                        logger.error(
                            "Programming error: function %s references generator %s "
                            "via its closure",
                            f,
                            f.__code__.co_freevars[i],
                        )

        start = monotonic_time()
        txn_id = self._TXN_ID

        # We don't really need these to be unique, so lets stop it from
        # growing really large.
        self._TXN_ID = (self._TXN_ID + 1) % (MAX_TXN_ID)

        name = "%s-%x" % (desc, txn_id)

        transaction_logger.debug("[TXN START] {%s}", name)

        try:
            i = 0
            N = 5
            while True:
                cursor = conn.cursor(
                    txn_name=name,
                    after_callbacks=after_callbacks,
                    async_after_callbacks=async_after_callbacks,
                    exception_callbacks=exception_callbacks,
                )
                try:
                    with opentracing.start_active_span(
                        "db.txn",
                        tags={
                            opentracing.SynapseTags.DB_TXN_DESC: desc,
                            opentracing.SynapseTags.DB_TXN_ID: name,
                        },
                    ):
                        r = func(cursor, *args, **kwargs)
                        opentracing.log_kv({"message": "commit"})
                        conn.commit()
                        return r
                except self.engine.module.OperationalError as e:
                    # This can happen if the database disappears mid
                    # transaction.
                    transaction_logger.warning(
                        "[TXN OPERROR] {%s} %s %d/%d",
                        name,
                        e,
                        i,
                        N,
                    )
                    if i < N:
                        i += 1
                        try:
                            with opentracing.start_active_span("db.rollback"):
                                conn.rollback()
                        except self.engine.module.Error as e1:
                            transaction_logger.warning("[TXN EROLL] {%s} %s", name, e1)
                        continue
                    raise
                except self.engine.module.DatabaseError as e:
                    if self.engine.is_deadlock(e):
                        transaction_logger.warning(
                            "[TXN DEADLOCK] {%s} %d/%d", name, i, N
                        )
                        if i < N:
                            i += 1
                            try:
                                with opentracing.start_active_span("db.rollback"):
                                    conn.rollback()
                            except self.engine.module.Error as e1:
                                transaction_logger.warning(
                                    "[TXN EROLL] {%s} %s",
                                    name,
                                    e1,
                                )
                            continue
                    raise
                finally:
                    # we're either about to retry with a new cursor, or we're about to
                    # release the connection. Once we release the connection, it could
                    # get used for another query, which might do a conn.rollback().
                    #
                    # In the latter case, even though that probably wouldn't affect the
                    # results of this transaction, python's sqlite will reset all
                    # statements on the connection [1], which will make our cursor
                    # invalid [2].
                    #
                    # In any case, continuing to read rows after commit()ing seems
                    # dubious from the PoV of ACID transactional semantics
                    # (sqlite explicitly says that once you commit, you may see rows
                    # from subsequent updates.)
                    #
                    # In psycopg2, cursors are essentially a client-side fabrication -
                    # all the data is transferred to the client side when the statement
                    # finishes executing - so in theory we could go on streaming results
                    # from the cursor, but attempting to do so would make us
                    # incompatible with sqlite, so let's make sure we're not doing that
                    # by closing the cursor.
                    #
                    # (*named* cursors in psycopg2 are different and are proper server-
                    # side things, but (a) we don't use them and (b) they are implicitly
                    # closed by ending the transaction anyway.)
                    #
                    # In short, if we haven't finished with the cursor yet, that's a
                    # problem waiting to bite us.
                    #
                    # TL;DR: we're done with the cursor, so we can close it.
                    #
                    # [1]: https://github.com/python/cpython/blob/v3.8.0/Modules/_sqlite/connection.c#L465
                    # [2]: https://github.com/python/cpython/blob/v3.8.0/Modules/_sqlite/cursor.c#L236
                    cursor.close()
        except Exception as e:
            transaction_logger.debug("[TXN FAIL] {%s} %s", name, e)
            raise
        finally:
            end = monotonic_time()
            duration = end - start

            current_context().add_database_transaction(duration)

            transaction_logger.debug("[TXN END] {%s} %f sec", name, duration)

            self._current_txn_total_time += duration
            self._txn_perf_counters.update(desc, duration)
            sql_txn_count.labels(desc).inc(1)
            sql_txn_duration.labels(desc).inc(duration)

    async def runInteraction(
        self,
        desc: str,
        func: Callable[..., R],
        *args: Any,
        db_autocommit: bool = False,
        isolation_level: Optional[int] = None,
        **kwargs: Any,
    ) -> R:
        """Starts a transaction on the database and runs a given function

        Arguments:
            desc: description of the transaction, for logging and metrics
            func: callback function, which will be called with a
                database transaction (twisted.enterprise.adbapi.Transaction) as
                its first argument, followed by `args` and `kwargs`.

            db_autocommit: Whether to run the function in "autocommit" mode,
                i.e. outside of a transaction. This is useful for transactions
                that are only a single query.

                Currently, this is only implemented for Postgres. SQLite will still
                run the function inside a transaction.

                WARNING: This means that if func fails half way through then
                the changes will *not* be rolled back. `func` may also get
                called multiple times if the transaction is retried, so must
                correctly handle that case.

            isolation_level: Set the server isolation level for this transaction.
            args: positional args to pass to `func`
            kwargs: named args to pass to `func`

        Returns:
            The result of func
        """

        async def _runInteraction() -> R:
            after_callbacks: List[_CallbackListEntry] = []
            async_after_callbacks: List[_AsyncCallbackListEntry] = []
            exception_callbacks: List[_CallbackListEntry] = []

            if not current_context():
                logger.warning("Starting db txn '%s' from sentinel context", desc)

            try:
                with opentracing.start_active_span(f"db.{desc}"):
                    result: R = await self.runWithConnection(
                        # mypy seems to have an issue with this, maybe a bug?
                        self.new_transaction,
                        desc,
                        after_callbacks,
                        async_after_callbacks,
                        exception_callbacks,
                        func,
                        *args,
                        db_autocommit=db_autocommit,
                        isolation_level=isolation_level,
                        **kwargs,
                    )

                # We order these assuming that async functions call out to external
                # systems (e.g. to invalidate a cache) and the sync functions make these
                # changes on any local in-memory caches/similar, and thus must be second.
                for async_callback, async_args, async_kwargs in async_after_callbacks:
                    await async_callback(*async_args, **async_kwargs)
                for after_callback, after_args, after_kwargs in after_callbacks:
                    after_callback(*after_args, **after_kwargs)
                return result
            except Exception:
                for exception_callback, after_args, after_kwargs in exception_callbacks:
                    exception_callback(*after_args, **after_kwargs)
                raise

        # To handle cancellation, we ensure that `after_callback`s and
        # `exception_callback`s are always run, since the transaction will complete
        # on another thread regardless of cancellation.
        #
        # We also wait until everything above is done before releasing the
        # `CancelledError`, so that logging contexts won't get used after they have been
        # finished.
        return await delay_cancellation(_runInteraction())

    async def runWithConnection(
        self,
        func: Callable[Concatenate[LoggingDatabaseConnection, P], R],
        *args: Any,
        db_autocommit: bool = False,
        isolation_level: Optional[int] = None,
        **kwargs: Any,
    ) -> R:
        """Wraps the .runWithConnection() method on the underlying db_pool.

        Arguments:
            func: callback function, which will be called with a
                database connection (twisted.enterprise.adbapi.Connection) as
                its first argument, followed by `args` and `kwargs`.
            args: positional args to pass to `func`
            db_autocommit: Whether to run the function in "autocommit" mode,
                i.e. outside of a transaction. This is useful for transaction
                that are only a single query. Currently only affects postgres.
            isolation_level: Set the server isolation level for this transaction.
            kwargs: named args to pass to `func`

        Returns:
            The result of func
        """
        curr_context = current_context()
        if not curr_context:
            logger.warning(
                "Starting db connection from sentinel context: metrics will be lost"
            )
            parent_context = None
        else:
            assert isinstance(curr_context, LoggingContext)
            parent_context = curr_context

        start_time = monotonic_time()

        def inner_func(conn: _PoolConnection, *args: P.args, **kwargs: P.kwargs) -> R:
            # We shouldn't be in a transaction. If we are then something
            # somewhere hasn't committed after doing work. (This is likely only
            # possible during startup, as `run*` will ensure changes are
            # committed/rolled back before putting the connection back in the
            # pool).
            assert not self.engine.in_transaction(conn)

            with LoggingContext(
                str(curr_context), parent_context=parent_context
            ) as context:
                with opentracing.start_active_span(
                    operation_name="db.connection",
                ):
                    sched_duration_sec = monotonic_time() - start_time
                    sql_scheduling_timer.observe(sched_duration_sec)
                    context.add_database_scheduled(sched_duration_sec)

                    if self._txn_limit > 0:
                        tid = self._db_pool.threadID()
                        self._txn_counters[tid] += 1

                        if self._txn_counters[tid] > self._txn_limit:
                            logger.debug(
                                "Reconnecting database connection over transaction limit"
                            )
                            conn.reconnect()
                            opentracing.log_kv(
                                {"message": "reconnected due to txn limit"}
                            )
                            self._txn_counters[tid] = 1

                    if self.engine.is_connection_closed(conn):
                        logger.debug("Reconnecting closed database connection")
                        conn.reconnect()
                        opentracing.log_kv({"message": "reconnected"})
                        if self._txn_limit > 0:
                            self._txn_counters[tid] = 1

                    try:
                        if db_autocommit:
                            self.engine.attempt_to_set_autocommit(conn, True)
                        if isolation_level is not None:
                            self.engine.attempt_to_set_isolation_level(
                                conn, isolation_level
                            )

                        db_conn = LoggingDatabaseConnection(
                            conn, self.engine, "runWithConnection"
                        )
                        return func(db_conn, *args, **kwargs)
                    finally:
                        if db_autocommit:
                            self.engine.attempt_to_set_autocommit(conn, False)
                        if isolation_level:
                            self.engine.attempt_to_set_isolation_level(conn, None)

        return await make_deferred_yieldable(
            self._db_pool.runWithConnection(inner_func, *args, **kwargs)
        )

    async def execute(self, desc: str, query: str, *args: Any) -> List[Tuple[Any, ...]]:
        """Runs a single query for a result set.

        Args:
            desc: description of the transaction, for logging and metrics
            query - The query string to execute
            *args - Query args.
        Returns:
            The result of decoder(results)
        """

        def interaction(txn: LoggingTransaction) -> List[Tuple[Any, ...]]:
            txn.execute(query, args)
            return txn.fetchall()

        return await self.runInteraction(desc, interaction)

    # "Simple" SQL API methods that operate on a single table with no JOINs,
    # no complex WHERE clauses, just a dict of values for columns.

    async def simple_insert(
        self,
        table: str,
        values: Dict[str, Any],
        desc: str = "simple_insert",
    ) -> None:
        """Executes an INSERT query on the named table.

        Args:
            table: string giving the table name
            values: dict of new column names and values for them
            desc: description of the transaction, for logging and metrics
        """
        await self.runInteraction(desc, self.simple_insert_txn, table, values)

    @staticmethod
    def simple_insert_txn(
        txn: LoggingTransaction, table: str, values: Dict[str, Any]
    ) -> None:
        keys, vals = zip(*values.items())

        sql = "INSERT INTO %s (%s) VALUES(%s)" % (
            table,
            ", ".join(k for k in keys),
            ", ".join("?" for _ in keys),
        )

        txn.execute(sql, vals)

    @staticmethod
    def simple_insert_returning_txn(
        txn: LoggingTransaction,
        table: str,
        values: Dict[str, Any],
        returning: StrCollection,
    ) -> Tuple[Any, ...]:
        """Executes a `INSERT INTO... RETURNING...` statement (or equivalent for
        SQLite versions that don't support it).
        """

        if txn.database_engine.supports_returning:
            sql = "INSERT INTO %s (%s) VALUES(%s) RETURNING %s" % (
                table,
                ", ".join(k for k in values.keys()),
                ", ".join("?" for _ in values.keys()),
                ", ".join(k for k in returning),
            )

            txn.execute(sql, list(values.values()))
            row = txn.fetchone()
            assert row is not None
            return row
        else:
            # For old versions of SQLite we do a standard insert and then can
            # use `last_insert_rowid` to get at the row we just inserted
            DatabasePool.simple_insert_txn(
                txn,
                table=table,
                values=values,
            )
            txn.execute("SELECT last_insert_rowid()")
            row = txn.fetchone()
            assert row is not None
            (rowid,) = row

            row = DatabasePool.simple_select_one_txn(
                txn, table=table, keyvalues={"rowid": rowid}, retcols=returning
            )
            assert row is not None
            return row

    async def simple_insert_many(
        self,
        table: str,
        keys: Collection[str],
        values: Collection[Collection[Any]],
        desc: str,
    ) -> None:
        """Executes an INSERT query on the named table.

        The input is given as a list of rows, where each row is a list of values.
        (Actually any iterable is fine.)

        Args:
            table: string giving the table name
            keys: list of column names
            values: for each row, a list of values in the same order as `keys`
            desc: description of the transaction, for logging and metrics
        """
        await self.runInteraction(
            desc, self.simple_insert_many_txn, table, keys, values
        )

    @staticmethod
    def simple_insert_many_txn(
        txn: LoggingTransaction,
        table: str,
        keys: Sequence[str],
        values: Collection[Iterable[Any]],
    ) -> None:
        """Executes an INSERT query on the named table.

        The input is given as a list of rows, where each row is a list of values.
        (Actually any iterable is fine.)

        Args:
            txn: The transaction to use.
            table: string giving the table name
            keys: list of column names
            values: for each row, a list of values in the same order as `keys`
        """
        # If there's nothing to insert, then skip executing the query.
        if not values:
            return

        if isinstance(txn.database_engine, PostgresEngine):
            # We use `execute_values` as it can be a lot faster than `execute_batch`,
            # but it's only available on postgres.
            sql = "INSERT INTO %s (%s) VALUES ?" % (
                table,
                ", ".join(k for k in keys),
            )

            txn.execute_values(sql, values, fetch=False)
        else:
            sql = "INSERT INTO %s (%s) VALUES(%s)" % (
                table,
                ", ".join(k for k in keys),
                ", ".join("?" for _ in keys),
            )

            txn.execute_batch(sql, values)

    async def simple_upsert(
        self,
        table: str,
        keyvalues: Dict[str, Any],
        values: Dict[str, Any],
        insertion_values: Optional[Dict[str, Any]] = None,
        where_clause: Optional[str] = None,
        desc: str = "simple_upsert",
    ) -> bool:
        """Insert a row with values + insertion_values; on conflict, update with values.

        All of our supported databases accept the nonstandard "upsert" statement in
        their dialect of SQL. We call this a "native upsert". The syntax looks roughly
        like:

            INSERT INTO table VALUES (values + insertion_values)
            ON CONFLICT (keyvalues)
            DO UPDATE SET (values); -- overwrite `values` columns only

        If (values) is empty, the resulting query is slighlty simpler:

            INSERT INTO table VALUES (insertion_values)
            ON CONFLICT (keyvalues)
            DO NOTHING;             -- do not overwrite any columns

        This function is a helper to build such queries.

        In order for upserts to make sense, the database must be able to determine when
        an upsert CONFLICTs with an existing row. Postgres and SQLite ensure this by
        requiring that a unique index exist on the column names used to detect a
        conflict (i.e. `keyvalues.keys()`).

        If there is no such index yet[*], we can "emulate" an upsert with a SELECT
        followed by either an INSERT or an UPDATE. This is unsafe unless *all* upserters
        run at the SERIALIZABLE isolation level: we cannot make the same atomicity
        guarantees that a native upsert can and are very vulnerable to races and
        crashes. Therefore to upsert without an appropriate unique index, we acquire a
        table-level lock before the emulated upsert.

        [*]: Some tables have unique indices added to them in the background. Those
             tables `T` are keys in the dictionary UNIQUE_INDEX_BACKGROUND_UPDATES,
             where `T` maps to the background update that adds a unique index to `T`.
             This dictionary is maintained by hand.

             At runtime, we constantly check to see if each of these background updates
             has run. If so, we deem the coresponding table safe to upsert into, because
             we can now use a native insert to do so. If not, we deem the table unsafe
             to upsert into and require an emulated upsert.

             Tables that do not appear in this dictionary are assumed to have an
             appropriate unique index and therefore be safe to upsert into.

        Args:
            table: The table to upsert into
            keyvalues: The unique key columns and their new values
            values: The nonunique columns and their new values
            insertion_values: additional key/values to use only when inserting
            where_clause: An index predicate to apply to the upsert.
            desc: description of the transaction, for logging and metrics
        Returns:
            Returns True if a row was inserted or updated (i.e. if `values` is
            not empty then this always returns True)
        """
        insertion_values = insertion_values or {}

        attempts = 0
        while True:
            try:
                # We can autocommit if it is safe to upsert
                autocommit = table not in self._unsafe_to_upsert_tables

                return await self.runInteraction(
                    desc,
                    self.simple_upsert_txn,
                    table,
                    keyvalues,
                    values,
                    insertion_values,
                    where_clause,
                    db_autocommit=autocommit,
                )
            except self.engine.module.IntegrityError as e:
                attempts += 1
                if attempts >= 5:
                    # don't retry forever, because things other than races
                    # can cause IntegrityErrors
                    raise

                # presumably we raced with another transaction: let's retry.
                logger.warning(
                    "IntegrityError when upserting into %s; retrying: %s", table, e
                )

    def simple_upsert_txn(
        self,
        txn: LoggingTransaction,
        table: str,
        keyvalues: Mapping[str, Any],
        values: Mapping[str, Any],
        insertion_values: Optional[Mapping[str, Any]] = None,
        where_clause: Optional[str] = None,
    ) -> bool:
        """
        Pick the UPSERT method which works best on the platform. Either the
        native one (Pg9.5+, SQLite >= 3.24), or fall back to an emulated method.

        Args:
            txn: The transaction to use.
            table: The table to upsert into
            keyvalues: The unique key tables and their new values
            values: The nonunique columns and their new values
            insertion_values: additional key/values to use only when inserting
            where_clause: An index predicate to apply to the upsert.
        Returns:
            Returns True if a row was inserted or updated (i.e. if `values` is
            not empty then this always returns True)
        """
        insertion_values = insertion_values or {}

        if table not in self._unsafe_to_upsert_tables:
            return self.simple_upsert_txn_native_upsert(
                txn,
                table,
                keyvalues,
                values,
                insertion_values=insertion_values,
                where_clause=where_clause,
            )
        else:
            return self.simple_upsert_txn_emulated(
                txn,
                table,
                keyvalues,
                values,
                insertion_values=insertion_values,
                where_clause=where_clause,
            )

    def simple_upsert_txn_emulated(
        self,
        txn: LoggingTransaction,
        table: str,
        keyvalues: Mapping[str, Any],
        values: Mapping[str, Any],
        insertion_values: Optional[Mapping[str, Any]] = None,
        where_clause: Optional[str] = None,
        lock: bool = True,
    ) -> bool:
        """
        Args:
            table: The table to upsert into
            keyvalues: The unique key tables and their new values
            values: The nonunique columns and their new values
            insertion_values: additional key/values to use only when inserting
            where_clause: An index predicate to apply to the upsert.
            lock: True to lock the table when doing the upsert.
                Must not be False unless the table has already been locked.
        Returns:
            Returns True if a row was inserted or updated (i.e. if `values` is
            not empty then this always returns True)
        """
        insertion_values = insertion_values or {}

        if lock:
            # We need to lock the table :(
            txn.database_engine.lock_table(txn, table)

        def _getwhere(key: str) -> str:
            # If the value we're passing in is None (aka NULL), we need to use
            # IS, not =, as NULL = NULL equals NULL (False).
            if keyvalues[key] is None:
                return "%s IS ?" % (key,)
            else:
                return "%s = ?" % (key,)

        # Generate a where clause of each keyvalue and optionally the provided
        # index predicate.
        where = [_getwhere(k) for k in keyvalues]
        if where_clause:
            where.append(where_clause)

        if not values:
            # If `values` is empty, then all of the values we care about are in
            # the unique key, so there is nothing to UPDATE. We can just do a
            # SELECT instead to see if it exists.
            sql = "SELECT 1 FROM %s WHERE %s" % (table, " AND ".join(where))
            sqlargs = list(keyvalues.values())
            txn.execute(sql, sqlargs)
            if txn.fetchall():
                # We have an existing record.
                return False
        else:
            # First try to update.
            sql = "UPDATE %s SET %s WHERE %s" % (
                table,
                ", ".join("%s = ?" % (k,) for k in values),
                " AND ".join(where),
            )
            sqlargs = list(values.values()) + list(keyvalues.values())

            txn.execute(sql, sqlargs)
            if txn.rowcount > 0:
                return True

        # We didn't find any existing rows, so insert a new one
        allvalues: Dict[str, Any] = {}
        allvalues.update(keyvalues)
        allvalues.update(values)
        allvalues.update(insertion_values)

        sql = "INSERT INTO %s (%s) VALUES (%s)" % (
            table,
            ", ".join(k for k in allvalues),
            ", ".join("?" for _ in allvalues),
        )
        txn.execute(sql, list(allvalues.values()))
        # successfully inserted
        return True

    @staticmethod
    def simple_upsert_txn_native_upsert(
        txn: LoggingTransaction,
        table: str,
        keyvalues: Mapping[str, Any],
        values: Mapping[str, Any],
        insertion_values: Optional[Mapping[str, Any]] = None,
        where_clause: Optional[str] = None,
    ) -> bool:
        """
        Use the native UPSERT functionality in PostgreSQL.

        Args:
            table: The table to upsert into
            keyvalues: The unique key tables and their new values
            values: The nonunique columns and their new values
            insertion_values: additional key/values to use only when inserting
            where_clause: An index predicate to apply to the upsert.

        Returns:
            Returns True if a row was inserted or updated (i.e. if `values` is
            not empty then this always returns True)
        """
        allvalues: Dict[str, Any] = {}
        allvalues.update(keyvalues)
        allvalues.update(insertion_values or {})

        if not values:
            latter = "NOTHING"
        else:
            allvalues.update(values)
            latter = "UPDATE SET " + ", ".join(k + "=EXCLUDED." + k for k in values)

        sql = "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT (%s) %sDO %s" % (
            table,
            ", ".join(k for k in allvalues),
            ", ".join("?" for _ in allvalues),
            ", ".join(k for k in keyvalues),
            f"WHERE {where_clause} " if where_clause else "",
            latter,
        )
        txn.execute(sql, list(allvalues.values()))

        return bool(txn.rowcount)

    async def simple_upsert_many(
        self,
        table: str,
        key_names: Collection[str],
        key_values: Collection[Collection[Any]],
        value_names: Collection[str],
        value_values: Collection[Collection[Any]],
        desc: str,
    ) -> None:
        """
        Upsert, many times.

        Args:
            table: The table to upsert into
            key_names: The key column names.
            key_values: A list of each row's key column values.
            value_names: The value column names
            value_values: A list of each row's value column values.
                Ignored if value_names is empty.
        """

        # We can autocommit if it safe to upsert
        autocommit = table not in self._unsafe_to_upsert_tables

        await self.runInteraction(
            desc,
            self.simple_upsert_many_txn,
            table,
            key_names,
            key_values,
            value_names,
            value_values,
            db_autocommit=autocommit,
        )

    def simple_upsert_many_txn(
        self,
        txn: LoggingTransaction,
        table: str,
        key_names: Collection[str],
        key_values: Collection[Iterable[Any]],
        value_names: Collection[str],
        value_values: Collection[Iterable[Any]],
    ) -> None:
        """
        Upsert, many times.

        Args:
            table: The table to upsert into
            key_names: The key column names.
            key_values: A list of each row's key column values.
            value_names: The value column names
            value_values: A list of each row's value column values.
                Ignored if value_names is empty.
        """
        # If there's nothing to upsert, then skip executing the query.
        if not key_values:
            return

        # No value columns, therefore make a blank list so that the following
        # zip() works correctly.
        if not value_names:
            value_values = [() for x in range(len(key_values))]
        elif len(value_values) != len(key_values):
            raise ValueError(
                f"{len(key_values)} key rows and {len(value_values)} value rows: should be the same number."
            )

        if table not in self._unsafe_to_upsert_tables:
            return self.simple_upsert_many_txn_native_upsert(
                txn, table, key_names, key_values, value_names, value_values
            )
        else:
            return self.simple_upsert_many_txn_emulated(
                txn,
                table,
                key_names,
                key_values,
                value_names,
                value_values,
            )

    def simple_upsert_many_txn_emulated(
        self,
        txn: LoggingTransaction,
        table: str,
        key_names: Iterable[str],
        key_values: Collection[Iterable[Any]],
        value_names: Collection[str],
        value_values: Iterable[Iterable[Any]],
    ) -> None:
        """
        Upsert, many times, but without native UPSERT support or batching.

        Args:
            table: The table to upsert into
            key_names: The key column names.
            key_values: A list of each row's key column values.
            value_names: The value column names
            value_values: A list of each row's value column values.
                Ignored if value_names is empty.
        """

        # Lock the table just once, to prevent it being done once per row.
        # Note that, according to Postgres' documentation, once obtained,
        # the lock is held for the remainder of the current transaction.
        self.engine.lock_table(txn, table)

        for keyv, valv in zip(key_values, value_values):
            _keys = dict(zip(key_names, keyv))
            _vals = dict(zip(value_names, valv))

            self.simple_upsert_txn_emulated(txn, table, _keys, _vals, lock=False)

    @staticmethod
    def simple_upsert_many_txn_native_upsert(
        txn: LoggingTransaction,
        table: str,
        key_names: Collection[str],
        key_values: Collection[Iterable[Any]],
        value_names: Collection[str],
        value_values: Iterable[Iterable[Any]],
    ) -> None:
        """
        Upsert, many times, using batching where possible.

        Args:
            table: The table to upsert into
            key_names: The key column names.
            key_values: A list of each row's key column values.
            value_names: The value column names
            value_values: A list of each row's value column values.
                Ignored if value_names is empty.
        """
        allnames: List[str] = []
        allnames.extend(key_names)
        allnames.extend(value_names)

        if not value_names:
            latter = "NOTHING"
        else:
            latter = "UPDATE SET " + ", ".join(
                k + "=EXCLUDED." + k for k in value_names
            )

        args = []

        for x, y in zip(key_values, value_values):
            args.append(tuple(x) + tuple(y))

        if isinstance(txn.database_engine, PostgresEngine):
            # We use `execute_values` as it can be a lot faster than `execute_batch`,
            # but it's only available on postgres.
            sql = "INSERT INTO %s (%s) VALUES ? ON CONFLICT (%s) DO %s" % (
                table,
                ", ".join(k for k in allnames),
                ", ".join(key_names),
                latter,
            )

            txn.execute_values(sql, args, fetch=False)

        else:
            sql = "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT (%s) DO %s" % (
                table,
                ", ".join(k for k in allnames),
                ", ".join("?" for _ in allnames),
                ", ".join(key_names),
                latter,
            )

            return txn.execute_batch(sql, args)

    @overload
    async def simple_select_one(
        self,
        table: str,
        keyvalues: Dict[str, Any],
        retcols: Collection[str],
        allow_none: Literal[False] = False,
        desc: str = "simple_select_one",
    ) -> Tuple[Any, ...]: ...

    @overload
    async def simple_select_one(
        self,
        table: str,
        keyvalues: Dict[str, Any],
        retcols: Collection[str],
        allow_none: Literal[True] = True,
        desc: str = "simple_select_one",
    ) -> Optional[Tuple[Any, ...]]: ...

    async def simple_select_one(
        self,
        table: str,
        keyvalues: Dict[str, Any],
        retcols: Collection[str],
        allow_none: bool = False,
        desc: str = "simple_select_one",
    ) -> Optional[Tuple[Any, ...]]:
        """Executes a SELECT query on the named table, which is expected to
        return a single row, returning multiple columns from it.

        Args:
            table: string giving the table name
            keyvalues: dict of column names and values to select the row with
            retcols: list of strings giving the names of the columns to return
            allow_none: If true, return None instead of failing if the SELECT
                statement returns no rows
            desc: description of the transaction, for logging and metrics
        """
        return await self.runInteraction(
            desc,
            self.simple_select_one_txn,
            table,
            keyvalues,
            retcols,
            allow_none,
            db_autocommit=True,
        )

    @overload
    async def simple_select_one_onecol(
        self,
        table: str,
        keyvalues: Dict[str, Any],
        retcol: str,
        allow_none: Literal[False] = False,
        desc: str = "simple_select_one_onecol",
    ) -> Any: ...

    @overload
    async def simple_select_one_onecol(
        self,
        table: str,
        keyvalues: Dict[str, Any],
        retcol: str,
        allow_none: Literal[True] = True,
        desc: str = "simple_select_one_onecol",
    ) -> Optional[Any]: ...

    async def simple_select_one_onecol(
        self,
        table: str,
        keyvalues: Dict[str, Any],
        retcol: str,
        allow_none: bool = False,
        desc: str = "simple_select_one_onecol",
    ) -> Optional[Any]:
        """Executes a SELECT query on the named table, which is expected to
        return a single row, returning a single column from it.

        Args:
            table: string giving the table name
            keyvalues: dict of column names and values to select the row with
            retcol: string giving the name of the column to return
            allow_none: If true, return None instead of raising StoreError if the SELECT
                statement returns no rows
            desc: description of the transaction, for logging and metrics
        """
        return await self.runInteraction(
            desc,
            self.simple_select_one_onecol_txn,
            table,
            keyvalues,
            retcol,
            allow_none=allow_none,
            db_autocommit=True,
        )

    @overload
    @classmethod
    def simple_select_one_onecol_txn(
        cls,
        txn: LoggingTransaction,
        table: str,
        keyvalues: Dict[str, Any],
        retcol: str,
        allow_none: Literal[False] = False,
    ) -> Any: ...

    @overload
    @classmethod
    def simple_select_one_onecol_txn(
        cls,
        txn: LoggingTransaction,
        table: str,
        keyvalues: Dict[str, Any],
        retcol: str,
        allow_none: Literal[True] = True,
    ) -> Optional[Any]: ...

    @classmethod
    def simple_select_one_onecol_txn(
        cls,
        txn: LoggingTransaction,
        table: str,
        keyvalues: Dict[str, Any],
        retcol: str,
        allow_none: bool = False,
    ) -> Optional[Any]:
        ret = cls.simple_select_onecol_txn(
            txn, table=table, keyvalues=keyvalues, retcol=retcol
        )

        if ret:
            return ret[0]
        else:
            if allow_none:
                return None
            else:
                raise StoreError(404, "No row found")

    @staticmethod
    def simple_select_onecol_txn(
        txn: LoggingTransaction,
        table: str,
        keyvalues: Dict[str, Any],
        retcol: str,
    ) -> List[Any]:
        sql = ("SELECT %(retcol)s FROM %(table)s") % {"retcol": retcol, "table": table}

        if keyvalues:
            sql += " WHERE %s" % " AND ".join("%s = ?" % k for k in keyvalues.keys())
            txn.execute(sql, list(keyvalues.values()))
        else:
            txn.execute(sql)

        return [r[0] for r in txn]

    async def simple_select_onecol(
        self,
        table: str,
        keyvalues: Optional[Dict[str, Any]],
        retcol: str,
        desc: str = "simple_select_onecol",
    ) -> List[Any]:
        """Executes a SELECT query on the named table, which returns a list
        comprising of the values of the named column from the selected rows.

        Args:
            table: table name
            keyvalues: column names and values to select the rows with
            retcol: column whos value we wish to retrieve.
            desc: description of the transaction, for logging and metrics

        Returns:
            Results in a list
        """
        return await self.runInteraction(
            desc,
            self.simple_select_onecol_txn,
            table,
            keyvalues,
            retcol,
            db_autocommit=True,
        )

    async def simple_select_list(
        self,
        table: str,
        keyvalues: Optional[Dict[str, Any]],
        retcols: Collection[str],
        desc: str = "simple_select_list",
    ) -> List[Tuple[Any, ...]]:
        """Executes a SELECT query on the named table, which may return zero or
        more rows, returning the result as a list of tuples.

        Args:
            table: the table name
            keyvalues:
                column names and values to select the rows with, or None to not
                apply a WHERE clause.
            retcols: the names of the columns to return
            desc: description of the transaction, for logging and metrics

        Returns:
            A list of tuples, one per result row, each the retcolumn's value for the row.
        """
        return await self.runInteraction(
            desc,
            self.simple_select_list_txn,
            table,
            keyvalues,
            retcols,
            db_autocommit=True,
        )

    @classmethod
    def simple_select_list_txn(
        cls,
        txn: LoggingTransaction,
        table: str,
        keyvalues: Optional[Dict[str, Any]],
        retcols: Iterable[str],
    ) -> List[Tuple[Any, ...]]:
        """Executes a SELECT query on the named table, which may return zero or
        more rows, returning the result as a list of tuples.

        Args:
            txn: Transaction object
            table: the table name
            keyvalues:
                column names and values to select the rows with, or None to not
                apply a WHERE clause.
            retcols: the names of the columns to return

        Returns:
            A list of tuples, one per result row, each the retcolumn's value for the row.
        """
        if keyvalues:
            sql = "SELECT %s FROM %s WHERE %s" % (
                ", ".join(retcols),
                table,
                " AND ".join("%s = ?" % (k,) for k in keyvalues),
            )
            txn.execute(sql, list(keyvalues.values()))
        else:
            sql = "SELECT %s FROM %s" % (", ".join(retcols), table)
            txn.execute(sql)

        return txn.fetchall()

    async def simple_select_many_batch(
        self,
        table: str,
        column: str,
        iterable: Iterable[Any],
        retcols: Collection[str],
        keyvalues: Optional[Dict[str, Any]] = None,
        desc: str = "simple_select_many_batch",
        batch_size: int = 100,
    ) -> List[Tuple[Any, ...]]:
        """Executes a SELECT query on the named table, which may return zero or
        more rows.

        Filters rows by whether the value of `column` is in `iterable`.

        Args:
            table: string giving the table name
            column: column name to test for inclusion against `iterable`
            iterable: list
            retcols: list of strings giving the names of the columns to return
            keyvalues: dict of column names and values to select the rows with
            desc: description of the transaction, for logging and metrics
            batch_size: the number of rows for each select query

        Returns:
            The results as a list of tuples.
        """
        keyvalues = keyvalues or {}

        results: List[Tuple[Any, ...]] = []

        for chunk in batch_iter(iterable, batch_size):
            rows = await self.runInteraction(
                desc,
                self.simple_select_many_txn,
                table,
                column,
                chunk,
                keyvalues,
                retcols,
                db_autocommit=True,
            )

            results.extend(rows)

        return results

    @classmethod
    def simple_select_many_txn(
        cls,
        txn: LoggingTransaction,
        table: str,
        column: str,
        iterable: Collection[Any],
        keyvalues: Dict[str, Any],
        retcols: Iterable[str],
    ) -> List[Tuple[Any, ...]]:
        """Executes a SELECT query on the named table, which may return zero or
        more rows.

        Filters rows by whether the value of `column` is in `iterable`.

        Args:
            txn: Transaction object
            table: string giving the table name
            column: column name to test for inclusion against `iterable`
            iterable: list
            keyvalues: dict of column names and values to select the rows with
            retcols: list of strings giving the names of the columns to return

        Returns:
            The results as a list of tuples.
        """
        # If there's nothing to select, then skip executing the query.
        if not iterable:
            return []

        clause, values = make_in_list_sql_clause(txn.database_engine, column, iterable)
        clauses = [clause]

        for key, value in keyvalues.items():
            clauses.append("%s = ?" % (key,))
            values.append(value)

        sql = "SELECT %s FROM %s WHERE %s" % (
            ", ".join(retcols),
            table,
            " AND ".join(clauses),
        )

        txn.execute(sql, values)
        return txn.fetchall()

    async def simple_update(
        self,
        table: str,
        keyvalues: Dict[str, Any],
        updatevalues: Dict[str, Any],
        desc: str,
    ) -> int:
        """
        Update rows in the given database table.
        If the given keyvalues don't match anything, nothing will be updated.

        Args:
            table: The database table to update.
            keyvalues: A mapping of column name to value to match rows on.
            updatevalues: A mapping of column name to value to replace in any matched rows.
            desc: description of the transaction, for logging and metrics.

        Returns:
            The number of rows that were updated. Will be 0 if no matching rows were found.
        """
        return await self.runInteraction(
            desc, self.simple_update_txn, table, keyvalues, updatevalues
        )

    @staticmethod
    def simple_update_txn(
        txn: LoggingTransaction,
        table: str,
        keyvalues: Mapping[str, Any],
        updatevalues: Mapping[str, Any],
    ) -> int:
        """
        Update rows in the given database table.
        If the given keyvalues don't match anything, nothing will be updated.

        Args:
            txn: The database transaction object.
            table: The database table to update.
            keyvalues: A mapping of column name to value to match rows on.
            updatevalues: A mapping of column name to value to replace in any matched rows.

        Returns:
            The number of rows that were updated. Will be 0 if no matching rows were found.
        """
        if keyvalues:
            where = "WHERE %s" % " AND ".join("%s = ?" % k for k in keyvalues.keys())
        else:
            where = ""

        update_sql = "UPDATE %s SET %s %s" % (
            table,
            ", ".join("%s = ?" % (k,) for k in updatevalues),
            where,
        )

        txn.execute(update_sql, list(updatevalues.values()) + list(keyvalues.values()))

        return txn.rowcount

    async def simple_update_many(
        self,
        table: str,
        key_names: Collection[str],
        key_values: Collection[Iterable[Any]],
        value_names: Collection[str],
        value_values: Iterable[Iterable[Any]],
        desc: str,
    ) -> None:
        """
        Update, many times, using batching where possible.
        If the keys don't match anything, nothing will be updated.

        Args:
            table: The table to update
            key_names: The key column names.
            key_values: A list of each row's key column values.
            value_names: The names of value columns to update.
            value_values: A list of each row's value column values.
        """

        await self.runInteraction(
            desc,
            self.simple_update_many_txn,
            table,
            key_names,
            key_values,
            value_names,
            value_values,
        )

    @staticmethod
    def simple_update_many_txn(
        txn: LoggingTransaction,
        table: str,
        key_names: Collection[str],
        key_values: Collection[Iterable[Any]],
        value_names: Collection[str],
        value_values: Collection[Iterable[Any]],
    ) -> None:
        """
        Update, many times, using batching where possible.
        If the keys don't match anything, nothing will be updated.

        Args:
            table: The table to update
            key_names: The key column names.
            key_values: A list of each row's key column values.
            value_names: The names of value columns to update.
            value_values: A list of each row's value column values.
        """

        if len(value_values) != len(key_values):
            raise ValueError(
                f"{len(key_values)} key rows and {len(value_values)} value rows: should be the same number."
            )
        # If there is nothing to update, then skip executing the query.
        if not key_values:
            return

        # List of tuples of (value values, then key values)
        # (This matches the order needed for the query)
        args = [tuple(vv) + tuple(kv) for vv, kv in zip(value_values, key_values)]

        # 'col1 = ?, col2 = ?, ...'
        set_clause = ", ".join(f"{n} = ?" for n in value_names)

        if key_names:
            # 'WHERE col3 = ? AND col4 = ? AND col5 = ?'
            where_clause = "WHERE " + (" AND ".join(f"{n} = ?" for n in key_names))
        else:
            where_clause = ""

        # UPDATE mytable SET col1 = ?, col2 = ? WHERE col3 = ? AND col4 = ?
        sql = f"UPDATE {table} SET {set_clause} {where_clause}"

        txn.execute_batch(sql, args)

    async def simple_update_one(
        self,
        table: str,
        keyvalues: Dict[str, Any],
        updatevalues: Dict[str, Any],
        desc: str = "simple_update_one",
    ) -> None:
        """Executes an UPDATE query on the named table, setting new values for
        columns in a row matching the key values.

        Args:
            table: string giving the table name
            keyvalues: dict of column names and values to select the row with
            updatevalues: dict giving column names and values to update
            desc: description of the transaction, for logging and metrics
        """
        await self.runInteraction(
            desc,
            self.simple_update_one_txn,
            table,
            keyvalues,
            updatevalues,
            db_autocommit=True,
        )

    @classmethod
    def simple_update_one_txn(
        cls,
        txn: LoggingTransaction,
        table: str,
        keyvalues: Dict[str, Any],
        updatevalues: Dict[str, Any],
    ) -> None:
        rowcount = cls.simple_update_txn(txn, table, keyvalues, updatevalues)

        if rowcount == 0:
            raise StoreError(404, "No row found (%s)" % (table,))
        if rowcount > 1:
            raise StoreError(500, "More than one row matched (%s)" % (table,))

    # Ideally we could use the overload decorator here to specify that the
    # return type is only optional if allow_none is True, but this does not work
    # when you call a static method from an instance.
    # See https://github.com/python/mypy/issues/7781
    @staticmethod
    def simple_select_one_txn(
        txn: LoggingTransaction,
        table: str,
        keyvalues: Dict[str, Any],
        retcols: Collection[str],
        allow_none: bool = False,
    ) -> Optional[Tuple[Any, ...]]:
        select_sql = "SELECT %s FROM %s" % (", ".join(retcols), table)

        if keyvalues:
            select_sql += " WHERE %s" % (" AND ".join("%s = ?" % k for k in keyvalues),)
            txn.execute(select_sql, list(keyvalues.values()))
        else:
            txn.execute(select_sql)

        row = txn.fetchone()

        if not row:
            if allow_none:
                return None
            raise StoreError(404, "No row found (%s)" % (table,))
        if txn.rowcount > 1:
            raise StoreError(500, "More than one row matched (%s)" % (table,))

        return row

    async def simple_delete_one(
        self, table: str, keyvalues: Dict[str, Any], desc: str = "simple_delete_one"
    ) -> None:
        """Executes a DELETE query on the named table, expecting to delete a
        single row.

        Args:
            table: string giving the table name
            keyvalues: dict of column names and values to select the row with
            desc: description of the transaction, for logging and metrics
        """
        await self.runInteraction(
            desc,
            self.simple_delete_one_txn,
            table,
            keyvalues,
            db_autocommit=True,
        )

    @staticmethod
    def simple_delete_one_txn(
        txn: LoggingTransaction, table: str, keyvalues: Dict[str, Any]
    ) -> None:
        """Executes a DELETE query on the named table, expecting to delete a
        single row.

        Args:
            table: string giving the table name
            keyvalues: dict of column names and values to select the row with
        """
        sql = "DELETE FROM %s WHERE %s" % (
            table,
            " AND ".join("%s = ?" % (k,) for k in keyvalues),
        )

        txn.execute(sql, list(keyvalues.values()))
        if txn.rowcount == 0:
            raise StoreError(404, "No row found (%s)" % (table,))
        if txn.rowcount > 1:
            raise StoreError(500, "More than one row matched (%s)" % (table,))

    async def simple_delete(
        self, table: str, keyvalues: Dict[str, Any], desc: str
    ) -> int:
        """Executes a DELETE query on the named table.

        Filters rows by the key-value pairs.

        Args:
            table: string giving the table name
            keyvalues: dict of column names and values to select the row with
            desc: description of the transaction, for logging and metrics

        Returns:
            The number of deleted rows.
        """
        return await self.runInteraction(
            desc, self.simple_delete_txn, table, keyvalues, db_autocommit=True
        )

    @staticmethod
    def simple_delete_txn(
        txn: LoggingTransaction, table: str, keyvalues: Dict[str, Any]
    ) -> int:
        """Executes a DELETE query on the named table.

        Filters rows by the key-value pairs.

        Args:
            table: string giving the table name
            keyvalues: dict of column names and values to select the row with

        Returns:
            The number of deleted rows.
        """
        sql = "DELETE FROM %s WHERE %s" % (
            table,
            " AND ".join("%s = ?" % (k,) for k in keyvalues),
        )

        txn.execute(sql, list(keyvalues.values()))
        return txn.rowcount

    async def simple_delete_many(
        self,
        table: str,
        column: str,
        iterable: Collection[Any],
        keyvalues: Dict[str, Any],
        desc: str,
    ) -> int:
        """Executes a DELETE query on the named table.

        Filters rows by if value of `column` is in `iterable`.

        Args:
            table: string giving the table name
            column: column name to test for inclusion against `iterable`
            iterable: list of values to match against `column`. NB cannot be a generator
                as it may be evaluated multiple times.
            keyvalues: dict of column names and values to select the rows with
            desc: description of the transaction, for logging and metrics

        Returns:
            Number rows deleted
        """
        return await self.runInteraction(
            desc,
            self.simple_delete_many_txn,
            table,
            column,
            iterable,
            keyvalues,
            db_autocommit=True,
        )

    @staticmethod
    def simple_delete_many_txn(
        txn: LoggingTransaction,
        table: str,
        column: str,
        values: Collection[Any],
        keyvalues: Dict[str, Any],
    ) -> int:
        """Executes a DELETE query on the named table.

        Deletes the rows:
          - whose value of `column` is in `values`; AND
          - that match extra column-value pairs specified in `keyvalues`.

        Args:
            txn: Transaction object
            table: string giving the table name
            column: column name to test for inclusion against `values`
            values: values of `column` which choose rows to delete
            keyvalues: dict of extra column names and values to select the rows
                with. They will be ANDed together with the main predicate.

        Returns:
            Number rows deleted
        """
        # If there's nothing to delete, then skip executing the query.
        if not values:
            return 0

        clause, values = make_in_list_sql_clause(txn.database_engine, column, values)
        clauses = [clause]

        for key, value in keyvalues.items():
            clauses.append("%s = ?" % (key,))
            values.append(value)

        sql = "DELETE FROM %s WHERE %s" % (table, " AND ".join(clauses))
        txn.execute(sql, values)

        return txn.rowcount

    @staticmethod
    def simple_delete_many_batch_txn(
        txn: LoggingTransaction,
        table: str,
        keys: Collection[str],
        values: Iterable[Iterable[Any]],
    ) -> None:
        """Executes a DELETE query on the named table.

        The input is given as a list of rows, where each row is a list of values.
        (Actually any iterable is fine.)

        Args:
            txn: The transaction to use.
            table: string giving the table name
            keys: list of column names
            values: for each row, a list of values in the same order as `keys`
        """

        if isinstance(txn.database_engine, PostgresEngine):
            # We use `execute_values` as it can be a lot faster than `execute_batch`,
            # but it's only available on postgres.
            sql = "DELETE FROM %s WHERE (%s) IN (VALUES ?)" % (
                table,
                ", ".join(k for k in keys),
            )

            txn.execute_values(sql, values, fetch=False)
        else:
            sql = "DELETE FROM %s WHERE (%s) = (%s)" % (
                table,
                ", ".join(k for k in keys),
                ", ".join("?" for _ in keys),
            )

            txn.execute_batch(sql, values)

    def get_cache_dict(
        self,
        db_conn: LoggingDatabaseConnection,
        table: str,
        entity_column: str,
        stream_column: str,
        max_value: int,
        limit: int = 100000,
    ) -> Tuple[Dict[Any, int], int]:
        """Gets roughly the last N changes in the given stream table as a
        map from entity to the stream ID of the most recent change.

        Also returns the minimum stream ID.
        """

        # This may return many rows for the same entity, but the `limit` is only
        # a suggestion so we don't care that much.
        #
        # Note: Some stream tables can have multiple rows with the same stream
        # ID. Instead of handling this with complicated SQL, we instead simply
        # add one to the returned minimum stream ID to ensure correctness.
        sql = f"""
            SELECT {entity_column}, {stream_column}
            FROM {table}
            ORDER BY {stream_column} DESC
            LIMIT ?
        """

        txn = db_conn.cursor(txn_name="get_cache_dict")
        txn.execute(sql, (limit,))

        # The rows come out in reverse stream ID order, so we want to keep the
        # stream ID of the first row for each entity.
        cache: Dict[Any, int] = {}
        for row in txn:
            cache.setdefault(row[0], int(row[1]))

        txn.close()

        if cache:
            # We add one here as we don't know if we have all rows for the
            # minimum stream ID.
            min_val = min(cache.values()) + 1
        else:
            min_val = max_value

        return cache, min_val

    @classmethod
    def simple_select_list_paginate_txn(
        cls,
        txn: LoggingTransaction,
        table: str,
        orderby: str,
        start: int,
        limit: int,
        retcols: Iterable[str],
        filters: Optional[Dict[str, Any]] = None,
        keyvalues: Optional[Dict[str, Any]] = None,
        exclude_keyvalues: Optional[Dict[str, Any]] = None,
        order_direction: str = "ASC",
    ) -> List[Tuple[Any, ...]]:
        """
        Executes a SELECT query on the named table with start and limit,
        of row numbers, which may return zero or number of rows from start to limit,
        returning the result as a list of dicts.

        Use `filters` to search attributes using SQL wildcards and/or `keyvalues` to
        select attributes with exact matches. All constraints are joined together
        using 'AND'.

        Args:
            txn: Transaction object
            table: the table name
            orderby: Column to order the results by.
            start: Index to begin the query at.
            limit: Number of results to return.
            retcols: the names of the columns to return
            filters:
                column names and values to filter the rows with, or None to not
                apply a WHERE ? LIKE ? clause.
            keyvalues:
                column names and values to select the rows with, or None to not
                apply a WHERE key = value clause.
            exclude_keyvalues:
                column names and values to exclude rows with, or None to not
                apply a WHERE key != value clause.
            order_direction: Whether the results should be ordered "ASC" or "DESC".

        Returns:
            The result as a list of tuples.
        """
        if order_direction not in ["ASC", "DESC"]:
            raise ValueError("order_direction must be one of 'ASC' or 'DESC'.")

        where_clause = "WHERE " if filters or keyvalues or exclude_keyvalues else ""
        arg_list: List[Any] = []
        if filters:
            where_clause += " AND ".join("%s LIKE ?" % (k,) for k in filters)
            arg_list += list(filters.values())
        where_clause += " AND " if filters and keyvalues else ""
        if keyvalues:
            where_clause += " AND ".join("%s = ?" % (k,) for k in keyvalues)
            arg_list += list(keyvalues.values())
        if exclude_keyvalues:
            where_clause += " AND ".join("%s != ?" % (k,) for k in exclude_keyvalues)
            arg_list += list(exclude_keyvalues.values())

        sql = "SELECT %s FROM %s %s ORDER BY %s %s LIMIT ? OFFSET ?" % (
            ", ".join(retcols),
            table,
            where_clause,
            orderby,
            order_direction,
        )
        txn.execute(sql, arg_list + [limit, start])

        return txn.fetchall()


def make_in_list_sql_clause(
    database_engine: BaseDatabaseEngine,
    column: str,
    iterable: Collection[Any],
    *,
    negative: bool = False,
) -> Tuple[str, list]:
    """Returns an SQL clause that checks the given column is in the iterable.

    On SQLite this expands to `column IN (?, ?, ...)`, whereas on Postgres
    it expands to `column = ANY(?)`. While both DBs support the `IN` form,
    using the `ANY` form on postgres means that it views queries with
    different length iterables as the same, helping the query stats.

    Args:
        database_engine
        column: Name of the column
        iterable: The values to check the column against.
        negative: Whether we should check for inequality, i.e. `NOT IN`

    Returns:
        A tuple of SQL query and the args
    """

    if database_engine.supports_using_any_list:
        # This should hopefully be faster, but also makes postgres query
        # stats easier to understand.
        if not negative:
            clause = f"{column} = ANY(?)"
        else:
            clause = f"{column} != ALL(?)"

        return clause, [list(iterable)]
    else:
        params = ",".join("?" for _ in iterable)
        if not negative:
            clause = f"{column} IN ({params})"
        else:
            clause = f"{column} NOT IN ({params})"
        return clause, list(iterable)


# These overloads ensure that `columns` and `iterable` values have the same length.
# Suppress "Single overload definition, multiple required" complaint.
@overload  # type: ignore[misc]
def make_tuple_in_list_sql_clause(
    database_engine: BaseDatabaseEngine,
    columns: Tuple[str, str],
    iterable: Collection[Tuple[Any, Any]],
) -> Tuple[str, list]: ...


def make_tuple_in_list_sql_clause(
    database_engine: BaseDatabaseEngine,
    columns: Tuple[str, ...],
    iterable: Collection[Tuple[Any, ...]],
) -> Tuple[str, list]:
    """Returns an SQL clause that checks the given tuple of columns is in the iterable.

    Args:
        database_engine
        columns: Names of the columns in the tuple.
        iterable: The tuples to check the columns against.

    Returns:
        A tuple of SQL query and the args
    """
    if len(columns) == 0:
        # Should be unreachable due to mypy, as long as the overloads are set up right.
        if () in iterable:
            return "TRUE", []
        else:
            return "FALSE", []

    if len(columns) == 1:
        # Use `= ANY(?)` on postgres.
        return make_in_list_sql_clause(
            database_engine, next(iter(columns)), [values[0] for values in iterable]
        )

    # There are multiple columns. Avoid using an `= ANY(?)` clause on postgres, as
    # indices are not used when there are multiple columns. Instead, use an `IN`
    # expression.
    #
    # `IN ((?, ...), ...)` with tuples is supported by postgres only, whereas
    # `IN (VALUES (?, ...), ...)` is supported by both sqlite and postgres.
    # Thus, the latter is chosen.

    if len(iterable) == 0:
        # A 0-length `VALUES` list is not allowed in sqlite or postgres.
        # Also note that a 0-length `IN (...)` clause (not using `VALUES`) is not
        # allowed in postgres.
        return "FALSE", []

    tuple_sql = "(%s)" % (",".join("?" for _ in columns),)
    return "(%s) IN (VALUES %s)" % (
        ",".join(column for column in columns),
        ",".join(tuple_sql for _ in iterable),
    ), [value for values in iterable for value in values]


KV = TypeVar("KV")


def make_tuple_comparison_clause(keys: List[Tuple[str, KV]]) -> Tuple[str, List[KV]]:
    """Returns a tuple comparison SQL clause

    Builds a SQL clause that looks like "(a, b) > (?, ?)"

    Args:
        keys: A set of (column, value) pairs to be compared.

    Returns:
        A tuple of SQL query and the args
    """
    return (
        "(%s) > (%s)" % (",".join(k[0] for k in keys), ",".join("?" for _ in keys)),
        [k[1] for k in keys],
    )
