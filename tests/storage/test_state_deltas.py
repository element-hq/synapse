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

import random
from typing import Any

from immutabledict import immutabledict

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes
from synapse.events import EventBase
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import RoomStreamToken
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.event_injection import (
    create_event,
    persist_message_and_state_event_in_one_batch,
)


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

        self.alice = self.register_user("alice", "password")
        self.alice_tok = self.login("alice", "password")
        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)

    def _persist_batch(self) -> tuple[EventBase, EventBase]:
        return self.get_success(
            persist_message_and_state_event_in_one_batch(
                self.hs, self.room_id, self.alice
            )
        )

    def _batch_positions(self) -> tuple[str, int, int]:
        """Persist a batch and return (state event id, batch minimum position,
        state event position), sanity-checking the batch shape."""
        message, state_event = self._persist_batch()
        message_pos = message.internal_metadata.stream_ordering
        state_pos = state_event.internal_metadata.stream_ordering
        assert message_pos is not None and state_pos is not None
        self.assertLess(message_pos, state_pos)
        return state_event.event_id, message_pos, state_pos

    def test_delta_row_is_stamped_at_batch_minimum(self) -> None:
        """Documents the precondition: the `current_state_delta_stream` row of
        a state event persisted in a batch is stamped with the batch's
        *minimum* stream ordering (see `_update_current_state_txn`), not the
        state event's own, so a window bounded on the stamp and a window
        bounded on the event disagree about which side of a mid-batch token
        the delta falls on."""
        state_event_id, message_pos, _state_pos = self._batch_positions()

        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                table="current_state_delta_stream",
                keyvalues={"room_id": self.room_id, "type": "m.call.member"},
                retcols=("stream_id", "event_id"),
                desc="test_delta_row_is_stamped_at_batch_minimum",
            )
        )
        self.assertEqual(rows, [(message_pos, state_event_id)])

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

    def test_rows_without_an_event_keep_their_stamp(self) -> None:
        """Rows with no event -- the clearance of the room's state when the
        last local user leaves -- have no event position to bound on and are
        returned at their own stamp, exactly as the stamp-bounded query
        returns them."""
        before = self.store.get_room_max_token()
        self.helper.leave(self.room_id, self.alice, tok=self.alice_tok)
        after = self.store.get_room_max_token()

        deltas = self.get_success(
            self.store.get_current_state_deltas_for_room_by_event_position(
                self.room_id, from_token=before, to_token=after
            )
        )

        self.assertTrue(deltas)
        self.assertEqual({d.event_id for d in deltas}, {None})
        self.assertIn(
            (EventTypes.Create, ""), {(d.event_type, d.state_key) for d in deltas}
        )
        self.assertCountEqual(
            deltas,
            self.get_success(
                self.store.get_current_state_deltas_for_room(
                    self.room_id, from_token=before, to_token=after
                )
            ),
        )

    def _persist_mixed_batch(self, kinds: list[bool], rng: random.Random) -> None:
        """Persist one batch of events, a state event for each True in
        `kinds` (over a handful of reused state keys) and a message for each
        False, all forked off the same forward extremities."""
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None
        prev_event_ids = self.get_success(
            self.store.get_prev_events_for_room(self.room_id)
        )
        events = []
        for i, is_state in enumerate(kinds):
            kwargs: dict[str, Any]
            if is_state:
                kwargs = {
                    "type": "m.call.member",
                    "state_key": f"k{rng.randrange(4)}",
                    "content": {"memberships": [{"device_id": f"d{rng.random()}"}]},
                }
            else:
                kwargs = {
                    "type": "m.room.message",
                    "content": {"msgtype": "m.text", "body": f"msg {i}"},
                }
            events.append(
                self.get_success(
                    create_event(
                        self.hs,
                        room_id=self.room_id,
                        sender=self.alice,
                        prev_event_ids=prev_event_ids,
                        **kwargs,
                    )
                )
            )
        self.get_success(persistence.persist_events(events))

    def test_matches_brute_force_over_every_window(self) -> None:
        """Cross-checks the query against a brute-force oracle.

        Persists a mix of batches (lone messages, lone state events, batches of
        two to four events with state events at varying positions, state keys
        reused across batches), then for every pair of positions in the room's
        stream -- and for tokens whose position for the room's writer is ahead
        of their minimum, which is what a multi-writer token looks like --
        checks that the deltas returned are exactly the rows whose effective
        position (the maximum of the row's stamp and its event's stream
        ordering) lies in the window, in stamp order.
        """
        rng = random.Random(4222)
        for _ in range(20):
            size = rng.choice([1, 1, 2, 3, 4])
            kinds = [rng.random() < 0.6 for _ in range(size)]
            if size == 1:
                kinds = [rng.random() < 0.5]
            self._persist_mixed_batch(kinds, rng)

        # The oracle: every row's effective position, straight from the tables.
        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                table="current_state_delta_stream",
                keyvalues={"room_id": self.room_id},
                retcols=("stream_id", "event_id"),
                desc="oracle_rows",
            )
        )
        event_positions = dict(
            self.get_success(
                self.store.db_pool.simple_select_list(
                    table="events",
                    keyvalues={"room_id": self.room_id},
                    retcols=("event_id", "stream_ordering"),
                    desc="oracle_events",
                )
            )
        )
        effective = {
            (stream_id, event_id): max(
                stream_id, event_positions.get(event_id, stream_id)
            )
            for stream_id, event_id in rows
        }
        self.assertGreater(len(rows), 10)
        self.assertTrue(
            any(effective[key] > key[0] for key in effective),
            "test setup: expected some rows stamped before their event",
        )

        lowest = min(event_positions.values()) - 1
        highest = max(event_positions.values())
        positions: list[int | None] = [None, *range(lowest, highest + 1)]

        def check(
            from_token: RoomStreamToken | None,
            to_token: RoomStreamToken | None,
            from_pos: int | None,
            to_pos: int | None,
        ) -> None:
            expected = {
                key
                for key, pos in effective.items()
                if (from_pos is None or from_pos < pos)
                and (to_pos is None or pos <= to_pos)
            }
            deltas = self.get_success(
                self.store.get_current_state_deltas_for_room_by_event_position(
                    self.room_id, from_token=from_token, to_token=to_token
                )
            )
            self.assertEqual(
                {(d.stream_id, d.event_id) for d in deltas},
                expected,
                f"window ({from_token}, {to_token}]",
            )
            stamps = [d.stream_id for d in deltas]
            self.assertEqual(stamps, sorted(stamps), "deltas not in stamp order")

        for from_pos in positions:
            for to_pos in positions:
                if from_pos is not None and to_pos is not None and to_pos <= from_pos:
                    continue
                check(
                    RoomStreamToken(stream=from_pos) if from_pos is not None else None,
                    RoomStreamToken(stream=to_pos) if to_pos is not None else None,
                    from_pos,
                    to_pos,
                )
                # The same window as a multi-writer token: the room's writer
                # ("master") sits at the position, the token's minimum behind.
                if from_pos is not None and to_pos is not None and from_pos % 3 == 0:
                    check(
                        RoomStreamToken(
                            stream=from_pos - 2,
                            instance_map=immutabledict({"master": from_pos}),
                        ),
                        RoomStreamToken(
                            stream=to_pos - 2,
                            instance_map=immutabledict({"master": to_pos}),
                        ),
                        from_pos,
                        to_pos,
                    )
