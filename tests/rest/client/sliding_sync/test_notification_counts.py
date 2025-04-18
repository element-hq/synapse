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
from http import HTTPStatus

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes, ReceiptTypes, RelationTypes
from synapse.rest.client import login, receipts, room, sync
from synapse.server import HomeServer
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase


class SlidingSyncNotificationCountsTestCase(SlidingSyncBase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        receipts.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        super().prepare(reactor, clock, hs)

    def setUp(self) -> None:
        super().setUp()

        self.user1_id = self.register_user("user1", "pass")
        self.user1_tok = self.login(self.user1_id, "pass")
        self.user2_id = self.register_user("user2", "pass")
        self.user2_tok = self.login(self.user2_id, "pass")

        # Create room1
        self.room_id1 = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.join(self.room_id1, self.user1_id, tok=self.user1_tok)
        self.helper.join(self.room_id1, self.user2_id, tok=self.user2_tok)

        self.sync_req = {
            "lists": {},
            "room_subscriptions": {
                self.room_id1: {
                    "required_state": [],
                    "timeline_limit": 1,
                },
            },
        }
        sync_resp, self.user1_start_token = self.do_sync(
            self.sync_req, tok=self.user1_tok
        )

        # send a read receipt to make sure the counts are 0
        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id1}/receipt/{ReceiptTypes.READ}/{sync_resp['rooms'][self.room_id1]['timeline'][0]['event_id']}",
            {},
            access_token=self.user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def test_main_thread_notification_count(self) -> None:
        # send an event from user 2
        self.helper.send(self.room_id1, body="new event", tok=self.user2_tok)

        # user 1 syncs
        sync_resp, from_token = self.do_sync(
            self.sync_req, tok=self.user1_tok, since=self.user1_start_token
        )

        # notification count should now be 1
        self.assertEqual(sync_resp["rooms"][self.room_id1]["notification_count"], 1)

    def test_main_thread_highlight_count(self) -> None:
        # send an event that mentions user1
        self.helper.send(self.room_id1, body="Hello user1", tok=self.user2_tok)

        # user 1 syncs
        sync_resp, from_token = self.do_sync(
            self.sync_req, tok=self.user1_tok, since=self.user1_start_token
        )

        # notification and highlight count should be 1
        self.assertEqual(sync_resp["rooms"][self.room_id1]["notification_count"], 1)
        self.assertEqual(sync_resp["rooms"][self.room_id1]["highlight_count"], 1)

    def test_thread_notification_count(self) -> None:
        room1_event_response1 = self.helper.send(
            self.room_id1, body="Thread root", tok=self.user2_tok
        )

        thread_id = room1_event_response1["event_id"]

        _, from_token = self.do_sync(
            self.sync_req, tok=self.user1_tok, since=self.user1_start_token
        )

        threaded_event_content = {
            "msgtype": "m.text",
            "body": "threaded response",
            "m.relates_to": {
                "event_id": thread_id,
                "rel_type": RelationTypes.THREAD,
            },
        }

        self.helper.send_event(
            self.room_id1,
            EventTypes.Message,
            threaded_event_content,
            None,
            self.user2_tok,
            HTTPStatus.OK,
            custom_headers=None,
        )

        sync_resp, _ = self.do_sync(self.sync_req, tok=self.user1_tok, since=from_token)

        self.assertEqual(
            sync_resp["rooms"][self.room_id1]["unread_thread_notifications"][thread_id][
                "notification_count"
            ],
            1,
        )
        self.assertEqual(
            sync_resp["rooms"][self.room_id1]["unread_thread_notifications"][thread_id][
                "highlight_count"
            ],
            0,
        )
