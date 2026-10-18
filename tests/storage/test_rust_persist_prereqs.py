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

"""The pieces that Rust event persistence builds on: the event JSON it
writes, the context it reads, the effects Python applies after a Rust
transaction, and the config and wiring. The persister itself needs the
native Rust Postgres driver:

    SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.storage.test_rust_persist_prereqs
"""

from contextlib import ExitStack
from typing import Any, Collection
from unittest import SkipTest
from unittest.mock import Mock, patch

from immutabledict import immutabledict
from prometheus_client import REGISTRY

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

import synapse.metrics
from synapse.api.constants import EventTypes, Membership, RelationTypes
from synapse.api.errors import PartialStateConflictError
from synapse.api.room_versions import RoomVersions
from synapse.config import ConfigError
from synapse.config.experimental import ExperimentalConfig
from synapse.config.homeserver import HomeServerConfig
from synapse.events import EventBase, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.logging.context import (
    LoggingContext,
    current_context,
    make_deferred_yieldable,
)
from synapse.metrics import SERVER_NAME_LABEL, _PreCollectHooks
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.controllers.persist_events import flush_rust_persist_metrics
from synapse.storage.databases.main.events import PersistEventsStore
from synapse.storage.databases.main.rust_persist import apply_rust_persist_effects
from synapse.storage.engines import PostgresEngine
from synapse.storage.rust_pool import RustConnectionPool
from synapse.synapse_rust.persist import (
    PersistEffects,
    WrittenChunk,
    persist_context_for_test,
    persist_gil_stats,
    persistence_json_for_test,
)
from synapse.types import create_requester
from synapse.util.caches.deferred_cache import DeferredCache
from synapse.util.caches.stream_change_cache import StreamChangeCache
from synapse.util.clock import Clock
from synapse.util.json import json_encoder

from tests import unittest
from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.server import make_request
from tests.test_utils.event_injection import create_event
from tests.utils import RUST_PERSIST_FOR_TESTS, default_config


class PersistenceJsonTestCase(unittest.TestCase):
    """The `event_json` columns Rust writes match what `_store_event_txn`
    writes."""

    def _python_columns(self, event: EventBase) -> tuple[str, str]:
        # As `_store_event_txn` builds them.
        d = event.get_dict_for_persistence()
        d.pop("redacted", None)
        d.pop("redacted_because", None)
        return (
            json_encoder.encode(event.internal_metadata.get_dict()),
            json_encoder.encode(d),
        )

    def _assert_same(self, event: EventBase) -> None:
        # The text, byte for byte, key order and escaping included.
        self.assertEqual(persistence_json_for_test(event), self._python_columns(event))

    def test_message(self) -> None:
        self._assert_same(
            make_event_from_dict(
                {
                    "type": "m.room.message",
                    "sender": "@alice:test",
                    "room_id": "!room:test",
                    "content": {"body": "hi", "msgtype": "m.text"},
                    "depth": 5,
                    "origin_server_ts": 1234,
                    "prev_events": ["$prev"],
                    "auth_events": ["$create", "$member"],
                    "hashes": {"sha256": "abc"},
                    "signatures": {"test": {"ed25519:a": "sig"}},
                    "unsigned": {"age_ts": 1000},
                },
                RoomVersions.V10,
                {"txn_id": "txn1", "token_id": 5, "device_id": "DEV"},
            )
        )

    def test_escapes_and_floats(self) -> None:
        # Room versions before 6 allow floats.
        self._assert_same(
            make_event_from_dict(
                {
                    "type": "m.room.message",
                    "sender": "@alice:test",
                    "room_id": "!room:test",
                    "content": {
                        "body": 'caf\u00e9 \U0001f44d \x01\x7f~ "\\ </',
                        "f": [1.5, 1e5, 1e16, 1e-7, -0.0],
                        "big": 12345678901234567890,
                    },
                    "depth": 5,
                    "origin_server_ts": 1234,
                    "prev_events": [["$prev:test", {}]],
                    "auth_events": [],
                    "event_id": "$event:test",
                    "hashes": {"sha256": "abc"},
                    "signatures": {"test": {"ed25519:a": "sig"}},
                    "unsigned": {"age_ts": 1000},
                },
                RoomVersions.V1,
                {"txn_id": "txn1", "token_id": 5, "device_id": "DEV"},
            )
        )

    def test_state_with_unsigned(self) -> None:
        # `replaces_state` and `invite_room_state` are stored; `prev_content`
        # is not.
        event = make_event_from_dict(
            {
                "type": "m.room.member",
                "state_key": "@bob:test",
                "sender": "@alice:test",
                "room_id": "!room:test",
                "content": {"membership": "invite"},
                "depth": 6,
                "origin_server_ts": 1235,
                "prev_events": ["$prev"],
                "auth_events": ["$create"],
                "hashes": {"sha256": "abc"},
                "signatures": {},
                "unsigned": {
                    "replaces_state": "$old",
                    "invite_room_state": [{"type": "m.room.name"}],
                    "prev_content": {"membership": "leave"},
                },
            },
            RoomVersions.V10,
            {
                "out_of_band_membership": True,
                "soft_failed": False,
                "send_on_behalf_of": "@as:test",
            },
        )
        self._assert_same(event)

    def test_v1_event_keeps_its_id(self) -> None:
        self._assert_same(
            make_event_from_dict(
                {
                    "event_id": "$v1:test",
                    "type": "m.room.topic",
                    "state_key": "",
                    "sender": "@alice:test",
                    "room_id": "!room:test",
                    "content": {"topic": "t"},
                    "depth": 2,
                    "origin_server_ts": 1,
                    "prev_events": [["$p:test", {}]],
                    "auth_events": [],
                    "hashes": {},
                    "signatures": {},
                },
                RoomVersions.V1,
            )
        )


class PersistContextTestCase(unittest.HomeserverTestCase):
    """The Rust persister reads the same fields of an `EventContext` that
    Python's persisting does."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user = self.register_user("alice", "pass")
        self.token = self.login("alice", "pass")
        self.room_id = self.helper.create_room_as(self.user, tok=self.token)

    def _assert_converts(self, context: EventContext) -> None:
        self.assertEqual(
            persist_context_for_test(context),
            {
                "rejected": context.rejected,
                "state_group": context._state_group,
                "state_group_before_event": context.state_group_before_event,
                "state_delta_due_to_event": context._state_delta_due_to_event,
                "state_group_deltas": context.state_group_deltas,
                "partial_state": context.partial_state,
                "app_service": context.app_service is not None,
            },
        )

    def test_state_event(self) -> None:
        _, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type=EventTypes.Topic,
                state_key="",
                sender=self.user,
                content={"topic": "a topic"},
            )
        )
        self.assertIsNotNone(context._state_delta_due_to_event)
        self.assertTrue(context.state_group_deltas)
        self._assert_converts(context)

    def test_message(self) -> None:
        _, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type=EventTypes.Message,
                sender=self.user,
                content={"body": "hi", "msgtype": "m.text"},
            )
        )
        self._assert_converts(context)

    def test_context_from_replication(self) -> None:
        """A context sent from another worker holds `immutabledict`s."""
        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type=EventTypes.Topic,
                state_key="",
                sender=self.user,
                content={"topic": "a topic"},
            )
        )
        serialized = self.get_success(
            context.serialize(event, self.hs.get_datastores().main)
        )
        received = EventContext.deserialize(
            self.hs.get_storage_controllers(), serialized
        )
        self.assertIsInstance(received._state_delta_due_to_event, immutabledict)
        self.assertTrue(received.state_group_deltas)
        self._assert_converts(received)

    def test_outlier_and_rejected(self) -> None:
        outlier = EventContext.for_outlier(self.hs.get_storage_controllers())
        self._assert_converts(outlier)

        rejected = EventContext.for_outlier(self.hs.get_storage_controllers())
        rejected.rejected = "auth_error"
        self._assert_converts(rejected)


class PersistEffectsTestCase(unittest.HomeserverTestCase):
    """Applying the effects a Rust transaction will report does to the Python
    caches what the `call_after` callbacks of a Python persist of the same
    events do.

    Each test persists a batch through Python, recording every cache update,
    then applies the effects expected for that batch and records again. The
    effects are written out by hand here: they are the list the Rust
    transaction must produce.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence
        self.user = self.register_user("alice", "pass")
        self.token = self.login("alice", "pass")
        self.room_id = self.helper.create_room_as(self.user, tok=self.token)
        self.remote_user = "@bob:remote"
        # Whether invalidations streamed to other workers wake replication:
        # only Postgres has the cache invalidation stream.
        self.streams_invalidations = isinstance(
            self.store.database_engine, PostgresEngine
        )

        # The store's caches by their method names, to record calls by name.
        self.cache_names: dict[int, str] = {}
        for name in dir(type(self.store)):
            cache = getattr(getattr(self.store, name, None), "cache", None)
            if isinstance(cache, DeferredCache):
                self.cache_names[id(cache)] = name

    def _record(self) -> tuple[ExitStack, set[tuple[Any, ...]]]:
        """Patch the cache update entry points to record their calls."""
        calls: set[tuple[Any, ...]] = set()
        stack = ExitStack()

        def wrap(cls: Any, name: str, record: Any) -> None:
            original = getattr(cls, name)

            def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                calls.add(record(self, *args, **kwargs))
                return original(self, *args, **kwargs)

            stack.enter_context(patch.object(cls, name, wrapper))

        def name(cache: DeferredCache) -> str:
            return self.cache_names.get(id(cache), repr(cache))

        wrap(DeferredCache, "invalidate", lambda c, key: ("invalidate", name(c), key))
        wrap(DeferredCache, "invalidate_all", lambda c: ("invalidate_all", name(c)))
        wrap(
            DeferredCache,
            "prefill",
            lambda c, key, value, callback=None: ("prefill", name(c), key, value),
        )
        wrap(
            StreamChangeCache,
            "entity_has_changed",
            lambda c, entity, pos: ("changed", c.name, entity, pos),
        )

        def record_instance(obj: Any, name: str) -> None:
            original = getattr(obj, name)

            def wrapper(*args: Any) -> Any:
                calls.add((name, *args))
                return original(*args)

            stack.enter_context(patch.object(obj, name, wrapper))

        record_instance(self.store, "_invalidate_local_get_event_cache")
        record_instance(self.hs.get_replication_notifier(), "notify_replication")

        original_finish = self.store._stream_id_gen._mark_ids_as_finished

        def finish(ids: list[int]) -> None:
            calls.add(("finished", tuple(ids)))
            original_finish(ids)

        stack.enter_context(
            patch.object(self.store._stream_id_gen, "_mark_ids_as_finished", finish)
        )
        record_instance(self.hs.get_notifier(), "on_new_replication_data")
        return stack, calls

    def _persist(self, *events: tuple[EventBase, EventContext]) -> set[tuple[Any, ...]]:
        stack, calls = self._record()
        with stack:
            self.get_success(self.persistence.persist_events(list(events)))
        return calls

    def _apply(self, effects: PersistEffects) -> set[tuple[Any, ...]]:
        stack, calls = self._record()
        with stack:
            apply_rust_persist_effects(self.store, effects)
        return calls

    def _create(self, **kwargs: Any) -> tuple[EventBase, EventContext]:
        return self.get_success(create_event(self.hs, room_id=self.room_id, **kwargs))

    def _event_effects(
        self,
        event: EventBase,
        relates_to: str | None = None,
    ) -> dict[str, Any]:
        """The effects of persisting one new event as a forward extremity."""
        stream = event.internal_metadata.stream_ordering
        assert stream is not None
        state_key = event.get_state_key()
        return {
            "events": [
                (
                    stream,
                    event.event_id,
                    event.room_id,
                    event.type,
                    state_key,
                    event.redacts,
                    relates_to,
                    False,
                )
            ],
            "event_cache_invalidations": [event.event_id]
            + ([event.redacts] if event.redacts else []),
            "stream_changes": [("events", event.room_id, stream)],
            "latest_event_ids": (event.room_id, [event.event_id]),
            "stream_ids": [stream],
        }

    def _state_effects(
        self,
        event: EventBase,
        context: EventContext,
        members_changed: Collection[str],
    ) -> dict[str, Any]:
        """The extra effects of a state event that changes current state."""
        stream = event.internal_metadata.stream_ordering
        assert stream is not None
        return {
            "state_caches": [(event.room_id, list(members_changed))],
            "state_group_prefills": [(event.event_id, context._state_group)],
            "stream_changes": [
                ("events", event.room_id, stream),
                ("current_state_deltas", event.room_id, stream),
            ]
            + [("membership", user, stream) for user in members_changed],
            # The state cache invalidation is streamed.
            "new_replication_data": self.streams_invalidations,
        }

    def _merge(self, *parts: dict[str, Any]) -> PersistEffects:
        merged: dict[str, Any] = {}
        for part in parts:
            for key, value in part.items():
                if isinstance(value, list):
                    merged.setdefault(key, []).extend(value)
                else:
                    merged[key] = value
        return PersistEffects(**merged)

    def test_message_in_a_thread(self) -> None:
        root, root_context = self._create(
            type=EventTypes.Message,
            sender=self.user,
            content={"body": "root", "msgtype": "m.text"},
        )
        self.get_success(self.persistence.persist_event(root, root_context))

        reply, context = self._create(
            type=EventTypes.Message,
            sender=self.user,
            content={
                "body": "reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": root.event_id,
                },
            },
        )
        python = self._persist((reply, context))

        effects = self._merge(
            self._event_effects(reply, relates_to=root.event_id),
            {"state_group_prefills": [(reply.event_id, context._state_group)]},
        )
        self.assertEqual(self._apply(effects), python)

    def test_state_change(self) -> None:
        topic, context = self._create(
            type=EventTypes.Topic,
            state_key="",
            sender=self.user,
            content={"topic": "new"},
        )
        python = self._persist((topic, context))

        effects = self._merge(
            self._event_effects(topic),
            self._state_effects(topic, context, members_changed=[]),
        )
        self.assertEqual(self._apply(effects), python)

    def test_remote_join_and_leave(self) -> None:
        join, context = self._create(
            type=EventTypes.Member,
            state_key=self.remote_user,
            sender=self.remote_user,
            content={"membership": Membership.JOIN},
        )
        python = self._persist((join, context))
        effects = self._merge(
            self._event_effects(join),
            self._state_effects(join, context, members_changed=[self.remote_user]),
        )
        self.assertEqual(self._apply(effects), python)

        # The last room shared with the remote user: its device list is no
        # longer tracked.
        leave, context = self._create(
            type=EventTypes.Member,
            state_key=self.remote_user,
            sender=self.remote_user,
            content={"membership": Membership.LEAVE},
        )
        python = self._persist((leave, context))
        effects = self._merge(
            self._event_effects(leave),
            self._state_effects(leave, context, members_changed=[self.remote_user]),
            {
                "cache_invalidations": [
                    ("get_device_list_last_stream_id_for_remote", [self.remote_user])
                ]
            },
        )
        self.assertEqual(self._apply(effects), python)

    def test_redaction_of_a_thread_reply(self) -> None:
        root, root_context = self._create(
            type=EventTypes.Message,
            sender=self.user,
            content={"body": "root", "msgtype": "m.text"},
        )
        self.get_success(self.persistence.persist_event(root, root_context))
        reply, reply_context = self._create(
            type=EventTypes.Message,
            sender=self.user,
            content={
                "body": "reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": root.event_id,
                },
            },
        )
        self.get_success(self.persistence.persist_event(reply, reply_context))

        redaction, context = self._create(
            type=EventTypes.Redaction,
            sender=self.user,
            content={"redacts": reply.event_id},
            redacts=reply.event_id,
        )
        python = self._persist((redaction, context))

        effects = self._merge(
            self._event_effects(redaction),
            {
                "state_group_prefills": [(redaction.event_id, context._state_group)],
                # `_handle_redact_relations`: the redacted event's relation
                # is gone.
                "cache_invalidations": [
                    ("get_relations_for_event", [self.room_id, root.event_id]),
                    ("get_thread_summary", [root.event_id]),
                    ("get_thread_participated", [root.event_id]),
                    ("get_threads", [self.room_id]),
                ],
                "new_replication_data": self.streams_invalidations,
            },
        )
        self.assertEqual(self._apply(effects), python)

    def test_stream_ids_finish_after_the_caches(self) -> None:
        """The current token must not pass an event before the stream change
        caches know about it, or a `/sync` in between would skip it."""
        id_gen = self.store._stream_id_gen
        allocation = id_gen.get_next()
        stream = self.get_success(allocation.__aenter__())
        self.assertLess(id_gen.get_current_token(), stream)
        self.assertFalse(
            self.store._events_stream_cache.has_entity_changed(self.room_id, stream - 1)
        )

        original_finish = id_gen._mark_ids_as_finished
        seen_by_cache: list[bool] = []

        def finish(ids: list[int]) -> None:
            seen_by_cache.append(
                self.store._events_stream_cache.has_entity_changed(
                    self.room_id, stream - 1
                )
            )
            original_finish(ids)

        with patch.object(id_gen, "_mark_ids_as_finished", finish):
            apply_rust_persist_effects(
                self.store,
                PersistEffects(
                    stream_changes=[("events", self.room_id, stream)],
                    stream_ids=[stream],
                ),
            )

        self.assertEqual(seen_by_cache, [True])
        self.assertEqual(id_gen.get_current_token(), stream)

    def test_cache_stream_ids_finish_after_the_caches(self) -> None:
        """As Python's `get_next_txn`: a cache stream ID is finished once the
        cache is invalidated locally."""
        id_gen = self.store._cache_id_gen
        if id_gen is None:
            raise SkipTest("the cache stream needs Postgres")
        allocation = id_gen.get_next()
        stream_id = self.get_success(allocation.__aenter__())
        self.assertLess(
            id_gen.get_current_token_for_writer(id_gen._instance_name), stream_id
        )

        calls: list[str] = []
        original_finish = id_gen._mark_ids_as_finished
        original_invalidate = self.store._attempt_to_invalidate_cache

        def finish(ids: list[int]) -> None:
            calls.append("finish")
            original_finish(ids)

        def invalidate(cache_name: str, key: Collection[Any] | None) -> bool:
            calls.append("invalidate")
            return original_invalidate(cache_name, key)

        notifier = self.hs.get_replication_notifier()
        original_notify = notifier.notify_replication

        def notify() -> None:
            calls.append("notify")
            original_notify()

        with (
            patch.object(id_gen, "_mark_ids_as_finished", finish),
            patch.object(self.store, "_attempt_to_invalidate_cache", invalidate),
            patch.object(notifier, "notify_replication", notify),
        ):
            apply_rust_persist_effects(
                self.store,
                PersistEffects(
                    cache_invalidations=[("get_threads", [self.room_id])],
                    cache_stream_ids=[stream_id],
                ),
            )

        self.assertEqual(calls, ["invalidate", "finish", "notify"])
        self.assertEqual(
            id_gen.get_current_token_for_writer(id_gen._instance_name), stream_id
        )

    def test_sticky_stream_ids_finish_after_the_caches(self) -> None:
        """As Python's `get_next_mult_txn`: the sticky events stream IDs are
        finished once the events whose soft failure was lifted are dropped
        from the event cache, then replication is told, and
        `stream_positions` moves on."""
        id_gen = self.store._sticky_events_id_gen
        allocation = id_gen.get_next()
        stream_id = self.get_success(allocation.__aenter__())
        self.assertLess(
            id_gen.get_current_token_for_writer(id_gen._instance_name), stream_id
        )

        calls: list[str] = []
        original_finish = id_gen._mark_ids_as_finished
        original_invalidate = self.store._invalidate_local_get_event_cache
        notifier = self.hs.get_replication_notifier()
        original_notify = notifier.notify_replication

        def finish(ids: list[int]) -> None:
            calls.append("finish")
            original_finish(ids)

        def invalidate(event_id: str) -> None:
            calls.append("invalidate")
            original_invalidate(event_id)

        def notify() -> None:
            calls.append("notify")
            original_notify()

        with (
            patch.object(id_gen, "_mark_ids_as_finished", finish),
            patch.object(self.store, "_invalidate_local_get_event_cache", invalidate),
            patch.object(notifier, "notify_replication", notify),
        ):
            apply_rust_persist_effects(
                self.store,
                PersistEffects(
                    event_cache_invalidations=["$sticky"],
                    sticky_stream_ids=[stream_id],
                ),
            )

        self.assertEqual(calls, ["invalidate", "finish", "notify"])
        self.assertEqual(
            id_gen.get_current_token_for_writer(id_gen._instance_name), stream_id
        )

        self.pump()
        positions = self.get_success(
            self.store.db_pool.simple_select_onecol(
                "stream_positions",
                {
                    "stream_name": "sticky_events",
                    "instance_name": id_gen._instance_name,
                },
                "stream_id",
            )
        )
        self.assertEqual(positions, [stream_id])


class RustPersistConfigTestCase(unittest.TestCase):
    def _parse(self, level: Any) -> ExperimentalConfig:
        config_dict = default_config(server_name="test")
        config_dict["experimental_features"] = {"rust_event_persistence": level}
        config = HomeServerConfig()
        config.parse_config_dict(config_dict, "", "")
        return config.experimental

    def test_levels(self) -> None:
        config = self._parse("prepare")
        self.assertEqual(config.rust_event_persistence, "prepare")
        self.assertTrue(config.rust_event_persistence_at_least("shadow"))
        self.assertTrue(config.rust_event_persistence_at_least("prepare"))
        self.assertFalse(config.rust_event_persistence_at_least("transaction"))

        # YAML reads an unquoted `off` as False.
        self.assertEqual(self._parse(False).rust_event_persistence, "off")

        # An unquoted `on` is True, which names no level.
        for bad in ("on", True, 1, None, "Shadow"):
            with self.subTest(level=bad), self.assertRaises(ConfigError):
                self._parse(bad)


class FlushRustPersistMetricsTestCase(unittest.TestCase):
    """`flush_rust_persist_metrics` adds what Rust counted to the metrics the
    Python path updates."""

    def _sample(self, name: str, **labels: str) -> float:
        value = REGISTRY.get_sample_value(
            name, {SERVER_NAME_LABEL: self.server_name, **labels}
        )
        return value or 0.0

    def test_counts_are_added(self) -> None:
        self.server_name = "flush.test"
        counts = {
            "persisted_events": 3,
            "persisted_events_by_type": {("m.room.message", "local"): 2},
            "state_delta": 1,
            "state_delta_single_event": 0,
            "state_delta_reuse_delta": 0,
            "state_resolutions": 4,
            "potential_times_prune_extremities": 0,
            "times_pruned_extremities": 0,
            "forward_extremities": {1: 2, 3: 1},
            "stale_forward_extremities": {},
            "state_dag_forward_extremities": {},
            "persisted_position": 42,
            "txn_timings": [("persist_events", 0.5)],
        }
        persister = Mock(spec=["take_metrics"])
        persister.take_metrics.return_value = counts
        flush_rust_persist_metrics(persister, self.server_name)

        self.assertEqual(
            self._sample("synapse_storage_events_persisted_events_total"), 3
        )
        self.assertEqual(
            self._sample(
                "synapse_storage_events_persisted_events_sep_total",
                type="m.room.message",
                origin_type="local",
            ),
            2,
        )
        self.assertEqual(self._sample("synapse_storage_events_state_delta_total"), 1)
        self.assertEqual(
            self._sample(
                "synapse_storage_events_state_resolutions_during_persistence_total"
            ),
            4,
        )
        self.assertEqual(
            self._sample("synapse_storage_events_forward_extremities_persisted_count"),
            3,
        )
        self.assertEqual(
            self._sample("synapse_storage_events_forward_extremities_persisted_sum"),
            5,
        )
        self.assertEqual(self._sample("synapse_event_persisted_position"), 42)
        # The interactions of failed persists.
        self.assertEqual(
            self._sample(
                "synapse_storage_transaction_time_count_total", desc="persist_events"
            ),
            1,
        )
        self.assertEqual(
            self._sample(
                "synapse_storage_transaction_time_sum_total", desc="persist_events"
            ),
            0.5,
        )

        # A position that did not move is not set again, so the Python path
        # can move the gauge on without being pulled back.
        synapse.metrics.event_persisted_position.labels(
            **{SERVER_NAME_LABEL: self.server_name}
        ).set(50)
        persister.take_metrics.return_value = {
            **counts,
            "persisted_position": None,
            "txn_timings": [],
        }
        flush_rust_persist_metrics(persister, self.server_name)
        self.assertEqual(self._sample("synapse_event_persisted_position"), 50)
        self.assertEqual(
            self._sample("synapse_storage_events_persisted_events_total"), 6
        )


class PreCollectHooksTestCase(unittest.TestCase):
    def test_several_hooks_per_homeserver(self) -> None:
        hooks = _PreCollectHooks()  # type: ignore[missing-server-name-label]
        ran: list[str] = []
        hooks.register_hook(homeserver_instance_id="hs", hook=lambda: ran.append("a"))
        hooks.register_hook(homeserver_instance_id="hs", hook=lambda: ran.append("b"))
        list(hooks.collect())
        self.assertEqual(ran, ["a", "b"])

        hooks.unregister_hooks_for_homeserver_instance_id("hs")
        list(hooks.collect())
        self.assertEqual(ran, ["a", "b"])


class PersisterTestCase(unittest.HomeserverTestCase):
    """`RustHandlers.persister` on the events writer, on the native driver."""

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_event_persistence"] = (
            "shadow"
        )
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        if RUST_PERSIST_FOR_TESTS == "off":
            raise SkipTest("SYNAPSE_TEST_RUST_PERSIST overrides the level")
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def test_persister_is_built(self) -> None:
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None
        self.assertIsNotNone(self.hs.get_rust_handlers().persister)
        self.assertIsNotNone(persistence._rust_persister)

    def test_metrics_flush(self) -> None:
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None
        # Nothing has been counted yet, so this checks the shape of what Rust
        # reports against what the flush reads.
        persistence._flush_rust_persist_metrics()

    def test_gil_stats_are_persist_sites(self) -> None:
        for site in persist_gil_stats():
            self.assertTrue(site.startswith("persist_"), site)


class _CountingPersister:
    """The persister, counting the chunks it prepares and writes, and keeping
    the effects of the last write."""

    def __init__(self, persister: Any) -> None:
        self._persister = persister
        self.prepares = 0
        self.transactions = 0
        # Chunks written with Rust taking their stream orderings.
        self.chunks = 0
        # Batches persisted in Rust.
        self.batches = 0
        self.last_effects: PersistEffects | None = None

    def prepare_chunk(self, *args: Any, **kwargs: Any) -> Any:
        self.prepares += 1
        return self._persister.prepare_chunk(*args, **kwargs)

    def persist_events_txn(self, *args: Any, **kwargs: Any) -> Any:
        self.transactions += 1

        def keep(result: Any) -> Any:
            self.last_effects = result
            return result

        return self._persister.persist_events_txn(*args, **kwargs).addCallback(keep)

    def persist_chunk(self, *args: Any, **kwargs: Any) -> Any:
        self.chunks += 1

        def keep(result: Any) -> Any:
            self.last_effects = result
            return result

        return self._persister.persist_chunk(*args, **kwargs).addCallback(keep)

    def persist_batch(self, *args: Any, **kwargs: Any) -> Any:
        self.batches += 1
        return self._persister.persist_batch(*args, **kwargs)

    def batch_next(self, *args: Any, **kwargs: Any) -> Any:
        def keep(step: Any) -> Any:
            if isinstance(step, WrittenChunk):
                self.chunks += 1
                self.last_effects = step.effects
            return step

        return self._persister.batch_next(*args, **kwargs).addCallback(keep)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._persister, name)


def _txn_count(desc: str) -> float:
    """`synapse_storage_transaction_time_count` for `desc`."""
    return (
        REGISTRY.get_sample_value(
            "synapse_storage_transaction_time_count_total",
            {"desc": desc, SERVER_NAME_LABEL: "test"},
        )
        or 0.0
    )


def _txn_secs(desc: str) -> float:
    """`synapse_storage_transaction_time_sum` for `desc`."""
    return (
        REGISTRY.get_sample_value(
            "synapse_storage_transaction_time_sum_total",
            {"desc": desc, SERVER_NAME_LABEL: "test"},
        )
        or 0.0
    )


class RustTransactionTestCase(unittest.HomeserverTestCase):
    """`rust_event_persistence: transaction`: Rust writes the chunks it
    prepared."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_event_persistence"] = (
            "transaction"
        )
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        if RUST_PERSIST_FOR_TESTS not in (None, "transaction"):
            raise SkipTest("SYNAPSE_TEST_RUST_PERSIST overrides the level")
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persister = _CountingPersister(persistence._rust_persister)
        persistence._rust_persister = self.persister  # type: ignore[assignment]
        self.user = self.register_user("alice", "pass")
        self.token = self.login("alice", "pass")

    def test_rust_writes_the_chunk(self) -> None:
        """The event is written by Rust, not Python, and the applier puts it
        in the event cache, as `_add_to_cache` does."""
        room_id = self.helper.create_room_as(self.user, tok=self.token)
        before = self.persister.transactions
        with (
            patch.object(
                PersistEventsStore,
                "_persist_events_txn",
                side_effect=AssertionError("Python wrote the chunk"),
            ),
            patch.object(
                self.store,
                "_get_event_cache",
                Mock(wraps=self.store._get_event_cache),
            ) as event_cache,
        ):
            event_id = self.helper.send(room_id, "hello", tok=self.token)["event_id"]
        self.assertEqual(self.persister.transactions, before + 1)
        assert self.persister.last_effects is not None
        self.assertIn(event_id, self.persister.last_effects.event_cache_prefills)
        self.assertIn(
            (event_id,), [c.args[0] for c in event_cache.set_local.call_args_list]
        )
        event = self.get_success(self.store.get_event(event_id))
        self.assertEqual(event.content["body"], "hello")

    def test_lowered_level_writes_in_python(self) -> None:
        room_id = self.helper.create_room_as(self.user, tok=self.token)
        self.hs.config.experimental.rust_event_persistence = "prepare"
        prepares, transactions = self.persister.prepares, self.persister.transactions
        self.helper.send(room_id, "hello", tok=self.token)
        # Rust prepared the chunk, so it is the level that kept Rust out.
        self.assertEqual(self.persister.prepares, prepares + 1)
        self.assertEqual(self.persister.transactions, transactions)

    @unittest.override_config({"include_profile_updates_in_sync": True})
    def test_profile_updates_write_in_rust(self) -> None:
        """A server that records profile updates writes in Rust: the rows
        are written, and the profile updates stream IDs the transaction took
        are finished."""
        bob = self.register_user("bob", "pass")
        bob_token = self.login("bob", "pass")
        room_id = self.helper.create_room_as(self.user, tok=self.token)
        self.helper.invite(room_id, self.user, bob, tok=self.token)
        transactions = self.persister.transactions
        self.helper.join(room_id, bob, tok=bob_token)
        self.assertEqual(self.persister.transactions, transactions + 1)
        effects = self.persister.last_effects
        assert effects is not None
        self.assertNotEqual(effects.profile_stream_ids, [])
        rows = self.get_success(
            self.store.db_pool.execute(
                "test",
                "SELECT COUNT(*) FROM profile_updates WHERE stream_id = ANY(?)",
                effects.profile_stream_ids,
            )
        )
        self.assertEqual(rows[0][0], len(effects.profile_stream_ids))
        self.assertEqual(
            self.store._profile_updates_id_gen.get_current_token(),
            max(effects.profile_stream_ids),
        )

    def test_txn_metrics(self) -> None:
        """The Rust transaction is counted as Python's would be."""
        room_id = self.helper.create_room_as(self.user, tok=self.token)
        before = _txn_count("persist_events")
        self.helper.send(room_id, "hello", tok=self.token)
        self.assertEqual(_txn_count("persist_events"), before + 1)

    def test_gil_count(self) -> None:
        """Writing a chunk takes the GIL from tokio at no persist site: the
        events cross at the call and the effects come back in its result."""
        room_id = self.helper.create_room_as(self.user, tok=self.token)
        # The first persist opens the pool, at `persist_db_pool`.
        self.helper.send(room_id, "warm up", tok=self.token)
        before = persist_gil_stats()
        transactions = self.persister.transactions
        self.helper.send(room_id, "hello", tok=self.token)
        self.assertEqual(self.persister.transactions, transactions + 1)
        after = persist_gil_stats()
        changed = {
            site: after[site][0] - before.get(site, (0, 0.0))[0]
            for site in after
            if after[site][0] != before.get(site, (0, 0.0))[0]
        }
        self.assertEqual(changed, {})

    def test_cancelled_persist_applies_its_effects(self) -> None:
        """Cancelling the persist while Rust writes the chunk waits for the
        write, and the effects are applied, so the cache stream IDs the
        transaction took are finished and the event cache is filled."""
        room_id = self.helper.create_room_as(self.user, tok=self.token)

        # Hold the Rust call's Deferred until the persist has been cancelled.
        gate: list[tuple[Any, Any, defer.Deferred]] = []
        blocking = self.persister._persister

        class Gated:
            def persist_events_txn(self, *args: Any, **kwargs: Any) -> Any:
                d: defer.Deferred = defer.Deferred()
                gate.append((args, kwargs, d))
                # As the persister's Deferreds do.
                return make_deferred_yieldable(d)

            def __getattr__(self, name: str) -> Any:
                return getattr(blocking, name)

        self.persister._persister = Gated()

        persist_events = self.hs.get_datastores().persist_events
        assert persist_events is not None
        original = persist_events._persist_events_in_rust
        cancelled = []

        async def cancel_then_wait(*args: Any, **kwargs: Any) -> None:
            d = defer.ensureDeferred(original(*args, **kwargs))
            d.cancel()
            try:
                await make_deferred_yieldable(d)
            except defer.CancelledError:
                cancelled.append(True)

        with patch.object(
            persist_events, "_persist_events_in_rust", side_effect=cancel_then_wait
        ):
            # A state change, so the transaction takes cache stream IDs.
            channel = make_request(
                self.reactor,
                self.site,
                "PUT",
                f"/rooms/{room_id}/state/m.room.topic/",
                {"topic": "hello"},
                access_token=self.token,
                await_result=False,
            )
            self.pump()
            self.assertEqual(len(gate), 1)
            # Cancelled, but waiting for the transaction.
            self.assertEqual(cancelled, [])
            args, kwargs, d = gate[0]
            blocking.persist_events_txn(*args, **kwargs).chainDeferred(d)
            channel.await_result()
        self.assertEqual(channel.code, 200, channel.json_body)
        event_id = channel.json_body["event_id"]
        self.assertEqual(cancelled, [True])

        effects = self.persister.last_effects
        assert effects is not None
        self.assertIn(event_id, effects.event_cache_prefills)
        self.assertIsNotNone(self.store._get_event_cache.get_local((event_id,)))
        assert self.store._cache_id_gen is not None
        self.assertNotEqual(effects.cache_stream_ids, [])
        self.assertGreaterEqual(
            self.store._cache_id_gen.get_current_token(),
            max(effects.cache_stream_ids),
        )


class RustFullTestCase(unittest.HomeserverTestCase):
    """`rust_event_persistence: full`: Rust also takes the chunks' stream
    orderings."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_event_persistence"] = (
            "full"
        )
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        if RUST_PERSIST_FOR_TESTS not in (None, "full"):
            raise SkipTest("SYNAPSE_TEST_RUST_PERSIST overrides the level")
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence
        self.persister = _CountingPersister(persistence._rust_persister)
        persistence._rust_persister = self.persister  # type: ignore[assignment]
        self.user = self.register_user("alice", "pass")
        self.token = self.login("alice", "pass")
        self.room_id = self.helper.create_room_as(self.user, tok=self.token)

    def _build(
        self, body: str, *, txn_id: str | None = None
    ) -> tuple[EventBase, EventContext]:
        """A message, sent with `txn_id` from `DEVICE` if given."""
        event, unpersisted_context = self.get_success(
            self.hs.get_event_creation_handler().create_event(
                create_requester(self.user, device_id="DEVICE" if txn_id else None),
                {
                    "type": EventTypes.Message,
                    "content": {"msgtype": "m.text", "body": body},
                    "room_id": self.room_id,
                    "sender": self.user,
                },
                txn_id=txn_id,
            )
        )
        context = self.get_success(unpersisted_context.persist(event))
        return event, context

    def _stream_position(self, stream_name: str) -> int | None:
        rows = self.get_success(
            self.store.db_pool.simple_select_onecol(
                "stream_positions",
                {"stream_name": stream_name, "instance_name": "master"},
                "stream_id",
            )
        )
        return rows[0] if rows else None

    def _event_row(self, event_id: str) -> tuple[int, str, int]:
        """`(stream_ordering, instance_name, received_ts)` of the event's
        `events` row."""
        row = self.get_success(
            self.store.db_pool.simple_select_one(
                "events",
                {"event_id": event_id},
                ("stream_ordering", "instance_name", "received_ts"),
            )
        )
        return row[0], row[1], row[2]

    def test_rust_takes_stream_orderings(self) -> None:
        """Rust takes the chunk's stream orderings and sets them on the
        events, and the applier finishes them and moves `stream_positions`
        on, as `get_next_mult` does."""
        id_gen = self.store._stream_id_gen
        chunks = self.persister.chunks
        with patch.object(
            id_gen, "get_next_mult", side_effect=AssertionError("Python allocated")
        ):
            event_id = self.helper.send(self.room_id, "hello", tok=self.token)[
                "event_id"
            ]
        self.assertEqual(self.persister.chunks, chunks + 1)
        effects = self.persister.last_effects
        assert effects is not None
        self.assertFalse(effects.backfilled)
        (stream_ordering,) = effects.stream_ids

        self.assertEqual(
            self._event_row(event_id),
            (stream_ordering, "master", self.clock.time_msec()),
        )
        self.assertGreaterEqual(id_gen.get_current_token(), stream_ordering)
        assert id_gen._writers
        self.assertEqual(self._stream_position("events"), stream_ordering)

    def test_chunk_with_duplicate(self) -> None:
        """A chunk takes one ordering per entry, in order. An event that
        appears twice keeps the later one, as in Python, and every ordering
        is finished. What follows the write sees the last ordering."""
        first = self._build("one")
        second = self._build("two")
        with patch.object(
            self.store,
            "get_latest_event_ids_in_room",
            Mock(wraps=self.store.get_latest_event_ids_in_room),
        ) as latest:
            self.get_success(self.persistence.persist_events([first, second, first]))
        effects = self.persister.last_effects
        assert effects is not None
        self.assertEqual(len(effects.stream_ids), 3)
        self.assertEqual(effects.stream_ids, sorted(effects.stream_ids))
        _, second_id, last_id = effects.stream_ids

        self.assertEqual(self._event_row(first[0].event_id)[0], last_id)
        self.assertEqual(self._event_row(second[0].event_id)[0], second_id)
        self.assertGreaterEqual(self.store._stream_id_gen.get_current_token(), last_id)
        # `_after_persisting_events` ran with the chunk.
        self.assertEqual(
            REGISTRY.get_sample_value(
                "synapse_event_persisted_position", {SERVER_NAME_LABEL: "test"}
            ),
            last_id,
        )
        # Both were built on the same extremity.
        latest.prefill.assert_called_with(
            (self.room_id,), frozenset([first[0].event_id, second[0].event_id])
        )

    def _join_bob(self) -> str:
        bob = self.register_user("bob", "pass")
        bob_token = self.login("bob", "pass")
        self.helper.join(self.room_id, bob, tok=bob_token)
        return bob

    def _redaction_rows(self) -> list[tuple[str, str]]:
        return self.get_success(
            self.store.db_pool.simple_select_list(
                "room_ban_redactions", None, ("user_id", "redacting_event_id")
            )
        )

    def test_follow_ups_run_before_orderings_finish(self) -> None:
        """What follows the write, the MSC4293 redactions among it, runs in
        Rust before Python applies the chunk and the token passes it, as
        inside `get_next_mult`."""
        self.hs.config.experimental.msc4293_enabled = True
        bob = self._join_bob()
        id_gen = self.store._stream_id_gen
        seen: list[tuple[int, list[int], list[tuple[str, str]]]] = []

        def applying(
            store: Any, effects: PersistEffects, *args: Any, **kwargs: Any
        ) -> None:
            rows = self.get_success(
                self.store.db_pool.simple_select_list(
                    "room_ban_redactions", None, ("user_id", "redacting_event_id")
                )
            )
            seen.append((id_gen.get_current_token(), effects.stream_ids, rows))
            apply_rust_persist_effects(store, effects, *args, **kwargs)

        with patch(
            "synapse.storage.controllers.persist_events.apply_rust_persist_effects",
            side_effect=applying,
        ):
            ban_id = self.helper.change_membership(
                self.room_id,
                self.user,
                bob,
                "ban",
                {"org.matrix.msc4293.redact_events": True},
                tok=self.token,
            )["event_id"]
        ((token, (stream_ordering,), rows),) = seen
        self.assertLess(token, stream_ordering)
        self.assertEqual(rows, [(bob, ban_id)])
        self.assertGreaterEqual(id_gen.get_current_token(), stream_ordering)

    def test_follow_up_failure(self) -> None:
        """If what follows the write fails, the chunk is written and
        applied, its orderings finished and its state groups released, and
        the persist fails, as Python's would."""
        self.hs.config.experimental.msc4293_enabled = True
        bob = self._join_bob()
        # Python indexes the current power levels, so fails without them.
        self.get_success(
            self.store.db_pool.simple_delete(
                "current_state_events",
                {"room_id": self.room_id, "type": EventTypes.PowerLevels},
                desc="test",
            )
        )
        event, context = self._build("hello")
        self.get_success(self.persistence.persist_events([(event, context)]))
        ban, ban_context = self.get_success(
            self.hs.get_event_creation_handler().create_event(
                create_requester(self.user),
                {
                    "type": EventTypes.Member,
                    "state_key": bob,
                    "content": {
                        "membership": "ban",
                        "org.matrix.msc4293.redact_events": True,
                    },
                    "room_id": self.room_id,
                    "sender": self.user,
                },
            )
        )
        context = self.get_success(ban_context.persist(ban))
        failure = self.get_failure(
            self.persistence.persist_events([(ban, context)]), RuntimeError
        )
        self.assertIn("power_levels", str(failure.value))
        stream_ordering = self._event_row(ban.event_id)[0]
        self.assertGreaterEqual(
            self.store._stream_id_gen.get_current_token(), stream_ordering
        )
        self.assertEqual(self._redaction_rows(), [])
        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                "state_groups_persisting", None, ("state_group",)
            )
        )
        self.assertEqual(rows, [])

    def test_batch_is_one_rust_call(self) -> None:
        """A batch of 150 events is one Rust call, written in two chunks,
        with none of it in Python's store entry point."""
        # Siblings on the room's extremity: an event cannot be built on
        # one not yet persisted.
        pairs = [self._build(f"message {i}") for i in range(150)]
        batches, chunks = self.persister.batches, self.persister.chunks
        with patch.object(
            PersistEventsStore,
            "_persist_events_and_state_updates",
            side_effect=AssertionError("persisted in Python"),
        ):
            self.get_success(self.persistence.persist_events(pairs))
        self.assertEqual(self.persister.batches, batches + 1)
        self.assertEqual(self.persister.chunks, chunks + 2)
        effects = self.persister.last_effects
        assert effects is not None
        self.assertEqual(len(effects.stream_ids), 50)

        orderings = [self._event_row(event.event_id)[0] for event, _ in pairs]
        self.assertEqual(orderings, sorted(orderings))
        self.assertGreaterEqual(
            self.store._stream_id_gen.get_current_token(), orderings[-1]
        )

    def test_transaction_ids(self) -> None:
        """An event repeating the transaction ID of an earlier one, in the
        same batch or an earlier one, is not persisted and maps to it."""
        self.login("alice", "pass", device_id="DEVICE")
        first = self._build("one", txn_id="txn")
        again = self._build("one again", txn_id="txn")
        persisted, _ = self.get_success(self.persistence.persist_events([first, again]))
        self.assertEqual(
            [event.event_id for event in persisted], [first[0].event_id] * 2
        )

        later = self._build("one later", txn_id="txn")
        persisted, _ = self.get_success(self.persistence.persist_events([later]))
        self.assertEqual([event.event_id for event in persisted], [first[0].event_id])
        for event, _ in (again, later):
            self.assertIsNone(
                self.get_success(self.store.get_event(event.event_id, allow_none=True))
            )

    def test_state_groups_held_while_writing(self) -> None:
        """The batch's state groups are marked as being persisted while each
        chunk is written, and no longer once it is."""
        held: list[list[tuple[int, str]]] = []
        apply_chunk = self.persistence._apply_rust_batch_chunk

        async def checking(*args: Any) -> None:
            held.append(
                await self.store.db_pool.simple_select_list(
                    "state_groups_persisting",
                    None,
                    ("state_group", "instance_name"),
                )
            )
            await apply_chunk(*args)

        event, context = self._build("hello")
        with patch.object(
            self.persistence, "_apply_rust_batch_chunk", side_effect=checking
        ):
            self.get_success(self.persistence.persist_events([(event, context)]))
        assert context.state_group is not None
        assert context.state_group_before_event is not None
        groups = {context.state_group, context.state_group_before_event}
        self.assertEqual(
            [sorted(rows) for rows in held],
            [sorted((group, "master") for group in groups)],
        )
        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                "state_groups_persisting", None, ("state_group",)
            )
        )
        self.assertEqual(rows, [])

    def test_deleted_state_group_fails(self) -> None:
        """A chunk whose state group was deleted meanwhile is not written."""
        event, context = self._build("hello")
        self.get_success(
            self.store.db_pool.simple_delete_one(
                "state_groups", {"id": context.state_group}
            )
        )
        failure = self.get_failure(
            self.persistence.persist_events([(event, context)]), RuntimeError
        )
        self.assertIn("state groups have been deleted", str(failure.value))
        self.assertIsNone(
            self.get_success(self.store.get_event(event.event_id, allow_none=True))
        )

    def _pending_deletion(self, state_group: int) -> tuple[int, int] | None:
        """`(sequence_number, insertion_ts)` of the group's pending deletion."""
        row = self.get_success(
            self.store.db_pool.simple_select_one(
                "state_groups_pending_deletion",
                {"state_group": state_group},
                ("sequence_number", "insertion_ts"),
                allow_none=True,
            )
        )
        return (row[0], row[1]) if row is not None else None

    def test_pending_deletion_after_persisting(self) -> None:
        """Once a chunk is written, its state groups are no longer pending
        deletion. If the write fails, their pending deletion is pushed
        back, as they may now be referenced."""
        event, context = self._build("hello")
        assert context.state_group is not None
        self.get_success(
            self.store.db_pool.simple_insert(
                "state_groups_pending_deletion",
                {"state_group": context.state_group, "insertion_ts": 0},
            )
        )
        before = self._pending_deletion(context.state_group)
        assert before is not None

        failing, failing_context = self._build("partial")
        failing_context.partial_state = True
        self.reactor.advance(1)
        self.get_failure(
            self.persistence.persist_events([(failing, failing_context)]),
            PartialStateConflictError,
        )
        after = self._pending_deletion(context.state_group)
        assert after is not None
        self.assertGreater(after[0], before[0])
        self.assertEqual(after[1], self.clock.time_msec())

        self.get_success(self.persistence.persist_events([(event, context)]))
        self.assertIsNone(self._pending_deletion(context.state_group))

    def test_replaced_event_and_chunks(self) -> None:
        """A batch whose first event repeats a persisted transaction ID, and
        whose other 150 go in two chunks: each chunk hands Python the events
        it wrote."""
        self.login("alice", "pass", device_id="DEVICE")
        original = self._build("original", txn_id="txn")
        self.get_success(self.persistence.persist_events([original]))

        repeat = self._build("repeat", txn_id="txn")
        new = [self._build(f"message {i}") for i in range(150)]
        chunks = self.persister.chunks
        persisted, _ = self.get_success(self.persistence.persist_events([repeat, *new]))
        self.assertEqual(self.persister.chunks, chunks + 2)
        effects = self.persister.last_effects
        assert effects is not None
        self.assertEqual(len(effects.stream_ids), 50)
        self.assertEqual(
            [event.event_id for event in persisted],
            [original[0].event_id] + [event.event_id for event, _ in new],
        )
        self.assertIsNone(
            self.get_success(self.store.get_event(repeat[0].event_id, allow_none=True))
        )
        orderings = [self._event_row(event.event_id)[0] for event, _ in new]
        self.assertEqual(orderings, sorted(orderings))
        for i, (event, _) in enumerate(new):
            stored = self.get_success(self.store.get_event(event.event_id))
            self.assertEqual(stored.content["body"], f"message {i}")

    def test_same_event_in_two_chunks(self) -> None:
        """An event in two chunks of a batch is written once, and every
        ordering the batch took is finished."""
        first = self._build("first")
        rest = [self._build(f"message {i}") for i in range(100)]
        self.get_success(self.persistence.persist_events([first, *rest, first]))
        rows = self.get_success(
            self.store.db_pool.simple_select_onecol(
                "events", {"event_id": first[0].event_id}, "stream_ordering"
            )
        )
        self.assertEqual(len(rows), 1)
        effects = self.persister.last_effects
        assert effects is not None
        self.assertEqual(len(effects.stream_ids), 2)
        self.assertGreaterEqual(
            self.store._stream_id_gen.get_current_token(), max(effects.stream_ids)
        )

    def test_logcontext_kept(self) -> None:
        """Awaiting the batch resumes in the caller's logcontext, so what
        follows it is accounted to the caller."""
        event, context = self._build("hello")
        seen = []

        async def persist() -> None:
            with LoggingContext(name="persist", server_name="test") as ctx:
                await self.persistence._persist_event_batch_in_rust(
                    self.room_id, [(event, context)], False
                )
                seen.append(current_context() is ctx)

        self.get_success(defer.ensureDeferred(persist()))
        self.assertEqual(seen, [True])

    def _current_topic(self) -> list[str]:
        return self.get_success(
            self.store.db_pool.simple_select_onecol(
                "current_state_events",
                {"room_id": self.room_id, "type": EventTypes.Topic},
                "event_id",
            )
        )

    def test_update_current_state(self) -> None:
        """Rust recalculates the current state from the extremities, writes
        the change with a stream ID it takes, and Python applies it."""
        topic = self.helper.send_state(
            self.room_id, EventTypes.Topic, {"topic": "hi"}, tok=self.token
        )["event_id"]
        self.get_success(
            self.store.db_pool.simple_delete(
                "current_state_events",
                {"room_id": self.room_id, "type": EventTypes.Topic},
                desc="test",
            )
        )
        before = self.store._stream_id_gen.get_current_token()
        with patch.object(
            self.persistence,
            "_calculate_current_state",
            side_effect=AssertionError("calculated in Python"),
        ):
            self.get_success(self.persistence.update_current_state(self.room_id))
        self.assertEqual(self._current_topic(), [topic])
        after = self.store._stream_id_gen.get_current_token()
        self.assertEqual(after, before + 1)
        # The delta row is at the stream ID taken.
        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                "current_state_delta_stream",
                {"room_id": self.room_id, "stream_id": after},
                ("type", "event_id", "prev_event_id"),
            )
        )
        # No previous event, as its row was gone.
        self.assertEqual(rows, [(EventTypes.Topic, topic, None)])

    def test_update_current_state_unchanged(self) -> None:
        """A room whose current state is right takes no stream ID."""
        before = self.store._stream_id_gen.get_current_token()
        with patch.object(
            self.persistence,
            "_calculate_current_state",
            side_effect=AssertionError("calculated in Python"),
        ):
            self.get_success(self.persistence.update_current_state(self.room_id))
        self.assertEqual(self.store._stream_id_gen.get_current_token(), before)

    def test_update_current_state_falls_back(self) -> None:
        """A room on state resolution v1 whose extremities need resolving
        is recalculated in Python."""
        self.room_id = self.helper.create_room_as(
            self.user, room_version=RoomVersions.V1.identifier, tok=self.token
        )
        # Two topics on the same extremity, so the room forks.
        pairs = []
        for topic in ("A", "B"):
            event, unpersisted = self.get_success(
                self.hs.get_event_creation_handler().create_event(
                    create_requester(self.user),
                    {
                        "type": EventTypes.Topic,
                        "state_key": "",
                        "content": {"topic": topic},
                        "room_id": self.room_id,
                        "sender": self.user,
                    },
                )
            )
            pairs.append((event, self.get_success(unpersisted.persist(event))))
        for pair in pairs:
            self.get_success(self.persistence.persist_events([pair]))
        calculate = self.persistence._calculate_current_state
        id_gen = self.store._stream_id_gen
        before = id_gen.get_current_token()
        tokens = []

        async def in_python(room_id: str) -> Any:
            # Rust took no stream ID before handing back.
            tokens.append(id_gen.get_current_token())
            return await calculate(room_id)

        with patch.object(
            self.persistence, "_calculate_current_state", side_effect=in_python
        ):
            self.get_success(self.persistence.update_current_state(self.room_id))
        self.assertEqual(tokens, [before])

    def _fork_topics(self) -> None:
        """Two topics on the same extremity, persisted one after the other,
        so the room has two extremities in two state groups."""
        pairs = []
        for topic in ("A", "B"):
            event, unpersisted = self.get_success(
                self.hs.get_event_creation_handler().create_event(
                    create_requester(self.user),
                    {
                        "type": EventTypes.Topic,
                        "state_key": "",
                        "content": {"topic": topic},
                        "room_id": self.room_id,
                        "sender": self.user,
                    },
                )
            )
            pairs.append((event, self.get_success(unpersisted.persist(event))))
        for pair in pairs:
            self.get_success(self.persistence.persist_events([pair]))

    def _delete_current_state(self, event_type: str, state_key: str) -> None:
        self.get_success(
            self.store.db_pool.simple_delete(
                "current_state_events",
                {"room_id": self.room_id, "type": event_type, "state_key": state_key},
                desc="test",
            )
        )

    def test_update_current_state_resolves(self) -> None:
        """Extremities in several state groups are resolved in Rust, to the
        state Python resolves to, and the resolution is recorded."""
        self._fork_topics()
        expected = self.get_success(
            self.persistence._calculate_current_state(self.room_id)
        )
        self._delete_current_state(EventTypes.Topic, "")
        handler = self.hs.get_state_resolution_handler()
        handler._state_res_metrics.clear()
        with patch.object(
            self.persistence,
            "_calculate_current_state",
            side_effect=AssertionError("calculated in Python"),
        ):
            self.get_success(self.persistence.update_current_state(self.room_id))
        self.assertEqual(self._current_topic(), [expected[(EventTypes.Topic, "")]])
        self.assertIn(self.room_id, handler._state_res_metrics)

    def test_update_current_state_membership(self) -> None:
        """A membership the update restores moves the membership tables on
        and streams the state caches' invalidation."""
        bob = self.register_user("bob", "pass")
        bob_token = self.login("bob", "pass")
        self.helper.join(self.room_id, bob, tok=bob_token)
        self._delete_current_state(EventTypes.Member, bob)
        self.get_success(
            self.store.db_pool.simple_delete(
                "sliding_sync_membership_snapshots",
                {"room_id": self.room_id, "user_id": bob},
                desc="test",
            )
        )
        cache_position = self.store._cache_id_gen.get_current_token()  # type: ignore[union-attr]
        self.get_success(self.persistence.update_current_state(self.room_id))

        (membership,) = self.get_success(
            self.store.db_pool.simple_select_onecol(
                "current_state_events",
                {"room_id": self.room_id, "type": EventTypes.Member, "state_key": bob},
                "membership",
            )
        )
        self.assertEqual(membership, "join")
        (snapshot,) = self.get_success(
            self.store.db_pool.simple_select_onecol(
                "sliding_sync_membership_snapshots",
                {"room_id": self.room_id, "user_id": bob},
                "membership",
            )
        )
        self.assertEqual(snapshot, "join")
        cache_rows = self.get_success(
            self.store.db_pool.execute(
                "test",
                "SELECT cache_func FROM cache_invalidation_stream_by_instance"
                " WHERE stream_id > ?",
                cache_position,
            )
        )
        self.assertIn(("cs_cache_fake",), cache_rows)

    def test_update_current_state_apply_failure(self) -> None:
        """If applying the update's effects fails, its stream ID is still
        finished."""

        class ApplyFailed(Exception):
            pass

        self._delete_current_state(EventTypes.Topic, "")
        self.helper.send_state(
            self.room_id, EventTypes.Topic, {"topic": "hi"}, tok=self.token
        )
        self._delete_current_state(EventTypes.Topic, "")
        with patch(
            "synapse.storage.controllers.persist_events.apply_rust_persist_effects",
            side_effect=ApplyFailed(),
        ):
            self.get_failure(
                self.persistence.update_current_state(self.room_id), ApplyFailed
            )
        (stream_id,) = self.get_success(
            self.store.db_pool.execute(
                "test",
                "SELECT MAX(stream_id) FROM current_state_delta_stream"
                " WHERE room_id = ?",
                self.room_id,
            )
        )[0]
        self.assertGreaterEqual(
            self.store._stream_id_gen.get_current_token(), stream_id
        )

    def test_txn_metrics(self) -> None:
        """Each Rust interaction of a batch is counted as Python's would
        be: the orderings, the state group guard and the write."""
        names = (
            "_load_next_mult_id",
            "mark_state_groups_as_persisting",
            "persist_events",
            "finish_persisting",
        )
        before = {name: _txn_count(name) for name in names}
        secs = _txn_secs("persist_events")
        self.helper.send(self.room_id, "hello", tok=self.token)
        self.assertEqual(
            {name: _txn_count(name) - before[name] for name in names},
            dict.fromkeys(names, 1.0),
        )
        self.assertGreater(_txn_secs("persist_events"), secs)

        # A batch of two chunks: two writes, and so on.
        before = {name: _txn_count(name) for name in names}
        pairs = [self._build(f"message {i}") for i in range(150)]
        self.get_success(self.persistence.persist_events(pairs))
        self.assertEqual(
            {name: _txn_count(name) - before[name] for name in names},
            dict.fromkeys(names, 2.0),
        )

        # The update of a room's current state.
        before_update = _txn_count("update_current_state")
        self.get_success(
            self.store.db_pool.simple_delete(
                "current_state_events",
                {"room_id": self.room_id, "type": EventTypes.Name},
                desc="test",
            )
        )
        self.helper.send_state(
            self.room_id, EventTypes.Name, {"name": "n"}, tok=self.token
        )
        self.get_success(
            self.store.db_pool.simple_delete(
                "current_state_events",
                {"room_id": self.room_id, "type": EventTypes.Name},
                desc="test",
            )
        )
        self.get_success(self.persistence.update_current_state(self.room_id))
        self.assertEqual(_txn_count("update_current_state"), before_update + 1)

    def test_failed_txn_metrics(self) -> None:
        """The interactions of a persist that fails are counted when the
        metrics are next collected."""
        event, context = self._build("partial")
        context.partial_state = True
        before = _txn_count("persist_events")
        self.get_failure(
            self.persistence.persist_events([(event, context)]),
            PartialStateConflictError,
        )
        self.persistence._flush_rust_persist_metrics()
        self.assertEqual(_txn_count("persist_events"), before + 1)

    def test_gil_count(self) -> None:
        """Taking the stream orderings and setting them on the events takes
        the GIL at no persist site."""
        # The first persist opens the pool, at `persist_db_pool`.
        self.helper.send(self.room_id, "warm up", tok=self.token)
        before = persist_gil_stats()
        chunks = self.persister.chunks
        self.helper.send(self.room_id, "hello", tok=self.token)
        self.assertEqual(self.persister.chunks, chunks + 1)
        after = persist_gil_stats()
        changed = {
            site: after[site][0] - before.get(site, (0, 0.0))[0]
            for site in after
            if after[site][0] != before.get(site, (0, 0.0))[0]
        }
        self.assertEqual(changed, {})

    def test_backfill_takes_negative_orderings(self) -> None:
        """A backfilled chunk takes its stream orderings from the backfill
        stream, negated on the events."""
        id_gen = self.store._backfill_id_gen
        event, context = self._build("old")
        with patch.object(
            id_gen, "get_next_mult", side_effect=AssertionError("Python allocated")
        ):
            self.get_success(
                self.persistence.persist_events([(event, context)], backfilled=True)
            )
        effects = self.persister.last_effects
        assert effects is not None
        self.assertTrue(effects.backfilled)
        (backfill_id,) = effects.stream_ids
        self.assertGreater(backfill_id, 0)
        self.assertEqual(event.internal_metadata.stream_ordering, -backfill_id)
        self.assertEqual(
            self._event_row(event.event_id),
            (-backfill_id, "master", self.clock.time_msec()),
        )
        # The backfill token is negated too.
        self.assertLessEqual(id_gen.get_current_token(), -backfill_id)
        assert id_gen._writers
        self.assertEqual(self._stream_position("backfill"), -backfill_id)

    def test_failed_persist_finishes_its_orderings(self) -> None:
        """A chunk whose transaction fails leaves no stream ordering
        unfinished, so the stream does not stall, and replication is told."""
        event, context = self._build("partial")
        context.partial_state = True
        notifier = self.hs.get_replication_notifier()
        with patch.object(
            notifier, "notify_replication", wraps=notifier.notify_replication
        ) as notify:
            self.get_failure(
                self.persistence.persist_events([(event, context)]),
                PartialStateConflictError,
            )
        notify.assert_called()
        failed = event.internal_metadata.stream_ordering
        assert failed is not None

        event_id = self.helper.send(self.room_id, "after", tok=self.token)["event_id"]
        position = self.get_success(self.store.get_position_for_event(event_id))
        self.assertGreater(position.stream, failed)
        # A leaked ordering would hold the token below it.
        self.assertGreaterEqual(
            self.store._stream_id_gen.get_current_token(), position.stream
        )

    def test_applier_failure_finishes_orderings(self) -> None:
        """If applying the effects raises before the stream orderings are
        finished, they are still finished, and replication is told."""

        class ApplyFailed(Exception):
            pass

        event, context = self._build("hello")
        notifier = self.hs.get_replication_notifier()
        with (
            patch.object(
                self.store._events_stream_cache,
                "entity_has_changed",
                side_effect=ApplyFailed(),
            ),
            patch.object(
                notifier, "notify_replication", wraps=notifier.notify_replication
            ) as notify,
        ):
            self.get_failure(
                self.persistence.persist_events([(event, context)]), ApplyFailed
            )
        notify.assert_called()
        effects = self.persister.last_effects
        assert effects is not None
        (stream_ordering,) = effects.stream_ids
        self.assertGreaterEqual(
            self.store._stream_id_gen.get_current_token(), stream_ordering
        )
        # `batch_abort` released the state groups.
        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                "state_groups_persisting", None, ("state_group",)
            )
        )
        self.assertEqual(rows, [])

    def test_cancelled_persist_applies_its_effects(self) -> None:
        """Cancelling the batch while Rust writes a chunk waits for Rust,
        so the room's queue cannot start another batch alongside, and the
        chunk's effects are applied and its orderings finished."""
        gate: list[tuple[Any, Any, defer.Deferred]] = []
        blocking = self.persister._persister

        class Gated:
            def batch_next(self, *args: Any, **kwargs: Any) -> Any:
                # Only the first step, which writes the chunk.
                if gate:
                    return blocking.batch_next(*args, **kwargs)
                d: defer.Deferred = defer.Deferred()
                gate.append((args, kwargs, d))
                # As the persister's Deferreds do.
                return make_deferred_yieldable(d)

            def __getattr__(self, name: str) -> Any:
                return getattr(blocking, name)

        self.persister._persister = Gated()

        original = self.persistence._persist_event_batch_in_rust
        cancelled = []

        async def cancel_then_wait(*args: Any) -> dict[str, str]:
            d = defer.ensureDeferred(original(*args))
            d.cancel()
            try:
                return await make_deferred_yieldable(d)
            except defer.CancelledError:
                cancelled.append(True)
                return {}

        event, context = self._build("hello")
        with patch.object(
            self.persistence,
            "_persist_event_batch_in_rust",
            side_effect=cancel_then_wait,
        ):
            persisted = defer.ensureDeferred(
                self.persistence.persist_events([(event, context)])
            )
            self.pump()
            self.assertEqual(len(gate), 1)
            # Cancelled, but waiting for Rust.
            self.assertEqual(cancelled, [])
            args, kwargs, d = gate[0]
            blocking.batch_next(*args, **kwargs).chainDeferred(d)
            self.get_success(persisted)
        self.assertEqual(cancelled, [True])

        effects = self.persister.last_effects
        assert effects is not None
        (stream_ordering,) = effects.stream_ids
        self.assertEqual(self._event_row(event.event_id)[0], stream_ordering)
        self.assertGreaterEqual(
            self.store._stream_id_gen.get_current_token(), stream_ordering
        )


class RustFullRedactionsTestCase(unittest.HomeserverTestCase):
    """`rust_event_persistence: full` with MSC4293: Rust records the
    redactions a ban or kick starts, and the joins and leaves that end
    them, as `_after_persisting_events` does."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        experimental = config.setdefault("experimental_features", {})
        experimental["rust_event_persistence"] = "full"
        experimental["msc4293_enabled"] = True
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        if RUST_PERSIST_FOR_TESTS not in (None, "full"):
            raise SkipTest("SYNAPSE_TEST_RUST_PERSIST overrides the level")
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence
        self.alice = self.register_user("alice", "pass")
        self.alice_token = self.login("alice", "pass")
        self.carol = self.register_user("carol", "pass")
        self.carol_token = self.login("carol", "pass")
        self.dave = self.register_user("dave", "pass")
        self.dave_token = self.login("dave", "pass")

    def _room(self, room_version: str | None = None) -> str:
        """A room Alice created, which Carol and Dave joined."""
        room_id = self.helper.create_room_as(
            self.alice, room_version=room_version, tok=self.alice_token
        )
        for user, token in (
            (self.carol, self.carol_token),
            (self.dave, self.dave_token),
        ):
            self.helper.invite(room_id, self.alice, user, tok=self.alice_token)
            self.helper.join(room_id, user, tok=token)
        return room_id

    def _rows(self, room_id: str) -> list[tuple[str, str, int | None]]:
        return self.get_success(
            self.store.db_pool.simple_select_list(
                "room_ban_redactions",
                {"room_id": room_id},
                ("user_id", "redacting_event_id", "redact_end_ordering"),
            )
        )

    def _membership(
        self,
        room_id: str,
        sender: str,
        token: str,
        target: str,
        membership: str,
        *,
        redact: bool = False,
    ) -> str:
        return self.helper.change_membership(
            room_id,
            sender,
            target,
            membership,
            {"org.matrix.msc4293.redact_events": True} if redact else {},
            tok=token,
        )["event_id"]

    def _stream_ordering(self, event_id: str) -> int:
        return self.get_success(self.store.get_position_for_event(event_id)).stream

    def test_redaction_power(self) -> None:
        """Whether Carol, not a creator, may start redacting Dave's events
        follows the power levels as `_can_sender_redact` reads them."""
        for levels, allowed in (
            ({"users": {self.carol: 75}, "redact": 50}, True),
            (
                {
                    "users": {self.carol: 75},
                    "redact": 50,
                    "events": {"m.room.redaction": 100},
                },
                False,
            ),
            ({"users": {self.carol: 75}, "events_default": 80}, False),
            ({"users": {self.dave: 0}, "users_default": 75}, True),
        ):
            room_id = self._room()
            self.helper.send_state(
                room_id, EventTypes.PowerLevels, levels, tok=self.alice_token
            )
            ban = self._membership(
                room_id, self.carol, self.carol_token, self.dave, "ban", redact=True
            )
            self.assertEqual(
                self._rows(room_id), [(self.dave, ban, None)] if allowed else [], levels
            )

    def test_redaction_power_before_msc4289(self) -> None:
        """In a room version without creator power, the power levels name
        the creator too."""
        room_id = self._room(room_version="11")
        self.helper.send_state(
            room_id,
            EventTypes.PowerLevels,
            {"users": {self.alice: 100, self.carol: 75}, "redact": 50},
            tok=self.alice_token,
        )
        ban = self._membership(
            room_id, self.carol, self.carol_token, self.dave, "ban", redact=True
        )
        self.assertEqual(self._rows(room_id), [(self.dave, ban, None)])

    def test_unban_ends_and_ban_starts_again(self) -> None:
        """An unban ends the redactions at its stream ordering. A ban after
        starts them again, without an end."""
        room_id = self._room()
        self._membership(
            room_id, self.alice, self.alice_token, self.dave, "ban", redact=True
        )
        unban = self._membership(
            room_id, self.alice, self.alice_token, self.dave, "leave"
        )
        ((user, _, end),) = self._rows(room_id)
        self.assertEqual(end, self._stream_ordering(unban))

        self.helper.invite(room_id, self.alice, self.dave, tok=self.alice_token)
        self.helper.join(room_id, self.dave, tok=self.dave_token)
        ban = self._membership(
            room_id, self.alice, self.alice_token, self.dave, "ban", redact=True
        )
        self.assertEqual(self._rows(room_id), [(self.dave, ban, None)])

    def test_self_leave_keeps_redactions(self) -> None:
        """A user leaving of their own accord, by rejecting an invite after
        a kick, ends nothing."""
        room_id = self._room()
        kick = self._membership(
            room_id, self.alice, self.alice_token, self.dave, "leave", redact=True
        )
        self.helper.invite(room_id, self.alice, self.dave, tok=self.alice_token)
        self.helper.leave(room_id, self.dave, tok=self.dave_token)
        self.assertEqual(self._rows(room_id), [(self.dave, kick, None)])

    def test_backfilled_join_ends_at_current_token(self) -> None:
        """A backfilled join ends the redactions at the events stream's
        current token, not its negative ordering."""
        room_id = self._room()
        self._membership(
            room_id, self.alice, self.alice_token, self.dave, "ban", redact=True
        )
        self._membership(room_id, self.alice, self.alice_token, self.dave, "leave")
        self.helper.invite(room_id, self.alice, self.dave, tok=self.alice_token)
        event, unpersisted = self.get_success(
            self.hs.get_event_creation_handler().create_event(
                create_requester(self.dave),
                {
                    "type": EventTypes.Member,
                    "state_key": self.dave,
                    "content": {"membership": "join"},
                    "room_id": room_id,
                    "sender": self.dave,
                },
            )
        )
        context = self.get_success(unpersisted.persist(event))
        self.get_success(
            self.persistence.persist_events([(event, context)], backfilled=True)
        )
        token = self.store._stream_id_gen.get_current_token()
        ((_, _, end),) = self._rows(room_id)
        self.assertEqual(end, token)

    def test_redacted_in_the_same_chunk(self) -> None:
        """A message and the ban that redacts it, persisted together: the
        message is dropped from the event cache after the chunk's prefills,
        so reading it again shows it redacted."""
        room_id = self._room()
        pairs = []
        for event_dict in (
            {
                "type": EventTypes.Message,
                "content": {"msgtype": "m.text", "body": "noise"},
                "sender": self.dave,
            },
            {
                "type": EventTypes.Member,
                "state_key": self.dave,
                "content": {
                    "membership": "ban",
                    "org.matrix.msc4293.redact_events": True,
                },
                "sender": self.alice,
            },
        ):
            event, unpersisted = self.get_success(
                self.hs.get_event_creation_handler().create_event(
                    create_requester(str(event_dict["sender"])),
                    {**event_dict, "room_id": room_id},
                )
            )
            pairs.append((event, self.get_success(unpersisted.persist(event))))
        self.get_success(self.persistence.persist_events(pairs))
        message = self.get_success(self.store.get_event(pairs[0][0].event_id))
        self.assertEqual(message.content, {})


class RustTransactionWorkerTestCase(BaseMultiWorkerStreamTestCase):
    """`rust_event_persistence: transaction` on a worker event persister:
    the worker writes in Rust, to the main database."""

    level = "transaction"

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_event_persistence"] = (
            self.level
        )
        config["stream_writers"] = {"events": ["worker1"]}
        config["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "worker1": {"host": "testserv", "port": 1001},
        }
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        if RUST_PERSIST_FOR_TESTS not in (None, self.level):
            raise SkipTest("SYNAPSE_TEST_RUST_PERSIST overrides the level")
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")

    def test_worker_writes_in_rust(self) -> None:
        worker = self.make_worker_hs(
            "synapse.app.generic_worker", {"worker_name": "worker1"}
        )
        persistence = worker.get_storage_controllers().persistence
        assert persistence is not None
        persister = _CountingPersister(persistence._rust_persister)
        persistence._rust_persister = persister  # type: ignore[assignment]

        user = self.register_user("alice", "pass")
        token = self.login("alice", "pass")
        room_id = self.helper.create_room_as(user, tok=token)
        event_id = self.helper.send(room_id, "hello", tok=token)["event_id"]

        if self.level == "full":
            self.assertGreater(persister.chunks, 0)
        else:
            self.assertGreater(persister.transactions, 0)
        assert persister.last_effects is not None
        self.assertIn(event_id, persister.last_effects.event_cache_prefills)
        # The main process reads what the worker's Rust pool wrote.
        store = self.hs.get_datastores().main
        position = self.get_success(store.get_position_for_event(event_id))
        self.assertEqual(position.instance_name, "worker1")
        event = self.get_success(store.get_event(event_id))
        self.assertEqual(event.content["body"], "hello")
        if self.level == "full":
            # The worker took the ordering, finished it and moved its
            # `stream_positions` row on.
            self.assertEqual(persister.last_effects.stream_ids, [position.stream])
            worker_id_gen = worker.get_datastores().main._stream_id_gen
            self.assertGreaterEqual(
                worker_id_gen.get_current_token_for_writer("worker1"),
                position.stream,
            )
            row = self.get_success(
                store.db_pool.simple_select_one_onecol(
                    "stream_positions",
                    {"stream_name": "events", "instance_name": "worker1"},
                    "stream_id",
                )
            )
            self.assertEqual(row, position.stream)


class RustFullWorkerTestCase(RustTransactionWorkerTestCase):
    """`rust_event_persistence: full` on a worker event persister: the
    worker takes the stream orderings in Rust."""

    level = "full"


class PersisterOffTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        if RUST_PERSIST_FOR_TESTS not in (None, "off"):
            raise SkipTest("SYNAPSE_TEST_RUST_PERSIST overrides the level")
        return self.setup_test_homeserver()

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_event_persistence"] = "off"
        return config

    def test_no_persister_when_off(self) -> None:
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None
        self.assertIsNone(self.hs.get_rust_handlers().persister)
        self.assertIsNone(persistence._rust_persister)
