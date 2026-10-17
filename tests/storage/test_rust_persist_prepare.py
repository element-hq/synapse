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

"""How `_persist_event_batch` prepares chunks in Rust
(`rust/src/persist/prepare.rs`) at the `shadow` and `prepare` levels of
`rust_event_persistence`. These need the native Rust Postgres driver:

    SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.storage.test_rust_persist_prepare
"""

from typing import Any
from unittest import SkipTest
from unittest.mock import Mock

import attr

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, StickyEventField
from synapse.api.errors import SynapseError
from synapse.api.room_versions import RoomVersions
from synapse.config.experimental import RustEventPersistenceLevel
from synapse.events.snapshot import EventPersistencePair
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.controllers.persist_events import (
    _PreparedChunk,
    rust_prepare_counter,
)
from synapse.storage.rust_pool import RustConnectionPool
from synapse.types import create_requester
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.test_utils.event_injection import inject_event

_RESULTS = ("rust", "matched", "mismatch", "fallback", "error")


class _PrepareTestBase(unittest.HomeserverTestCase):
    """A homeserver at `level`, whatever `SYNAPSE_TEST_RUST_PERSIST` says,
    with sticky events on."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    level: RustEventPersistenceLevel

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        experimental = config.setdefault("experimental_features", {})
        experimental["rust_event_persistence"] = self.level
        experimental["msc4354_enabled"] = True
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
        # The level is read per chunk, so this overrides the environment.
        hs.config.experimental.rust_event_persistence = self.level
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence
        self.persistence._rust_persist_strict = True

        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")
        self.carol = self.register_user("carol", "pass")
        self.carol_tok = self.login("carol", "pass")
        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        self._counts_before = self._counts()

    def _counts(self) -> dict[str, float]:
        return {
            result: rust_prepare_counter.labels(
                level=self.level, result=result, server_name=self.hs.hostname
            )._value.get()
            for result in _RESULTS
        }

    def _counted(self) -> dict[str, float]:
        """What was counted since `prepare`, leaving out zeros."""
        after = self._counts()
        return {
            result: after[result] - self._counts_before[result]
            for result in _RESULTS
            if after[result] != self._counts_before[result]
        }

    def _send(self, body: str, expect_code: int = 200) -> str | None:
        return self.helper.send(
            self.room_id, body, tok=self.alice_tok, expect_code=expect_code
        ).get("event_id")

    def _power_levels(self, users: dict[str, int]) -> None:
        # The creator has unlimited power in room version 12.
        self.helper.send_state(
            self.room_id,
            EventTypes.PowerLevels,
            {"users": users, "events_default": 0},
            tok=self.alice_tok,
        )

    def _sticky(self, sender: str) -> str:
        """A soft-failed sticky event, as federation would persist one."""
        return self.get_success(
            inject_event(
                self.hs,
                room_id=self.room_id,
                sender=sender,
                type=EventTypes.Message,
                content={"msgtype": "m.text", "body": "sticky"},
                internal_metadata={"soft_failed": True},
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=10).as_millis()
                ),
            )
        ).event_id

    def _soft_failed_sticky_events(self) -> set[str]:
        return set(
            self.get_success(
                self.store.db_pool.runInteraction(
                    "test",
                    self.store._get_soft_failed_sticky_events_txn,
                    self.room_id,
                    senders=None,
                )
            )
        )

    def _new_event(
        self, room_id: str, event_type: str, content: dict[str, Any], **kwargs: Any
    ) -> EventPersistencePair:
        """An event of Alice's, not yet persisted."""
        event, unpersisted_context = self.get_success(
            self.hs.get_event_creation_handler().create_event(
                create_requester(self.alice),
                {
                    "type": event_type,
                    "content": content,
                    "room_id": room_id,
                    "sender": self.alice,
                    **kwargs,
                },
                prev_event_ids=self.get_success(
                    self.store.get_prev_events_for_room(room_id)
                ),
            )
        )
        return event, self.get_success(unpersisted_context.persist(event))

    def _fork(self, room_id: str) -> None:
        """Send two topics with the same prev events, so persisting the second
        resolves state across the fork."""
        pairs = [
            self._new_event(room_id, EventTypes.Topic, {"topic": topic}, state_key="")
            for topic in ("A", "B")
        ]
        for pair in pairs:
            self.get_success(
                self.hs.get_event_creation_handler().handle_new_client_event(
                    create_requester(self.alice), events_and_context=[pair]
                )
            )

    def _room_lifecycle(self) -> int:
        """Persist a room's worth of changes, one chunk each. Returns the
        number of chunks."""
        self._send("hello")
        self.helper.send_state(
            self.room_id, EventTypes.Name, {"name": "The room"}, tok=self.alice_tok
        )
        self.helper.join(self.room_id, self.carol, tok=self.carol_tok)
        self._power_levels({self.bob: -1})
        sticky = self._sticky(self.bob)
        self.assertEqual(self._soft_failed_sticky_events(), {sticky})
        # Bob may speak again, so his sticky event passes.
        self._power_levels({self.bob: 0})
        self.assertEqual(self._soft_failed_sticky_events(), set())
        self.helper.leave(self.room_id, self.carol, tok=self.carol_tok)
        self._send("bye")
        return 8


class ShadowTestCase(_PrepareTestBase):
    level = "shadow"

    def test_room_lifecycle(self) -> None:
        chunks = self._room_lifecycle()
        self.assertEqual(self._counted(), {"matched": chunks})

    def _tamper(self, change: dict[str, Any]) -> None:
        """Make Rust's results differ from Python's by `change`."""
        prepare_in_rust = self.persistence._prepare_chunk_in_rust

        async def tampered(
            room_id: str,
            chunk: list[EventPersistencePair],
            backfilled: bool,
            *,
            shadow: bool,
        ) -> _PreparedChunk | None:
            prepared = await prepare_in_rust(room_id, chunk, backfilled, shadow=shadow)
            assert prepared is not None
            return attr.evolve(prepared, **change)

        self.persistence._prepare_chunk_in_rust = tampered  # type: ignore[method-assign]

    def test_mismatch_fails_strict_mode(self) -> None:
        self._tamper({"new_forward_extremities": {"$bogus"}})
        self._send("hello", expect_code=500)
        self.assertEqual(self._counted(), {"mismatch": 1})

    def test_mismatch_uses_python(self) -> None:
        self.persistence._rust_persist_strict = False
        self._tamper({"new_forward_extremities": {"$bogus"}})
        with self.assertLogs(
            "synapse.storage.controllers.persist_events", "WARNING"
        ) as logs:
            event_id = self._send("hello")
        self.assertIn("new_forward_extremities", logs.output[0])
        self.assertEqual(self._counted(), {"mismatch": 1})
        self.assertEqual(
            self.get_success(self.store.get_latest_event_ids_in_room(self.room_id)),
            frozenset({event_id}),
        )

    def _shift_chains(self, new: bool) -> None:
        """Move the new chains (`new`) or the extended existing ones in
        Rust's results to other chain IDs."""
        prepare_in_rust = self.persistence._prepare_chunk_in_rust

        async def shifted(
            room_id: str,
            chunk: list[EventPersistencePair],
            backfilled: bool,
            *,
            shadow: bool,
        ) -> _PreparedChunk | None:
            prepared = await prepare_in_rust(room_id, chunk, backfilled, shadow=shadow)
            assert prepared is not None
            for links in prepared.new_event_links.values():
                if (links.sequence_number == 1) == new:
                    links.chain_id += 1_000_000
            return prepared

        self.persistence._prepare_chunk_in_rust = shifted  # type: ignore[method-assign]

    def test_new_chains_are_compared_by_shape(self) -> None:
        # Rust's dry run and Python take new chain IDs from the sequence
        # separately, so the comparison names new chains after their events.
        self._shift_chains(new=True)
        # Carol's join starts a chain.
        self.helper.join(self.room_id, self.carol, tok=self.carol_tok)
        self.assertEqual(self._counted(), {"matched": 1})

    def test_existing_chains_are_compared_by_id(self) -> None:
        self.helper.join(self.room_id, self.carol, tok=self.carol_tok)
        self.persistence._rust_persist_strict = False
        self._shift_chains(new=False)
        # Carol's leave extends the chain of her join.
        with self.assertLogs("synapse.storage.controllers.persist_events", "WARNING"):
            self.helper.leave(self.room_id, self.carol, tok=self.carol_tok)
        self.assertEqual(self._counted(), {"matched": 1, "mismatch": 1})

    def test_fork(self) -> None:
        self._fork(self.room_id)
        self.assertEqual(self._counted(), {"matched": 2})

    def test_backfill(self) -> None:
        pair = self._new_event(
            self.room_id, EventTypes.Message, {"msgtype": "m.text", "body": "old"}
        )
        self.get_success(self.persistence.persist_events([pair], backfilled=True))
        self.assertEqual(self._counted(), {"matched": 1})

    def test_rust_rejection_python_accepts(self) -> None:
        self.persistence._rust_persist_strict = False
        self.persistence._rust_persister = Mock(
            prepare_chunk=Mock(return_value=defer.fail(SynapseError(500, "no")))
        )
        with self.assertLogs("synapse.storage.controllers.persist_events", "WARNING"):
            self._send("hello")
        self.assertEqual(self._counted(), {"mismatch": 1})

    def test_rust_error_is_logged(self) -> None:
        self.persistence._rust_persist_strict = False
        self.persistence._rust_persister = Mock(
            prepare_chunk=Mock(return_value=defer.fail(RuntimeError("boom")))
        )
        with self.assertLogs("synapse.storage.controllers.persist_events", "ERROR"):
            self._send("hello")
        self.assertEqual(self._counted(), {"error": 1})


class PrepareTestCase(_PrepareTestBase):
    level = "prepare"

    def test_room_lifecycle(self) -> None:
        chunks = self._room_lifecycle()
        self.assertEqual(self._counted(), {"rust": chunks})

        state = self.get_success(
            self.store.get_partial_filtered_current_state_ids(self.room_id)
        )
        self.assertEqual(
            {key for key in state if key[0] == EventTypes.Member},
            {
                (EventTypes.Member, self.alice),
                (EventTypes.Member, self.bob),
                (EventTypes.Member, self.carol),
            },
        )
        membership = self.get_success(
            self.store.get_event(state[(EventTypes.Member, self.carol)])
        ).membership
        self.assertEqual(membership, "leave")

        # The sliding sync tables have the room's name and Carol's leave.
        (name,) = self.get_success(
            self.store.db_pool.simple_select_one(
                "sliding_sync_joined_rooms",
                {"room_id": self.room_id},
                ["room_name"],
            )
        )
        self.assertEqual(name, "The room")
        (carol_membership,) = self.get_success(
            self.store.db_pool.simple_select_one(
                "sliding_sync_membership_snapshots",
                {"room_id": self.room_id, "user_id": self.carol},
                ["membership"],
            )
        )
        self.assertEqual(carol_membership, "leave")

        # Every state event is in the chain cover index.
        missing = self.get_success(
            self.store.db_pool.execute(
                "test",
                """SELECT e.event_id FROM events AS e
                LEFT JOIN event_auth_chains AS c USING (event_id)
                WHERE e.room_id = ? AND e.state_key IS NOT NULL
                    AND c.event_id IS NULL""",
                self.room_id,
            )
        )
        self.assertEqual(missing, [])

    def test_fork(self) -> None:
        self._fork(self.room_id)
        self.assertEqual(self._counted(), {"rust": 2})
        # Rust resolved state, and its work was recorded.
        handler = self.hs.get_state_resolution_handler()
        self.assertIn(self.room_id, handler._state_res_metrics)
        state = self.get_success(
            self.store.get_partial_filtered_current_state_ids(self.room_id)
        )
        topic = self.get_success(self.store.get_event(state[(EventTypes.Topic, "")]))
        self.assertIn(topic.content["topic"], ("A", "B"))

    def test_state_res_v1_falls_back(self) -> None:
        room_id = self.helper.create_room_as(
            self.alice, room_version=RoomVersions.V1.identifier, tok=self.alice_tok
        )
        before = self._counted()
        self._fork(room_id)
        after = self._counted()
        # The first topic needs no resolution. The second does.
        self.assertEqual(after.get("rust", 0) - before.get("rust", 0), 1)
        self.assertEqual(after.get("fallback"), 1)

    def test_backfill(self) -> None:
        pair = self._new_event(
            self.room_id, EventTypes.Message, {"msgtype": "m.text", "body": "old"}
        )
        self.get_success(self.persistence.persist_events([pair], backfilled=True))
        self.assertEqual(self._counted(), {"rust": 1})
        stream_ordering = pair[0].internal_metadata.stream_ordering
        assert stream_ordering is not None
        self.assertLess(stream_ordering, 0)

    def test_rejection_is_raised(self) -> None:
        """A chunk Rust rejects is not computed again in Python."""
        self.persistence._rust_persist_strict = False
        self.persistence._rust_persister = Mock(
            prepare_chunk=Mock(return_value=defer.fail(SynapseError(500, "no")))
        )
        in_python = Mock(side_effect=AssertionError("computed in Python"))
        self.persistence._prepare_chunk_in_python = in_python  # type: ignore[method-assign]
        self._send("hello", expect_code=500)
        self.assertEqual(self._counted(), {"rust": 1})
        in_python.assert_not_called()

    def test_fallback(self) -> None:
        self.persistence._rust_persister = Mock(
            prepare_chunk=Mock(
                return_value=defer.succeed(Mock(state_res_fallback="no index"))
            )
        )
        event_id = self._send("hello")
        self.assertEqual(self._counted(), {"fallback": 1})
        self.assertEqual(
            self.get_success(self.store.get_latest_event_ids_in_room(self.room_id)),
            frozenset({event_id}),
        )

    def test_error_uses_python(self) -> None:
        self.persistence._rust_persist_strict = False
        self.persistence._rust_persister = Mock(
            prepare_chunk=Mock(return_value=defer.fail(RuntimeError("boom")))
        )
        with self.assertLogs("synapse.storage.controllers.persist_events", "ERROR"):
            event_id = self._send("hello")
        self.assertEqual(self._counted(), {"error": 1})
        self.assertEqual(
            self.get_success(self.store.get_latest_event_ids_in_room(self.room_id)),
            frozenset({event_id}),
        )

    def test_error_fails_strict_mode(self) -> None:
        self.persistence._rust_persister = Mock(
            prepare_chunk=Mock(return_value=defer.fail(RuntimeError("boom")))
        )
        self._send("hello", expect_code=500)
        self.assertEqual(self._counted(), {"error": 1})
