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
from synapse.api.constants import AccountDataTypes
from synapse.rest.client import login, room, sendtodevice, sync
from synapse.server import HomeServer
from synapse.types import StreamKeyType
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import TimedOutException

logger = logging.getLogger(__name__)


class SlidingSyncAccountDataExtensionTestCase(SlidingSyncBase):
    """Tests for the account_data sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        sendtodevice.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.account_data_handler = hs.get_account_data_handler()

    def test_no_data_initial_sync(self) -> None:
        """
        Test that enabling the account_data extension works during an intitial sync,
        even if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Make an initial Sliding Sync request with the account_data extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        self.assertIncludes(
            {
                global_event["type"]
                for global_event in response_body["extensions"]["account_data"].get(
                    "global"
                )
            },
            # Even though we don't have any global account data set, Synapse saves some
            # default push rules for us.
            {AccountDataTypes.PUSH_RULES},
            exact=True,
        )
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_no_data_incremental_sync(self) -> None:
        """
        Test that enabling account_data extension works during an incremental sync, even
        if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the account_data extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # There has been no account data changes since the `from_token` so we shouldn't
        # see any account data here.
        self.assertIncludes(
            {
                global_event["type"]
                for global_event in response_body["extensions"]["account_data"].get(
                    "global"
                )
            },
            set(),
            exact=True,
        )
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_global_account_data_initial_sync(self) -> None:
        """
        On initial sync, we should return all global account data on initial sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Update the global account data
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user_id=user1_id,
                account_data_type="org.matrix.foobarbaz",
                content={"foo": "bar"},
            )
        )

        # Make an initial Sliding Sync request with the account_data extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # It should show us all of the global account data
        self.assertIncludes(
            {
                global_event["type"]
                for global_event in response_body["extensions"]["account_data"].get(
                    "global"
                )
            },
            {AccountDataTypes.PUSH_RULES, "org.matrix.foobarbaz"},
            exact=True,
        )
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_global_account_data_incremental_sync(self) -> None:
        """
        On incremental sync, we should only account data that has changed since the
        `from_token`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Add some global account data
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user_id=user1_id,
                account_data_type="org.matrix.foobarbaz",
                content={"foo": "bar"},
            )
        )

        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Add some other global account data
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user_id=user1_id,
                account_data_type="org.matrix.doodardaz",
                content={"doo": "dar"},
            )
        )

        # Make an incremental Sliding Sync request with the account_data extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertIncludes(
            {
                global_event["type"]
                for global_event in response_body["extensions"]["account_data"].get(
                    "global"
                )
            },
            # We should only see the new global account data that happened after the `from_token`
            {"org.matrix.doodardaz"},
            exact=True,
        )
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_room_account_data_initial_sync(self) -> None:
        """
        On initial sync, we return all account data for a given room but only for
        rooms that we request and are being returned in the Sliding Sync response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room and add some room account data
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )

        # Create another room with some room account data
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )

        # Make an initial Sliding Sync request with the account_data extension enabled
        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                "account_data": {
                    "enabled": True,
                    "rooms": [room_id1, room_id2],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        self.assertIsNotNone(response_body["extensions"]["account_data"].get("global"))
        # Even though we requested room2, we only expect room1 to show up because that's
        # the only room in the Sliding Sync response (room2 is not one of our room
        # subscriptions or in a sliding window list).
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id1},
            exact=True,
        )
        self.assertIncludes(
            {
                event["type"]
                for event in response_body["extensions"]["account_data"]
                .get("rooms")
                .get(room_id1)
            },
            {"org.matrix.roorarraz"},
            exact=True,
        )

    def test_room_account_data_incremental_sync(self) -> None:
        """
        On incremental sync, we return all account data for a given room but only for
        rooms that we request and are being returned in the Sliding Sync response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room and add some room account data
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )

        # Create another room with some room account data
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )

        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                "account_data": {
                    "enabled": True,
                    "rooms": [room_id1, room_id2],
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Add some other room account data
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz2",
                content={"roo": "rar"},
            )
        )
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz2",
                content={"roo": "rar"},
            )
        )

        # Make an incremental Sliding Sync request with the account_data extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertIsNotNone(response_body["extensions"]["account_data"].get("global"))
        # Even though we requested room2, we only expect room1 to show up because that's
        # the only room in the Sliding Sync response (room2 is not one of our room
        # subscriptions or in a sliding window list).
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id1},
            exact=True,
        )
        # We should only see the new room account data that happened after the `from_token`
        self.assertIncludes(
            {
                event["type"]
                for event in response_body["extensions"]["account_data"]
                .get("rooms")
                .get(room_id1)
            },
            {"org.matrix.roorarraz2"},
            exact=True,
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

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the account_data extension enabled
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
        # Bump the global account data to trigger new results
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user1_id,
                "org.matrix.foobarbaz",
                {"foo": "bar"},
            )
        )
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see the global account data update
        self.assertIncludes(
            {
                global_event["type"]
                for global_event in channel.json_body["extensions"]["account_data"].get(
                    "global"
                )
            },
            {"org.matrix.foobarbaz"},
            exact=True,
        )
        self.assertIncludes(
            channel.json_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        from the account_data extension doesn't trigger a false-positive for new data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
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
            user1_id,
            # We choose `StreamKeyType.PRESENCE` because we're testing for account data
            # and don't want to contaminate the account data results using
            # `StreamKeyType.ACCOUNT_DATA`.
            wake_stream_key=StreamKeyType.PRESENCE,
        )
        # Block for a little bit more to ensure we don't see any new results.
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=4000)
        # Wait for the sync to complete (wait for the rest of the 10 second timeout,
        # 5000 + 4000 + 1200 > 10000)
        channel.await_result(timeout_ms=1200)
        self.assertEqual(channel.code, 200, channel.json_body)

        self.assertIsNotNone(
            channel.json_body["extensions"]["account_data"].get("global")
        )
        self.assertIsNotNone(
            channel.json_body["extensions"]["account_data"].get("rooms")
        )
