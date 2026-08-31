#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.events import EventBase
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import RoomStreamToken
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.event_injection import create_event


class StateDeltasByEventPositionTestCase(unittest.HomeserverTestCase):
    """Tests for `get_current_state_deltas_for_room_by_event_position`, which
    bounds each delta on the position of its state event rather than on the
    delta row's own `stream_id` (rows are stamped with the minimum stream
    ordering of their persist batch, so the two can differ)."""

    servlets = [
        synapse.rest.admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.persistence = hs.get_storage_controllers().persistence
        assert self.persistence is not None

        self.alice = self.register_user("alice", "password")
        self.alice_tok = self.login("alice", "password")
        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)

    def _persist_batch(self) -> tuple[EventBase, EventBase]:
        """Persist a message and a state event in a single persist batch, with
        the message first, so that the message's stream ordering is the batch
        minimum and the state event's delta row is stamped before the state
        event itself."""
        assert self.persistence is not None
        prev_event_ids = self.get_success(
            self.store.get_prev_events_for_room(self.room_id)
        )

        message, message_ctx = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.room.message",
                sender=self.alice,
                content={"msgtype": "m.text", "body": "batched message"},
                prev_event_ids=prev_event_ids,
            )
        )
        state_event, state_ctx = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.call.member",
                state_key=self.alice,
                sender=self.alice,
                content={"memberships": [{"device_id": "BATCHED"}]},
                prev_event_ids=prev_event_ids,
            )
        )

        self.get_success(
            self.persistence.persist_events(
                [(message, message_ctx), (state_event, state_ctx)]
            )
        )
        return message, state_event

    def _batch_positions(self) -> tuple[str, int, int]:
        """Persist a batch and return (state event id, batch minimum position,
        state event position), sanity-checking the batch shape."""
        message, state_event = self._persist_batch()
        message_pos = message.internal_metadata.stream_ordering
        state_pos = state_event.internal_metadata.stream_ordering
        assert message_pos is not None and state_pos is not None
        self.assertLess(message_pos, state_pos)
        return state_event.event_id, message_pos, state_pos

    def test_mid_batch_delta_is_in_window(self) -> None:
        """A window whose lower bound splits a persist batch contains the
        batch's state event delta, which the stamp-bounded query misses."""
        state_event_id, message_pos, state_pos = self._batch_positions()

        from_token = RoomStreamToken(stream=message_pos)
        to_token = RoomStreamToken(stream=state_pos)

        # The stamp-bounded query misses the delta: the row is stamped at the
        # batch minimum, below the window.
        deltas = self.get_success(
            self.store.get_current_state_deltas_for_room(
                self.room_id, from_token=from_token, to_token=to_token
            )
        )
        self.assertEqual([d.event_id for d in deltas], [])

        # The by-event-position query recovers it.
        deltas = self.get_success(
            self.store.get_current_state_deltas_for_room_by_event_position(
                self.room_id, from_token=from_token, to_token=to_token
            )
        )
        self.assertEqual([d.event_id for d in deltas], [state_event_id])

    def test_delta_is_not_reported_before_its_event(self) -> None:
        """A window ending at the batch minimum must not contain the state
        event's delta: its effective position is the event's own, beyond the
        window. (The stamp-bounded query reports it here, one window early.)"""
        state_event_id, message_pos, _state_pos = self._batch_positions()

        to_token = RoomStreamToken(stream=message_pos)

        deltas = self.get_success(
            self.store.get_current_state_deltas_for_room(
                self.room_id, from_token=None, to_token=to_token
            )
        )
        self.assertIn(state_event_id, [d.event_id for d in deltas])

        deltas = self.get_success(
            self.store.get_current_state_deltas_for_room_by_event_position(
                self.room_id, from_token=None, to_token=to_token
            )
        )
        self.assertNotIn(state_event_id, [d.event_id for d in deltas])

    def test_no_lower_bound(self) -> None:
        """With no lower bound the event-driven query is unnecessary and the
        query returns everything up to the upper bound, batch rows included."""
        state_event_id, _message_pos, state_pos = self._batch_positions()

        deltas = self.get_success(
            self.store.get_current_state_deltas_for_room_by_event_position(
                self.room_id,
                from_token=None,
                to_token=RoomStreamToken(stream=state_pos),
            )
        )
        # The room's creation state plus the batched state event.
        self.assertIn(state_event_id, [d.event_id for d in deltas])

    def test_no_upper_bound(self) -> None:
        """With no upper bound a mid-batch lower bound still recovers the
        batch's state event delta."""
        state_event_id, message_pos, _state_pos = self._batch_positions()

        deltas = self.get_success(
            self.store.get_current_state_deltas_for_room_by_event_position(
                self.room_id,
                from_token=RoomStreamToken(stream=message_pos),
                to_token=None,
            )
        )
        self.assertEqual([d.event_id for d in deltas], [state_event_id])

    def test_unfiltered_event_driven_query(self) -> None:
        """Until `events.state_key` has been back-populated the event-driven
        query cannot filter on it; the unfiltered mode must find the same
        deltas."""
        state_event_id, message_pos, state_pos = self._batch_positions()

        deltas = self.get_success(
            self.store.db_pool.runInteraction(
                "test_unfiltered_event_driven_query",
                self.store.get_current_state_deltas_for_room_by_event_position_txn,
                self.room_id,
                from_token=RoomStreamToken(stream=message_pos),
                to_token=RoomStreamToken(stream=state_pos),
                events_state_key_populated=False,
            )
        )
        self.assertEqual([d.event_id for d in deltas], [state_event_id])

    def test_falls_back_until_index_built(self) -> None:
        """Until the `current_state_delta_stream(event_id)` index has been
        built, the query falls back to the stamp-bounded behaviour (which can
        miss mid-batch deltas but never scans without an index)."""
        state_event_id, message_pos, state_pos = self._batch_positions()

        # Pretend the index's background update is still pending.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": "current_state_delta_stream_event_id_index",
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False

        deltas = self.get_success(
            self.store.get_current_state_deltas_for_room_by_event_position(
                self.room_id,
                from_token=RoomStreamToken(stream=message_pos),
                to_token=RoomStreamToken(stream=state_pos),
            )
        )
        # Stamp-bounded behaviour: the mid-batch delta is missed.
        self.assertEqual([d.event_id for d in deltas], [])
