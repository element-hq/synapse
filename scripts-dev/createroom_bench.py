#!/usr/bin/env python
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

"""Time `/createRoom` against a real Synapse on Postgres.

Starts the server the way `persist_bench.py` does, once per
`rust_event_persistence` level. For each scenario it creates five warm-up
rooms, then `--rooms` rooms one after another, and reports the mean and median
request time and the CPU of the reactor thread and of the process per room.

    poetry run python scripts-dev/createroom_bench.py --levels off,full,full+rooms

A level with `+rooms` also turns on `rust_room_creation`. `--psycopg2` runs
the server on psycopg2 instead of the native driver.
"""

import argparse
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import persist_bench as pb  # noqa: E402

SCENARIOS: dict[str, dict[str, Any]] = {
    "plain": {},
    # The room the E2EE monitor on jki.re creates every minute.
    "e2ee_invite": {
        "preset": "trusted_private_chat",
        "is_direct": True,
        "invite": ["@invitee:bench"],
        "initial_state": [
            {
                "type": "m.room.encryption",
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            }
        ],
    },
    "private_invite": {"preset": "private_chat", "invite": ["@invitee:bench"]},
    "public_named": {"preset": "public_chat", "name": "Bench room", "topic": "A topic"},
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
                "state_key": "!a:bench",
                "content": {"via": ["bench"]},
            },
            {
                "type": "m.space.child",
                "state_key": "!b:bench",
                "content": {"via": ["bench"]},
            },
        ]
    },
}

CREATE_ROOM = "/_matrix/client/v3/createRoom"

# The local port `pg_roundtrip_proxy.py` listens on, with `--proxy-delay-ms`.
PROXY_PORT = 15432


class RoundTripProxy:
    """`pg_roundtrip_proxy.py` between the server and Postgres, counting the
    round trips of each request."""

    def __init__(self, delay_ms: float) -> None:
        self.stats_dir = tempfile.mkdtemp(prefix="pg-roundtrips-")
        self.stats = os.path.join(self.stats_dir, "stats.json")
        target = (
            f"{os.environ.get('PGHOST', 'localhost')}:"
            f"{os.environ.get('PGPORT', '5432')}"
        )
        self.process = subprocess.Popen(
            [
                sys.executable,
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "pg_roundtrip_proxy.py"
                ),
                f"--listen={PROXY_PORT}",
                f"--target={target}",
                f"--delay-ms={delay_ms}",
                f"--stats={self.stats}",
            ]
        )
        time.sleep(1)
        self.check_running()
        os.environ["PGHOST"] = "127.0.0.1"
        os.environ["PGPORT"] = str(PROXY_PORT)

    def check_running(self) -> None:
        if self.process.poll() is not None:
            raise RuntimeError(
                f"pg_roundtrip_proxy.py exited with {self.process.returncode}"
            )

    def reset(self) -> None:
        self.process.send_signal(signal.SIGUSR2)
        time.sleep(0.05)

    def turns(self) -> int:
        """The round trips since the last `reset`."""
        # The proxy replaces the file atomically, so it is complete once it
        # is gone and back.
        if os.path.exists(self.stats):
            os.unlink(self.stats)
        self.process.send_signal(signal.SIGUSR1)
        deadline = time.monotonic() + 10
        while not os.path.exists(self.stats):
            self.check_running()
            if time.monotonic() > deadline:
                raise RuntimeError("pg_roundtrip_proxy.py did not write its stats")
            time.sleep(0.01)
        with open(self.stats) as f:
            return json.load(f)["turns"]

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait()
        shutil.rmtree(self.stats_dir, ignore_errors=True)


def bench_level(
    level: str,
    rooms: int,
    rust_driver: bool,
    scenarios: list[str],
    proxy: RoundTripProxy | None = None,
) -> list[dict[str, Any]]:
    """Benchmark a server at `level`: a `rust_event_persistence` level,
    optionally followed by `+rooms` for `rust_room_creation: on`."""
    results = []
    persist_level, _, extra = level.partition("+")
    if extra not in ("", "rooms"):
        raise ValueError(f"unknown level suffix {extra!r} in {level!r}")
    experimental = {"rust_room_creation": "on"} if extra == "rooms" else {}
    workdir = tempfile.mkdtemp(prefix=f"createroom-bench-{persist_level}-")
    server = pb.Server(
        persist_level, workdir, rust_driver=rust_driver, experimental=experimental
    )
    try:
        server.start()
        token = pb.register("creator")
        pb.register("invitee")
        for name in scenarios:
            body = SCENARIOS[name]
            for _ in range(5):
                pb.ok("POST", CREATE_ROOM, body, token)
            before = server.sample()
            times = []
            turns = []
            for _ in range(rooms):
                if proxy is not None:
                    # Let the last room's background work finish first.
                    time.sleep(0.5)
                    proxy.reset()
                t0 = time.perf_counter()
                pb.ok("POST", CREATE_ROOM, body, token)
                times.append(time.perf_counter() - t0)
                if proxy is not None:
                    turns.append(proxy.turns())
            after = server.sample()
            row = {
                "level": level,
                "scenario": name,
                "mean_ms": 1000 * statistics.mean(times),
                "p50_ms": 1000 * statistics.median(times),
                "reactor_cpu_ms": 1000
                * (after.reactor_cpu - before.reactor_cpu)
                / rooms,
                "process_cpu_ms": 1000
                * (after.process_cpu - before.process_cpu)
                / rooms,
            }
            if turns:
                # Background work from the request counts too, if it
                # overlaps.
                row["round_trips"] = statistics.median(turns)
            results.append(row)
            print(row, flush=True)
    finally:
        server.stop()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--levels",
        default="off,full,full+rooms",
        help="comma-separated `rust_event_persistence` levels; append `+rooms`"
        " to a level for `rust_room_creation: on`",
    )
    parser.add_argument("--rooms", type=int, default=40)
    parser.add_argument("--psycopg2", action="store_true")
    parser.add_argument(
        "--scenarios",
        default=",".join(SCENARIOS),
        help="comma-separated scenarios to run, from: " + ", ".join(SCENARIOS),
    )
    parser.add_argument(
        "--proxy-delay-ms",
        type=float,
        help="run Postgres connections through pg_roundtrip_proxy.py, adding this"
        " much latency to each round trip (0 to only count), and report the round"
        " trips per request. The extra wall time over a run without the proxy,"
        " divided by the delay, is the number of round trips the request waits"
        " for in turn.",
    )
    parser.add_argument("--json", help="also write the results to this file")
    args = parser.parse_args()

    proxy = None
    if args.proxy_delay_ms is not None:
        proxy = RoundTripProxy(args.proxy_delay_ms)

    results = []
    try:
        for level in args.levels.split(","):
            results += bench_level(
                level,
                args.rooms,
                rust_driver=not args.psycopg2,
                scenarios=args.scenarios.split(","),
                proxy=proxy,
            )
    finally:
        if proxy is not None:
            proxy.stop()

    print(
        f"\n{'scenario':<16} {'level':<12} {'mean ms':>8} {'p50 ms':>8}"
        f" {'reactor ms':>11} {'process ms':>11} {'round trips':>12}"
    )
    for r in results:
        print(
            f"{r['scenario']:<16} {r['level']:<12} {r['mean_ms']:>8.1f}"
            f" {r['p50_ms']:>8.1f} {r['reactor_cpu_ms']:>11.1f}"
            f" {r['process_cpu_ms']:>11.1f} {r.get('round_trips', ''):>12}"
        )
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
