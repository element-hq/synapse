#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import override_config
from tests.utils import default_config

DEFAULT_SERVER_NOTICES_CONFIG = {
    "system_mxid_localpart": "notices",
    "system_mxid_display_name": "test display name",
    "system_mxid_avatar_url": None,
    "room_name": "Server Notices",
    "auto_join": False,
}


class ServerNoticesTests(unittest.HomeserverTestCase):
    servlets = [
        sync.register_servlets,
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = default_config("test")

        config.update({"server_notices": DEFAULT_SERVER_NOTICES_CONFIG})

        # apply any additional config which was specified via the override_config
        # decorator.
        if self._extra_config is not None:
            config.update(self._extra_config)

        return config

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self._admin_user_id = self.register_user(
            "server_notices_admin", "abc123", admin=True
        )
        self._admin_user_access_token = self.login("server_notices_admin", "abc123")

        self._test_user_id = self.register_user("server_notices_test_user", "abc123")
        self._test_user_access_token = self.login("server_notices_test_user", "abc123")

        self._server_notice_content = {
            "msgtype": "m.text",
            "formatted_body": "<p>Do the hussle.</p>",
            "body": "Do the hussle.",
            "format": "org.matrix.custom.html",
        }

    def _send_server_notice(
        self,
        admin_access_token: str,
        target_user_id: str,
        notice_content: JsonDict,
    ) -> None:
        # Send a server notice.
        channel = self.make_request(
            "POST",
            "/_synapse/admin/v1/send_server_notice",
            content={
                "user_id": target_user_id,
                "content": notice_content,
            },
            access_token=admin_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def _check_user_received_server_notice(
        self,
        target_user_id: str,
        target_access_token: str,
        expected_content: JsonDict,
        user_accepts_invite: bool,
    ) -> None:
        # Have the target user sync.
        channel = self.make_request(
            "GET", "/_matrix/client/v3/sync", access_token=target_access_token
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        sync_body = channel.json_body

        if user_accepts_invite:
            # Get the Room ID to join
            room_id = list(sync_body["rooms"]["invite"].keys())[0]

            # Join the room
            self.helper.join(room_id, target_user_id, tok=target_access_token)

        for _ in range(5):
            # Sync until we're joined to the room.
            channel = self.make_request(
                "GET", "/_matrix/client/v3/sync", access_token=target_access_token
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            sync_body = channel.json_body

            if "join" in sync_body["rooms"] and len(sync_body["rooms"]["join"]) > 0:
                # Retrieve the server notices message.
                room_id = list(sync_body["rooms"]["join"].keys())[0]
                room = sync_body["rooms"]["join"][room_id]
                messages = [
                    x
                    for x in room["timeline"]["events"]
                    if x["type"] == "m.room.message"
                ]
                break

            # Sleep and try again.
            self.get_success(self.clock.sleep(0.1))
        else:
            self.fail(
                f"Failed to join the server notices room. No 'join' field in sync_body['rooms']: {sync_body['rooms']}"
            )

        # Should be the expected server notices content.
        self.assertDictEqual(messages[-1]["content"], expected_content)

    def test_send_server_notice(self) -> None:
        """
        Test the happy path of sending a server notice to a user.
        """
        # Send a server notice. The server notice room does not yet exist.
        self._send_server_notice(
            self._admin_user_access_token,
            self._test_user_id,
            self._server_notice_content,
        )

        self._check_user_received_server_notice(
            self._test_user_id,
            self._test_user_access_token,
            self._server_notice_content,
            # User must accept the invite manually.
            True,
        )

        # Send another server notice. In this case, the room already exists.
        self._send_server_notice(
            self._admin_user_access_token,
            self._test_user_id,
            self._server_notice_content,
        )

        self._check_user_received_server_notice(
            self._test_user_id,
            self._test_user_access_token,
            self._server_notice_content,
            # User is already in the room, no need to join it.
            False,
        )

    @override_config(
        {
            "server_notices": {
                **DEFAULT_SERVER_NOTICES_CONFIG,
                "auto_join": True,
            }
        }
    )
    def test_send_server_notice_auto_join(self) -> None:
        """
        Test the happy path of sending a server notice to a user, with auto_join enabled.
        """
        # Send a server notice. The server notice room does not yet exist.
        self._send_server_notice(
            self._admin_user_access_token,
            self._test_user_id,
            self._server_notice_content,
        )

        self._check_user_received_server_notice(
            self._test_user_id,
            self._test_user_access_token,
            self._server_notice_content,
            # User does not need to join the room manually. They should be auto-joined.
            False,
        )

    @override_config(
        {
            "server_notices": {
                **DEFAULT_SERVER_NOTICES_CONFIG,
                "auto_join": True,
            }
        }
    )
    def test_send_server_notice_suspended_user_auto_join(self) -> None:
        """Test sending a server notice to a user that's suspended, with auto-join enabled.

        This is a regression test for https://github.com/element-hq/synapse/pull/18750, where
        previously the suspended user would not be allowed to join the server notices room.
        """
        # Suspend the target user.
        channel = self.make_request(
            "PUT",
            f"/_synapse/admin/v1/suspend/{self._test_user_id}",
            content={"suspend": True},
            access_token=self._admin_user_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Send a server notice. The server notices room will be created and the user auto-joined.
        self._send_server_notice(
            self._admin_user_access_token,
            self._test_user_id,
            self._server_notice_content,
        )

        self._check_user_received_server_notice(
            self._test_user_id,
            self._test_user_access_token,
            self._server_notice_content,
            # User does not need to join the room manually. They should be auto-joined.
            False,
        )
