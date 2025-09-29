from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.errors import Codes
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest


class FetchEventTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.other_user_tok = self.login("user", "pass")

        self.room_id1 = self.helper.create_room_as(
            self.other_user, tok=self.other_user_tok, is_public=True
        )
        resp = self.helper.send(self.room_id1, body="Hey now", tok=self.other_user_tok)
        self.event_id = resp["event_id"]

    def test_no_auth(self) -> None:
        """
        Try to get an event without authentication.
        """
        channel = self.make_request(
            "GET",
            f"/_synapse/admin/v1/fetch_event/{self.event_id}",
        )

        self.assertEqual(401, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_not_admin(self) -> None:
        """
        If the user is not a server admin, an error 403 is returned.
        """

        channel = self.make_request(
            "GET",
            f"/_synapse/admin/v1/fetch_event/{self.event_id}",
            access_token=self.other_user_tok,
        )

        self.assertEqual(403, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_fetch_event(self) -> None:
        """
        Test that we can successfully fetch an event
        """
        channel = self.make_request(
            "GET",
            f"/_synapse/admin/v1/fetch_event/{self.event_id}",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            channel.json_body["event"]["content"],
            {"body": "Hey now", "msgtype": "m.text"},
        )
        self.assertEqual(channel.json_body["event"]["event_id"], self.event_id)
        self.assertEqual(channel.json_body["event"]["type"], "m.room.message")
        self.assertEqual(channel.json_body["event"]["sender"], self.other_user)
