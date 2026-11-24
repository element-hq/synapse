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

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.errors import Codes
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest


class ServerAnnouncementsTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.handler = hs.get_server_announcements_handler()
        self.config = hs.config.serverannouncements
        self.creator = self.config.room_management_mxid

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.other_user_tok = self.login("user", "pass")

        self.sender = self.register_user("sender", "pass")

        self.general = ServerAnnouncementRoom(
            config_id="general",
            room_name="Server announcements",
            room_topic="Planned maintenance and incidents",
            room_avatar_url="mxc://test/avatar",
            room_avatar_path=None,
            allowed_user_ids=[self.sender],
        )
        self.maintenance = ServerAnnouncementRoom(
            config_id="maintenance",
            room_name="Maintenance",
            room_topic=None,
            room_avatar_url=None,
            room_avatar_path=None,
            allowed_user_ids=[],
            excluded_user_ids=[self.other_user],
        )
        self.config.rooms = [self.general, self.maintenance]

    def _reconcile(self) -> dict[str, str]:
        self.get_success(self.handler.reconcile_rooms())
        room_ids = {}
        for config_id in ("general", "maintenance"):
            room_id = self.get_success(
                self.store.get_server_announcement_room(config_id)
            )
            assert room_id is not None
            room_ids[config_id] = room_id
        return room_ids

    def _expected(self, room: ServerAnnouncementRoom, room_id: str | None) -> JsonDict:
        return {
            "id": room.config_id,
            "room_id": room_id,
            "room_name": room.room_name,
            "room_topic": room.room_topic,
            "room_avatar_url": room.room_avatar_url,
            "allowed_user_ids": room.allowed_user_ids,
            "excluded_user_ids": room.excluded_user_ids,
        }

    def test_get_no_auth(self) -> None:
        for url in (
            "/_synapse/admin/v1/server_announcements",
            "/_synapse/admin/v1/server_announcements/general",
        ):
            channel = self.make_request("GET", url)
            self.assertEqual(401, channel.code, msg=channel.json_body)
            self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_get_requester_is_not_admin(self) -> None:
        for url in (
            "/_synapse/admin/v1/server_announcements",
            "/_synapse/admin/v1/server_announcements/general",
        ):
            channel = self.make_request("GET", url, access_token=self.other_user_tok)
            self.assertEqual(403, channel.code, msg=channel.json_body)
            self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_list_not_configured(self) -> None:
        """An unconfigured feature gives an empty list, not an error."""
        self.config.rooms = []
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/server_announcements",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            channel.json_body, {"management_user_id": self.creator, "rooms": []}
        )

    def test_list(self) -> None:
        room_ids = self._reconcile()
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/server_announcements",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            channel.json_body,
            {
                "management_user_id": self.creator,
                "rooms": [
                    self._expected(self.general, room_ids["general"]),
                    self._expected(self.maintenance, room_ids["maintenance"]),
                ],
            },
        )

    def test_list_before_reconcile(self) -> None:
        """A configured room that has not been created yet has no room ID."""
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/server_announcements",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            [room["room_id"] for room in channel.json_body["rooms"]], [None, None]
        )

    def test_get_single(self) -> None:
        room_ids = self._reconcile()
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/server_announcements/maintenance",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            channel.json_body,
            self._expected(self.maintenance, room_ids["maintenance"]),
        )

    def test_get_single_unknown(self) -> None:
        self._reconcile()
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/server_announcements/unknown",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    def test_get_removed_from_config(self) -> None:
        """A room whose id left the config is neither listed nor found."""
        room_ids = self._reconcile()
        self.config.rooms = [self.general]

        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/server_announcements",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            channel.json_body["rooms"],
            [self._expected(self.general, room_ids["general"])],
        )

        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/server_announcements/maintenance",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    def _send(
        self,
        content: JsonDict,
        config_id: str = "general",
        access_token: str | None = None,
        expected_code: int = 200,
    ) -> JsonDict:
        channel = self.make_request(
            "POST",
            f"/_synapse/admin/v1/server_announcements/{config_id}/send",
            content=content,
            access_token=access_token or self.admin_user_tok,
        )
        self.assertEqual(expected_code, channel.code, msg=channel.json_body)
        return channel.json_body

    def test_send_no_auth(self) -> None:
        channel = self.make_request(
            "POST", "/_synapse/admin/v1/server_announcements/general/send"
        )
        self.assertEqual(401, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_send_requester_is_not_admin(self) -> None:
        body = self._send(
            {"content": {"msgtype": "m.text", "body": "x"}},
            access_token=self.other_user_tok,
            expected_code=403,
        )
        self.assertEqual(Codes.FORBIDDEN, body["errcode"])

    def test_send_not_enabled(self) -> None:
        self.config.rooms = []
        body = self._send(
            {"content": {"msgtype": "m.text", "body": "x"}}, expected_code=400
        )
        self.assertEqual(
            "Server announcements are not enabled on this server", body["error"]
        )

    def test_send_unknown_id(self) -> None:
        self._reconcile()
        body = self._send(
            {"content": {"msgtype": "m.text", "body": "x"}},
            config_id="unknown",
            expected_code=404,
        )
        self.assertEqual(Codes.NOT_FOUND, body["errcode"])

    def test_send_removed_id(self) -> None:
        """A room whose id left the config is not sent into."""
        self._reconcile()
        self.config.rooms = [self.general]
        body = self._send(
            {"content": {"msgtype": "m.text", "body": "x"}},
            config_id="maintenance",
            expected_code=404,
        )
        self.assertEqual(Codes.NOT_FOUND, body["errcode"])

    def test_send_purged_room(self) -> None:
        """A room purged at runtime gives a 500 until the next restart."""
        room_ids = self._reconcile()
        channel = self.make_request(
            "DELETE",
            f"/_synapse/admin/v1/rooms/{room_ids['general']}",
            {"purge": True},
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.reactor.advance(1)

        body = self._send(
            {"content": {"msgtype": "m.text", "body": "x"}}, expected_code=500
        )
        self.assertIn("restart", body["error"])

    def test_send_missing_content(self) -> None:
        self._reconcile()
        body = self._send({"type": "m.room.message"}, expected_code=400)
        self.assertEqual(Codes.MISSING_PARAM, body["errcode"])

    @parameterized.expand(
        [
            ("content_string", {"content": "x"}),
            ("content_list", {"content": [1]}),
            ("type_number", {"type": 42, "content": {"body": "x"}}),
        ]
    )
    def test_send_malformed_body(self, _name: str, request_body: JsonDict) -> None:
        self._reconcile()
        body = self._send(request_body, expected_code=400)
        self.assertEqual(Codes.BAD_JSON, body["errcode"])

    def test_send_state_key_rejected(self) -> None:
        self._reconcile()
        body = self._send(
            {"type": "m.room.topic", "state_key": "", "content": {"topic": "x"}},
            expected_code=400,
        )
        self.assertEqual(Codes.INVALID_PARAM, body["errcode"])

    def test_send_member_rejected(self) -> None:
        self._reconcile()
        body = self._send(
            {"type": "m.room.member", "content": {"membership": "join"}},
            expected_code=400,
        )
        self.assertEqual(Codes.INVALID_PARAM, body["errcode"])

    def test_send_invalid_message_content(self) -> None:
        self._reconcile()
        self._send({"content": {"foo": "bar"}}, expected_code=400)

    def test_send(self) -> None:
        """A non-member receives the announcement in /sync, sent by the
        management user.
        """
        room_ids = self._reconcile()
        body = self._send({"content": {"msgtype": "m.text", "body": "Hello"}})
        self.assertEqual(body["room_id"], room_ids["general"])

        channel = self.make_request("GET", "/sync", access_token=self.other_user_tok)
        self.assertEqual(200, channel.code, msg=channel.json_body)
        events = channel.json_body["rooms"]["join"][room_ids["general"]]["timeline"][
            "events"
        ]
        event = next(e for e in events if e["event_id"] == body["event_id"])
        self.assertEqual(event["sender"], self.creator)
        self.assertEqual(event["type"], "m.room.message")
        self.assertEqual(event["content"], {"msgtype": "m.text", "body": "Hello"})

    def test_send_put_idempotent(self) -> None:
        self._reconcile()
        event_ids = []
        for _ in range(2):
            channel = self.make_request(
                "PUT",
                "/_synapse/admin/v1/server_announcements/general/send/txn1",
                content={"content": {"msgtype": "m.text", "body": "Hello"}},
                access_token=self.admin_user_tok,
            )
            self.assertEqual(200, channel.code, msg=channel.json_body)
            event_ids.append(channel.json_body["event_id"])
        self.assertEqual(event_ids[0], event_ids[1])

    def test_send_edit(self) -> None:
        self._reconcile()
        original = self._send({"content": {"msgtype": "m.text", "body": "Helo"}})
        new_content = {"msgtype": "m.text", "body": "Hello"}
        edit = self._send(
            {
                "content": {
                    "msgtype": "m.text",
                    "body": "* Hello",
                    "m.new_content": new_content,
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": original["event_id"],
                    },
                }
            }
        )

        event = self.get_success(self.store.get_event(edit["event_id"]))
        self.assertEqual(event.content["m.new_content"], new_content)
        self.assertEqual(
            event.content["m.relates_to"]["event_id"], original["event_id"]
        )

    def test_send_redaction(self) -> None:
        self._reconcile()
        original = self._send({"content": {"msgtype": "m.text", "body": "Oops"}})
        redaction = self._send(
            {
                "type": "m.room.redaction",
                "content": {"redacts": original["event_id"]},
            }
        )

        event = self.get_success(self.store.get_event(redaction["event_id"]))
        self.assertEqual(event.redacts, original["event_id"])
        redacted = self.get_success(self.store.get_event(original["event_id"]))
        self.assertEqual(dict(redacted.content), {})
