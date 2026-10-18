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

    poetry run python scripts-dev/createroom_bench.py --levels off,full

`--psycopg2` runs the server on psycopg2 instead of the native driver.
"""

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import persist_bench as pb  # noqa: E402

SCENARIOS: dict[str, dict[str, Any]] = {
    "plain": {},
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


def bench_level(level: str, rooms: int, rust_driver: bool) -> list[dict[str, Any]]:
    results = []
    workdir = tempfile.mkdtemp(prefix=f"createroom-bench-{level}-")
    server = pb.Server(level, workdir, rust_driver=rust_driver)
    try:
        server.start()
        token = pb.register("creator")
        pb.register("invitee")
        for name, body in SCENARIOS.items():
            for _ in range(5):
                pb.ok("POST", CREATE_ROOM, body, token)
            before = server.sample()
            times = []
            for _ in range(rooms):
                t0 = time.perf_counter()
                pb.ok("POST", CREATE_ROOM, body, token)
                times.append(time.perf_counter() - t0)
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
            results.append(row)
            print(row, flush=True)
    finally:
        server.stop()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default="off,full")
    parser.add_argument("--rooms", type=int, default=40)
    parser.add_argument("--psycopg2", action="store_true")
    parser.add_argument("--json", help="also write the results to this file")
    args = parser.parse_args()

    results = []
    for level in args.levels.split(","):
        results += bench_level(level, args.rooms, rust_driver=not args.psycopg2)

    print(
        f"\n{'scenario':<16} {'level':<12} {'mean ms':>8} {'p50 ms':>8}"
        f" {'reactor ms':>11} {'process ms':>11}"
    )
    for r in results:
        print(
            f"{r['scenario']:<16} {r['level']:<12} {r['mean_ms']:>8.1f}"
            f" {r['p50_ms']:>8.1f} {r['reactor_cpu_ms']:>11.1f}"
            f" {r['process_cpu_ms']:>11.1f}"
        )
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
