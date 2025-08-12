#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, Mock

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import login, notifications, receipts, room
from synapse.server import HomeServer
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class HTTPPusherTests(HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
        receipts.register_servlets,
        notifications.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main
        self.module_api = homeserver.get_module_api()
        self.event_creation_handler = homeserver.get_event_creation_handler()
        self.sync_handler = homeserver.get_sync_handler()
        self.auth_handler = homeserver.get_auth_handler()

        self.user_id = self.register_user("user", "pass")
        self.access_token = self.login("user", "pass")
        self.other_user_id = self.register_user("otheruser", "pass")
        self.other_access_token = self.login("otheruser", "pass")

        # Create a room
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.access_token)

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # Mock out the calls over federation.
        fed_transport_client = Mock(spec=["send_transaction"])
        fed_transport_client.send_transaction = AsyncMock(return_value={})

        return self.setup_test_homeserver(
            federation_transport_client=fed_transport_client,
        )

    def test_notify_for_local_invites(self) -> None:
        """
        Local users will get notified for invites
        """
        # Check we start with no pushes
        self._request_notifications(from_token=None, limit=1, expected_count=0)

        # Send an invite
        self.helper.invite(
            room=self.room_id,
            src=self.user_id,
            targ=self.other_user_id,
            tok=self.access_token,
        )

        # We should have a notification now
        channel = self.make_request(
            "GET",
            "/notifications",
            access_token=self.other_access_token,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(len(channel.json_body["notifications"]), 1, channel.json_body)
        self.assertEqual(
            channel.json_body["notifications"][0]["event"]["content"]["membership"],
            "invite",
            channel.json_body,
        )

    def test_pagination_of_notifications(self) -> None:
        """
        Check that pagination of notifications works.
        """
        # Check we start with no pushes
        self._request_notifications(from_token=None, limit=1, expected_count=0)

        # Send an invite and have the other user join the room.
        self.helper.invite(
            room=self.room_id,
            src=self.user_id,
            targ=self.other_user_id,
            tok=self.access_token,
        )
        self.helper.join(self.room_id, self.other_user_id, tok=self.other_access_token)

        # Send 5 messages in the room and note down their event IDs.
        sent_event_ids = []
        for _ in range(5):
            resp = self.helper.send_event(
                self.room_id,
                "m.room.message",
                {"body": "honk", "msgtype": "m.text"},
                tok=self.access_token,
            )
            sent_event_ids.append(resp["event_id"])

        # We expect to get notifications for messages in reverse order.
        # So reverse this list of event IDs to make it easier to compare
        # against later.
        sent_event_ids.reverse()

        # We should have a few notifications now. Let's try and fetch the first 2.
        notification_event_ids, _ = self._request_notifications(
            from_token=None, limit=2, expected_count=2
        )

        # Check we got the expected event IDs back.
        self.assertEqual(notification_event_ids, sent_event_ids[:2])

        # Try requesting again without a 'from' query parameter. We should get the
        # same two notifications back.
        notification_event_ids, next_token = self._request_notifications(
            from_token=None, limit=2, expected_count=2
        )
        self.assertEqual(notification_event_ids, sent_event_ids[:2])

        # Ask for the next 5 notifications, though there should only be
        # 4 remaining; the next 3 messages and the invite.
        #
        # We need to use the "next_token" from the response as the "from"
        # query parameter in the next request in order to paginate.
        notification_event_ids, next_token = self._request_notifications(
            from_token=next_token, limit=5, expected_count=4
        )
        # Ensure we chop off the invite on the end.
        notification_event_ids = notification_event_ids[:-1]
        self.assertEqual(notification_event_ids, sent_event_ids[2:])

    def _request_notifications(
        self, from_token: Optional[str], limit: int, expected_count: int
    ) -> Tuple[List[str], str]:
        """
        Make a request to /notifications to get the latest events to be notified about.

        Only the event IDs are returned. The request is made by the "other user".

        Args:
            from_token: An optional starting parameter.
            limit: The maximum number of results to return.
            expected_count: The number of events to expect in the response.

        Returns:
            A list of event IDs that the client should be notified about.
            Events are returned newest-first.
        """
        # Construct the request path.
        path = f"/notifications?limit={limit}"
        if from_token is not None:
            path += f"&from={from_token}"

        channel = self.make_request(
            "GET",
            path,
            access_token=self.other_access_token,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            len(channel.json_body["notifications"]), expected_count, channel.json_body
        )

        # Extract the necessary data from the response.
        next_token = channel.json_body["next_token"]
        event_ids = [
            event["event"]["event_id"] for event in channel.json_body["notifications"]
        ]

        return event_ids, next_token

    def test_parameters(self) -> None:
        """
        Test that appropriate errors are returned when query parameters are malformed.
        """
        # Test that no parameters are required.
        channel = self.make_request(
            "GET",
            "/notifications",
            access_token=self.other_access_token,
        )
        self.assertEqual(channel.code, 200)

        # Test that limit cannot be negative
        channel = self.make_request(
            "GET",
            "/notifications?limit=-1",
            access_token=self.other_access_token,
        )
        self.assertEqual(channel.code, 400)

        # Test that the 'limit' parameter must be an integer.
        channel = self.make_request(
            "GET",
            "/notifications?limit=foobar",
            access_token=self.other_access_token,
        )
        self.assertEqual(channel.code, 400)

        # Test that the 'from' parameter must be an integer.
        channel = self.make_request(
            "GET",
            "/notifications?from=osborne",
            access_token=self.other_access_token,
        )
        self.assertEqual(channel.code, 400)
