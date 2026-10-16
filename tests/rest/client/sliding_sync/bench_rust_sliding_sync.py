#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Sliding sync in Rust against Python, timed per request.

Not a test: trial only collects `test_*.py`, and the cases skip unless
`SYNAPSE_BENCH_SLIDING_SYNC` is set. Build the extension in release mode
first, as a debug build makes Rust look several times slower:

    poetry run maturin develop --release
    SYNAPSE_BENCH_SLIDING_SYNC=1 SYNAPSE_TEST_RUST_DRIVER=1 \\
        SYNAPSE_TEST_RUST_SLIDING_SYNC=on POSTGRES=1 \\
        run-trial.sh tests.rest.client.sliding_sync.bench_rust_sliding_sync

Each scenario runs on two connections of one user, one served by Python and
one by Rust, in turn, so both see the same data and the same warm caches.
Every request is timed on its own. The table gives the median of `REPS`:

- wall: the whole request, servlet included.
- reactor: CPU time on the reactor thread, less the time inside Rust calls.
  The trial wrapper runs each Rust call's top-level future on the reactor
  thread through `block_on`, where production runs it on a tokio worker. Any
  Python that Rust calls back into counts as Rust's.
- process: CPU time of the whole process, Rust's threads included.
- db: time in database queries, Python's and Rust's. Rust runs some queries
  side by side and adds up each one's time, so this can exceed wall.
- queries: database queries, Python's and Rust's.
- txns: database transactions. Rust counts only its explicit transactions,
  not single reads, so compare queries across the two sides.

Trial runs Python's database calls on the reactor thread, which is not the
case in production. Python's reactor CPU therefore includes its query time,
and the `db` column shows how much of it that is.

Set `SYNAPSE_BENCH_OUT` to a path to also append the tables there, and
`SYNAPSE_BENCH_SCALE` to a fraction to make the accounts smaller for a quick
check of the script itself. Set `SYNAPSE_BENCH_PROFILE` to a directory to get
a cProfile dump of one more request per scenario and side, run after the
timed ones.
"""

import cProfile
import os
import re
import statistics
import time
from typing import Any, Callable
from unittest import SkipTest

import attr

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes, ReceiptTypes
from synapse.handlers.sliding_sync import (
    rust_sliding_sync_db_time,
    rust_sliding_sync_queries,
    rust_sliding_sync_requests_counter,
    rust_sliding_sync_transactions,
)
from synapse.metrics import SERVER_NAME_LABEL
from synapse.rest.client import account_data, login, receipts, room, sync, tags
from synapse.server import HomeServer
from synapse.storage.database import sql_query_timer, sql_txn_count
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import _BlockingRustCalls

REPS = 7

SCALE = float(os.environ.get("SYNAPSE_BENCH_SCALE", "1"))

REQUIRED_STATE = [
    [EventTypes.Create, ""],
    [EventTypes.Name, ""],
    [EventTypes.RoomAvatar, ""],
    [EventTypes.Topic, ""],
    [EventTypes.RoomEncryption, ""],
    [EventTypes.CanonicalAlias, ""],
    [EventTypes.PowerLevels, ""],
    [EventTypes.Member, "$LAZY"],
    [EventTypes.Member, "$ME"],
]

EXTENSIONS = {
    "account_data": {"enabled": True},
    "receipts": {"enabled": True},
}


def _list_body(
    end: int, timeline_limit: int = 20, extensions: bool = False
) -> JsonDict:
    body: JsonDict = {
        "lists": {
            "all": {
                "ranges": [[0, end]],
                "required_state": REQUIRED_STATE,
                "timeline_limit": timeline_limit,
            }
        }
    }
    if extensions:
        body["extensions"] = EXTENSIONS
    return body


def _counter_total(counter: object, server_name: str) -> float:
    """The sum over every label set of a counter for one server."""
    total = 0.0
    for metric in counter.collect():  # type: ignore[attr-defined]
        for sample in metric.samples:
            if (
                sample.name.endswith("_total")
                and sample.labels.get(SERVER_NAME_LABEL) == server_name
            ):
                total += sample.value
    return total


def _histogram_total(histogram: object, server_name: str, suffix: str) -> float:
    """The sum over every label set of a histogram's `_sum` or `_count` for
    one server."""
    total = 0.0
    for metric in histogram.collect():  # type: ignore[attr-defined]
        for sample in metric.samples:
            if (
                sample.name.endswith(suffix)
                and sample.labels.get(SERVER_NAME_LABEL) == server_name
            ):
                total += sample.value
    return total


class _TimedRustCalls(_BlockingRustCalls):
    """Blocking Rust calls that add up the reactor thread's CPU time inside
    them."""

    def __init__(self, handler: Any) -> None:
        super().__init__(handler)
        self.cpu = 0.0

    def __getattr__(self, name: str) -> Any:
        call = super().__getattr__(name)
        if getattr(self._handler, f"{name}_blocking", None) is None:
            return call

        def timed(*args: Any, **kwargs: Any) -> Any:
            start = time.thread_time()
            try:
                return call(*args, **kwargs)
            finally:
                self.cpu += time.thread_time() - start

        return timed


@attr.s(auto_attribs=True, frozen=True)
class Sample:
    wall: float
    reactor: float
    process: float
    db: float
    queries: float
    txns: float


class SlidingSyncBenchmark(SlidingSyncBase):
    """Times sliding sync requests in Rust and in Python."""

    servlets = [
        synapse.rest.admin.register_servlets,
        account_data.register_servlets,
        login.register_servlets,
        receipts.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        tags.register_servlets,
    ]

    # Room creation over the fake HTTP channel is slow at this size.
    timeout = 3600

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Trial's config sets the event cache to one entry, which turns Python's
        # off. A server keeps the events of the first screen cached.
        config["event_cache_size"] = 100_000
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        if not os.environ.get("SYNAPSE_BENCH_SLIDING_SYNC"):
            raise SkipTest("set SYNAPSE_BENCH_SLIDING_SYNC to run the benchmark")
        super().prepare(reactor, clock, hs)
        rust = hs.get_rust_handlers().sliding_sync
        if rust is None:
            raise SkipTest("needs the native Rust Postgres driver")
        self.rust = _TimedRustCalls(rust)
        self.handler = hs.get_sliding_sync_handler()
        self.handler._rust_sliding_sync_strict = True
        self.server_name = hs.hostname

        self.user1_id = self.register_user("user1", "pass")
        self.user1_tok = self.login(self.user1_id, "pass")
        self.user2_id = self.register_user("user2", "pass")
        self.user2_tok = self.login(self.user2_id, "pass")
        self.conn_seq = 0
        self.lines: list[str] = []

    def _rust_requests(self) -> float:
        return rust_sliding_sync_requests_counter.labels(
            result="rust", **{SERVER_NAME_LABEL: self.server_name}
        )._value.get()

    def _new_conn(self, side: str) -> str:
        self.conn_seq += 1
        return f"{side}-{self.conn_seq}"

    def _sync(
        self, side: str, conn_id: str, body: JsonDict, since: str | None
    ) -> tuple[JsonDict, str]:
        self.handler._rust_sliding_sync = (
            self.rust if side == "rust" else None  # type: ignore[assignment]
        )
        before = self._rust_requests()
        response, pos = self.do_sync(
            {**body, "conn_id": conn_id}, since=since, tok=self.user1_tok
        )
        self.assertEqual(self._rust_requests() - before, int(side == "rust"))
        return response, pos

    def _db_totals(self) -> tuple[float, float, float]:
        """Database time, queries and transactions so far, Python's and
        Rust's."""
        name = self.server_name
        return (
            _histogram_total(sql_query_timer, name, "_sum")
            + _histogram_total(rust_sliding_sync_db_time, name, "_sum"),
            _histogram_total(sql_query_timer, name, "_count")
            + _histogram_total(rust_sliding_sync_queries, name, "_sum"),
            _counter_total(sql_txn_count, name)
            + _histogram_total(rust_sliding_sync_transactions, name, "_sum"),
        )

    def _timed_sync(
        self, side: str, conn_id: str, body: JsonDict, since: str | None
    ) -> tuple[Sample, str]:
        db, queries, txns = self._db_totals()
        rust_cpu = self.rust.cpu
        wall = time.perf_counter()
        reactor = time.thread_time()
        process = time.process_time()

        _, pos = self._sync(side, conn_id, body, since)

        wall = time.perf_counter() - wall
        reactor = time.thread_time() - reactor - (self.rust.cpu - rust_cpu)
        process = time.process_time() - process
        db_after, queries_after, txns_after = self._db_totals()
        sample = Sample(
            wall=wall,
            reactor=reactor,
            process=process,
            db=db_after - db,
            queries=queries_after - queries,
            txns=txns_after - txns,
        )
        return sample, pos

    def _bench(
        self,
        name: str,
        rep: Callable[[str], tuple[Sample, str]],
    ) -> None:
        """Run `rep` once per side to warm up, then `REPS` times per side in
        turn, and record the medians."""
        for side in ("python", "rust"):
            rep(side)
        samples: dict[str, list[Sample]] = {"python": [], "rust": []}
        for _ in range(REPS):
            for side in ("python", "rust"):
                samples[side].append(rep(side)[0])
        profile_dir = os.environ.get("SYNAPSE_BENCH_PROFILE")
        if profile_dir:
            for side in ("python", "rust"):
                profile = cProfile.Profile()
                profile.runcall(rep, side)
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                profile.dump_stats(
                    os.path.join(
                        profile_dir, f"{self._testMethodName}-{slug}-{side}.prof"
                    )
                )
        for side in ("python", "rust"):
            s = samples[side]
            self.lines.append(
                f"| {name} | {side} "
                f"| {statistics.median(x.wall for x in s) * 1000:.1f} "
                f"| {statistics.median(x.reactor for x in s) * 1000:.1f} "
                f"| {statistics.median(x.process for x in s) * 1000:.1f} "
                f"| {statistics.median(x.db for x in s) * 1000:.1f} "
                f"| {statistics.median(x.queries for x in s):.0f} "
                f"| {statistics.median(x.txns for x in s):.0f} |"
            )

    def _report(self, title: str) -> None:
        table = "\n".join(
            [
                f"### {title}",
                "",
                "| scenario | side | wall ms | reactor ms | process ms | db ms "
                "| queries | txns |",
                "|---|---|---|---|---|---|---|---|",
                *self.lines,
                "",
            ]
        )
        print("\n" + table)
        out = os.environ.get("SYNAPSE_BENCH_OUT")
        if out:
            with open(out, "a") as f:
                f.write(table + "\n")

    def _make_rooms(self, count: int, messages: int) -> list[str]:
        """`count` rooms that user2 created and user1 joined, each with
        `messages` messages from user2, a receipt from each user and a name.
        """
        count = max(int(count * SCALE), 20)
        rooms = []
        for i in range(count):
            room_id = self.helper.create_room_as(
                self.user2_id,
                tok=self.user2_tok,
                extra_content={"name": f"room {i}"},
            )
            self.helper.join(room_id, self.user1_id, tok=self.user1_tok)
            event_id = ""
            for _ in range(messages):
                event_id = self.helper.send(room_id, "hi", tok=self.user2_tok)[
                    "event_id"
                ]
            for tok in (self.user1_tok, self.user2_tok):
                channel = self.make_request(
                    "POST",
                    f"/_matrix/client/v3/rooms/{room_id}/receipt/{ReceiptTypes.READ}/{event_id}",
                    content={},
                    access_token=tok,
                )
                self.assertEqual(channel.code, 200, channel.json_body)
            rooms.append(room_id)
        for room_id in rooms[:10]:
            channel = self.make_request(
                "PUT",
                f"/_matrix/client/v3/user/{self.user1_id}/rooms/{room_id}/tags/u.work",
                content={},
                access_token=self.user1_tok,
            )
            self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/user/{self.user1_id}/account_data/m.direct",
            content={self.user2_id: rooms[:5]},
            access_token=self.user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return rooms

    def _initial(self, body: JsonDict) -> Callable[[str], tuple[Sample, str]]:
        def rep(side: str) -> tuple[Sample, str]:
            return self._timed_sync(side, self._new_conn(side), body, None)

        return rep

    def _incremental(
        self,
        first_body: JsonDict,
        body: JsonDict,
        before: Callable[[], None] | None = None,
    ) -> Callable[[str], tuple[Sample, str]]:
        """Requests on one connection per side, after an untimed
        `first_body`. `before` runs ahead of each Python request, so both
        sides see what it does."""
        conns = {}
        for side in ("python", "rust"):
            conn_id = self._new_conn(side)
            _, pos = self._sync(side, conn_id, first_body, None)
            conns[side] = (conn_id, pos)

        def rep(side: str) -> tuple[Sample, str]:
            if before is not None and side == "python":
                before()
            conn_id, pos = conns[side]
            sample, pos = self._timed_sync(side, conn_id, body, pos)
            conns[side] = (conn_id, pos)
            return sample, pos

        return rep

    def _expansion(
        self, first_body: JsonDict, body: JsonDict
    ) -> Callable[[str], tuple[Sample, str]]:
        """The second request of a new connection."""

        def rep(side: str) -> tuple[Sample, str]:
            conn_id = self._new_conn(side)
            _, pos = self._sync(side, conn_id, first_body, None)
            return self._timed_sync(side, conn_id, body, pos)

        return rep

    def test_200_rooms(self) -> None:
        """An account in 200 rooms of 20 messages each."""
        rooms = self._make_rooms(200, 20)
        top = rooms[-1]

        def send_one() -> None:
            self.helper.send(top, "hi", tok=self.user2_tok)

        for extensions in (False, True):
            suffix = ", extensions" if extensions else ""
            first = _list_body(19, extensions=extensions)
            self._bench(f"initial [0,19]{suffix}", self._initial(first))
            self._bench(
                f"incremental, nothing changed{suffix}",
                self._incremental(first, first),
            )
            self._bench(
                f"incremental, one event{suffix}",
                self._incremental(first, first, send_one),
            )
            self._bench(
                f"range [0,19] to [0,99]{suffix}",
                self._expansion(first, _list_body(99, extensions=extensions)),
            )
        self._report("200 rooms, 20 messages each")

    def test_2000_rooms(self) -> None:
        """An account in 2000 rooms of one message each."""
        self._make_rooms(2000, 1)

        for extensions in (False, True):
            suffix = ", extensions" if extensions else ""
            first = _list_body(19, extensions=extensions)
            self._bench(f"initial [0,19]{suffix}", self._initial(first))
            self._bench(
                f"incremental, nothing changed{suffix}",
                self._incremental(first, first),
            )
            wide = _list_body(499, timeline_limit=1, extensions=extensions)
            self._bench(
                f"incremental, 500 rooms sent{suffix}",
                self._incremental(wide, wide),
            )
        self._report("2000 rooms, one message each")
