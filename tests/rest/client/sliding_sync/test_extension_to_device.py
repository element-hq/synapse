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
from typing import List

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import login, sendtodevice, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, StreamKeyType
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import TimedOutException

logger = logging.getLogger(__name__)


class SlidingSyncToDeviceExtensionTestCase(SlidingSyncBase):
    """Tests for the to-device sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
        sendtodevice.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def _assert_to_device_response(
        self, response_body: JsonDict, expected_messages: List[JsonDict]
    ) -> str:
        """Assert the sliding sync response was successful and has the expected
        to-device messages.

        Returns the next_batch token from the to-device section.
        """
        extensions = response_body["extensions"]
        to_device = extensions["to_device"]
        self.assertIsInstance(to_device["next_batch"], str)
        self.assertEqual(to_device["events"], expected_messages)

        return to_device["next_batch"]

    def test_no_data(self) -> None:
        """Test that enabling to-device extension works, even if there is
        no-data
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "to_device": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # We expect no to-device messages
        self._assert_to_device_response(response_body, [])

    def test_data_initial_sync(self) -> None:
        """Test that we get to-device messages when we don't specify a since
        token"""

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", "d1")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass", "d2")

        # Send the to-device message
        test_msg = {"foo": "bar"}
        chan = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/1234",
            content={"messages": {user1_id: {"d1": test_msg}}},
            access_token=user2_tok,
        )
        self.assertEqual(chan.code, 200, chan.result)

        sync_body = {
            "lists": {},
            "extensions": {
                "to_device": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_to_device_response(
            response_body,
            [{"content": test_msg, "sender": user2_id, "type": "m.test"}],
        )

    def test_data_incremental_sync(self) -> None:
        """Test that we get to-device messages over incremental syncs"""

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", "d1")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass", "d2")

        sync_body: JsonDict = {
            "lists": {},
            "extensions": {
                "to_device": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # No to-device messages yet.
        next_batch = self._assert_to_device_response(response_body, [])

        test_msg = {"foo": "bar"}
        chan = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/1234",
            content={"messages": {user1_id: {"d1": test_msg}}},
            access_token=user2_tok,
        )
        self.assertEqual(chan.code, 200, chan.result)

        sync_body = {
            "lists": {},
            "extensions": {
                "to_device": {
                    "enabled": True,
                    "since": next_batch,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        next_batch = self._assert_to_device_response(
            response_body,
            [{"content": test_msg, "sender": user2_id, "type": "m.test"}],
        )

        # The next sliding sync request should not include the to-device
        # message.
        sync_body = {
            "lists": {},
            "extensions": {
                "to_device": {
                    "enabled": True,
                    "since": next_batch,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_to_device_response(response_body, [])

        # An initial sliding sync request should not include the to-device
        # message, as it should have been deleted
        sync_body = {
            "lists": {},
            "extensions": {
                "to_device": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_to_device_response(response_body, [])

    def test_wait_for_new_data(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive.

        (Only applies to incremental syncs with a `timeout` specified)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", "d1")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass", "d2")

        sync_body = {
            "lists": {},
            "extensions": {
                "to_device": {
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
        # Bump the to-device messages to trigger new results
        test_msg = {"foo": "bar"}
        send_to_device_channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/1234",
            content={"messages": {user1_id: {"d1": test_msg}}},
            access_token=user2_tok,
        )
        self.assertEqual(
            send_to_device_channel.code, 200, send_to_device_channel.result
        )
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        self._assert_to_device_response(
            channel.json_body,
            [{"content": test_msg, "sender": user2_id, "type": "m.test"}],
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        from the To-Device extension doesn't trigger a false-positive for new data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "to_device": {
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

        self._assert_to_device_response(channel.json_body, [])
