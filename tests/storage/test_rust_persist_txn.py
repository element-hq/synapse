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

"""The Rust write transaction (`rust/src/persist/txn/`) against Python's.

Each test builds the same DAG in two rooms, the twins. Room A's chunks are
persisted in Python. Room B's are prepared in Rust, then the part of the
transaction ported to Rust so far runs and commits, then the rest of
Python's transaction steps run. The test then compares the rows of every
table the transaction writes, after normalising the IDs that differ between
the rooms, and the cache updates each room's persists asked for.

These need the native Rust Postgres driver:

    SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.storage.test_rust_persist_txn
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest import SkipTest
from unittest.mock import Mock, patch

import attr
from unpaddedbase64 import encode_base64

from twisted.internet.defer import Deferred
from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, StickyEvent
from synapse.api.errors import PartialStateConflictError
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.crypto.event_signing import compute_content_hash
from synapse.events import EventBase, make_event_from_dict
from synapse.events.snapshot import EventContext, EventPersistencePair
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main.events import PersistEventsStore
from synapse.storage.databases.main.rust_persist import apply_rust_persist_effects
from synapse.storage.rust_pool import RustConnectionPool
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.synapse_rust.events import redact_event
from synapse.synapse_rust.persist import PersistEffects
from synapse.types import create_requester
from synapse.util.caches.deferred_cache import DeferredCache
from synapse.util.clock import Clock

from tests import unittest

# The tables `_persist_events_txn` writes. Each is compared on the rows of
# the room, by its `room_id` column if it has one, else by `event_id`.
# `event_edges` is also compared by `event_id`, see `_UNFILLED_ROOM_ID`.
_TABLES = (
    "current_state_delta_stream",
    "current_state_events",
    "destination_rooms_sticky_events_backlog",
    "device_lists_remote_extremeties",
    "event_auth",
    "event_auth_chain_links",
    "event_auth_chain_to_calculate",
    "event_auth_chains",
    "event_backward_extremities",
    "event_edges",
    "event_expiry",
    "event_failed_pull_attempts",
    "event_forward_extremities",
    "event_json",
    "event_labels",
    "event_push_actions",
    "event_push_actions_staging",
    "event_relations",
    "event_search",
    "event_to_state_groups",
    "event_txn_id_device_id",
    "events",
    "ex_outlier_stream",
    "local_current_membership",
    "msc4242_state_dag_edges",
    "msc4242_state_dag_forward_extremities",
    "partial_state_events",
    "redactions",
    "rejections",
    "room_depth",
    "room_memberships",
    "room_retention",
    "rooms",
    "sliding_sync_joined_rooms",
    "sliding_sync_membership_snapshots",
    "state_events",
    "sticky_events",
    "stream_ordering_to_exterm",
    "threads",
    "timeline_gaps",
)

# Tables with a `room_id` column that is no longer filled in, compared by
# `event_id` instead.
_UNFILLED_ROOM_ID = {"event_edges"}

# Tables of remote users, compared on the twin's own remote users, see
# `_remote_user`.
_REMOTE_USER_TABLES = {"device_lists_remote_extremeties"}

# Integer columns holding a position on a stream or a state group, by
# column name or `(table, column)`, and which sequence the value is from.
# They differ between the twins, so they are compared by their rank among
# the twin's values from that sequence. Columns not listed are compared as
# they are.
_STREAM_COLUMNS: dict[str | tuple[str, str], str] = {
    "bump_stamp": "events",
    "event_stream_ordering": "events",
    "stream_ordering": "events",
    ("current_state_delta_stream", "stream_id"): "events",
    ("ex_outlier_stream", "event_stream_ordering"): "ex_outlier",
    ("sticky_events", "stream_id"): "sticky",
    "state_group": "state_groups",
}

# Chain IDs, compared by the lowest event ID in the chain.
_CHAIN_COLUMNS = {
    ("event_auth_chains", "chain_id"),
    ("event_auth_chain_links", "origin_chain_id"),
    ("event_auth_chain_links", "target_chain_id"),
}

# Parsed and compared as JSON. The hashes and signatures of an event cover
# its room ID, so they differ between the twins.
_JSON_COLUMNS = {
    ("event_json", "json"),
    ("event_json", "internal_metadata"),
}

# Which positional arguments of a `call_after` callback hold a stream
# position or state group, by callback name, or by its name and owner.
_EFFECT_ARGS: dict[str | tuple[str, str | None], tuple[str | None, ...]] = {
    "_invalidate_caches_for_event": ("events",),
    "entity_has_changed": (None, "events"),
    "prefill": (None, "state_groups"),
    # An ID generator's stream IDs, by the generator's stream name.
    ("_mark_ids_as_finished", "caches"): ("cache",),
    ("_mark_ids_as_finished", "sticky_events"): ("sticky",),
}


def _effect_kinds(name: str, owner: str | None, args: tuple) -> tuple[str | None, ...]:
    """The kind of each argument of an effect, for `_Normaliser.position`."""
    kinds = _EFFECT_ARGS.get((name, owner), _EFFECT_ARGS.get(name, ()))
    return kinds + (None,) * (len(args) - len(kinds))


# Marks each twin's create event, so the rooms get different IDs in room
# versions that derive the room ID from it.
_TWIN_KEY = "org.matrix.synapse.test.twin"


@attr.s(auto_attribs=True)
class _Twin:
    """One of the two rooms."""

    name: str
    room_id: str = ""
    # The scenario's events, by index.
    events: list[str] = attr.Factory(list)
    # The cache updates its persists asked for: `(callback, owner, args,
    # kwargs)`, with stream positions as they are.
    effects: list[tuple[str, str | None, tuple, dict[str, Any]]] = attr.Factory(list)
    # The cache stream rows its persists wrote.
    cache_rows: list[tuple] = attr.Factory(list)


class _Normaliser:
    """Rewrites a twin's values so they compare equal with the other
    twin's: the room ID, event IDs, stream positions, state groups and
    chain IDs."""

    def __init__(
        self,
        twin: _Twin,
        event_ids: dict[str, str],
        ranks: dict[str, dict[int, int]],
    ) -> None:
        self._ranks = ranks
        names = {twin.room_id: "!room", f"twin-{twin.name}": "twin", **event_ids}
        self._names = names
        self._pattern = re.compile(
            "|".join(re.escape(k) for k in sorted(names, key=len, reverse=True))
        )
        self.chains: dict[int, str] = {}

    def string(self, value: str) -> str:
        return self._pattern.sub(lambda m: self._names[m.group(0)], value)

    def json(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.string(value)
        if isinstance(value, list):
            return [self.json(v) for v in value]
        if isinstance(value, dict):
            return {self.string(k): self.json(v) for k, v in value.items()}
        return value

    def position(self, kind: str | None, value: Any) -> Any:
        if kind is None or value is None:
            return value
        if isinstance(value, (list, tuple)):
            return tuple(self.position(kind, v) for v in value)
        rank = self._ranks.get(kind, {}).get(value)
        return f"{kind}#{rank}" if rank is not None else f"{kind}?{value}"

    def value(self, table: str, column: str, value: Any) -> Any:
        if (table, column) in _CHAIN_COLUMNS and value is not None:
            return self.chains.get(value, f"chain?{value}")
        if (table, column) in _JSON_COLUMNS and value is not None:
            parsed = json.loads(value)
            # The content hash covers the order of the auth events. Where it
            # holds, each twin's order is the one its event was built with,
            # so sorting them hides only event creation picking them from a
            # set. An event stored redacted no longer matches its hash.
            as_built = False
            if column == "json":
                _, digest = compute_content_hash(parsed, hashlib.sha256)
                as_built = parsed.get("hashes", {}).get("sha256") == encode_base64(
                    digest
                )
                parsed.pop("hashes", None)
                parsed.pop("signatures", None)
            normalised = self.json(parsed)
            if as_built and isinstance(normalised.get("auth_events"), list):
                normalised["auth_events"].sort()
            return json.dumps(normalised, sort_keys=True)
        kind = _STREAM_COLUMNS.get((table, column), _STREAM_COLUMNS.get(column))
        if isinstance(value, int) and not isinstance(value, bool) and kind:
            return self.position(kind, value)
        if isinstance(value, (list, tuple)):
            return tuple(self.value(table, column, v) for v in value)
        if isinstance(value, (set, frozenset)):
            return tuple(sorted(self.value(table, column, v) for v in value))
        if isinstance(value, str):
            return self.string(value)
        return value


class _TwinTestBase(unittest.HomeserverTestCase):
    """Builds DAGs in twin rooms and compares them. See the module docs."""

    maxDiff = None

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    room_version = "12"

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        experimental = config.setdefault("experimental_features", {})
        experimental["rust_event_persistence"] = "prepare"
        experimental["msc4354_enabled"] = True
        # For `event_expiry`.
        config["enable_ephemeral_messages"] = True
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        if persistence._rust_persister is None:
            raise SkipTest("SYNAPSE_TEST_RUST_PERSIST=off turns the persister off")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence
        self.persistence._rust_persist_strict = True
        persister = persistence._rust_persister
        assert persister is not None
        self.persister = _RecordingPersister(persister)
        persistence._rust_persister = self.persister  # type: ignore[assignment]
        events_store = hs.get_datastores().persist_events
        assert events_store is not None
        self.events_store = events_store

        self.alice = self.register_user("alice", "pass")
        # A device, for transaction IDs.
        self.login("alice", "pass", device_id="DEVICE")
        self.bob = self.register_user("bob", "pass")
        self.twins = (_Twin("a"), _Twin("b"))
        for twin in self.twins:
            with self._persisting(twin):
                twin.room_id, _, _ = self.get_success(
                    self.hs.get_room_creation_handler().create_room(
                        create_requester(self.alice),
                        {
                            "room_version": self.room_version,
                            "creation_content": {_TWIN_KEY: f"twin-{twin.name}"},
                        },
                        ratelimit=False,
                    )
                )

    # Persisting.

    @contextmanager
    def _persisting(self, twin: _Twin) -> Iterator[None]:
        """Persist the twin's events: room A's in Python, room B's through
        Rust as far as it goes. Records the cache updates and cache stream
        rows."""
        self.hs.config.experimental.rust_event_persistence = (
            "off" if twin.name == "a" else "prepare"
        )
        store = self.events_store
        python_txn = PersistEventsStore._persist_events_txn.__get__(store)
        if twin.name == "a":

            def txn(t: LoggingTransaction, **kwargs: Any) -> None:
                start = len(t.after_callbacks or [])
                python_txn(t, **kwargs)
                twin.effects.extend(_merged_effects(self._python_effects(t, start)))
        else:

            def txn(t: LoggingTransaction, **kwargs: Any) -> None:
                self._rust_txn(twin, t, **kwargs)

        cache_stream_before = self._cache_stream_position()
        store._persist_events_txn = txn  # type: ignore[method-assign,assignment]
        try:
            yield
        finally:
            del store._persist_events_txn
            self.hs.config.experimental.rust_event_persistence = "prepare"
        twin.cache_rows.extend(self._cache_stream_rows(cache_stream_before))

    def _rust_txn(
        self,
        twin: _Twin,
        txn: LoggingTransaction,
        *,
        room_id: str,
        events_and_contexts: list[EventPersistencePair],
        **kwargs: Any,
    ) -> None:
        """Room B's transaction: the Rust part, committed first, then the
        rest in Python."""
        prepared = self.persister.last_prepared
        assert prepared is not None
        # The transaction must take the chunk prepared for it.
        self.assertEqual(
            self.persister.last_chunk,
            (room_id, [event.event_id for event, _ in events_and_contexts]),
        )
        effects, left, redacted = self.persister.persist_events_txn_blocking(
            room_id,
            events_and_contexts,
            prepared,
            inhibit_local_membership_updates=kwargs["inhibit_local_membership_updates"],
        )
        calls = _rust_effects(effects)
        # As the facade will once Rust commits, so the steps after Rust's,
        # and the next persist, see the caches Rust invalidated. This also
        # finishes the cache and sticky events stream IDs, which is
        # recorded. The test entry allocates no event stream IDs. Unlike
        # production, the notifiers are called, and the `stream_positions`
        # update started, inside the Python transaction, and a retry of it
        # would apply the effects twice.
        self.assertEqual(effects.stream_ids, [])
        calls.extend(
            self._apply_recorded(
                effects,
                {
                    event.event_id: event
                    for event, _ in PersistEventsStore._filter_events_and_contexts_for_duplicates(
                        events_and_contexts
                    )
                },
            )
        )
        # The events left, as the steps after Rust's see them.
        left_events_and_contexts = []
        for i in left:
            event, context = events_and_contexts[i]
            if i in redacted:
                event = redact_event(event)
                event.internal_metadata.redacted_by = redacted[i]
            left_events_and_contexts.append((event, context))
        start = len(txn.after_callbacks or [])
        _python_steps_after_rust(
            self.events_store,
            txn,
            room_id=room_id,
            events_and_contexts=left_events_and_contexts,
            **kwargs,
        )
        calls.extend(self._python_effects(txn, start))
        twin.effects.extend(_merged_effects(calls))

    def _apply_recorded(
        self, effects: PersistEffects, events: dict[str, EventBase]
    ) -> list[tuple[str, str | None, tuple, dict[str, Any]]]:
        """Apply Rust's effects, recording the stream IDs finished and the
        notifiers called, as `_python_effects` records Python's."""
        calls: list[tuple[str, str | None, tuple, dict[str, Any]]] = []
        replication_notifier = self.hs.get_replication_notifier()
        notifier = self.hs.get_notifier()

        # How deep in recorded calls we are. Only the applier's own calls
        # are recorded, as Python records only its transaction's callbacks:
        # `on_new_replication_data` notifies replication itself.
        depth = [0]

        def recording(
            name: str, owner: str, original: Callable[..., Any]
        ) -> Callable[..., Any]:
            def call(*args: Any) -> Any:
                if not depth[0]:
                    calls.append((name, owner, tuple(list(a) for a in args), {}))
                depth[0] += 1
                try:
                    return original(*args)
                finally:
                    depth[0] -= 1

            return call

        def run_as_background_process(desc: str, func: Any, *args: Any) -> Any:
            # Only the `stream_positions` updates. The invalidations start
            # others, which Python runs after its transaction unrecorded.
            if desc == "MultiWriterIdGenerator._update_table":
                calls.append(
                    (
                        "run_as_background_process",
                        _background_process_owner(desc, args),
                        (),
                        {},
                    )
                )
            return original_run(desc, func, *args)

        original_run = self.hs.run_as_background_process
        with ExitStack() as stack:
            for id_gen in (self.store._cache_id_gen, self.store._sticky_events_id_gen):
                assert id_gen is not None
                stack.enter_context(
                    patch.object(
                        id_gen,
                        "_mark_ids_as_finished",
                        recording(
                            "_mark_ids_as_finished",
                            id_gen._stream_name,
                            id_gen._mark_ids_as_finished,
                        ),
                    )
                )
            stack.enter_context(
                patch.object(
                    replication_notifier,
                    "notify_replication",
                    recording(
                        "notify_replication",
                        "ReplicationNotifier",
                        replication_notifier.notify_replication,
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    notifier,
                    "on_new_replication_data",
                    recording(
                        "on_new_replication_data",
                        "Notifier",
                        notifier.on_new_replication_data,
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    self.hs, "run_as_background_process", run_as_background_process
                )
            )
            apply_rust_persist_effects(self.store, effects, events)
        return calls

    def _python_effects(
        self, txn: LoggingTransaction, start: int
    ) -> list[tuple[str, str | None, tuple, dict[str, Any]]]:
        effects = []
        for callback, args, kwargs in (txn.after_callbacks or [])[start:]:
            name = callback.__name__
            owner = getattr(callback, "__self__", None)
            # A single-key cache's `prefill` and `invalidate` are lambdas
            # over the cache.
            cache = next(
                (
                    cell.cell_contents
                    for cell in getattr(callback, "__closure__", None) or ()
                    if isinstance(cell.cell_contents, DeferredCache)
                ),
                None,
            )
            if owner is self.store:
                owner_name = None
            elif isinstance(owner, DeferredCache):
                owner_name = self._cache_names().get(id(owner), "DeferredCache")
            elif isinstance(owner, MultiWriterIdGenerator):
                owner_name = owner._stream_name
            elif owner is not None:
                owner_name = getattr(owner, "name", None) or type(owner).__name__
            elif name == "run_as_background_process":
                # By what it runs: `args` are its description, the server
                # name, and the function and its arguments.
                owner_name = _background_process_owner(str(args[0]), args[3:])
                args = ()
            elif name == "local_prefill":
                # `_add_to_cache`'s, by the events it puts in the cache.
                to_prefill = next(
                    cell.cell_contents
                    for cell in callback.__closure__ or ()
                    if isinstance(cell.cell_contents, list)
                )
                if not to_prefill:
                    continue
                owner_name = "_get_event_cache"
                args = (frozenset(event.event_id for event in to_prefill),)
            elif name == "<lambda>" and cache is not None:
                # A single-key cache's `prefill` or `invalidate`, by the
                # method the lambda calls on the cache.
                code_names = callback.__code__.co_names
                name = "prefill" if "prefill" in code_names else "invalidate"
                owner_name = self._cache_names().get(id(cache), "DeferredCache")
            else:
                owner_name = callback.__qualname__
            effects.append((name, owner_name, args, kwargs))
        return effects

    def _cache_names(self) -> dict[int, str]:
        """The store's cached functions' names, by the ID of their cache."""
        names = {}
        for name in dir(type(self.store)):
            cache = getattr(getattr(self.store, name, None), "cache", None)
            if isinstance(cache, DeferredCache):
                names[id(cache)] = name
        return names

    def _cache_stream_position(self) -> int:
        rows = self.get_success(
            self.store.db_pool.execute(
                "test",
                "SELECT COALESCE(MAX(stream_id), 0) FROM cache_invalidation_stream_by_instance",
            )
        )
        return rows[0][0]

    def _cache_stream_rows(self, after: int) -> list[tuple]:
        return self.get_success(
            self.store.db_pool.execute(
                "test",
                """SELECT stream_id, instance_name, cache_func, keys, invalidation_ts
                FROM cache_invalidation_stream_by_instance
                WHERE stream_id > ? ORDER BY stream_id""",
                after,
            )
        )

    # Building the DAG.

    def _build(
        self,
        twin: _Twin,
        event_type: str,
        content: dict[str, Any] | Callable[[_Twin], dict[str, Any]],
        *,
        state_key: str | None = None,
        prev: list[int] | None = None,
        redacts: int | None = None,
        sender: str | None = None,
        txn_id: str | None = None,
        sticky_ms: int | None = None,
        push: bool = False,
    ) -> EventPersistencePair:
        """An event of the twin's, not yet persisted, added to its events.
        `prev` and `redacts` are indices of the twin's events. `content` can
        be a function of the twin, for content that names its events. With
        `push`, its push actions are staged, as sending it would."""
        sender = sender or self.alice
        if callable(content):
            content = content(twin)
        event_dict: dict[str, Any] = {
            "type": event_type,
            "content": dict(content),
            "room_id": twin.room_id,
            "sender": sender,
        }
        if state_key is not None:
            event_dict["state_key"] = state_key
        if sticky_ms is not None:
            event_dict[StickyEvent.EVENT_FIELD_NAME] = {"duration_ms": sticky_ms}
        if redacts is not None:
            redacts_id = twin.events[redacts]
            event_dict["redacts"] = redacts_id
            if KNOWN_ROOM_VERSIONS[self.room_version].updated_redaction_rules:
                event_dict["content"]["redacts"] = redacts_id
        event, unpersisted_context = self.get_success(
            self.hs.get_event_creation_handler().create_event(
                create_requester(sender, device_id="DEVICE" if txn_id else None),
                event_dict,
                txn_id=txn_id,
                prev_event_ids=(
                    [twin.events[i] for i in prev] if prev is not None else None
                ),
            )
        )
        context = self.get_success(unpersisted_context.persist(event))
        if push:
            self.get_success(
                self.hs.get_bulk_push_rule_evaluator().action_for_events_by_user(
                    [(event, context)]
                )
            )
        twin.events.append(event.event_id)
        return event, context

    def _step(
        self,
        build: Callable[[_Twin], list[EventPersistencePair]],
        *,
        backfilled: bool = False,
    ) -> None:
        """Build a chunk in each twin with `build`, and persist it."""
        for twin in self.twins:
            chunk = build(twin)
            with self._persisting(twin):
                self.get_success(
                    self.persistence.persist_events(chunk, backfilled=backfilled)
                )
        self.assertEqual(len(self.twins[0].events), len(self.twins[1].events))

    def _send(
        self,
        event_type: str,
        content: dict[str, Any] | Callable[[_Twin], dict[str, Any]],
        **kwargs: Any,
    ) -> int:
        """Persist one event in each twin. Returns its index."""
        self._step(lambda twin: [self._build(twin, event_type, content, **kwargs)])
        return len(self.twins[0].events) - 1

    def _message(self, body: str, **kwargs: Any) -> int:
        return self._send(
            EventTypes.Message, {"msgtype": "m.text", "body": body}, **kwargs
        )

    def _outlier(self, pair: EventPersistencePair) -> EventPersistencePair:
        """A copy of the event as an outlier. Outliers have no state, except
        in a state DAG room, where `/get_missing_events` persists them with
        it."""
        event, context = pair
        copy = make_event_from_dict(
            event.get_pdu_json(),
            event.room_version,
            event.internal_metadata.get_dict(),
        )
        copy.internal_metadata.outlier = True
        if event.room_version.msc4242_state_dags:
            return copy, context
        return copy, EventContext.for_outlier(self.hs.get_storage_controllers())

    # Comparing.

    def _dump(self, twin: _Twin) -> dict[str, list[tuple]]:
        """The twin's rows of each table, normalised and sorted."""
        raw: dict[str, tuple[list[str], list[tuple]]] = {}
        for table in _TABLES:
            columns = self._columns(table)
            if table in _REMOTE_USER_TABLES:
                where = "user_id LIKE ?"
            elif "room_id" in columns and table not in _UNFILLED_ROOM_ID:
                where = "room_id = ?"
            elif table == "event_auth_chain_links":
                where = """origin_chain_id IN (
                    SELECT chain_id FROM event_auth_chains
                    INNER JOIN events USING (event_id) WHERE room_id = ?
                )"""
            else:
                where = "event_id IN (SELECT event_id FROM events WHERE room_id = ?)"
            # The driver cannot decode a `tsvector`.
            selected = [
                f"{column}::text" if data_type == "tsvector" else column
                for column, data_type in zip(columns, self._types[table])
            ]
            rows = self.get_success(
                self.store.db_pool.execute(
                    "test",
                    f"SELECT {', '.join(selected)} FROM {table} WHERE {where}",
                    _remote_user(twin, "%")
                    if table in _REMOTE_USER_TABLES
                    else twin.room_id,
                )
            )
            raw[table] = (columns, rows)

        normaliser = self._normaliser(twin, raw)
        dump = {}
        for table, (columns, rows) in raw.items():
            dump[table] = sorted(
                (
                    tuple(
                        normaliser.value(table, column, value)
                        for column, value in zip(columns, row)
                    )
                    for row in rows
                ),
                key=repr,
            )
        dump["cache_invalidation_stream_by_instance"] = _cache_rows(
            normaliser, twin.cache_rows
        )
        dump["effects"] = sorted(
            (
                (
                    name,
                    owner,
                    tuple(
                        normaliser.position(kind, arg)
                        if kind
                        else normaliser.value("", "", arg)
                        for kind, arg in zip(_effect_kinds(name, owner, args), args)
                    ),
                    tuple(sorted(kwargs.items())),
                )
                for name, owner, args, kwargs in twin.effects
            ),
            key=repr,
        )
        return dump

    def _normaliser(
        self, twin: _Twin, raw: dict[str, tuple[list[str], list[tuple]]]
    ) -> _Normaliser:
        # The scenario's events by index, then the rest (the room's
        # creation) by stream ordering.
        event_ids = {event_id: f"$e{i}" for i, event_id in enumerate(twin.events)}
        columns, rows = raw["events"]
        by_ordering = sorted(
            rows, key=lambda row: row[columns.index("stream_ordering")]
        )
        for row in by_ordering:
            event_id = row[columns.index("event_id")]
            event_ids.setdefault(event_id, f"$s{len(event_ids)}")

        values: dict[str, set[int]] = {}
        for table, (columns, rows) in raw.items():
            for i, column in enumerate(columns):
                kind = _STREAM_COLUMNS.get((table, column), _STREAM_COLUMNS.get(column))
                if not kind:
                    continue
                values.setdefault(kind, set()).update(
                    row[i] for row in rows if isinstance(row[i], int)
                )
        values.setdefault("cache", set()).update(row[0] for row in twin.cache_rows)
        # Positions that only an effect holds, such as the stream ID of a
        # sticky event that was redacted.
        for name, owner, args, _ in twin.effects:
            for kind, arg in zip(_effect_kinds(name, owner, args), args):
                if not kind:
                    continue
                found = values.setdefault(kind, set())
                if isinstance(arg, int):
                    found.add(arg)
                elif isinstance(arg, (list, tuple)):
                    found.update(v for v in arg if isinstance(v, int))
        ranks = {
            kind: {value: rank for rank, value in enumerate(sorted(found))}
            for kind, found in values.items()
        }
        normaliser = _Normaliser(twin, event_ids, ranks)

        columns, rows = raw["event_auth_chains"]
        for row in rows:
            chain_id = row[columns.index("chain_id")]
            name = normaliser.string(row[columns.index("event_id")])
            if chain_id not in normaliser.chains or name < normaliser.chains[chain_id]:
                normaliser.chains[chain_id] = name
        return normaliser

    _column_cache: dict[str, list[str]] = {}
    _types: dict[str, list[str]] = {}

    def _columns(self, table: str) -> list[str]:
        if table not in self._column_cache:
            rows = self.get_success(
                self.store.db_pool.execute(
                    "test",
                    """SELECT column_name, data_type FROM information_schema.columns
                    WHERE table_name = ? ORDER BY ordinal_position""",
                    table,
                )
            )
            self.assertTrue(rows, f"no table {table}")
            self._column_cache[table] = [row[0] for row in rows]
            self._types[table] = [row[1] for row in rows]
        return self._column_cache[table]

    def _assert_twins_match(self) -> None:
        a, b = (self._dump(twin) for twin in self.twins)
        differences = [table for table in a if a[table] != b[table]]
        for table in differences:
            self.assertEqual(a[table], b[table], f"{table} differs")


class _RecordingPersister:
    """The Rust persister, remembering the last chunk it prepared, which
    room B's transaction takes."""

    def __init__(self, persister: Any) -> None:
        self._persister = persister
        self.last_prepared: Any = None
        # The room and event IDs `last_prepared` is for.
        self.last_chunk: tuple[str, list[str]] | None = None

    def prepare_chunk(
        self,
        room_id: str,
        events_and_contexts: list[EventPersistencePair],
        *args: Any,
        **kwargs: Any,
    ) -> "Deferred[Any]":
        chunk = (room_id, [event.event_id for event, _ in events_and_contexts])

        def record(prepared: Any) -> Any:
            self.last_prepared = prepared
            self.last_chunk = chunk
            return prepared

        return self._persister.prepare_chunk(
            room_id, events_and_contexts, *args, **kwargs
        ).addCallback(record)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._persister, name)


# Callbacks that only tell others to look, compared by whether they were
# called.
_NOTIFIERS = {
    "notify_replication",
    "on_new_replication_data",
    "run_as_background_process",
}


def _background_process_owner(desc: str, args: tuple) -> str:
    """What a background process of a persist runs: the `stream_positions`
    update of an ID generator, by its stream, or its description."""
    if desc == "MultiWriterIdGenerator._update_table":
        # `runInteraction`'s description and function.
        assert args[0] == desc, args
        id_gen = getattr(args[1], "__self__", None)
        assert isinstance(id_gen, MultiWriterIdGenerator), args
        return f"stream_positions:{id_gen._stream_name}"
    return desc


def _merged_effects(
    effects: list[tuple[str, str | None, tuple, dict[str, Any]]],
) -> list[tuple[str, str | None, tuple, dict[str, Any]]]:
    """One persist's effects, with each stream's finished IDs in one call,
    and each notifier called once: Python queues them per allocation of
    IDs, and applying Rust's effects does each once."""
    merged = []
    finished: dict[str | None, list[int]] = {}
    notified = set()
    for name, owner, args, kwargs in effects:
        if name == "_mark_ids_as_finished":
            finished.setdefault(owner, []).extend(args[0])
        elif name in _NOTIFIERS:
            notified.add((name, owner))
        else:
            merged.append((name, owner, args, kwargs))
    merged.extend(
        ("_mark_ids_as_finished", owner, (sorted(ids),), {})
        for owner, ids in finished.items()
    )
    merged.extend((name, owner, (), {}) for name, owner in notified)
    return merged


def _rust_effects(
    effects: PersistEffects,
) -> list[tuple[str, str | None, tuple, dict[str, Any]]]:
    """Rust's effects as the Python transaction's `call_after` callbacks."""
    calls: list[tuple[str, str | None, tuple, dict[str, Any]]] = []
    for *args, backfilled in effects.events:
        calls.append(
            (
                "_invalidate_caches_for_event",
                None,
                tuple(args),
                {"backfilled": backfilled},
            )
        )
    for event_id in effects.event_cache_invalidations:
        calls.append(("_invalidate_local_get_event_cache", None, (event_id,), {}))
    for room_id, members_changed in effects.state_caches:
        calls.append(
            ("_invalidate_state_caches", None, (room_id, set(members_changed)), {})
        )
    for cache_name, keys in effects.cache_invalidations:
        calls.append(("invalidate", cache_name, (tuple(keys),), {}))
    # The stream IDs finished and the notifiers called are recorded as
    # they are applied, by `_apply_recorded`.
    for cache, entity, stream_id in effects.stream_changes:
        calls.append(
            (
                "entity_has_changed",
                _STREAM_CHANGE_CACHES[cache],
                (entity, stream_id),
                {},
            )
        )
    if effects.event_cache_prefills:
        calls.append(
            (
                "local_prefill",
                "_get_event_cache",
                (frozenset(effects.event_cache_prefills),),
                {},
            )
        )
    for event_id, state_group in effects.state_group_prefills:
        calls.append(
            ("prefill", "_get_state_group_for_event", ((event_id,), state_group), {})
        )
    return calls


# The names of the stream change caches, by `PersistEffects.stream_changes`
# key.
_STREAM_CHANGE_CACHES = {
    "events": "EventsRoomStreamChangeCache",
    "current_state_deltas": "_curr_state_delta_stream_cache",
    "membership": "MembershipStreamChangeCache",
}


def _python_steps_after_rust(
    store: PersistEventsStore,
    txn: LoggingTransaction,
    *,
    room_id: str,
    events_and_contexts: list[EventPersistencePair],
    inhibit_local_membership_updates: bool,
    state_delta_for_room: Any,
    new_forward_extremities: set[str] | None,
    new_event_links: Any,
    sliding_sync_table_changes: Any,
    new_state_dag_forward_extremities: set[str] | None = None,
    sticky_events_to_un_soft_fail: Any = frozenset(),
) -> None:
    """The steps of `_persist_events_txn` that Rust does not do yet, as it
    runs them: the profile updates at the end of `_update_current_state_txn`.
    `events_and_contexts` are the events Rust left.

    A copy, so that the production transaction need not be split where the
    Rust port has got to. It goes once Rust runs the whole transaction. A
    step added to Python's transaction meanwhile runs in room A only, so its
    rows differ and the comparison fails."""
    if state_delta_for_room:
        assert sliding_sync_table_changes is not None
        store._record_profile_updates_for_state_delta_txn(
            txn, room_id, sliding_sync_table_changes
        )


def _power_levels(user_id: str, level: int) -> dict[str, Any]:
    """Power levels where `user_id` at `level` can send state if it is 50."""
    return {
        "users": {user_id: level},
        "users_default": 0,
        "events_default": 0,
        "state_default": 50,
        "invite": 50,
        "ban": 50,
        "kick": 50,
        "redact": 50,
        "events": {},
    }


def _rehash(pdu: dict[str, Any]) -> None:
    """Give a hand-built event the content hash of its changed content."""
    _, digest = compute_content_hash(pdu, hashlib.sha256)
    pdu["hashes"] = {"sha256": encode_base64(digest)}


def _remote_user(twin: _Twin, localpart: str = "remote") -> str:
    """A remote user of the twin's own, on a server named for the twin, as
    the twins' users are compared by name."""
    return f"@{localpart}:twin-{twin.name}.example"


def _cache_rows(normaliser: _Normaliser, rows: list[tuple]) -> list[tuple]:
    """The cache stream rows, normalised, in stream order, but for the
    order Python's sets give. Each run of rows that Python writes from a
    set is merged: the `cs_cache_fake` rows of a room into its members and
    the sizes of the rows, and the single-key rows of a cache into their
    keys. The twins share the clock, so the times compare as they are."""
    merged: list[tuple] = []
    for _, instance_name, cache_func, keys, ts in rows:
        keys = normaliser.value("", "", keys)
        if cache_func == "cs_cache_fake":
            group = (instance_name, cache_func, keys[0], ts)
            members, sizes = tuple(keys[1:]), (len(keys),)
        elif keys is not None and len(keys) == 1:
            group = (instance_name, cache_func, None, ts)
            members, sizes = keys, (1,)
        else:
            merged.append(((instance_name, cache_func, keys, ts), (), ()))
            continue
        if merged and merged[-1][0] == group:
            _, run_members, run_sizes = merged.pop()
            members, sizes = run_members + members, run_sizes + sizes
        merged.append((group, members, sizes))
    return [
        (*group, tuple(sorted(members)), tuple(sorted(sizes)))
        for group, members, sizes in merged
    ]


class PersistTxnTestCase(_TwinTestBase):
    def test_room_creation(self) -> None:
        self._assert_twins_match()

    def test_messages(self) -> None:
        for body in ("one", "two", "three"):
            self._message(body)
        self._assert_twins_match()

    def test_state_changes(self) -> None:
        self._send(EventTypes.Topic, {"topic": "Topic"}, state_key="")
        self._send(EventTypes.Name, {"name": "Name"}, state_key="")
        self._send(
            EventTypes.Member,
            {"membership": "invite"},
            state_key=self.bob,
        )
        self._send(
            EventTypes.Member,
            {"membership": "join"},
            state_key=self.bob,
            sender=self.bob,
        )
        self._message("hello", sender=self.bob)
        self._assert_twins_match()

    def test_fork(self) -> None:
        base = self._message("base")
        self._send(EventTypes.Topic, {"topic": "A"}, state_key="", prev=[base])
        # State resolution orders the topics by timestamp, then by event ID,
        # which differs between the twins.
        self.reactor.advance(1)
        self._send(EventTypes.Topic, {"topic": "B"}, state_key="", prev=[base])
        # Joins the two branches, resolving state.
        self._message("merge")
        self._assert_twins_match()

    def test_redaction_after_target(self) -> None:
        target = self._message("secret")
        self._send(EventTypes.Redaction, {}, redacts=target)
        self._assert_twins_match()

    def test_redaction_before_target(self) -> None:
        base = self._message("base")
        built: dict[str, EventPersistencePair] = {}

        def target(twin: _Twin) -> list[EventPersistencePair]:
            # Built, but persisted after its redaction.
            built[twin.name] = self._build(
                twin, EventTypes.Message, {"msgtype": "m.text", "body": "secret"}
            )
            return []

        self._step(target)
        target_index = len(self.twins[0].events) - 1

        def redaction(twin: _Twin) -> list[EventPersistencePair]:
            event, context = self._build(
                twin, EventTypes.Redaction, {}, redacts=target_index, prev=[base]
            )
            # Without the target, the recheck cannot pass yet, so it is
            # stored as still needed.
            event.internal_metadata.recheck_redaction = True
            return [(event, context)]

        self._step(redaction)
        self._step(lambda twin: [built[twin.name]])
        self._assert_twins_match()

    def test_redaction_of_later_redaction(self) -> None:
        """A redaction is never redacted as it is persisted, so a redaction
        of it that came first needs censoring again."""
        message = self._message("secret")
        built: dict[str, EventPersistencePair] = {}

        def target(twin: _Twin) -> list[EventPersistencePair]:
            built[twin.name] = self._build(
                twin, EventTypes.Redaction, {}, redacts=message
            )
            return []

        self._step(target)
        self._send(
            EventTypes.Redaction,
            {},
            redacts=len(self.twins[0].events) - 1,
            prev=[message],
        )
        # As if the censor job had run.
        self.get_success(
            self.store.db_pool.simple_update(
                "redactions", {}, {"have_censored": True}, desc="test"
            )
        )
        self._step(lambda twin: [built[twin.name]])
        self._assert_twins_match()

    def test_relation(self) -> None:
        root = self._message("root")
        self._send(
            EventTypes.Message,
            lambda twin: {
                "msgtype": "m.text",
                "body": "reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": twin.events[root]},
            },
        )
        self._assert_twins_match()

    def test_transaction_ids(self) -> None:
        self._message("hello", txn_id="txn1")
        self._assert_twins_match()

    def test_url_and_unicode(self) -> None:
        self._send(
            EventTypes.Message,
            {"msgtype": "m.image", "body": "h\u00e9llo \U0001f44d", "url": "mxc://a/b"},
        )
        # Not a string, so not a URL.
        self._send(EventTypes.Message, {"msgtype": "m.text", "body": "x", "url": 1})
        self._assert_twins_match()

    def test_redaction_recheck(self) -> None:
        """A redaction that needs a recheck, which passes as Alice redacts
        her own event, is stored as no longer needing one."""
        target = self._message("secret")

        def redaction(twin: _Twin) -> list[EventPersistencePair]:
            event, context = self._build(twin, EventTypes.Redaction, {}, redacts=target)
            event.internal_metadata.recheck_redaction = True
            return [(event, context)]

        self._step(redaction)
        self._assert_twins_match()

    def test_redaction_recheck_in_chunk(self) -> None:
        """The same, with the redaction and its target in one chunk."""
        base = self._message("base")

        def chunk(twin: _Twin) -> list[EventPersistencePair]:
            target = self._build(
                twin, EventTypes.Message, {"msgtype": "m.text", "body": "secret"}
            )
            event, context = self._build(
                twin,
                EventTypes.Redaction,
                {},
                redacts=len(twin.events) - 1,
                prev=[base],
            )
            event.internal_metadata.recheck_redaction = True
            return [target, (event, context)]

        self._step(chunk)
        self._assert_twins_match()

    def test_partial_state_conflict(self) -> None:
        """An event with partial state in a room without fails, as the room
        must have been un-partial stated meanwhile."""
        for twin in self.twins:
            event, context = self._build(
                twin, EventTypes.Message, {"msgtype": "m.text", "body": "partial"}
            )
            context.partial_state = True
            with self._persisting(twin):
                self.get_failure(
                    self.persistence.persist_events([(event, context)]),
                    PartialStateConflictError,
                )
        self._assert_twins_match()

    def test_sticky_redacted(self) -> None:
        """Redacting a sticky event makes it stop being sticky."""
        target = self._message("sticky", sticky_ms=60_000)
        self._send(EventTypes.Redaction, {}, redacts=target)
        self._assert_twins_match()

    def test_rejected(self) -> None:
        def rejected(twin: _Twin) -> list[EventPersistencePair]:
            event, context = self._build(
                twin, EventTypes.Topic, {"topic": "no"}, state_key=""
            )
            context.rejected = "auth_error"
            return [(event, context)]

        self._step(rejected)
        self._message("after")
        self._assert_twins_match()

    def _sticky_event_ids(self, twin: _Twin) -> list[str]:
        """The twin's sticky events, by scenario index, in stream order."""
        rows = self.get_success(
            self.store.db_pool.execute(
                "test",
                "SELECT event_id FROM sticky_events WHERE room_id = ? ORDER BY stream_id",
                twin.room_id,
            )
        )
        return [f"$e{twin.events.index(event_id)}" for (event_id,) in rows]

    def test_sticky_events(self) -> None:
        """The sticky events of a chunk take sticky stream IDs in one go.
        An event whose stickiness has run out is not sticky."""
        self._step(
            lambda twin: [
                self._build(
                    twin,
                    EventTypes.Message,
                    {"msgtype": "m.text", "body": body},
                    sticky_ms=sticky_ms,
                )
                for body, sticky_ms in (("one", 60_000), ("over", 0), ("two", 1_000))
            ]
        )
        self._message("not sticky")
        self._assert_twins_match()
        self.assertEqual(self._sticky_event_ids(self.twins[1]), ["$e0", "$e2"])

    def _sticky_outlier_step(self) -> dict[str, EventPersistencePair]:
        """Persist a sticky message as an outlier, and return each twin's
        event to de-outlier it with."""
        built: dict[str, EventPersistencePair] = {}

        def outlier(twin: _Twin) -> list[EventPersistencePair]:
            built[twin.name] = self._build(
                twin,
                EventTypes.Message,
                {"msgtype": "m.text", "body": "sticky"},
                sticky_ms=60_000,
            )
            return [self._outlier(built[twin.name])]

        self._step(outlier, backfilled=True)
        return built

    def test_sticky_de_outlier(self) -> None:
        """An outlier is not sticky until it is de-outliered."""
        built = self._sticky_outlier_step()
        self.assertEqual(self._sticky_event_ids(self.twins[1]), [])
        self._step(lambda twin: [built[twin.name]])
        self._assert_twins_match()
        self.assertEqual(self._sticky_event_ids(self.twins[1]), ["$e0"])

    def test_sticky_de_outlier_redacted(self) -> None:
        """A sticky event de-outliered and redacted in one chunk.

        It stays sticky, in Python too: its `sticky_events` row has the
        chunk's stream ordering, but its `events` row keeps the one it was
        first persisted with, which `delete_sticky_events_txn` looks for."""
        built = self._sticky_outlier_step()
        self._step(
            lambda twin: [
                built[twin.name],
                self._build(twin, EventTypes.Redaction, {}, redacts=0),
            ]
        )
        self._assert_twins_match()
        self.assertEqual(self._sticky_event_ids(self.twins[1]), ["$e0"])

    def test_sticky_un_soft_failed(self) -> None:
        """A soft-failed sticky event of Bob's stops being soft failed when
        he joins, and moves to the end of the sticky stream."""

        def soft_failed(twin: _Twin) -> list[EventPersistencePair]:
            event, context = self._build(
                twin,
                EventTypes.Message,
                {"msgtype": "m.text", "body": "sticky"},
                sender=self.bob,
                sticky_ms=60_000,
            )
            event.internal_metadata.soft_failed = True
            return [(event, context)]

        self._step(soft_failed)
        self._message("other sticky", sticky_ms=60_000)
        self.assertEqual(self._sticky_event_ids(self.twins[1]), ["$e0", "$e1"])
        self._join_bob()
        self._assert_twins_match()
        self.assertEqual(self._sticky_event_ids(self.twins[1]), ["$e1", "$e0"])
        event = self.get_success(self.store.get_event(self.twins[1].events[0]))
        self.assertFalse(event.internal_metadata.is_soft_failed())

    def test_sticky_un_soft_failed_several(self) -> None:
        """Several soft-failed sticky events lifted at once keep their order
        in the stream."""

        def soft_failed(twin: _Twin) -> list[EventPersistencePair]:
            pairs = []
            for body in ("one", "two", "three"):
                event, context = self._build(
                    twin,
                    EventTypes.Message,
                    {"msgtype": "m.text", "body": body},
                    sender=self.bob,
                    sticky_ms=60_000,
                )
                event.internal_metadata.soft_failed = True
                pairs.append((event, context))
            return pairs

        self._step(soft_failed)
        self._join_bob()
        self._assert_twins_match()
        self.assertEqual(self._sticky_event_ids(self.twins[1]), ["$e0", "$e1", "$e2"])

    def test_sticky_rejected(self) -> None:
        """A rejected event is not sticky, and does not stop the rest of
        its chunk being so."""

        def chunk(twin: _Twin) -> list[EventPersistencePair]:
            event, context = self._build(
                twin,
                EventTypes.Message,
                {"msgtype": "m.text", "body": "rejected"},
                sticky_ms=60_000,
            )
            context.rejected = "auth_error"
            return [
                (event, context),
                self._build(
                    twin,
                    EventTypes.Message,
                    {"msgtype": "m.text", "body": "accepted"},
                    sticky_ms=60_000,
                ),
            ]

        self._step(chunk)
        self._assert_twins_match()
        self.assertEqual(self._sticky_event_ids(self.twins[1]), ["$e1"])

    def test_sticky_backfilled(self) -> None:
        """A backfilled sticky event, with a negative stream ordering, which
        does not bump the room."""
        self._message("now")
        self._step(
            lambda twin: [
                self._build(
                    twin,
                    EventTypes.Message,
                    {"msgtype": "m.text", "body": "old"},
                    sticky_ms=60_000,
                )
            ],
            backfilled=True,
        )
        self._assert_twins_match()

    def test_sticky_redacted_in_chunk(self) -> None:
        """A sticky event redacted in its own chunk is stored redacted, and
        so is not sticky."""
        self._step(
            lambda twin: [
                self._build(
                    twin,
                    EventTypes.Message,
                    {"msgtype": "m.text", "body": "sticky"},
                    sticky_ms=60_000,
                ),
                self._build(twin, EventTypes.Redaction, {}, redacts=0),
            ]
        )
        self._assert_twins_match()
        self.assertEqual(self._sticky_event_ids(self.twins[1]), [])

    def test_sticky_de_outlier_and_new(self) -> None:
        """A de-outliered sticky event takes its sticky stream ID before the
        new sticky events of its chunk."""
        built = self._sticky_outlier_step()
        self._step(
            lambda twin: [
                built[twin.name],
                self._build(
                    twin,
                    EventTypes.Message,
                    {"msgtype": "m.text", "body": "new"},
                    sticky_ms=60_000,
                ),
            ]
        )
        self._assert_twins_match()
        self.assertEqual(self._sticky_event_ids(self.twins[1]), ["$e0", "$e1"])

    def test_outlier_then_de_outlier(self) -> None:
        built: dict[str, EventPersistencePair] = {}

        def outlier(twin: _Twin) -> list[EventPersistencePair]:
            built[twin.name] = self._build(
                twin, EventTypes.Topic, {"topic": "outlier"}, state_key=""
            )
            return [self._outlier(built[twin.name])]

        # Federation persists outliers as backfilled.
        self._step(outlier, backfilled=True)
        self._step(lambda twin: [built[twin.name]])
        self._assert_twins_match()

    def test_de_outlier_with_gap(self) -> None:
        """An event de-outliered while its prev event is an outlier: the
        prev event becomes a backward extremity, with a timeline gap. The
        event's own extremity and failed pull attempts go."""
        prev = self._message("prev")
        built: dict[str, EventPersistencePair] = {}

        def outlier(twin: _Twin) -> list[EventPersistencePair]:
            built[twin.name] = self._build(
                twin, EventTypes.Topic, {"topic": "gap"}, state_key=""
            )
            return [self._outlier(built[twin.name])]

        self._step(outlier, backfilled=True)
        for twin in self.twins:
            # The prev event as if we had only fetched it as an outlier.
            self.get_success(
                self.store.db_pool.simple_update_one(
                    "events", {"event_id": twin.events[prev]}, {"outlier": True}
                )
            )
            event_id = built[twin.name][0].event_id
            for table, row in (
                ("event_backward_extremities", {}),
                (
                    "event_failed_pull_attempts",
                    {"num_attempts": 1, "last_attempt_ts": 0, "last_cause": "x"},
                ),
            ):
                self.get_success(
                    self.store.db_pool.simple_insert(
                        table,
                        {"room_id": twin.room_id, "event_id": event_id, **row},
                        desc="test",
                    )
                )
        self._step(lambda twin: [built[twin.name]])
        self._assert_twins_match()
        gaps = self.get_success(
            self.store.db_pool.simple_select_onecol(
                "timeline_gaps", {"room_id": self.twins[1].room_id}, "stream_ordering"
            )
        )
        self.assertEqual(len(gaps), 1)

    def test_duplicates_in_chunk(self) -> None:
        def duplicated(twin: _Twin) -> list[EventPersistencePair]:
            pair = self._build(twin, EventTypes.Topic, {"topic": "twice"}, state_key="")
            return [self._outlier(pair), pair]

        self._step(duplicated)
        self._assert_twins_match()

    def test_duplicates_in_chunk_outlier_last(self) -> None:
        """The event cache gets the copy that was written, the earliest
        non-outlier, as Python's `_add_to_cache` reads the deduplicated
        chunk."""
        event_ids = []

        def duplicated(twin: _Twin) -> list[EventPersistencePair]:
            pair = self._build(twin, EventTypes.Topic, {"topic": "twice"}, state_key="")
            event_ids.append(pair[0].event_id)
            return [pair, self._outlier(pair)]

        # Trial's event cache holds one entry, so watch what goes in.
        with patch.object(
            self.store, "_get_event_cache", Mock(wraps=self.store._get_event_cache)
        ) as event_cache:
            self._step(duplicated)
        self._assert_twins_match()
        cached = {
            call.args[0][0]: call.args[1].internal_metadata.is_outlier()
            for call in event_cache.set_local.call_args_list
            if call.args[0][0] in event_ids
        }
        self.assertEqual(cached, dict.fromkeys(event_ids, False))

    def test_backfill(self) -> None:
        self._message("now")
        self._step(
            lambda twin: [
                self._build(
                    twin, EventTypes.Message, {"msgtype": "m.text", "body": "old"}
                )
            ],
            backfilled=True,
        )
        self._assert_twins_match()

    def _join_bob(self) -> None:
        self._send(EventTypes.Member, {"membership": "invite"}, state_key=self.bob)
        self._send(
            EventTypes.Member,
            {"membership": "join", "displayname": "Bob", "avatar_url": "mxc://a/b"},
            state_key=self.bob,
            sender=self.bob,
        )

    def test_relations_and_their_redaction(self) -> None:
        """Each kind of relation, then redactions of them that clear what
        was computed from them. Redacting the thread's latest reply moves
        the thread back, and redacting its last reply deletes it."""
        root = self._message("root")

        def relates(rel_type: str, **extra: Any) -> int:
            return self._send(
                EventTypes.Message,
                lambda twin: {
                    "msgtype": "m.text",
                    "body": rel_type,
                    "m.relates_to": {
                        "rel_type": rel_type,
                        "event_id": twin.events[root],
                        **extra,
                    },
                },
            )

        edit = relates("m.replace")
        reference = relates("m.reference")
        annotation = relates("m.annotation", key="\U0001f44d")
        first_reply = relates("m.thread")
        second_reply = relates("m.thread")
        for target in (edit, reference, annotation, second_reply, first_reply):
            self._send(EventTypes.Redaction, {}, redacts=target)
        self._assert_twins_match()

    def test_relation_and_redaction_in_one_chunk(self) -> None:
        """A redaction runs its relation cleanup before the chunk's
        relations are stored, so the relation it redacts in the same chunk
        is kept."""
        root = self._message("root")

        def chunk(twin: _Twin) -> list[EventPersistencePair]:
            reply = self._build(
                twin,
                EventTypes.Message,
                {
                    "msgtype": "m.text",
                    "body": "reply",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": twin.events[root],
                    },
                },
            )
            redaction = self._build(
                twin,
                EventTypes.Redaction,
                {},
                redacts=len(twin.events) - 1,
                prev=[root],
            )
            return [reply, redaction]

        self._step(chunk)
        self._assert_twins_match()

    def test_thread_replies_in_one_chunk(self) -> None:
        """Each reply compares with the one before, so the thread's latest
        event is the later reply."""
        root = self._message("root")

        def replies(twin: _Twin) -> list[EventPersistencePair]:
            return [
                self._build(
                    twin,
                    EventTypes.Message,
                    {
                        "msgtype": "m.text",
                        "body": body,
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": twin.events[root],
                        },
                    },
                    prev=[root],
                )
                for body in ("first", "second")
            ]

        self._step(replies)
        self._assert_twins_match()

    def test_retention_in_one_chunk(self) -> None:
        """Two policies, whose cache stream rows keep the events' order."""

        def policies(twin: _Twin) -> list[EventPersistencePair]:
            # Different state keys, so state resolution has nothing to break
            # a tie on.
            return [
                self._build(
                    twin,
                    EventTypes.Retention,
                    {"max_lifetime": lifetime},
                    state_key=state_key,
                    prev=[0],
                )
                for state_key, lifetime in (("", 1000), ("other", 2000))
            ]

        self._message("base")
        self._step(policies)
        self._assert_twins_match()

    def test_search(self) -> None:
        self._send(EventTypes.Name, {"name": "Name"}, state_key="")
        self._send(
            EventTypes.Topic,
            {
                "topic": "plain",
                "m.topic": {
                    "m.text": [
                        {"mimetype": "text/html", "body": "<b>rich</b>"},
                        {"body": "from m.topic"},
                    ]
                },
            },
            state_key="",
        )
        self._message("searchable words")
        self._assert_twins_match()

    def test_retention(self) -> None:
        self._send(
            EventTypes.Retention,
            {"min_lifetime": 1000, "max_lifetime": 86_400_000},
            state_key="",
        )
        # No lifetimes, so no policy.
        self._send(EventTypes.Retention, {}, state_key="")
        self._assert_twins_match()

    def test_labels_and_expiry(self) -> None:
        self._send(
            EventTypes.Message,
            {
                "msgtype": "m.text",
                "body": "labelled",
                "org.matrix.labels": ["#fun", "#work"],
                "org.matrix.self_destruct_after": 2_000_000_000_000,
            },
        )
        # Not an integer, so no expiry.
        self._send(
            EventTypes.Message,
            {
                "msgtype": "m.text",
                "body": "x",
                "org.matrix.self_destruct_after": "1",
            },
        )
        self._assert_twins_match()

    def test_members(self) -> None:
        self._join_bob()
        self._send(
            EventTypes.Member,
            # A NUL is not allowed in the database, so it is not stored.
            {"membership": "join", "displayname": "B\u0000b", "avatar_url": 1},
            state_key=self.bob,
            sender=self.bob,
        )
        self._assert_twins_match()

    def test_sliding_sync_state(self) -> None:
        """The state columns of `sliding_sync_joined_rooms` and of the
        membership snapshots."""
        self._join_bob()
        self._send(
            EventTypes.RoomEncryption,
            {"algorithm": "m.megolm.v1.aes-sha2"},
            state_key="",
        )
        self._send(EventTypes.Name, {"name": "Room"}, state_key="")
        self._send(
            EventTypes.Tombstone,
            {"body": "Moved", "replacement_room": "!new:test"},
            state_key="",
        )
        self._send(
            EventTypes.Member,
            {"membership": "leave"},
            state_key=self.bob,
            sender=self.bob,
        )
        self._assert_twins_match()

    def test_server_leaves(self) -> None:
        """The last local member leaving takes the room's current state."""
        self._join_bob()
        self._send(
            EventTypes.Member,
            {"membership": "leave"},
            state_key=self.bob,
            sender=self.bob,
        )
        self._send(EventTypes.Member, {"membership": "leave"}, state_key=self.alice)
        self._assert_twins_match()
        for twin in self.twins:
            self.assertEqual(
                self.get_success(
                    self.store.db_pool.simple_select_onecol(
                        "current_state_events", {"room_id": twin.room_id}, "event_id"
                    )
                ),
                [],
            )

    def _remote_membership(
        self, twin: _Twin, membership: str, localpart: str = "remote"
    ) -> EventPersistencePair:
        """A membership event of one of the twin's remote users' own, as it
        arrives over federation."""
        remote = _remote_user(twin, localpart)
        event, _ = self._build(
            twin, EventTypes.Member, {"membership": "invite"}, state_key=remote
        )
        pdu = event.get_pdu_json()
        pdu["content"] = {"membership": membership}
        pdu["sender"] = remote
        _rehash(pdu)
        copy = make_event_from_dict(pdu, event.room_version, {})
        twin.events[-1] = copy.event_id
        context = self.get_success(
            self.hs.get_state_handler().compute_event_context(copy)
        )
        return copy, context

    def _track_device_lists(self, localpart: str = "remote") -> None:
        """Track the device list of each twin's remote user."""
        for twin in self.twins:
            self.get_success(
                self.store.db_pool.simple_upsert(
                    "device_lists_remote_extremeties",
                    {"user_id": _remote_user(twin, localpart)},
                    {"stream_id": "1"},
                )
            )

    def _tracks_device_list(self, twin: _Twin, localpart: str = "remote") -> bool:
        return bool(
            self.get_success(
                self.store.db_pool.simple_select_onecol(
                    "device_lists_remote_extremeties",
                    {"user_id": _remote_user(twin, localpart)},
                    "stream_id",
                )
            )
        )

    def test_remote_members(self) -> None:
        """A remote user's device list is no longer tracked once the server
        shares no room with them."""
        self._skip_hand_built_in_state_dag_rooms()
        self._track_device_lists()
        # Invited, so not in a room with the server.
        self._step(
            lambda twin: [
                self._build(
                    twin,
                    EventTypes.Member,
                    {"membership": "invite"},
                    state_key=_remote_user(twin),
                )
            ]
        )
        self._assert_twins_match()
        self.assertFalse(self._tracks_device_list(self.twins[1]))

        self._track_device_lists()
        self._step(lambda twin: [self._remote_membership(twin, "join")])
        self._assert_twins_match()
        self.assertTrue(self._tracks_device_list(self.twins[1]))

        self._step(lambda twin: [self._remote_membership(twin, "leave")])
        self._assert_twins_match()
        self.assertFalse(self._tracks_device_list(self.twins[1]))

    def test_server_leaves_remote_member(self) -> None:
        """When the server leaves, the members still joined have their
        caches invalidated too, and a remote one's device list is no
        longer tracked."""
        self._skip_hand_built_in_state_dag_rooms()
        self._step(lambda twin: [self._remote_membership(twin, "join")])
        self._track_device_lists()
        self._send(EventTypes.Member, {"membership": "leave"}, state_key=self.alice)
        self._assert_twins_match()
        self.assertFalse(self._tracks_device_list(self.twins[1]))

    def test_server_leaves_remote_members(self) -> None:
        """Two remote users unsubscribed by one persist."""
        self._skip_hand_built_in_state_dag_rooms()
        for localpart in ("one", "two"):
            self._step(lambda twin: [self._remote_membership(twin, "join", localpart)])
            self._track_device_lists(localpart)
        self._send(EventTypes.Member, {"membership": "leave"}, state_key=self.alice)
        self._assert_twins_match()
        for localpart in ("one", "two"):
            self.assertFalse(self._tracks_device_list(self.twins[1], localpart))

    def test_many_members(self) -> None:
        """More members change than one `cs_cache_fake` row names."""
        self._step(
            lambda twin: [
                self._build(
                    twin,
                    EventTypes.Member,
                    {"membership": "invite"},
                    state_key=f"@u{i:03}:test",
                )
                for i in range(60)
            ]
        )
        self._assert_twins_match()
        # The last persist's rows.
        self.assertEqual(
            [len(row[3]) for row in self.twins[1].cache_rows[-2:]], [51, 11]
        )

    def _fork_against_bob(self, build: Callable[[int], int]) -> bool:
        """Bob, with power, sends `build(base)` on a fork from `base`, and
        Alice takes his power away on the other fork. State resolution
        then removes what Bob sent from the current state. Returns whether
        it does: in a state DAG room, Alice's event follows Bob's on the
        state DAG, so they do not conflict."""
        self._join_bob()
        self._send(EventTypes.PowerLevels, _power_levels(self.bob, 50), state_key="")
        base = self._message("base")
        build(base)
        # Alice's event sorts after Bob's.
        self.reactor.advance(1)
        self._send(
            EventTypes.PowerLevels,
            _power_levels(self.bob, 0),
            state_key="",
            prev=[base],
        )
        return not KNOWN_ROOM_VERSIONS[self.room_version].msc4242_state_dags

    def test_state_removed_by_state_res(self) -> None:
        """A state key that leaves the current state."""
        removed = self._fork_against_bob(
            lambda base: self._send(
                EventTypes.Topic,
                {"topic": "Bob's"},
                state_key="",
                prev=[base],
                sender=self.bob,
            )
        )
        self._assert_twins_match()
        self.assertEqual(
            removed,
            bool(
                self.get_success(
                    self.store.db_pool.execute(
                        "test",
                        """SELECT 1 FROM current_state_delta_stream
                    WHERE room_id = ? AND type = ? AND event_id IS NULL""",
                        self.twins[1].room_id,
                        EventTypes.Topic,
                    )
                )
            ),
        )

    def test_membership_removed_by_state_res(self) -> None:
        """A local member's membership that leaves the current state, with
        their membership snapshot."""
        carol = self.register_user("carol", "pass")
        removed = self._fork_against_bob(
            lambda base: self._send(
                EventTypes.Member,
                {"membership": "invite"},
                state_key=carol,
                prev=[base],
                sender=self.bob,
            )
        )
        self._assert_twins_match()
        for table in ("local_current_membership", "sliding_sync_membership_snapshots"):
            self.assertEqual(
                self.get_success(
                    self.store.db_pool.simple_select_onecol(
                        table,
                        {"room_id": self.twins[1].room_id, "user_id": carol},
                        "membership",
                    )
                ),
                [] if removed else ["invite"],
                table,
            )

    def test_push_actions(self) -> None:
        """Push actions move from the staging table, and a redaction
        removes them."""
        self._join_bob()
        target = self._send(
            EventTypes.Message,
            {"msgtype": "m.text", "body": "hello bob"},
            push=True,
        )
        self._assert_twins_match()
        for twin in self.twins:
            # Bob is notified.
            self.assertTrue(
                self.get_success(
                    self.store.db_pool.simple_select_onecol(
                        "event_push_actions",
                        {"room_id": twin.room_id, "user_id": self.bob},
                        "event_id",
                    )
                )
            )
        self._send(EventTypes.Redaction, {}, redacts=target, push=True)
        self._assert_twins_match()
        rows = self.get_success(
            self.store.db_pool.simple_select_onecol(
                "event_push_actions",
                {"room_id": self.twins[1].room_id},
                "event_id",
            )
        )
        # The redaction itself does not notify.
        self.assertEqual(rows, [])
        self.assertEqual(
            self.get_success(
                self.store.db_pool.simple_select_onecol(
                    "event_push_actions_staging", {}, "event_id"
                )
            ),
            [],
        )

    def _skip_hand_built_in_state_dag_rooms(self) -> None:
        if KNOWN_ROOM_VERSIONS[self.room_version].msc4242_state_dags:
            raise SkipTest("a hand-built event has no state DAG context")

    def _out_of_band(
        self,
        twin: _Twin,
        membership: str,
        stripped_state: list[dict[str, Any]] | None,
    ) -> EventPersistencePair:
        """An out-of-band membership of Bob's, as a remote invite or knock,
        or its rejection, arrives over federation: an outlier."""
        event, _ = self._build(
            twin, EventTypes.Member, {"membership": "invite"}, state_key=self.bob
        )
        pdu = event.get_pdu_json()
        pdu["content"] = {"membership": membership}
        if membership != "invite":
            pdu["sender"] = self.bob
        pdu["unsigned"] = {}
        if stripped_state is not None:
            pdu["unsigned"][f"{membership}_room_state"] = stripped_state
        _rehash(pdu)
        copy = make_event_from_dict(pdu, event.room_version, {})
        copy.internal_metadata.outlier = True
        copy.internal_metadata.out_of_band_membership = True
        copy.internal_metadata.stream_ordering = None
        twin.events[-1] = copy.event_id
        return copy, EventContext.for_outlier(self.hs.get_storage_controllers())

    def test_out_of_band_invite_and_rejection(self) -> None:
        self._skip_hand_built_in_state_dag_rooms()

        def stripped(event_type: str, content: dict[str, Any]) -> dict[str, Any]:
            return {
                "type": event_type,
                "state_key": "",
                "sender": "@remote:other",
                "content": content,
            }

        state = [
            stripped(EventTypes.Create, {"type": "m.space"}),
            stripped(EventTypes.RoomEncryption, {"algorithm": "m.megolm.v1"}),
            stripped(EventTypes.Name, {"name": "Remote"}),
            # Malformed, so ignored.
            {"type": EventTypes.Tombstone, "content": {}},
        ]
        self._step(lambda twin: [self._out_of_band(twin, "invite", state)])
        self._step(lambda twin: [self._out_of_band(twin, "leave", None)])
        self._assert_twins_match()

    def test_out_of_band_knock(self) -> None:
        self._skip_hand_built_in_state_dag_rooms()
        state = [
            {
                "type": EventTypes.Create,
                "state_key": "",
                "sender": "@remote:other",
                "content": {},
            },
            {
                "type": EventTypes.Name,
                "state_key": "",
                "sender": "@remote:other",
                # A NUL leaves the column out.
                "content": {"name": "a\u0000b"},
            },
        ]
        self._step(lambda twin: [self._out_of_band(twin, "knock", state)])
        # No stripped state.
        self._step(lambda twin: [self._out_of_band(twin, "invite", None)])
        self._assert_twins_match()

    def test_out_of_band_backfilled(self) -> None:
        """A backfilled membership is not current."""
        self._skip_hand_built_in_state_dag_rooms()
        self._step(
            lambda twin: [self._out_of_band(twin, "invite", [])], backfilled=True
        )
        self._assert_twins_match()

    def test_mismatch_is_found(self) -> None:
        """The comparison sees a row that differs."""
        self._message("hello")
        self.get_success(
            self.store.db_pool.simple_update(
                "events",
                {"event_id": self.twins[1].events[0]},
                {"depth": 1000},
                desc="test",
            )
        )
        with self.assertRaises(AssertionError):
            self._assert_twins_match()

    def test_missing_room(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Room does not exist !nope:test"):
            self.persister.persist_events_txn_blocking(
                "!nope:test",
                [],
                self.persister.last_prepared,
                inhibit_local_membership_updates=False,
            )


class StateDagPersistTxnTestCase(PersistTxnTestCase):
    room_version = "org.matrix.msc4242.12"

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["experimental_features"]["msc4242_enabled"] = True
        return config
