#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes, RelationTypes
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.rest.client import login, relations, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest


class ServerAnnouncementsHistoryTestCase(unittest.HomeserverTestCase):
    """Tests that a local user who has never been in a server announcement room
    can read its history through the endpoints clients use outside of `/sync`.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        room.register_deprecated_servlets,
        relations.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.announcer = self.register_user("announcer", "pass")
        self.announcer_tok = self.login("announcer", "pass")
        self.register_user("reader", "pass")
        self.reader_tok = self.login("reader", "pass")

        hs.config.serverannouncements.rooms = [
            ServerAnnouncementRoom(
                config_id="news",
                room_name="Server News",
                room_topic="News from the server",
                room_avatar_url=None,
                room_avatar_path=None,
                allowed_user_ids=[self.announcer],
            )
        ]
        self.get_success(hs.get_server_announcements_handler().reconcile_rooms())

        room_id = self.get_success(
            hs.get_datastores().main.get_server_announcement_room("news")
        )
        assert room_id is not None
        self.room_id = room_id

        self.announcement_id = self.helper.send(
            self.room_id, "hello", tok=self.announcer_tok
        )["event_id"]

    def _get(self, path: str) -> JsonDict:
        channel = self.make_request("GET", path, access_token=self.reader_tok)
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def _send_relation(self, rel_type: str, content: JsonDict) -> str:
        return self.helper.send_event(
            self.room_id,
            EventTypes.Reaction
            if rel_type == RelationTypes.ANNOTATION
            else EventTypes.Message,
            content={
                **content,
                "m.relates_to": {
                    **content.get("m.relates_to", {}),
                    "rel_type": rel_type,
                    "event_id": self.announcement_id,
                },
            },
            tok=self.announcer_tok,
        )["event_id"]

    def test_messages(self) -> None:
        """The reader can paginate back to the announcement."""
        body = self._get(f"/rooms/{self.room_id}/messages?dir=b")
        self.assertIn(
            self.announcement_id, [event["event_id"] for event in body["chunk"]]
        )

    def test_context(self) -> None:
        """The reader can load the announcement with its surrounding events."""
        body = self._get(f"/rooms/{self.room_id}/context/{self.announcement_id}")
        self.assertEqual(body["event"]["event_id"], self.announcement_id)

    def test_event(self) -> None:
        """The reader can fetch the announcement by its ID."""
        body = self._get(f"/rooms/{self.room_id}/event/{self.announcement_id}")
        self.assertEqual(body["event_id"], self.announcement_id)

    def test_relations(self) -> None:
        """The reader can list the reactions to the announcement."""
        reaction_id = self._send_relation(
            RelationTypes.ANNOTATION, {"m.relates_to": {"key": "👍"}}
        )

        body = self._get(
            f"/_matrix/client/v1/rooms/{self.room_id}/relations/{self.announcement_id}"
        )
        self.assertEqual([event["event_id"] for event in body["chunk"]], [reaction_id])

    def test_threads(self) -> None:
        """The reader can list the announcement as a thread root."""
        self._send_relation(RelationTypes.THREAD, {"msgtype": "m.text", "body": "a"})

        body = self._get(f"/_matrix/client/v1/rooms/{self.room_id}/threads")
        self.assertEqual(
            [event["event_id"] for event in body["chunk"]], [self.announcement_id]
        )

    def test_room_initial_sync(self) -> None:
        """The reader can load a snapshot of the room including the announcement."""
        body = self._get(f"/rooms/{self.room_id}/initialSync")
        self.assertIn(
            self.announcement_id,
            [event["event_id"] for event in body["messages"]["chunk"]],
        )
