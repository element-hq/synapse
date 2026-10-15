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

"""The Rust federation store (`rust/src/storage/federation_store.rs`) against
the Python store methods it ports, on the same database. These need the native
Rust Postgres driver:

    SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.storage.test_federation_store
"""

from typing import Any
from unittest import SkipTest

from signedjson.key import (
    encode_verify_key_base64,
    generate_signing_key,
    get_verify_key,
)

from twisted.internet.testing import MemoryReactor

from synapse.handlers.e2e_keys import SignatureListItem
from synapse.server import HomeServer
from synapse.storage.rust_pool import RustConnectionPool
from synapse.synapse_rust.federation import FederationStoreForTest
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest

DEST = "remote.example.com"
OTHER = "other.example.com"
ROOM = "!room:test"


def _sorted_edus(edus: list[tuple[str, JsonDict]]) -> list[tuple[str, JsonDict]]:
    """The EDUs in a fixed order. Python orders the users as the database
    returns them, and Rust by their first update."""
    return sorted(
        edus,
        key=lambda edu: (edu[0], edu[1]["user_id"], edu[1].get("stream_id", 0)),
    )


class FederationStoreTestCase(unittest.HomeserverTestCase):
    """Each test runs a Rust query and the Python store method against the
    same fixture and compares."""

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        # `get_device_updates_by_remote` only works on a federation sender.
        config["federation_sender_instances"] = ["master"]
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        db_pool = self.store.db_pool._db_pool
        assert isinstance(db_pool, RustConnectionPool)
        assert db_pool._pool is not None
        self.rust = FederationStoreForTest(db_pool._pool, False)
        self.rust_with_names = FederationStoreForTest(db_pool._pool, True)

    # To-device messages.

    def _to_device_edu(self, n: int) -> JsonDict:
        return {
            "sender": "@alice:test",
            "type": "m.test",
            "message_id": str(n),
            "messages": {"@u:remote": {"D": {"n": n}}},
        }

    def _queue_to_device(self, destination: str, n: int) -> None:
        self.get_success(
            self.store.add_remote_messages_from_client_to_device_inbox(
                {destination: self._to_device_edu(n)}
            )
        )

    def test_new_device_msgs_for_remote(self) -> None:
        for n in range(3):
            self._queue_to_device(DEST, n)
            self._queue_to_device(OTHER, n)
        current = self.store.get_to_device_stream_token()

        for last in (0, current - 3, current):
            for limit in (0, 1, 2, 10):
                self.assertEqual(
                    self.rust.get_new_device_msgs_for_remote(
                        DEST, last, current, limit
                    ),
                    self.get_success(
                        self.store.get_new_device_msgs_for_remote(
                            DEST, last, current, limit
                        )
                    ),
                    (last, limit),
                )

    def test_delete_device_msgs_for_remote(self) -> None:
        for n in range(3):
            self._queue_to_device(DEST, n)
        self._queue_to_device(OTHER, 0)
        current = self.store.get_to_device_stream_token()

        self.rust.delete_device_msgs_for_remote(DEST, current - 2)

        messages, _ = self.get_success(
            self.store.get_new_device_msgs_for_remote(DEST, 0, current, 10)
        )
        self.assertEqual(messages, [self._to_device_edu(2)])
        messages, _ = self.get_success(
            self.store.get_new_device_msgs_for_remote(OTHER, 0, current, 10)
        )
        self.assertEqual(len(messages), 1)

    # Device list updates.

    def _poke(self, user_id: str, device_id: str, host: str = DEST) -> None:
        self.get_success(
            self.store.add_device_change_to_streams(user_id, [device_id], [ROOM])
        )
        self.get_success(
            self.store.add_device_list_outbound_pokes(
                user_id=user_id,
                device_id=device_id,
                room_id=ROOM,
                hosts=[host],
                context={},
            )
        )

    def _cross_signing_key(self, user_id: str, key_type: str) -> str:
        """Give the user a cross-signing key, and return its device ID."""
        public = encode_verify_key_base64(get_verify_key(generate_signing_key("x")))
        key = {
            "user_id": user_id,
            "usage": [key_type],
            "keys": {f"ed25519:{public}": public},
        }
        self.get_success(self.store.set_e2e_cross_signing_key(user_id, key_type, key))
        return public

    def _device_keys(self, user_id: str, device_id: str) -> None:
        self.get_success(
            self.store.set_e2e_device_keys(
                user_id,
                device_id,
                self.clock.time_msec(),
                {
                    "user_id": user_id,
                    "device_id": device_id,
                    "algorithms": ["m.olm.v1.curve25519-aes-sha2"],
                    "keys": {f"ed25519:{device_id}": "key"},
                    "signatures": {user_id: {f"ed25519:{device_id}": "self"}},
                },
            )
        )

    def _device_list_fixture(self) -> None:
        """Pokes to DEST for: devices with and without keys and names, a
        device's repeated update, a deleted device, a hidden device, a
        cross-signed device, and both kinds of cross-signing key update. And
        one poke for another destination."""
        alice = "@alice:test"
        bob = "@bob:test"

        self.get_success(self.store.store_device(alice, "A1", "Alice's phone"))
        self.get_success(self.store.store_device(alice, "A2", None))
        self.get_success(self.store.store_device(bob, "B1", "Bob's laptop"))
        self._device_keys(alice, "A1")
        self._device_keys(bob, "B1")

        master = self._cross_signing_key(alice, "master")
        self_signing = self._cross_signing_key(alice, "self_signing")
        self.get_success(
            self.store.store_e2e_cross_signing_signatures(
                alice,
                [
                    SignatureListItem(
                        signing_key_id=f"ed25519:{self_signing}",
                        target_user_id=alice,
                        target_device_id="A1",
                        signature="cross",  # type: ignore[arg-type]
                    )
                ],
            )
        )

        self._poke(alice, "A1")
        self._poke(bob, "B1")
        self._poke(alice, "A2")
        self._poke(alice, master)
        self._poke(alice, "A1")
        self._poke(bob, "GONE")
        self._poke(alice, self_signing)
        self._poke(bob, "B1", host=OTHER)

        # A hidden device is sent as deleted.
        self.get_success(self.store.store_device(bob, "HIDDEN", None))
        self.get_success(
            self.store.db_pool.simple_update_one(
                "devices", {"user_id": bob, "device_id": "HIDDEN"}, {"hidden": True}
            )
        )
        self._poke(bob, "HIDDEN")

    def _assert_device_updates(self, from_stream_id: int, limit: int) -> None:
        now = self.store.get_device_stream_token().stream
        python = self.get_success(
            self.store.get_device_updates_by_remote(DEST, from_stream_id, limit)
        )
        for rust, names in ((self.rust, False), (self.rust_with_names, True)):
            self.hs.config.federation.allow_device_name_lookup_over_federation = names
            python = self.get_success(
                self.store.get_device_updates_by_remote(DEST, from_stream_id, limit)
            )
            stream_id, edus = rust.get_device_updates_by_remote(
                DEST, from_stream_id, now, limit
            )
            self.assertEqual(stream_id, python[0], (from_stream_id, limit, names))
            self.assertEqual(
                _sorted_edus(edus),
                _sorted_edus(python[1]),
                (from_stream_id, limit, names),
            )

    def test_device_updates_by_remote(self) -> None:
        self._device_list_fixture()
        for limit in (1, 2, 3, 100):
            self._assert_device_updates(0, limit)

        # The fixture has every kind of update.
        _, edus = self.rust_with_names.get_device_updates_by_remote(
            DEST, 0, self.store.get_device_stream_token().stream, 100
        )
        types = [edu_type for edu_type, _ in edus]
        self.assertEqual(types.count("m.signing_key_update"), 1)
        self.assertEqual(types.count("org.matrix.signing_key_update"), 1)
        updates = {
            content["device_id"]: content
            for edu_type, content in edus
            if edu_type == "m.device_list_update"
        }
        self.assertEqual(set(updates), {"A1", "A2", "B1", "GONE", "HIDDEN"})
        self.assertEqual(updates["A1"]["device_display_name"], "Alice's phone")
        self.assertIn("cross", str(updates["A1"]["keys"]["signatures"]))
        self.assertNotIn("keys", updates["A2"])
        self.assertTrue(updates["GONE"]["deleted"])
        self.assertTrue(updates["HIDDEN"]["deleted"])
        # A1 was updated again after A2, so it follows A2.
        self.assertEqual(updates["A1"]["prev_id"], [updates["A2"]["stream_id"]])

    def test_device_updates_by_remote_paginates(self) -> None:
        """Walk the stream a few updates at a time, marking each batch sent,
        so the prev IDs come from the last batch."""
        self._device_list_fixture()
        position = 0
        for _ in range(10):
            self._assert_device_updates(position, 3)
            stream_id, edus = self.rust.get_device_updates_by_remote(
                DEST, position, self.store.get_device_stream_token().stream, 3
            )
            if not edus:
                break
            self.rust.mark_as_sent_devices_by_remote(DEST, stream_id)
            position = stream_id
        else:
            self.fail("never caught up")

    def test_mark_as_sent_devices_by_remote(self) -> None:
        """The pokes sent are deleted, and each user's last one recorded."""
        self._device_list_fixture()
        stream_id, _ = self.get_success(
            self.store.get_device_updates_by_remote(DEST, 0, limit=4)
        )
        pokes = self.get_success(
            self.store.db_pool.simple_select_list(
                "device_lists_outbound_pokes",
                {"destination": DEST},
                ["user_id", "stream_id"],
            )
        )
        expected: dict[str, int] = {}
        for user_id, poke_stream_id in pokes:
            if poke_stream_id <= stream_id:
                expected[user_id] = max(expected.get(user_id, 0), poke_stream_id)

        self.rust.mark_as_sent_devices_by_remote(DEST, stream_id)

        remaining = self.get_success(
            self.store.db_pool.simple_select_list(
                "device_lists_outbound_pokes",
                {"destination": DEST},
                ["stream_id"],
            )
        )
        self.assertTrue(remaining)
        self.assertTrue(all(row[0] > stream_id for row in remaining))
        last_success = self.get_success(
            self.store.db_pool.simple_select_list(
                "device_lists_outbound_last_success",
                {"destination": DEST},
                ["user_id", "stream_id"],
            )
        )
        self.assertEqual(dict(last_success), expected)
        # Another destination's pokes are left alone.
        other = self.get_success(
            self.store.db_pool.simple_select_list(
                "device_lists_outbound_pokes", {"destination": OTHER}, ["stream_id"]
            )
        )
        self.assertEqual(len(other), 1)

        # Marking again moves a user's last success on.
        self.rust.mark_as_sent_devices_by_remote(
            DEST, self.store.get_device_stream_token().stream
        )
        last_success = self.get_success(
            self.store.db_pool.simple_select_list(
                "device_lists_outbound_last_success",
                {"destination": DEST},
                ["user_id", "stream_id"],
            )
        )
        self.assertEqual(
            {user_id for user_id, _ in last_success}, {"@alice:test", "@bob:test"}
        )
        self.assertTrue(
            all(stream > expected.get(user_id, 0) for user_id, stream in last_success)
        )

    def test_set_destination_last_successful_stream_ordering(self) -> None:
        self.rust.set_destination_last_successful_stream_ordering(DEST, 5)
        self.assertEqual(
            self.get_success(
                self.store.get_destination_last_successful_stream_ordering(DEST)
            ),
            5,
        )
        self.rust.set_destination_last_successful_stream_ordering(DEST, 7)
        self.assertEqual(
            self.get_success(
                self.store.get_destination_last_successful_stream_ordering(DEST)
            ),
            7,
        )
