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

"""The Rust `filter_events_for_server` (`rust/src/visibility.rs`) against the
Python one, on the same rooms. These need the native Rust Postgres driver:

    SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.test_rust_server_visibility
"""

import itertools
import random
from typing import Any
from unittest import SkipTest

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.server import HomeServer
from synapse.storage.rust_pool import RustConnectionPool
from synapse.synapse_rust.sync import SyncStoreForTest
from synapse.util.clock import Clock
from synapse.visibility import filter_events_for_server

from tests import unittest
from tests.test_utils.event_injection import inject_member_event
from tests.test_visibility import inject_message_event, inject_visibility_event
from tests.utils import create_room

ROOM_ID = "!test:local"
LOCAL = "local"
SERVERS = ["a", "b", "c"]
VISIBILITIES = ["world_readable", "shared", "invited", "joined"]
MEMBERSHIPS = ["join", "invite", "leave", "ban", "knock"]

# Every combination of the three flags.
FLAGS = [
    {
        "redact": redact,
        "filter_out_erased_senders": erased,
        "filter_out_remote_partial_state_events": partial,
    }
    for redact, erased, partial in itertools.product((False, True), repeat=3)
]


class RustServerVisibilityTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage = hs.get_storage_controllers()
        db_pool = self.store.db_pool._db_pool
        assert isinstance(db_pool, RustConnectionPool)
        assert db_pool._pool is not None
        rust_handler = hs.get_rust_handlers().sync
        assert rust_handler is not None
        self.rust = SyncStoreForTest(db_pool._pool, rust_handler)

        self.get_success(create_room(hs, ROOM_ID, f"@creator:{LOCAL}"))

    def _compare(
        self, target: str, events: list[EventBase], flags: dict[str, Any]
    ) -> list[EventBase]:
        """Filter with both, check they agree, and return the result."""
        python = self.get_success(
            filter_events_for_server(self.storage, target, LOCAL, events, **flags)
        )
        rust = self.rust.filter_events_for_server(target, LOCAL, events, **flags)
        assert rust is not None, "Rust wanted to wait for the full state"
        self.assertEqual(
            [event.get_pdu_json() for event in rust],
            [event.get_pdu_json() for event in python],
            f"target={target} flags={flags}",
        )
        self.assertEqual(
            [event.internal_metadata.is_redacted() for event in rust],
            [event.internal_metadata.is_redacted() for event in python],
        )
        return python

    def _random_room(self, seed: int) -> list[EventBase]:
        """A room history of visibility changes, memberships of users on
        several servers and messages, in a random order."""
        rng = random.Random(seed)
        users = [f"@u{i}:{server}" for server in SERVERS + [LOCAL] for i in range(2)]
        events: list[EventBase] = []
        for _ in range(40):
            action = rng.random()
            if action < 0.15:
                event = self.get_success(
                    inject_visibility_event(
                        self.hs, ROOM_ID, rng.choice(users), rng.choice(VISIBILITIES)
                    )
                )
            elif action < 0.5:
                event = self.get_success(
                    inject_member_event(
                        self.hs, ROOM_ID, rng.choice(users), rng.choice(MEMBERSHIPS)
                    )
                )
            else:
                event = self.get_success(
                    inject_message_event(self.hs, ROOM_ID, rng.choice(users))
                )
            events.append(event)

        for user in rng.sample(users, 2):
            self.get_success(self.store.mark_user_erased(user))
        return events

    @parameterized.expand([(seed,) for seed in range(6)])
    def test_random_rooms(self, seed: int) -> None:
        events = self._random_room(seed)
        rng = random.Random(seed)
        outcomes = set()
        for target in SERVERS + ["nobody"]:
            batch = rng.sample(events, 15)
            for flags in FLAGS:
                filtered = self._compare(target, batch, flags)
                outcomes.add(
                    "omitted"
                    if len(filtered) < len(batch)
                    else "redacted"
                    if any(e.internal_metadata.is_redacted() for e in filtered)
                    else "all"
                )
        # The rooms are random, but each should exercise the filter.
        self.assertIn("omitted", outcomes)
        self.assertIn("redacted", outcomes)

    def test_outlier(self) -> None:
        """An outlier has no state, so any server may see it."""
        self.get_success(
            inject_visibility_event(self.hs, ROOM_ID, f"@creator:{LOCAL}", "joined")
        )
        message = self.get_success(
            inject_message_event(self.hs, ROOM_ID, f"@creator:{LOCAL}")
        )
        outlier = self._inject_outlier()
        for flags in FLAGS:
            filtered = self._compare("a", [outlier, message], flags)
            self.assertEqual(filtered[0].event_id, outlier.event_id)

    def test_partial_state_room(self) -> None:
        """Remote events in a room with partial state are left out, and an
        event that needs the memberships waits for the full state."""
        self.get_success(
            inject_visibility_event(self.hs, ROOM_ID, f"@creator:{LOCAL}", "joined")
        )
        self.get_success(inject_member_event(self.hs, ROOM_ID, "@u:a", "join"))
        remote = self.get_success(inject_message_event(self.hs, ROOM_ID, "@u:a"))
        local = self.get_success(
            inject_message_event(self.hs, ROOM_ID, f"@creator:{LOCAL}")
        )

        self.get_success(
            self.store.db_pool.simple_insert(
                "partial_state_rooms", {"room_id": ROOM_ID}
            )
        )
        self.store.is_partial_state_room.invalidate((ROOM_ID,))
        for flags in FLAGS:
            self._compare("a", [remote, local], flags)

        self.get_success(
            self.store.db_pool.simple_insert(
                "partial_state_events", {"room_id": ROOM_ID, "event_id": local.event_id}
            )
        )
        self.assertIsNone(
            self.rust.filter_events_for_server(
                "a",
                LOCAL,
                [local],
                redact=False,
                filter_out_erased_senders=True,
                filter_out_remote_partial_state_events=False,
            )
        )

    def _inject_outlier(self) -> EventBase:
        builder = self.hs.get_event_builder_factory().for_room_version(
            RoomVersions.V1,
            {
                "type": "m.room.member",
                "sender": "@test:user",
                "state_key": "@test:user",
                "room_id": ROOM_ID,
                "content": {"membership": "join"},
            },
        )
        event = self.get_success(builder.build(prev_event_ids=[], auth_event_ids=[]))
        event.internal_metadata.outlier = True
        persistence = self.storage.persistence
        assert persistence is not None
        self.get_success(
            persistence.persist_event(event, EventContext.for_outlier(self.storage))
        )
        return event
