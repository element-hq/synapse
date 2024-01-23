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

        user_id = self.register_user("user", "pass")
        access_token = self.login("user", "pass")
        other_user_id = self.register_user("otheruser", "pass")
        other_access_token = self.login("otheruser", "pass")

        # Create a room
        room = self.helper.create_room_as(user_id, tok=access_token)

        # Check we start with no pushes
        channel = self.make_request(
            "GET",
            "/notifications",
            access_token=other_access_token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(len(channel.json_body["notifications"]), 0, channel.json_body)

        # Send an invite
        self.helper.invite(room=room, src=user_id, targ=other_user_id, tok=access_token)

        # We should have a notification now
        channel = self.make_request(
            "GET",
            "/notifications",
            access_token=other_access_token,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(len(channel.json_body["notifications"]), 1, channel.json_body)
        self.assertEqual(
            channel.json_body["notifications"][0]["event"]["content"]["membership"],
            "invite",
            channel.json_body,
        )
