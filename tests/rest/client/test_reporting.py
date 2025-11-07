#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 Callum Brown
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

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import login, reporting, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import override_config


class ReportEventTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        reporting.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")
        self.other_user = self.register_user("user", "pass")
        self.other_user_tok = self.login("user", "pass")

        self.room_id = self.helper.create_room_as(
            self.other_user, tok=self.other_user_tok, is_public=True
        )
        self.helper.join(self.room_id, user=self.admin_user, tok=self.admin_user_tok)
        resp = self.helper.send(self.room_id, tok=self.admin_user_tok)
        self.event_id = resp["event_id"]
        self.report_path = f"rooms/{self.room_id}/report/{self.event_id}"

    def test_reason_str_and_score_int(self) -> None:
        data = {"reason": "this makes me sad", "score": -100}
        self._assert_status(200, data)

    def test_no_reason(self) -> None:
        data = {"score": 0}
        self._assert_status(200, data)

    def test_no_score(self) -> None:
        data = {"reason": "this makes me sad"}
        self._assert_status(200, data)

    def test_no_reason_and_no_score(self) -> None:
        data: JsonDict = {}
        self._assert_status(200, data)

    def test_reason_int_and_score_str(self) -> None:
        data = {"reason": 10, "score": "string"}
        self._assert_status(400, data)

    def test_reason_zero_and_score_blank(self) -> None:
        data = {"reason": 0, "score": ""}
        self._assert_status(400, data)

    def test_reason_and_score_null(self) -> None:
        data = {"reason": None, "score": None}
        self._assert_status(400, data)

    @override_config({"experimental_features": {"msc4277_enabled": True}})
    def test_score_str(self) -> None:
        data = {"score": "string"}
        self._assert_status(200, data)

    def test_cannot_report_nonexistent_event(self) -> None:
        """
        Tests that we don't accept event reports for events which do not exist.
        """
        channel = self.make_request(
            "POST",
            f"rooms/{self.room_id}/report/$nonsenseeventid:test",
            {"reason": "i am very sad"},
            access_token=self.other_user_tok,
        )
        self.assertEqual(404, channel.code, msg=channel.result["body"])
        self.assertEqual(
            "Unable to report event: it does not exist or you aren't able to see it.",
            channel.json_body["error"],
            msg=channel.result["body"],
        )

    @override_config({"experimental_features": {"msc4277_enabled": True}})
    def test_event_existence_hidden(self) -> None:
        """
        Tests that the requester cannot infer the existence of an event.
        """
        channel = self.make_request(
            "POST",
            f"rooms/{self.room_id}/report/$nonsenseeventid:test",
            {"reason": "i am very sad"},
            access_token=self.other_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.result["body"])

    def test_cannot_report_event_if_not_in_room(self) -> None:
        """
        Tests that we don't accept event reports for events that exist, but for which
        the reporter should not be able to view (because they are not in the room).
        """
        # Have the admin user create a room (the "other" user will not join this room).
        new_room_id = self.helper.create_room_as(tok=self.admin_user_tok)

        # Have the admin user send an event in this room.
        response = self.helper.send_event(
            new_room_id,
            "m.room.message",
            content={
                "msgtype": "m.text",
                "body": "This event has some bad words in it! Flip!",
            },
            tok=self.admin_user_tok,
        )
        event_id = response["event_id"]

        # Have the "other" user attempt to report it. Perhaps they found the event ID
        # in a screenshot or something...
        channel = self.make_request(
            "POST",
            f"rooms/{new_room_id}/report/{event_id}",
            {"reason": "I'm not in this room but I have opinions anyways!"},
            access_token=self.other_user_tok,
        )

        # The "other" user is not in the room, so their report should be rejected.
        self.assertEqual(404, channel.code, msg=channel.result["body"])
        self.assertEqual(
            "Unable to report event: it does not exist or you aren't able to see it.",
            channel.json_body["error"],
            msg=channel.result["body"],
        )

    def _assert_status(self, response_status: int, data: JsonDict) -> None:
        channel = self.make_request(
            "POST", self.report_path, data, access_token=self.other_user_tok
        )
        self.assertEqual(response_status, channel.code, msg=channel.result["body"])


class ReportRoomTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        reporting.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.other_user = self.register_user("user", "pass")
        self.other_user_tok = self.login("user", "pass")

        self.room_id = self.helper.create_room_as(
            self.other_user, tok=self.other_user_tok, is_public=True
        )
        self.report_path = f"/_matrix/client/v3/rooms/{self.room_id}/report"

    def test_reason_str(self) -> None:
        data = {"reason": "this makes me sad"}
        self._assert_status(200, data)

    def test_no_reason(self) -> None:
        data = {"not_reason": "for typechecking"}
        self._assert_status(400, data)

    def test_reason_nonstring(self) -> None:
        data = {"reason": 42}
        self._assert_status(400, data)

    def test_reason_null(self) -> None:
        data = {"reason": None}
        self._assert_status(400, data)

    def test_cannot_report_nonexistent_room(self) -> None:
        """
        Tests that we don't accept event reports for rooms which do not exist.
        """
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/rooms/!bloop:example.org/report",
            {"reason": "i am very sad"},
            access_token=self.other_user_tok,
            shorthand=False,
        )
        self.assertEqual(404, channel.code, msg=channel.result["body"])
        self.assertEqual(
            "Room does not exist",
            channel.json_body["error"],
            msg=channel.result["body"],
        )

    @override_config({"experimental_features": {"msc4277_enabled": True}})
    def test_room_existence_hidden(self) -> None:
        """
        Tests that the requester cannot infer the existence of a room.
        """
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/rooms/!bloop:example.org/report",
            {"reason": "i am very sad"},
            access_token=self.other_user_tok,
            shorthand=False,
        )
        self.assertEqual(200, channel.code, msg=channel.result["body"])

    def _assert_status(self, response_status: int, data: JsonDict) -> None:
        channel = self.make_request(
            "POST",
            self.report_path,
            data,
            access_token=self.other_user_tok,
            shorthand=False,
        )
        self.assertEqual(response_status, channel.code, msg=channel.result["body"])


class ReportUserTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        reporting.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.other_user = self.register_user("user", "pass")
        self.other_user_tok = self.login("user", "pass")

        self.target_user_id = self.register_user("target_user", "pass")

    def test_reason_str(self) -> None:
        data = {"reason": "this makes me sad"}
        self._assert_status(200, data)

        rows = self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_onecol(
                table="user_reports",
                keyvalues={"target_user_id": self.target_user_id},
                retcol="id",
                desc="get_user_report_ids",
            )
        )
        self.assertEqual(len(rows), 1)

    def test_no_reason(self) -> None:
        data = {"not_reason": "for typechecking"}
        self._assert_status(400, data)

    def test_reason_nonstring(self) -> None:
        data = {"reason": 42}
        self._assert_status(400, data)

    def test_reason_null(self) -> None:
        data = {"reason": None}
        self._assert_status(400, data)

    def test_reason_long(self) -> None:
        data = {"reason": "x" * 1001}
        self._assert_status(400, data)

    def test_cannot_report_nonlocal_user(self) -> None:
        """
        Tests that we ignore reports for nonlocal users.
        """
        target_user_id = "@bloop:example.org"
        data = {"reason": "i am very sad"}
        self._assert_status(200, data, target_user_id)
        self._assert_no_reports_for_user(target_user_id)

    def test_can_report_nonexistent_user(self) -> None:
        """
        Tests that we ignore reports for nonexistent users.
        """
        target_user_id = f"@bloop:{self.hs.hostname}"
        data = {"reason": "i am very sad"}
        self._assert_status(200, data, target_user_id)
        self._assert_no_reports_for_user(target_user_id)

    def _assert_no_reports_for_user(self, target_user_id: str) -> None:
        rows = self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_onecol(
                table="user_reports",
                keyvalues={"target_user_id": target_user_id},
                retcol="id",
                desc="get_user_report_ids",
            )
        )
        self.assertEqual(len(rows), 0)

    def _assert_status(
        self, response_status: int, data: JsonDict, user_id: str | None = None
    ) -> None:
        if user_id is None:
            user_id = self.target_user_id
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/users/{user_id}/report",
            data,
            access_token=self.other_user_tok,
            shorthand=False,
        )
        self.assertEqual(response_status, channel.code, msg=channel.result["body"])
