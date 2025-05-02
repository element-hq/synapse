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
from typing import List

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.errors import Codes
from synapse.rest.client import login, reporting, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest


# Based upon EventReportsTestCase
class RoomReportsTestCase(unittest.HomeserverTestCase):
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

        self.room_id1 = self.helper.create_room_as(
            self.other_user, tok=self.other_user_tok, is_public=True
        )
        self.helper.join(self.room_id1, user=self.admin_user, tok=self.admin_user_tok)

        self.room_id2 = self.helper.create_room_as(
            self.other_user, tok=self.other_user_tok, is_public=True
        )
        self.helper.join(self.room_id2, user=self.admin_user, tok=self.admin_user_tok)

        # Every user reports both rooms
        self._report_room(self.room_id1, self.other_user_tok)
        self._report_room(self.room_id2, self.other_user_tok)
        self._report_room_without_parameters(self.room_id1, self.admin_user_tok)
        self._report_room_without_parameters(self.room_id2, self.admin_user_tok)

        self.url = "/_synapse/admin/v1/room_reports"

    def test_no_auth(self) -> None:
        """
        Try to get an event report without authentication.
        """
        channel = self.make_request("GET", self.url, {})

        self.assertEqual(401, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_no_admin(self) -> None:
        """
        If the user is not a server admin, an error 403 is returned.
        """

        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.other_user_tok,
        )

        self.assertEqual(403, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_default_success(self) -> None:
        """
        Testing list of reported rooms
        """

        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["room_reports"]), 4)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])

    def test_limit(self) -> None:
        """
        Testing list of reported rooms with limit
        """

        channel = self.make_request(
            "GET",
            self.url + "?limit=2",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["room_reports"]), 2)
        self.assertEqual(channel.json_body["next_token"], 2)
        self._check_fields(channel.json_body["room_reports"])

    def test_from(self) -> None:
        """
        Testing list of reported rooms with a defined starting point (from)
        """

        channel = self.make_request(
            "GET",
            self.url + "?from=2",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["room_reports"]), 2)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])

    def test_limit_and_from(self) -> None:
        """
        Testing list of reported rooms with a defined starting point and limit
        """

        channel = self.make_request(
            "GET",
            self.url + "?from=2&limit=1",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(channel.json_body["next_token"], 2)
        self.assertEqual(len(channel.json_body["room_reports"]), 1)
        self._check_fields(channel.json_body["room_reports"])

    def test_filter_room(self) -> None:
        """
        Testing list of reported rooms with a filter of room
        """

        channel = self.make_request(
            "GET",
            self.url + "?room_id=%s" % self.room_id1,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 2)
        self.assertEqual(len(channel.json_body["room_reports"]), 2)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])

        for report in channel.json_body["room_reports"]:
            self.assertEqual(report["room_id"], self.room_id1)

    def test_filter_user(self) -> None:
        """
        Testing list of reported rooms with a filter of user
        """

        channel = self.make_request(
            "GET",
            self.url + "?user_id=%s" % self.other_user,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 2)
        self.assertEqual(len(channel.json_body["room_reports"]), 2)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])

        for report in channel.json_body["room_reports"]:
            self.assertEqual(report["user_id"], self.other_user)

    def test_filter_user_and_room(self) -> None:
        """
        Testing list of reported rooms with a filter of user and room
        """

        channel = self.make_request(
            "GET",
            self.url + "?user_id=%s&room_id=%s" % (self.other_user, self.room_id1),
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 1)
        self.assertEqual(len(channel.json_body["room_reports"]), 1)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])

        for report in channel.json_body["room_reports"]:
            self.assertEqual(report["user_id"], self.other_user)
            self.assertEqual(report["room_id"], self.room_id1)

    def test_valid_search_order(self) -> None:
        """
        Testing search order. Order by timestamps.
        """

        # fetch the most recent first, largest timestamp
        channel = self.make_request(
            "GET",
            self.url + "?dir=b",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["room_reports"]), 4)
        report = 1
        while report < len(channel.json_body["room_reports"]):
            self.assertGreaterEqual(
                channel.json_body["room_reports"][report - 1]["received_ts"],
                channel.json_body["room_reports"][report]["received_ts"],
            )
            report += 1

        # fetch the oldest first, smallest timestamp
        channel = self.make_request(
            "GET",
            self.url + "?dir=f",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["room_reports"]), 4)
        report = 1
        while report < len(channel.json_body["room_reports"]):
            self.assertLessEqual(
                channel.json_body["room_reports"][report - 1]["received_ts"],
                channel.json_body["room_reports"][report]["received_ts"],
            )
            report += 1

    def test_invalid_search_order(self) -> None:
        """
        Testing that a invalid search order returns a 400
        """

        channel = self.make_request(
            "GET",
            self.url + "?dir=bar",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])
        self.assertEqual(
            "Query parameter 'dir' must be one of ['b', 'f']",
            channel.json_body["error"],
        )

    def test_limit_is_negative(self) -> None:
        """
        Testing that a negative limit parameter returns a 400
        """

        channel = self.make_request(
            "GET",
            self.url + "?limit=-5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])

    def test_from_is_negative(self) -> None:
        """
        Testing that a negative from parameter returns a 400
        """

        channel = self.make_request(
            "GET",
            self.url + "?from=-5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])

    def test_next_token(self) -> None:
        """
        Testing that `next_token` appears at the right place
        """

        #  `next_token` does not appear
        # Number of results is the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=4",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["room_reports"]), 2)
        self.assertNotIn("room_reports", channel.json_body)

        #  `next_token` does not appear
        # Number of max results is larger than the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["room_reports"]), 4)
        self.assertNotIn("next_token", channel.json_body)

        #  `next_token` does appear
        # Number of max results is smaller than the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=3",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["room_reports"]), 3)
        self.assertEqual(channel.json_body["next_token"], 3)

        # Check
        # Set `from` to value of `next_token` for request remaining entries
        #  `next_token` does not appear
        channel = self.make_request(
            "GET",
            self.url + "?from=3",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["room_reports"]), 1)
        self.assertNotIn("next_token", channel.json_body)

    def _report_room(self, room_id: str, user_tok: str) -> None:
        """Report a room"""
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{room_id}/report",
            {"reason": "this makes me sad"},
            access_token=user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)

    def _report_room_without_parameters(self, room_id: str, user_tok: str) -> None:
        """Report a room, but omit reason"""
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{room_id}/report",
            {},
            access_token=user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)

    def _check_fields(self, content: List[JsonDict]) -> None:
        """Checks that all attributes are present in a room report"""
        for c in content:
            self.assertIn("id", c)
            self.assertIn("received_ts", c)
            self.assertIn("room_id", c)
            self.assertIn("user_id", c)
            self.assertIn("canonical_alias", c)
            self.assertIn("name", c)
            self.assertIn("reason", c)

        self.assertEqual(len(c.keys()), 7)

    def test_count_correct_despite_table_deletions(self) -> None:
        """
        Tests that the count matches the number of rows, even if rows in joined tables
        are missing.
        """

        # Delete rows from room_stats_state for one of our rooms.
        self.get_success(
            self.hs.get_datastores().main.db_pool.simple_delete(
                "room_stats_state", {"room_id": self.room_id1}, desc="_"
            )
        )

        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        # The 'total' field is 10 because only 10 reports will actually
        # be retrievable since we deleted the rows in the room_stats_state
        # table.
        self.assertEqual(channel.json_body["total"], 2)
        # This is consistent with the number of rows actually returned.
        self.assertEqual(len(channel.json_body["room_reports"]), 2)
