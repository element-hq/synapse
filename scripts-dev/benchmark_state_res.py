#!/usr/bin/env python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

import argparse
import asyncio
import cProfile
import hashlib
import io
import json
import pstats
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any, Collection, Iterable, Iterator, Mapping, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import synapse.event_auth  # noqa: E402  # Import first to resolve circular import dependency
import synapse.state  # noqa: F401,E402
import synapse.state.v2 as v2  # noqa: E402
from synapse.api.room_versions import (  # noqa: E402
    RoomVersion,
    RoomVersions,
)
from synapse.events import EventBase, make_event_from_dict  # noqa: E402
from synapse.state import StateDifference  # noqa: E402
from synapse.util.duration import Duration  # noqa: E402


class _RustResolverDisabledForBenchmark(RuntimeError):
    pass


@contextmanager
def _disable_rust_lattice_fold_resolver() -> Iterator[None]:
    import synapse.synapse_rust.state_res as rust_res

    original = rust_res.resolve_v2_via_lattice_fold
    original_logger_exception = v2.logger.exception

    def disabled_resolver(*args: Any, **kwargs: Any) -> Any:
        raise _RustResolverDisabledForBenchmark(
            "Rust lattice-fold state resolver disabled for benchmark"
        )

    def logger_exception(message: object, *args: Any, **kwargs: Any) -> None:
        if isinstance(kwargs.get("exc_info"), _RustResolverDisabledForBenchmark):
            return
        original_logger_exception(message, *args, **kwargs)

    cast(Any, rust_res).resolve_v2_via_lattice_fold = disabled_resolver
    cast(Any, v2.logger).exception = logger_exception
    try:
        yield
    finally:
        cast(Any, rust_res).resolve_v2_via_lattice_fold = original
        cast(Any, v2.logger).exception = original_logger_exception


# Mock Clock
class MockClock:
    def time_msec(self) -> int:
        return int(time.time() * 1000)

    async def sleep(self, duration: Duration) -> None:
        await asyncio.sleep(0)


# Mock Room Version
class MockRoomVersion:
    def __init__(self, real_version: RoomVersion, state_res: int | None) -> None:
        self.state_res = state_res
        for attr in dir(real_version):
            if not attr.startswith("_"):
                try:
                    setattr(self, attr, getattr(real_version, attr))
                except (AttributeError, TypeError):
                    pass
        self.state_res = state_res


# Mock Event compatible with PyO3 translation layer
class MockEvent:
    room_version: MockRoomVersion | None = None

    class _InternalMetadata:
        def is_soft_failed(self) -> bool:
            return False

    def __init__(
        self,
        event_id: str,
        sender: str,
        event_type: str,
        state_key: str | None,
        content: dict,
        origin_server_ts: int = 0,
        depth: int = 1,
        auth_event_ids: list[str] | None = None,
        prev_event_ids: list[str] | None = None,
        room_id: str = "!room:example.com",
    ):
        self.event_id = event_id
        self.sender = sender
        self.type = event_type
        self.state_key = state_key
        self.content = content
        self.room_id = room_id
        self._auth_event_ids: list[str] = auth_event_ids or []
        self._prev_event_ids: list[str] = prev_event_ids or []
        self.depth = depth
        self.origin_server_ts = origin_server_ts
        self.rejected_reason = None
        self.internal_metadata = MockEvent._InternalMetadata()

    @property
    def membership(self) -> str | None:
        return self.content.get("membership")

    def auth_event_ids(self) -> list[str]:
        return self._auth_event_ids

    def prev_event_ids(self) -> list[str]:
        return self._prev_event_ids

    def get_state_key(self) -> str | None:
        return self.state_key


# Mock Store matching Synapse's StateResolutionStore API
class MockStateResolutionStore:
    def __init__(self, event_map: dict[str, Any]):
        self.event_map = event_map
        self.auth_chains: dict[str, set[str]] = {}

    async def get_events(
        self, event_ids: Collection[str], allow_rejected: bool = False
    ) -> dict[str, EventBase]:
        missing = [eid for eid in event_ids if eid not in self.event_map]
        if missing:
            raise KeyError(f"Missing benchmark events: {missing}")

        return cast(
            dict[str, EventBase],
            {eid: self.event_map[eid] for eid in event_ids},
        )

    def _get_auth_chain(self, event_ids: Iterable[str]) -> list[str]:
        event_ids = list(event_ids)
        if self.auth_chains and all(eid in self.auth_chains for eid in event_ids):
            result = set()
            for eid in event_ids:
                result.update(self.auth_chains[eid])
            return list(result)

        result = set()
        stack = list(event_ids)
        while stack:
            event_id = stack.pop()
            if event_id in result:
                continue
            result.add(event_id)
            event = self.event_map[event_id]
            for aid in event.auth_event_ids():
                stack.append(aid)
        return list(result)

    async def get_auth_chain_difference(
        self,
        room_id: str,
        auth_sets: list[set[str]],
        conflicted_state: set[str] | None,
        additional_backwards_reachable_conflicted_events: set[str] | None,
    ) -> StateDifference:
        chains = [frozenset(self._get_auth_chain(a)) for a in auth_sets]
        common = set(chains[0]).intersection(*chains[1:])
        return StateDifference(
            auth_difference=set().union(*chains) - common,
            conflicted_subgraph=None,
        )


def _make_benchmark_event(
    *,
    room_version: RoomVersion,
    event_id: str,
    sender: str,
    event_type: str,
    state_key: str | None,
    content: dict[str, Any],
    origin_server_ts: int,
    depth: int,
    auth_event_ids: list[str] | None = None,
    prev_event_ids: list[str] | None = None,
    room_id: str = "!room:example.com",
) -> EventBase:
    reference_hashes = {"sha256": "benchmark"}
    event_dict: dict[str, Any] = {
        "room_id": room_id,
        "type": event_type,
        "sender": sender,
        "content": content,
        "depth": depth,
        "origin_server_ts": origin_server_ts,
        "hashes": {"sha256": "aGVsbG8="},
        "signatures": {},
        "auth_events": [
            (event_id, reference_hashes) for event_id in (auth_event_ids or [])
        ],
        "prev_events": [
            (event_id, reference_hashes) for event_id in (prev_event_ids or [])
        ],
        "event_id": event_id,
    }
    if state_key is not None:
        event_dict["state_key"] = state_key
    return make_event_from_dict(event_dict, room_version=room_version)


@dataclass(slots=True)
class RunStats:
    total_s: float
    bookkeeping_s: float
    resolve_s: float
    merge_points: int


def _print_profile(profile: cProfile.Profile, title: str, limit: int) -> None:
    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream)
    stats.sort_stats("cumtime")
    stats.print_stats(limit)
    print(f"\n{title}")
    print(stream.getvalue().rstrip())


def _resolved_state_checksum(state: Mapping[tuple[str, str], str]) -> str:
    encoded_state = json.dumps(
        [
            [event_type, state_key, event_id]
            for (event_type, state_key), event_id in sorted(state.items())
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded_state.encode()).hexdigest()


def _print_results_table(
    stats_py: RunStats,
    stats_rust: RunStats,
    checksum_py: str,
    checksum_rust: str,
    title: str,
) -> None:
    speedup_py = "1.0x (Baseline)"
    speedup_rust = f"{stats_py.total_s / stats_rust.total_s:.1f}x"
    speedup_width = max(len("Speedup"), len(speedup_py), len(speedup_rust))
    checksum_width = len(checksum_py)

    print(
        dedent(
            f"""
            {title}
            +--------------------+---------------+{"-" * (speedup_width + 2)}+{"-" * (checksum_width + 2)}+
            | {"Implementation":<18} | {"Duration (s)":<13} | {"Speedup":<{speedup_width}} | {"State SHA-256":<{checksum_width}} |
            +--------------------+---------------+{"-" * (speedup_width + 2)}+{"-" * (checksum_width + 2)}+
            | {"Python V2":<18} | {stats_py.total_s:<13.5f} | {speedup_py:<{speedup_width}} | {checksum_py:<{checksum_width}} |
            | {"Rust (rezzy)":<18} | {stats_rust.total_s:<13.5f} | {speedup_rust:<{speedup_width}} | {checksum_rust:<{checksum_width}} |
            +--------------------+---------------+{"-" * (speedup_width + 2)}+{"-" * (checksum_width + 2)}+
            """
        ).rstrip()
    )


def _print_stage_breakdown(stats_py: RunStats, stats_rust: RunStats) -> None:
    def format_stage(label: str, stats: RunStats) -> str:
        parts = [f"{label:<6} total: {stats.total_s:.5f}s"]
        if stats.bookkeeping_s:
            parts.append(f"bookkeeping: {stats.bookkeeping_s:.5f}s")
        if stats.resolve_s != stats.total_s:
            parts.append(f"resolver: {stats.resolve_s:.5f}s")
        parts.append(f"merge points: {stats.merge_points}")
        return "  - " + ", ".join(parts)

    print("\nStage breakdown:")
    print(format_stage("Python", stats_py))
    print(format_stage("Rust", stats_rust))


def _load_jsonl_events(path: str) -> tuple[dict[str, Any], list[MockEvent]]:
    print(f"Loading DAG from {path}...")
    event_map: dict[str, Any] = {}
    events_list: list[MockEvent] = []
    room_id_hint = None
    filename = Path(path).name
    match = re.match(
        r"^(?:local|remote)-dag-(.+)-v\d+-.+\.jsonl$",
        filename,
    )
    if match:
        room_id_hint = f"!{match.group(1)}"

    with open(path, "r") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue

            d = json.loads(line)
            if "event_id" not in d:
                keys = ", ".join(sorted(d.keys()))
                raise ValueError(
                    "The --jsonl input must include a top-level 'event_id' field for each "
                    f"event. The first parsed row at line {line_no} had keys: {keys}. "
                    "This file looks like a DAG export that omits event IDs, so the "
                    "benchmark cannot build its event_map or resolve prev/auth chains from it."
                )

            ev = MockEvent(
                event_id=d["event_id"],
                sender=d["sender"],
                event_type=d["type"],
                state_key=d.get("state_key"),
                content=d["content"],
                origin_server_ts=d["origin_server_ts"],
                depth=d["depth"],
                auth_event_ids=d["auth_events"],
                prev_event_ids=d["prev_events"],
                room_id=d.get("room_id", room_id_hint) or "",
            )
            if not ev.room_id:
                raise ValueError(
                    "The --jsonl input must include a top-level 'room_id' field, or use "
                    "a file name that encodes the room id (for example local-dag-<room>-v*.jsonl). "
                    f"The row at line {line_no} had no room_id and the filename {filename!r} "
                    "did not match the expected pattern."
                )
            event_map[ev.event_id] = ev
            events_list.append(ev)

    return event_map, events_list


async def main() -> None:
    import logging

    logging.basicConfig(level=logging.INFO, force=True)
    parser = argparse.ArgumentParser(
        description="Benchmark state resolution V2 (Rust vs Python)"
    )
    parser.add_argument(
        "-p", "--partitions", type=int, default=50, help="Number of partitions (P)"
    )
    parser.add_argument(
        "-n",
        "--events",
        type=int,
        default=100,
        help="Conflicting events per partition (N)",
    )
    parser.add_argument(
        "--jsonl",
        type=str,
        default=None,
        help="Path to JSONL DAG file to resolve",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="Print a cProfile summary for each benchmarked run",
    )
    parser.add_argument(
        "--profile-limit",
        type=int,
        default=20,
        help="Number of cProfile rows to print per run",
    )
    args = parser.parse_args()
    P = args.partitions
    N = args.events

    room_id = "!room:example.com"
    real_version = RoomVersions.V2
    event_map: dict[str, Any] = {}
    events_list: list[MockEvent] = []
    auth_chains: dict[str, set[str]] = {}
    if args.jsonl:
        if "v11" in args.jsonl:
            real_version = getattr(
                RoomVersions, "V11", RoomVersions.V10
            )  # Fallback to V10 if V11 not found?
        elif "v6" in args.jsonl:
            real_version = RoomVersions.V6
        else:
            real_version = RoomVersions.V6
    room_version_rust = MockRoomVersion(real_version, real_version.state_res)
    room_version_py = MockRoomVersion(real_version, real_version.state_res)

    # All MockEvent instances will share room_version_rust initially
    MockEvent.room_version = room_version_rust

    if args.jsonl:
        event_map, events_list = _load_jsonl_events(args.jsonl)

        # Sort events topologically by depth to simulate chronological ordering
        events_list.sort(key=lambda e: (e.depth, e.origin_server_ts))

        # Precompute auth chains for all events
        for ev in events_list:
            chain = {ev.event_id}
            for aid in ev.auth_event_ids():
                if aid in auth_chains:
                    chain.update(auth_chains[aid])
            auth_chains[ev.event_id] = chain

    async def run_simulation(
        room_version_to_use: MockRoomVersion,
    ) -> tuple[RunStats, dict]:
        # Set the room version for events
        MockEvent.room_version = room_version_to_use

        # Initialize store
        store = MockStateResolutionStore(event_map)
        store.auth_chains = auth_chains  # attach precomputed chains

        clock = MockClock()
        if not events_list:
            raise ValueError("JSONL DAG contains no events")
        room_id = events_list[0].room_id

        # Map from event_id to the state *after* that event
        event_states: dict[str, dict[tuple[str, str], str]] = {}
        start_time = time.perf_counter()
        bookkeeping_s = 0.0
        resolve_s = 0.0
        merge_points = 0
        try:
            # Iterate through events and construct/resolve state
            for ev in events_list:
                loop_start = time.perf_counter()
                prev_ids = ev.prev_event_ids()

                # Compute state before this event
                if not prev_ids:
                    state_before: dict[tuple[str, str], str] = {}
                elif len(prev_ids) == 1:
                    prev_id = prev_ids[0]
                    state_before = dict(event_states.get(prev_id, {}))
                else:
                    # Merge point! We must resolve the states after the prev events
                    state_sets = []
                    for pid in prev_ids:
                        if pid in event_states:
                            state_sets.append(event_states[pid])

                    if not state_sets:
                        state_before = {}
                    elif len(state_sets) == 1:
                        state_before = dict(state_sets[0])
                    else:
                        merge_points += 1
                        print(f"Resolving {len(state_sets)} states at {ev.event_id}")
                        resolve_start = time.perf_counter()
                        state_before = dict(
                            await v2.resolve_events_with_store(
                                cast(Any, clock),
                                room_id,
                                cast(RoomVersion, room_version_to_use),
                                state_sets,
                                None,
                                cast(Any, store),
                            )
                        )
                        resolve_s += time.perf_counter() - resolve_start

                # Compute state after this event
                state_after = dict(state_before)
                if ev.state_key is not None:
                    state_after[(ev.type, ev.state_key)] = ev.event_id

                event_states[ev.event_id] = state_after
                bookkeeping_s += time.perf_counter() - loop_start

            duration = time.perf_counter() - start_time
            # Get state of the last event
            final_state = event_states[events_list[-1].event_id]
            return (
                RunStats(
                    total_s=duration,
                    bookkeeping_s=bookkeeping_s - resolve_s,
                    resolve_s=resolve_s,
                    merge_points=merge_points,
                ),
                final_state,
            )
        finally:
            pass

    async def run_profiled_simulation(
        room_version_to_use: MockRoomVersion,
        title: str,
    ) -> tuple[RunStats, dict]:
        if not args.profile:
            return await run_simulation(room_version_to_use)

        profiler = cProfile.Profile()
        profiler.enable()
        try:
            result = await run_simulation(room_version_to_use)
        finally:
            profiler.disable()
        _print_profile(profiler, title, args.profile_limit)
        return result

    if args.jsonl:
        print("Simulating resolution using Rust (rezzy)...")
        stats_rust, res_rust = await run_profiled_simulation(
            room_version_rust, "cProfile: Rust run"
        )

        print("Simulating resolution using Python fallback...")
        try:
            with _disable_rust_lattice_fold_resolver():
                stats_py, res_py = await run_profiled_simulation(
                    room_version_py, "cProfile: Python run"
                )
        except Exception as e:
            print(
                "Python fallback benchmark failed for this DAG. "
                f"The Rust run completed, but the Python path raised: {type(e).__name__}: {e}"
            )
            return

        # Restore
        MockEvent.room_version = room_version_rust

        assert res_rust == res_py, (
            "Error: Resolved states differ between Rust and Python!"
        )

        _print_results_table(
            stats_py,
            stats_rust,
            _resolved_state_checksum(res_py),
            _resolved_state_checksum(res_rust),
            "Simulation Results:",
        )
        _print_stage_breakdown(stats_py, stats_rust)
        return

    if not args.jsonl:
        # Baseline Events
        bench_event_map: dict[str, EventBase] = {}

        # 1. CREATE
        create = _make_benchmark_event(
            room_version=real_version,
            event_id="$CREATE",
            sender="@alice:example.com",
            event_type="m.room.create",
            state_key="",
            content={"creator": "@alice:example.com"},
            origin_server_ts=1000,
            depth=1,
        )
        bench_event_map[create.event_id] = create

        # 2. MEMBERS
        alice_join = _make_benchmark_event(
            room_version=real_version,
            event_id="$IMA",
            sender="@alice:example.com",
            event_type="m.room.member",
            state_key="@alice:example.com",
            content={"membership": "join"},
            origin_server_ts=1001,
            depth=2,
            auth_event_ids=[create.event_id],
        )
        bench_event_map[alice_join.event_id] = alice_join

        pl = _make_benchmark_event(
            room_version=real_version,
            event_id="$IPOWER",
            sender="@alice:example.com",
            event_type="m.room.power_levels",
            state_key="",
            content={"users": {"@alice:example.com": 100}, "users_default": 0},
            origin_server_ts=1002,
            depth=3,
            auth_event_ids=[create.event_id, alice_join.event_id],
        )
        bench_event_map[pl.event_id] = pl

        # Join rules
        jr = _make_benchmark_event(
            room_version=real_version,
            event_id="$IJR",
            sender="@alice:example.com",
            event_type="m.room.join_rules",
            state_key="",
            content={"join_rule": "public"},
            origin_server_ts=1003,
            depth=4,
            auth_event_ids=[create.event_id, alice_join.event_id, pl.event_id],
        )
        bench_event_map[jr.event_id] = jr

        baseline_state = {
            ("m.room.create", ""): create.event_id,
            ("m.room.member", "@alice:example.com"): alice_join.event_id,
            ("m.room.power_levels", ""): pl.event_id,
            ("m.room.join_rules", ""): jr.event_id,
        }

        # Generate P parallel partitions, each with N conflicting events
        state_sets: list[dict[tuple[str, str], str]] = []

        for p in range(P):
            sender = f"@user_{p}:example.com"
            # Join user to the room first
            join_ev = _make_benchmark_event(
                room_version=real_version,
                event_id=f"$JOIN_{p}",
                sender=sender,
                event_type="m.room.member",
                state_key=sender,
                content={"membership": "join"},
                origin_server_ts=2000 + p,
                depth=5 + p,
                auth_event_ids=[create.event_id, jr.event_id, pl.event_id],
            )
            bench_event_map[join_ev.event_id] = join_ev

            part_state = dict(baseline_state)
            part_state[("m.room.member", sender)] = join_ev.event_id

            prev_id = join_ev.event_id
            for i in range(N):
                # Each event changes a topic or custom type to create conflicts
                ev_id = f"$EV_{p}_{i}"
                bench_ev: EventBase = _make_benchmark_event(
                    room_version=real_version,
                    event_id=ev_id,
                    sender=sender,
                    event_type=f"org.example.test_{i}",
                    state_key=f"state_key_{i}",
                    content={"value": f"val_{p}_{i}"},
                    origin_server_ts=3000 + p * N + i,
                    depth=6 + p * N + i,
                    auth_event_ids=[create.event_id, join_ev.event_id, pl.event_id],
                    prev_event_ids=[prev_id],
                )
                bench_event_map[ev_id] = bench_ev

                assert bench_ev.state_key is not None
                part_state[(bench_ev.type, bench_ev.state_key)] = ev_id
                prev_id = ev_id

            state_sets.append(part_state)

        clock = MockClock()
        store = MockStateResolutionStore(bench_event_map)

        print("Benchmark Configuration:")
        print(f"  - Partitions: {P}")
        print(f"  - Conflicting events per partition: {N}")
        print(f"  - Total events in map: {len(bench_event_map)}")
        print("  - Warm-up resolution...")

        async def run_resolution(
            room_version_to_use: MockRoomVersion,
            title: str,
        ) -> tuple[RunStats, dict]:
            MockEvent.room_version = room_version_to_use

            profiler = cProfile.Profile() if args.profile else None
            if profiler is not None:
                profiler.enable()

            try:
                start = time.perf_counter()
                resolved_state = dict(
                    await v2.resolve_events_with_store(
                        cast(Any, clock),
                        room_id,
                        cast(RoomVersion, room_version_to_use),
                        state_sets,
                        bench_event_map,
                        cast(Any, store),
                    )
                )
                duration = time.perf_counter() - start
            finally:
                if profiler is not None:
                    profiler.disable()
                    _print_profile(profiler, title, args.profile_limit)

            return (
                RunStats(
                    total_s=duration,
                    bookkeeping_s=0.0,
                    resolve_s=duration,
                    merge_points=1,
                ),
                resolved_state,
            )

        # Warmup
        await v2.resolve_events_with_store(
            cast(Any, clock),
            room_id,
            cast(RoomVersion, room_version_rust),
            state_sets,
            bench_event_map,
            cast(Any, store),
        )

        # 1. Benchmark Rust (rezzy)
        stats_rust, res_rust = await run_resolution(
            room_version_rust, "cProfile: Rust run"
        )

        # 2. Benchmark Python (fallback)
        # Disable only the Rust lattice-fold resolver while keeping the room
        # version's state resolution algorithm unchanged.
        with _disable_rust_lattice_fold_resolver():
            stats_py, res_py = await run_resolution(
                room_version_py, "cProfile: Python run"
            )

        # Restore
        MockEvent.room_version = room_version_rust

        assert res_rust == res_py, (
            "Error: Resolved states differ between Rust and Python!"
        )

        _print_results_table(
            stats_py,
            stats_rust,
            _resolved_state_checksum(res_py),
            _resolved_state_checksum(res_rust),
            "Benchmark Results:",
        )
        _print_stage_breakdown(stats_py, stats_rust)


if __name__ == "__main__":
    asyncio.run(main())
