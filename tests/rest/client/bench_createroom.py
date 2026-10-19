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

"""`/createRoom` timed per room in the trial harness.

Not a test: trial only collects `test_*.py`, and the case skips unless
`SYNAPSE_BENCH_CREATEROOM` is set:

    SYNAPSE_BENCH_CREATEROOM=1 run-trial.sh tests.rest.client.bench_createroom

Each scenario creates three warm-up rooms, then `SYNAPSE_BENCH_ROOMS` rooms
(default 30), and reports wall and CPU time per room. Other settings:

- `SYNAPSE_BENCH_SCENARIOS`: a comma-separated subset of the scenarios.
- `SYNAPSE_BENCH_PROFILE`: a directory to write one cProfile dump per scenario.
- `SYNAPSE_BENCH_COUNT`: count database interactions and SQL statements per
  room, and print the most frequent of each.
- `SYNAPSE_BENCH_TRACE`: with `SYNAPSE_BENCH_COUNT`, print a stack trace for
  each interaction with this description.
"""

import cProfile
import os
import time
import traceback
from collections import Counter
from typing import Any
from unittest import SkipTest

from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage import database
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase

SCENARIOS: dict[str, dict[str, Any]] = {
    "plain": {},
    "private_invite": {"preset": "private_chat", "invite": ["@other:test"]},
    "public_named": {
        "preset": "public_chat",
        "name": "Bench room",
        "topic": "A topic",
        # Filled in per room, as aliases must be unique.
        "room_alias_name": None,
    },
    "initial_state": {
        "initial_state": [
            {
                "type": "m.room.encryption",
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            },
            {
                "type": "m.room.guest_access",
                "state_key": "",
                "content": {"guest_access": "can_join"},
            },
            {
                "type": "m.space.child",
                "state_key": "!a:test",
                "content": {"via": ["test"]},
            },
            {
                "type": "m.space.child",
                "state_key": "!b:test",
                "content": {"via": ["test"]},
            },
        ]
    },
}


class CreateRoomBench(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        # The test default of one entry makes every read after persisting a
        # database fetch, which no server does.
        config["event_cache_size"] = 10000
        return config

    def setUp(self) -> None:
        if not os.environ.get("SYNAPSE_BENCH_CREATEROOM"):
            raise SkipTest("set SYNAPSE_BENCH_CREATEROOM to run the benchmark")
        super().setUp()

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user = self.register_user("creator", "pass")
        self.tok = self.login("creator", "pass")
        self.register_user("other", "pass")
        self.counting = False
        self.txn_counts: Counter[str] = Counter()
        self.sql_counts: Counter[str] = Counter()

    def _install_counters(self) -> None:
        """Count database interactions by description and SQL statements by text."""
        bench = self
        orig_run = database.DatabasePool.runInteraction
        orig_exec = database.LoggingTransaction._do_execute

        trace = os.environ.get("SYNAPSE_BENCH_TRACE")

        def run(self: Any, desc: str, *args: Any, **kwargs: Any) -> Any:
            if bench.counting:
                bench.txn_counts[desc] += 1
                if desc == trace:
                    traceback.print_stack(limit=12)
            return orig_run(self, desc, *args, **kwargs)

        def do_execute(
            self: Any, func: Any, sql: str, *args: Any, **kwargs: Any
        ) -> Any:
            if bench.counting:
                bench.sql_counts[" ".join(sql.split())[:90]] += 1
            return orig_exec(self, func, sql, *args, **kwargs)

        database.DatabasePool.runInteraction = run  # type: ignore[method-assign]
        database.LoggingTransaction._do_execute = do_execute  # type: ignore[method-assign]
        self.addCleanup(setattr, database.DatabasePool, "runInteraction", orig_run)
        self.addCleanup(setattr, database.LoggingTransaction, "_do_execute", orig_exec)

    def _create(self, body: dict[str, Any], i: int) -> None:
        body = dict(body)
        if "room_alias_name" in body:
            body["room_alias_name"] = f"bench-{i}"
        # Rooms created in the same fake millisecond get the same v12 room ID,
        # which is retried, so give each room its own.
        self.reactor.advance(0.001)
        channel = self.make_request("POST", "/createRoom", body, access_token=self.tok)
        self.assertEqual(channel.code, 200, channel.result)

    def _print_counts(self, name: str, n: int) -> None:
        total = sum(self.txn_counts.values()) / n
        print(f"\n== {name}: DB interactions per room (total {total:.1f}) ==")
        for desc, c in self.txn_counts.most_common(60):
            print(f"{c / n:7.1f}  {desc}")
        total = sum(self.sql_counts.values()) / n
        print(f"== {name}: SQL statements per room (total {total:.1f}) ==")
        for sql, c in self.sql_counts.most_common(60):
            print(f"{c / n:7.1f}  {sql}")
        self.txn_counts.clear()
        self.sql_counts.clear()

    def test_bench(self) -> None:
        n = int(os.environ.get("SYNAPSE_BENCH_ROOMS", "30"))
        profile_dir = os.environ.get("SYNAPSE_BENCH_PROFILE")
        only = os.environ.get("SYNAPSE_BENCH_SCENARIOS")
        count = bool(os.environ.get("SYNAPSE_BENCH_COUNT"))
        if count:
            self._install_counters()

        rows = []
        for name, body in SCENARIOS.items():
            if only and name not in only.split(","):
                continue
            for i in range(3):
                self._create(body, -i - 1)

            prof = cProfile.Profile() if profile_dir else None
            t0 = time.perf_counter()
            c0 = time.process_time()
            if prof:
                prof.enable()
            self.counting = True
            for i in range(n):
                self._create(body, i)
            self.counting = False
            if prof:
                prof.disable()
            wall = time.perf_counter() - t0
            cpu = time.process_time() - c0

            if count:
                self._print_counts(name, n)
            rows.append((name, wall / n * 1000, cpu / n * 1000))
            if prof and profile_dir:
                os.makedirs(profile_dir, exist_ok=True)
                prof.dump_stats(os.path.join(profile_dir, f"{name}.prof"))

        print()
        print(f"{'scenario':16} {'wall ms/room':>12} {'cpu ms/room':>12}")
        for name, wall, cpu in rows:
            print(f"{name:16} {wall:12.1f} {cpu:12.1f}")
