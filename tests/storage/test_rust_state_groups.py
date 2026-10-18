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

"""The Rust state group writer against Python's, on the same batches."""

from typing import Any

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase, make_event_from_dict
from synapse.events.snapshot import UnpersistedEventContext
from synapse.logging.context import (
    LoggingContext,
    current_context,
    make_deferred_yieldable,
)
from synapse.server import HomeServer
from synapse.types import StateMap
from synapse.util.clock import Clock

from tests import unittest
from tests.utils import USE_POSTGRES_FOR_TESTS, USE_RUST_DRIVER_FOR_TESTS

# The tables a chain of state groups writes to, and their columns.
TABLES = {
    "state_groups": ("id", "room_id", "event_id"),
    "state_group_edges": ("state_group", "prev_state_group"),
    "state_groups_state": ("state_group", "room_id", "type", "state_key", "event_id"),
}


class RustStateGroupWriterTestCase(unittest.HomeserverTestCase):
    if not (USE_POSTGRES_FOR_TESTS and USE_RUST_DRIVER_FOR_TESTS):
        skip = "Needs Postgres with the native driver"

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.state_store = homeserver.get_datastores().state
        self.rust_writer = self.state_store._rust_state_group_writer
        assert self.rust_writer is not None
        self.storage = homeserver.get_storage_controllers()

    def _batch(
        self, room_id: str, state_before: StateMap[str] | None, prev_group: int | None
    ) -> list[tuple[EventBase, UnpersistedEventContext]]:
        """Three events: two state events with a message between them."""
        batch = []
        for i, (event_type, state_key) in enumerate(
            [("m.room.topic", ""), ("m.room.message", None), ("m.room.member", "@a:t")]
        ):
            event_dict: dict[str, Any] = {
                "type": event_type,
                "sender": "@a:t",
                "room_id": room_id,
                "content": {"i": i},
                "auth_events": [],
                "prev_events": [],
                "depth": 1 + i,
                "hashes": {"sha256": "x"},
                "signatures": {},
                "origin_server_ts": 1 + i,
                # The same in both twins, unlike a hashed event ID.
                "event_id": f"$e{i}:t",
            }
            if state_key is not None:
                event_dict["state_key"] = state_key
            context = UnpersistedEventContext(
                storage=self.storage,
                state_group_before_event=prev_group if i == 0 else None,
                state_group_after_event=None,
                state_delta_due_to_event=None,
                prev_group_for_state_group_before_event=None,
                delta_ids_to_state_group_before_event=None,
                partial_state=False,
                state_map_before_event=state_before if i == 0 else None,
            )
            batch.append((make_event_from_dict(event_dict, RoomVersions.V1), context))
        return batch

    def _store(
        self,
        batch: list[tuple[EventBase, UnpersistedEventContext]],
        room_id: str,
        prev_group: int | None,
        rust: bool,
    ) -> None:
        self.state_store._rust_state_group_writer = self.rust_writer if rust else None
        try:
            self.get_success(
                self.state_store.store_state_deltas_for_batched(
                    batch,  # type: ignore[arg-type]
                    room_id,
                    prev_group,
                )
            )
        finally:
            self.state_store._rust_state_group_writer = self.rust_writer

    def _rows(self, room_id: str) -> dict[str, list[tuple]]:
        """The rows of `room_id`'s state groups, with each group ID replaced by
        its rank among them, so two rooms can be compared."""
        groups = self.get_success(
            self.state_store.db_pool.simple_select_onecol(
                "state_groups", {"room_id": room_id}, "id"
            )
        )
        rank = {group: i for i, group in enumerate(sorted(groups))}

        def norm(value: Any) -> Any:
            if isinstance(value, int):
                return rank.get(value, ("outside", value))
            if isinstance(value, str):
                return value.replace(room_id, "!room")
            return value

        rows: dict[str, list[tuple]] = {}
        for table, columns in TABLES.items():
            if table == "state_group_edges":
                found = self.get_success(
                    self.state_store.db_pool.simple_select_many_batch(
                        table, "state_group", groups, columns
                    )
                )
            else:
                found = self.get_success(
                    self.state_store.db_pool.simple_select_list(
                        table, {"room_id": room_id}, columns
                    )
                )
            rows[table] = sorted(tuple(norm(v) for v in row) for row in found)
        return rows

    def _contexts(
        self, batch: list[tuple[EventBase, UnpersistedEventContext]]
    ) -> list[tuple]:
        """Each context's groups, relative to the group before the batch, and
        its state delta."""
        base = batch[0][1].state_group_before_event
        assert base is not None
        result = []
        for _, context in batch:
            assert context.state_group_before_event is not None
            assert context.state_group_after_event is not None
            result.append(
                (
                    context.state_group_before_event - base,
                    context.state_group_after_event - base,
                    context.state_delta_due_to_event,
                )
            )
        return result

    def test_new_room(self) -> None:
        """A batch that starts a room stores the state before it as a full
        group, then the chain."""
        state_before = {("m.room.create", ""): "$create", ("m.room.name", ""): "$name"}
        batches = {}
        for rust, room_id in ((False, "!python:t"), (True, "!rust:t")):
            batch = self._batch(room_id, state_before, None)
            self._store(batch, room_id, None, rust)
            batches[rust] = batch

        self.assertEqual(self._rows("!python:t"), self._rows("!rust:t"))
        self.assertEqual(len(self._rows("!rust:t")["state_groups"]), 3)
        # The contexts get the same groups, relative to the first.
        self.assertEqual(self._contexts(batches[False]), self._contexts(batches[True]))

        # The state after the batch reads back.
        group = batches[True][-1][1].state_group_after_event
        assert group is not None
        expected = dict(state_before)
        expected[("m.room.topic", "")] = batches[True][0][0].event_id
        expected[("m.room.member", "@a:t")] = batches[True][2][0].event_id
        self.assertEqual(
            self.get_success(self.storage.state.get_state_ids_for_group(group)),
            expected,
        )

    def test_follows_existing_group(self) -> None:
        """A batch after an existing group chains on from it."""
        groups = {}
        for rust, room_id in ((False, "!python:t"), (True, "!rust:t")):
            prev_group = self.get_success(
                self.state_store.store_state_group(
                    "$prev", room_id, None, None, {("m.room.create", ""): "$create"}
                )
            )
            batch = self._batch(room_id, None, prev_group)
            self._store(batch, room_id, prev_group, rust)
            groups[rust] = (prev_group, batch)

        self.assertEqual(self._rows("!python:t"), self._rows("!rust:t"))
        for rust in (False, True):
            prev_group, batch = groups[rust]
            self.assertEqual(batch[0][1].state_group_before_event, prev_group)
            self.assertEqual(
                batch[1][1].state_group_after_event, batch[0][1].state_group_after_event
            )

    def test_missing_prev_group(self) -> None:
        """A deleted `prev_group` fails the write, with Python's message."""
        batch = self._batch("!rust:t", None, 999999)
        failure = self.get_failure(
            self.state_store.store_state_deltas_for_batched(
                batch,  # type: ignore[arg-type]
                "!rust:t",
                999999,
            ),
            Exception,
        )
        self.assertIn(
            "Trying to persist state with unpersisted prev_group: 999999",
            str(failure.value),
        )
        self.assertEqual(self._rows("!rust:t")["state_groups"], [])

    def test_keeps_logcontext(self) -> None:
        """Waiting for the Rust write leaves the caller in its own logcontext.

        The test homeserver makes Rust calls block, which would hide a mistake
        here, so this uses the Rust store directly.
        """
        writer = self.rust_writer
        self.state_store._rust_state_group_writer = getattr(writer, "_handler", writer)
        batch = self._batch("!rust:t", {("m.room.create", ""): "$create"}, None)

        async def store() -> str:
            await self.state_store.store_state_deltas_for_batched(
                batch,  # type: ignore[arg-type]
                "!rust:t",
                None,
            )
            return str(current_context())

        with LoggingContext(name="request", server_name="test"):
            context_after = self.get_success(
                make_deferred_yieldable(defer.ensureDeferred(store()))
            )
        # The code after the write runs in the caller's context.
        self.assertEqual(context_after, "request")
