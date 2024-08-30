#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
import logging

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import devices, login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, StreamKeyType
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import TimedOutException

logger = logging.getLogger(__name__)


class SlidingSyncE2eeExtensionTestCase(SlidingSyncBase):
    """Tests for the e2ee sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.e2e_keys_handler = hs.get_e2e_keys_handler()

    def test_no_data_initial_sync(self) -> None:
        """
        Test that enabling e2ee extension works during an intitial sync, even if there
        is no-data
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Make an initial Sliding Sync request with the e2ee extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "e2ee": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Device list updates are only present for incremental syncs
        self.assertIsNone(response_body["extensions"]["e2ee"].get("device_lists"))

        # Both of these should be present even when empty
        self.assertEqual(
            response_body["extensions"]["e2ee"]["device_one_time_keys_count"],
            {
                # This is always present because of
                # https://github.com/element-hq/element-android/issues/3725 and
                # https://github.com/matrix-org/synapse/issues/10456
                "signed_curve25519": 0
            },
        )
        self.assertEqual(
            response_body["extensions"]["e2ee"]["device_unused_fallback_key_types"],
            [],
        )

    def test_no_data_incremental_sync(self) -> None:
        """
        Test that enabling e2ee extension works during an incremental sync, even if
        there is no-data
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "e2ee": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the e2ee extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Device list shows up for incremental syncs
        self.assertEqual(
            response_body["extensions"]["e2ee"].get("device_lists", {}).get("changed"),
            [],
        )
        self.assertEqual(
            response_body["extensions"]["e2ee"].get("device_lists", {}).get("left"),
            [],
        )

        # Both of these should be present even when empty
        self.assertEqual(
            response_body["extensions"]["e2ee"]["device_one_time_keys_count"],
            {
                # Note that "signed_curve25519" is always returned in key count responses
                # regardless of whether we uploaded any keys for it. This is necessary until
                # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
                #
                # Also related:
                # https://github.com/element-hq/element-android/issues/3725 and
                # https://github.com/matrix-org/synapse/issues/10456
                "signed_curve25519": 0
            },
        )
        self.assertEqual(
            response_body["extensions"]["e2ee"]["device_unused_fallback_key_types"],
            [],
        )

    def test_wait_for_new_data(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive.

        (Only applies to incremental syncs with a `timeout` specified)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        test_device_id = "TESTDEVICE"
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass", device_id=test_device_id)

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)
        self.helper.join(room_id, user3_id, tok=user3_tok)

        sync_body = {
            "lists": {},
            "extensions": {
                "e2ee": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint + "?timeout=10000" + f"&pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Bump the device lists to trigger new results
        # Have user3 update their device list
        device_update_channel = self.make_request(
            "PUT",
            f"/devices/{test_device_id}",
            {
                "display_name": "New Device Name",
            },
            access_token=user3_tok,
        )
        self.assertEqual(
            device_update_channel.code, 200, device_update_channel.json_body
        )
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see the device list update
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]
            .get("device_lists", {})
            .get("changed"),
            [user3_id],
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"].get("device_lists", {}).get("left"),
            [],
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        from the E2EE extension doesn't trigger a false-positive for new data (see
        `device_one_time_keys_count.signed_curve25519`).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "e2ee": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?timeout=10000&pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Wake-up `notifier.wait_for_events(...)` that will cause us test
        # `SlidingSyncResult.__bool__` for new results.
        self._bump_notifier_wait_for_events(
            user1_id, wake_stream_key=StreamKeyType.ACCOUNT_DATA
        )
        # Block for a little bit more to ensure we don't see any new results.
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=4000)
        # Wait for the sync to complete (wait for the rest of the 10 second timeout,
        # 5000 + 4000 + 1200 > 10000)
        channel.await_result(timeout_ms=1200)
        self.assertEqual(channel.code, 200, channel.json_body)

        # Device lists are present for incremental syncs but empty because no device changes
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]
            .get("device_lists", {})
            .get("changed"),
            [],
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"].get("device_lists", {}).get("left"),
            [],
        )

        # Both of these should be present even when empty
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]["device_one_time_keys_count"],
            {
                # Note that "signed_curve25519" is always returned in key count responses
                # regardless of whether we uploaded any keys for it. This is necessary until
                # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
                #
                # Also related:
                # https://github.com/element-hq/element-android/issues/3725 and
                # https://github.com/matrix-org/synapse/issues/10456
                "signed_curve25519": 0
            },
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]["device_unused_fallback_key_types"],
            [],
        )

    def test_device_lists(self) -> None:
        """
        Test that device list updates are included in the response
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        test_device_id = "TESTDEVICE"
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass", device_id=test_device_id)

        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)
        self.helper.join(room_id, user3_id, tok=user3_tok)
        self.helper.join(room_id, user4_id, tok=user4_tok)

        sync_body = {
            "lists": {},
            "extensions": {
                "e2ee": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Have user3 update their device list
        channel = self.make_request(
            "PUT",
            f"/devices/{test_device_id}",
            {
                "display_name": "New Device Name",
            },
            access_token=user3_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # User4 leaves the room
        self.helper.leave(room_id, user4_id, tok=user4_tok)

        # Make an incremental Sliding Sync request with the e2ee extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Device list updates show up
        self.assertEqual(
            response_body["extensions"]["e2ee"].get("device_lists", {}).get("changed"),
            [user3_id],
        )
        self.assertEqual(
            response_body["extensions"]["e2ee"].get("device_lists", {}).get("left"),
            [user4_id],
        )

    def test_device_one_time_keys_count(self) -> None:
        """
        Test that `device_one_time_keys_count` are included in the response
        """
        test_device_id = "TESTDEVICE"
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", device_id=test_device_id)

        # Upload one time keys for the user/device
        keys: JsonDict = {
            "alg1:k1": "key1",
            "alg2:k2": {"key": "key2", "signatures": {"k1": "sig1"}},
            "alg2:k3": {"key": "key3"},
        }
        upload_keys_response = self.get_success(
            self.e2e_keys_handler.upload_keys_for_user(
                user1_id, test_device_id, {"one_time_keys": keys}
            )
        )
        self.assertDictEqual(
            upload_keys_response,
            {
                "one_time_key_counts": {
                    "alg1": 1,
                    "alg2": 2,
                    # Note that "signed_curve25519" is always returned in key count responses
                    # regardless of whether we uploaded any keys for it. This is necessary until
                    # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
                    #
                    # Also related:
                    # https://github.com/element-hq/element-android/issues/3725 and
                    # https://github.com/matrix-org/synapse/issues/10456
                    "signed_curve25519": 0,
                }
            },
        )

        # Make a Sliding Sync request with the e2ee extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "e2ee": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Check for those one time key counts
        self.assertEqual(
            response_body["extensions"]["e2ee"].get("device_one_time_keys_count"),
            {
                "alg1": 1,
                "alg2": 2,
                # Note that "signed_curve25519" is always returned in key count responses
                # regardless of whether we uploaded any keys for it. This is necessary until
                # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
                #
                # Also related:
                # https://github.com/element-hq/element-android/issues/3725 and
                # https://github.com/matrix-org/synapse/issues/10456
                "signed_curve25519": 0,
            },
        )

    def test_device_unused_fallback_key_types(self) -> None:
        """
        Test that `device_unused_fallback_key_types` are included in the response
        """
        test_device_id = "TESTDEVICE"
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", device_id=test_device_id)

        # We shouldn't have any unused fallback keys yet
        res = self.get_success(
            self.store.get_e2e_unused_fallback_key_types(user1_id, test_device_id)
        )
        self.assertEqual(res, [])

        # Upload a fallback key for the user/device
        self.get_success(
            self.e2e_keys_handler.upload_keys_for_user(
                user1_id,
                test_device_id,
                {"fallback_keys": {"alg1:k1": "fallback_key1"}},
            )
        )
        # We should now have an unused alg1 key
        fallback_res = self.get_success(
            self.store.get_e2e_unused_fallback_key_types(user1_id, test_device_id)
        )
        self.assertEqual(fallback_res, ["alg1"], fallback_res)

        # Make a Sliding Sync request with the e2ee extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "e2ee": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Check for the unused fallback key types
        self.assertListEqual(
            response_body["extensions"]["e2ee"].get("device_unused_fallback_key_types"),
            ["alg1"],
        )
