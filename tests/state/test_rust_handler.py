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
#
#

"""Tests for resolving state with the Rust state resolution
(`experimental_features.rust_state_res`). These need the native Rust Postgres
driver:

    SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.state.test_rust_handler
"""

from unittest import SkipTest
from unittest.mock import patch

from twisted.internet.testing import MemoryReactor

from synapse.api.room_versions import RoomVersions
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.state import _make_state_cache_entry, _rust_shadow_counter
from synapse.storage.rust_pool import RustConnectionPool
from synapse.types import create_requester
from synapse.util.clock import Clock

from tests import unittest


class RustStateResHandlerTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.user_id = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")
        self.handler = hs.get_state_resolution_handler()

    def _fork(self, room_id: str) -> tuple[str, str]:
        """Send two topics with the same prev events, so persisting the second
        resolves state across the fork. Returns their event IDs."""
        event_handler = self.hs.get_event_creation_handler()
        requester = create_requester(self.user_id)
        prev_event_ids = self.get_success(self.store.get_prev_events_for_room(room_id))

        event_ids = []
        for topic in ("A", "B"):
            event, unpersisted_context = self.get_success(
                event_handler.create_event(
                    requester,
                    {
                        "type": "m.room.topic",
                        "state_key": "",
                        "content": {"topic": topic},
                        "room_id": room_id,
                        "sender": self.user_id,
                    },
                    prev_event_ids=prev_event_ids,
                )
            )
            context = self.get_success(unpersisted_context.persist(event))
            self.get_success(
                event_handler.handle_new_client_event(
                    requester, events_and_context=[(event, context)]
                )
            )
            event_ids.append(event.event_id)
        return event_ids[0], event_ids[1]

    def _shadow_count(self, result: str) -> float:
        return _rust_shadow_counter.labels(
            result=result, server_name=self.hs.hostname
        )._value.get()

    def _current_topic(self, room_id: str) -> str | None:
        state = self.get_success(self.store.get_partial_current_state_ids(room_id))
        return state.get(("m.room.topic", ""))

    def test_shadow_mode_compares(self) -> None:
        """In shadow mode, resolutions run in both and agree."""
        self.handler._rust_state_res_mode = "shadow"
        self.handler._rust_state_res = self.hs.get_rust_handlers().state_resolution
        assert self.handler._rust_state_res is not None

        matches = self._shadow_count("match")
        mismatches = self._shadow_count("mismatch")
        errors = self._shadow_count("error")

        room_id = self.helper.create_room_as(self.user_id, tok=self.tok)
        topics = self._fork(room_id)

        self.assertGreater(self._shadow_count("match"), matches)
        self.assertEqual(self._shadow_count("mismatch"), mismatches)
        self.assertEqual(self._shadow_count("error"), errors)
        self.assertIn(self._current_topic(room_id), topics)

    def test_on_mode_uses_rust(self) -> None:
        """In "on" mode, v2 and v2.1 resolutions of state groups run in Rust,
        through its state group entry point, and the metrics come from its
        stats."""
        self.handler._rust_state_res_mode = "on"
        self.handler._rust_state_res = self.hs.get_rust_handlers().state_resolution
        assert self.handler._rust_state_res is not None

        for room_version in (RoomVersions.V10, RoomVersions.V12):
            room_id = self.helper.create_room_as(
                self.user_id, room_version=room_version.identifier, tok=self.tok
            )

            with (
                patch(
                    "synapse.state.v2.resolve_events_with_store",
                    side_effect=AssertionError("Python state res should not run"),
                ),
                patch.object(
                    self.handler,
                    "_resolve_with_rust",
                    side_effect=AssertionError("state groups resolve as groups"),
                ),
            ):
                topics = self._fork(room_id)

            self.assertIn(self._current_topic(room_id), topics)
            self.assertIn(room_id, self.handler._state_res_metrics)

    def test_on_mode_falls_back_without_chain_cover_index(self) -> None:
        """A room without a chain cover index is resolved in Python."""
        self.handler._rust_state_res_mode = "on"
        self.handler._rust_state_res = self.hs.get_rust_handlers().state_resolution

        room_id = self.helper.create_room_as(
            self.user_id, room_version=RoomVersions.V10.identifier, tok=self.tok
        )
        self.get_success(
            self.store.db_pool.simple_update(
                table="rooms",
                keyvalues={"room_id": room_id},
                updatevalues={"has_auth_chain_index": False},
                desc="test",
            )
        )

        with self.assertLogs("synapse.state", level="INFO") as logs:
            topics = self._fork(room_id)
        self.assertTrue(
            any("Falling back to Python state res" in line for line in logs.output)
        )
        self.assertIn(self._current_topic(room_id), topics)

    def test_resolve_state_groups(self) -> None:
        """Resolving state groups picks the cache entry's base as
        `_make_state_cache_entry` does, and a second resolution does not
        convert the groups' state again."""
        rust = self.hs.get_rust_handlers().state_resolution
        assert rust is not None

        room_id = self.helper.create_room_as(
            self.user_id, room_version=RoomVersions.V10.identifier, tok=self.tok
        )
        topic_a, topic_b = self._fork(room_id)
        groups = self.get_success(
            self.store._get_state_group_for_events([topic_a, topic_b])
        )
        state_controller = self.hs.get_storage_controllers().state
        state_groups = self.get_success(
            state_controller.get_state_for_groups(set(groups.values()))
        )
        state_sets = list(state_groups.values())

        rust.clear_event_cache()
        rust.clear_conflict_cache_for_test()
        self.assertEqual(rust.state_map_cache_len(), 0)

        # What the Python picks from the resolved state.
        resolved, _ = self.get_success(
            rust.resolve(room_id, RoomVersions.V10.identifier, state_sets)
        )
        expected = _make_state_cache_entry(resolved, state_groups)

        for attempt in range(2):
            rust.clear_conflict_cache_for_test()
            state_group, prev_group, delta_ids, state, stats = self.get_success(
                rust.resolve_state_groups(
                    room_id, RoomVersions.V10.identifier, state_groups
                )
            )
            self.assertEqual(
                (state_group, prev_group, delta_ids, state),
                (
                    expected.state_group,
                    expected.prev_group,
                    expected.delta_ids,
                    expected._state,
                ),
                f"attempt {attempt}",
            )
            # The fork's two groups differ only in the topic, so the resolved
            # state is one of them.
            self.assertIn(state_group, state_groups)
            self.assertIsNone(prev_group)
            self.assertIsNone(delta_ids)
            self.assertIsNone(state)
            self.assertEqual(
                stats.state_map_cache_hits, len(state_groups) if attempt else 0
            )
        self.assertEqual(rust.state_map_cache_len(), len(state_groups))

        # A group passed with partial state is converted afresh, not served
        # the cached full state.
        (group, full_state), *_ = state_groups.items()
        partial = {group: {("m.room.create", ""): full_state[("m.room.create", "")]}}
        _, _, _, _, stats = self.get_success(
            rust.resolve_state_groups(room_id, RoomVersions.V10.identifier, partial)
        )
        self.assertEqual(stats.state_map_cache_hits, 0)

        rust.clear_event_cache()
        self.assertEqual(rust.state_map_cache_len(), 0)

    # Big enough for every event the resolution reads. Trial's default is one.
    @unittest.override_config({"event_cache_size": 1000})
    def test_event_cache(self) -> None:
        """A second resolution reads its events, chain positions and chain
        links from the handler's caches, which the store's event cache
        invalidation empties."""
        rust = self.hs.get_rust_handlers().state_resolution
        assert rust is not None

        room_id = self.helper.create_room_as(
            self.user_id, room_version=RoomVersions.V10.identifier, tok=self.tok
        )
        topic_a, topic_b = self._fork(room_id)
        groups = self.get_success(
            self.store._get_state_group_for_events([topic_a, topic_b])
        )
        state_controller = self.hs.get_storage_controllers().state
        state_sets = [
            self.get_success(state_controller.get_state_ids_for_group(group))
            for group in groups.values()
        ]

        # Persisting the fork may already have resolved it here, depending on
        # the test's `rust_state_res` mode.
        rust.clear_event_cache()
        rust.clear_conflict_cache_for_test()

        first, first_stats = self.get_success(
            rust.resolve(room_id, RoomVersions.V10.identifier, state_sets)
        )
        self.assertGreater(first_stats.db_events, 0)
        self.assertEqual(first_stats.event_cache_hits, 0)
        self.assertEqual(first_stats.chain_position_cache_hits, 0)
        self.assertEqual(first_stats.chain_link_cache_hits, 0)
        cached = rust.event_cache_len()
        self.assertEqual(cached, first_stats.db_events)
        self.assertGreater(rust.chain_link_cache_len(), 0)

        # Clear the conflict cache, so the resolution runs again rather than
        # being answered from it.
        rust.clear_conflict_cache_for_test()
        second, second_stats = self.get_success(
            rust.resolve(room_id, RoomVersions.V10.identifier, state_sets)
        )
        self.assertEqual(second, first)
        self.assertEqual(second_stats.db_events, 0)
        self.assertEqual(second_stats.event_cache_hits, first_stats.db_events)
        # Every event in the state sets has its position cached, and every
        # chain its links, so the only chain query left is for the gap
        # events the auth difference needs.
        self.assertGreater(second_stats.chain_position_cache_hits, 0)
        self.assertGreater(second_stats.chain_link_cache_hits, 0)
        self.assertLess(second_stats.db_queries, first_stats.db_queries)

        self.store._invalidate_local_get_event_cache(topic_a)
        self.assertEqual(rust.event_cache_len(), cached - 1)

        self.store._invalidate_local_get_event_cache_room_id(room_id)
        self.assertEqual(rust.event_cache_len(), 0)
        self.assertEqual(rust.chain_link_cache_len(), 0)
