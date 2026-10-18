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

"""Benchmark event persistence at each `rust_event_persistence` level.

For each level, start a real Synapse process on a fresh Postgres database
with the native Rust driver, run the same workloads through the client API,
and report per workload: wall time, the CPU of the reactor thread and of the
process, the change in resident memory, and the `persist_events`
transactions from the metrics endpoint.

    poetry run python scripts-dev/persist_bench.py --levels off,full

Needs a Postgres server the `PG*` environment variables reach, with
permission to create databases.
"""

import argparse
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

LEVELS = ("off", "prepare", "transaction", "full")
CLIENT_PORT = 18008
METRICS_PORT = 19092
SHARED_SECRET = "bench"


@dataclass
class Sample:
    """What the process had used at a point in time."""

    wall: float
    reactor_cpu: float
    process_cpu: float
    rss_mb: float
    metrics: dict[str, float]


@dataclass
class Result:
    level: str
    workload: str
    events: int
    wall: float
    reactor_cpu: float
    process_cpu: float
    rss_delta_mb: float
    persist_txns: float
    persist_txn_secs: float
    extra: dict[str, float] = field(default_factory=dict)


def _cpu_of(path: str) -> float:
    """utime + stime of a `/proc/.../stat` file, in seconds."""
    with open(path) as f:
        fields = f.read().rsplit(")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    return (int(fields[11]) + int(fields[12])) / ticks


def _rss_mb(pid: int) -> float:
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


_METRIC = re.compile(r"^(?P<name>[a-z_]+)(?:\{(?P<labels>[^}]*)\})? (?P<value>\S+)$")


def _metrics() -> dict[str, float]:
    """The metrics this benchmark reads, keyed `name{desc}`."""
    conn = http.client.HTTPConnection("127.0.0.1", METRICS_PORT, timeout=30)
    conn.request("GET", "/_synapse/metrics")
    body = conn.getresponse().read().decode()
    wanted = {}
    for line in body.splitlines():
        match = _METRIC.match(line)
        if not match:
            continue
        name = match["name"]
        labels = match["labels"] or ""
        if name.startswith("synapse_storage_transaction_time_"):
            desc = re.search(r'desc="([^"]*)"', labels)
            if desc:
                wanted[f"{name}{{{desc[1]}}}"] = float(match["value"])
        elif name in (
            "synapse_storage_events_persisted_events_total",
            "synapse_storage_events_rust_prepare_total",
        ):
            wanted[f"{name}{{{labels}}}"] = float(match["value"])
    return wanted


class Server:
    """A Synapse process on its own database."""

    def __init__(self, level: str, workdir: str, rust_driver: bool = True) -> None:
        self.level = level
        self.workdir = workdir
        self.rust_driver = rust_driver
        self.database = f"persist_bench_{level}_{secrets.token_hex(3)}"
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        subprocess.run(["createdb", self.database], check=True)
        config = {
            "server_name": "bench",
            "pid_file": os.path.join(self.workdir, "homeserver.pid"),
            "report_stats": False,
            "signing_key_path": os.path.join(self.workdir, "signing.key"),
            "media_store_path": os.path.join(self.workdir, "media"),
            "registration_shared_secret": SHARED_SECRET,
            "enable_metrics": True,
            "suppress_key_server_warning": True,
            "trusted_key_servers": [],
            "listeners": [
                {
                    "port": CLIENT_PORT,
                    "bind_addresses": ["127.0.0.1"],
                    "type": "http",
                    "resources": [{"names": ["client"]}],
                },
                {
                    "port": METRICS_PORT,
                    "bind_addresses": ["127.0.0.1"],
                    "type": "metrics",
                },
            ],
            "database": {
                "name": "psycopg2",
                "use_rust_driver": self.rust_driver,
                "args": {
                    "database": self.database,
                    "host": os.environ.get("PGHOST", "localhost"),
                    "user": os.environ.get("PGUSER", "postgres"),
                    "password": os.environ.get("PGPASSWORD", ""),
                    "cp_min": 5,
                    "cp_max": 10,
                },
            },
            # Out of the way of the workloads.
            "rc_message": {"per_second": 100000, "burst_count": 100000},
            "rc_joins": {
                "local": {"per_second": 100000, "burst_count": 100000},
                "remote": {"per_second": 100000, "burst_count": 100000},
            },
            "rc_joins_per_room": {"per_second": 100000, "burst_count": 100000},
            "rc_registration": {"per_second": 100000, "burst_count": 100000},
            "rc_room_creation": {"per_second": 100000, "burst_count": 100000},
            "rc_login": {
                "address": {"per_second": 100000, "burst_count": 100000},
                "account": {"per_second": 100000, "burst_count": 100000},
            },
            "rc_invites": {
                "per_room": {"per_second": 100000, "burst_count": 100000},
                "per_user": {"per_second": 100000, "burst_count": 100000},
            },
            "experimental_features": {"rust_event_persistence": self.level},
            "log_config": os.path.join(self.workdir, "log.yaml"),
        }
        with open(os.path.join(self.workdir, "log.yaml"), "w") as f:
            f.write(
                "version: 1\n"
                "formatters: {f: {format: '%(asctime)s %(name)s %(levelname)s %(message)s'}}\n"
                f"handlers: {{file: {{class: logging.FileHandler, formatter: f, filename: {self.workdir}/homeserver.log}}}}\n"
                "root: {level: WARNING, handlers: [file]}\n"
                "disable_existing_loggers: false\n"
            )
        config_path = os.path.join(self.workdir, "homeserver.yaml")
        with open(config_path, "w") as f:
            json.dump(config, f)
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        subprocess.run(
            [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "-c",
                config_path,
                "--generate-keys",
            ],
            check=True,
            env=env,
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "synapse.app.homeserver", "-c", config_path],
            env=env,
            stdout=open(os.path.join(self.workdir, "stdout.log"), "w"),
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Synapse exited: see {self.workdir}")
            try:
                status, _ = request("GET", "/_matrix/client/versions")
                if status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError("Synapse did not start")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            self.process.wait(timeout=60)
        subprocess.run(["dropdb", "--if-exists", self.database], check=False)

    def sample(self) -> Sample:
        assert self.process is not None
        pid = self.process.pid
        return Sample(
            wall=time.monotonic(),
            # The reactor runs on the main thread, whose TID is the PID.
            reactor_cpu=_cpu_of(f"/proc/{pid}/task/{pid}/stat"),
            process_cpu=_cpu_of(f"/proc/{pid}/stat"),
            rss_mb=_rss_mb(pid),
            metrics=_metrics(),
        )


def request(
    method: str, path: str, body: Any = None, token: str | None = None
) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", CLIENT_PORT, timeout=300)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(
        method,
        path,
        body=json.dumps(body) if body is not None else None,
        headers=headers,
    )
    response = conn.getresponse()
    data = response.read()
    return response.status, json.loads(data) if data else None


def ok(method: str, path: str, body: Any = None, token: str | None = None) -> Any:
    status, data = request(method, path, body, token)
    if status != 200:
        raise RuntimeError(f"{method} {path}: {status} {data}")
    return data


def register(localpart: str) -> str:
    import hashlib
    import hmac

    nonce = ok("GET", "/_synapse/admin/v1/register")["nonce"]
    mac = hmac.new(SHARED_SECRET.encode(), digestmod=hashlib.sha1)
    mac.update(f"{nonce}\x00{localpart}\x00pass\x00notadmin".encode())
    return ok(
        "POST",
        "/_synapse/admin/v1/register",
        {
            "nonce": nonce,
            "username": localpart,
            "password": "pass",
            "mac": mac.hexdigest(),
        },
    )["access_token"]


def run_parallel(jobs: list[Callable[[], None]], concurrency: int) -> None:
    with ThreadPoolExecutor(concurrency) as pool:
        for future in [pool.submit(job) for job in jobs]:
            future.result()


def send(room_id: str, token: str, body: str) -> str:
    txn = secrets.token_hex(8)
    return ok(
        "PUT",
        f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
        {"msgtype": "m.text", "body": body},
        token,
    )["event_id"]


def create_room(token: str) -> str:
    return ok(
        "POST", "/_matrix/client/v3/createRoom", {"preset": "public_chat"}, token
    )["room_id"]


# Workloads: each sets up what it needs, then returns the jobs it times and
# the number of events they persist.


def messages(tokens: list[str], scale: int) -> tuple[list[Callable[[], None]], int]:
    """Each user sends `scale` messages into its own room, all at once."""
    rooms = [(create_room(token), token) for token in tokens]

    def sends(room_id: str, token: str) -> None:
        for i in range(scale):
            send(room_id, token, f"m{i}")

    return [partial(sends, room_id, token) for room_id, token in rooms], len(
        rooms
    ) * scale


def membership(tokens: list[str], scale: int) -> tuple[list[Callable[[], None]], int]:
    """Every user joins one room and leaves it, `scale // 10` times."""
    room_id = create_room(tokens[0])
    rounds = max(1, scale // 10)

    def churn(token: str) -> None:
        for _ in range(rounds):
            ok("POST", f"/_matrix/client/v3/rooms/{room_id}/join", {}, token)
            ok("POST", f"/_matrix/client/v3/rooms/{room_id}/leave", {}, token)

    return [partial(churn, token) for token in tokens[1:]], (
        len(tokens) - 1
    ) * rounds * 2


def big_state(tokens: list[str], scale: int) -> tuple[list[Callable[[], None]], int]:
    """A room with `scale * 20` state events, which every user then joins."""
    room_id = create_room(tokens[0])
    for i in range(scale * 20):
        ok(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/state/org.example.bench/{i}",
            {"i": i},
            tokens[0],
        )

    def join(token: str) -> None:
        ok("POST", f"/_matrix/client/v3/rooms/{room_id}/join", {}, token)

    return [partial(join, token) for token in tokens[1:]], len(tokens) - 1


def redactions(tokens: list[str], scale: int) -> tuple[list[Callable[[], None]], int]:
    """Redact `scale * 4` messages of one room, all at once."""
    room_id = create_room(tokens[0])
    event_ids = [send(room_id, tokens[0], f"r{i}") for i in range(scale * 4)]

    def redact(event_id: str) -> None:
        ok(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/redact/{event_id}/{secrets.token_hex(8)}",
            {},
            tokens[0],
        )

    return [partial(redact, event_id) for event_id in event_ids], len(event_ids)


WORKLOADS = {
    "messages": messages,
    "membership": membership,
    "big_state_joins": big_state,
    "redactions": redactions,
}


def bench_level(level: str, users: int, scale: int, concurrency: int) -> list[Result]:
    workdir = tempfile.mkdtemp(prefix=f"persist-bench-{level}-")
    server = Server(level, workdir)
    results = []
    try:
        server.start()
        tokens = [register(f"u{i}") for i in range(users)]
        for name, workload in WORKLOADS.items():
            jobs, events = workload(tokens, scale)
            before = server.sample()
            run_parallel(jobs, concurrency)
            after = server.sample()

            def delta(key: str) -> float:
                return after.metrics.get(key, 0.0) - before.metrics.get(key, 0.0)

            results.append(
                Result(
                    level=level,
                    workload=name,
                    events=events,
                    wall=after.wall - before.wall,
                    reactor_cpu=after.reactor_cpu - before.reactor_cpu,
                    process_cpu=after.process_cpu - before.process_cpu,
                    rss_delta_mb=after.rss_mb - before.rss_mb,
                    persist_txns=delta(
                        "synapse_storage_transaction_time_count_total{persist_events}"
                    ),
                    persist_txn_secs=delta(
                        "synapse_storage_transaction_time_sum_total{persist_events}"
                    ),
                )
            )
            print(results[-1], flush=True)
    finally:
        server.stop()
        shutil.rmtree(workdir, ignore_errors=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default=",".join(LEVELS))
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--scale", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json", help="Write the results here, as JSON.")
    args = parser.parse_args()

    results: list[Result] = []
    for _ in range(args.repeat):
        for level in args.levels.split(","):
            results.extend(bench_level(level, args.users, args.scale, args.concurrency))

    print(
        f"\n{'workload':<16} {'level':<12} {'events':>6} {'wall s':>8} "
        f"{'reactor s':>9} {'proc s':>8} {'ms/event':>9} {'txn ms':>8}"
    )
    for r in results:
        txn_ms = 1000 * r.persist_txn_secs / r.persist_txns if r.persist_txns else 0.0
        print(
            f"{r.workload:<16} {r.level:<12} {r.events:>6} {r.wall:>8.2f} "
            f"{r.reactor_cpu:>9.2f} {r.process_cpu:>8.2f} "
            f"{1000 * r.wall / max(r.events, 1):>9.2f} {txn_ms:>8.2f}"
        )
    if args.json:
        with open(args.json, "w") as f:
            json.dump([r.__dict__ for r in results], f, indent=2)


if __name__ == "__main__":
    main()
