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


from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.errors import Codes
from synapse.rest.client import login, reporting, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest


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

        self.room_ids = {}
        for i in range(4):
            room_id = self.helper.create_room_as(
                self.admin_user, tok=self.admin_user_tok
            )
            self.room_ids[i] = room_id
        for i in range(4, 10):
            room_id = self.helper.create_room_as(
                self.other_user, tok=self.other_user_tok
            )
            self.room_ids[i] = room_id

        for room_num, room_id in self.room_ids.items():
            if room_num <= 4:
                self._report_room(room_id, self.other_user_tok)
            else:
                self._report_room(room_id, self.admin_user_tok)

        self.url = "/_synapse/admin/v1/room_reports"

    def test_no_auth(self) -> None:
        """
        Try to get a room report without authentication.
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
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["room_reports"]), 10)
        self.assertNotIn("next_batch", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])

    def test_limit(self) -> None:
        """
        Testing list of reported rooms with limit
        """
        # grab the 5th report to get its id
        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )
        report_5 = channel.json_body["room_reports"][4]

        channel = self.make_request(
            "GET",
            self.url + "?limit=5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["room_reports"]), 5)
        self.assertEqual(channel.json_body["next_batch"], report_5["id"])
        self._check_fields(channel.json_body["room_reports"])

    def test_from(self) -> None:
        """
        Testing list of reported rooms with a defined starting point (from)
        """
        # grab the fifth report to get its id
        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )
        report_5 = channel.json_body["room_reports"][4]
        from_id = report_5["id"]

        channel = self.make_request(
            "GET",
            self.url + f"?from={from_id}",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["room_reports"]), 5)
        self.assertNotIn("next_batch", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])

    def test_limit_and_from(self) -> None:
        """
        Testing list of reported rooms with a defined starting point and limit
        """
        # grab the second most recent report to get its id
        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )
        report_2 = channel.json_body["room_reports"][1]
        from_id = report_2["id"]

        report_5 = channel.json_body["room_reports"][4]

        channel = self.make_request(
            "GET",
            self.url + f"?from={from_id}&limit=3",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["room_reports"]), 3)
        self.assertEqual(channel.json_body["next_batch"], report_5["id"])
        self._check_fields(channel.json_body["room_reports"])

    def test_filter_room(self) -> None:
        """
        Testing list of reported rooms with a filter of room
        """

        channel = self.make_request(
            "GET",
            self.url + "?room_id=%s" % self.room_ids[1],
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 1)
        self.assertEqual(len(channel.json_body["room_reports"]), 1)
        self.assertNotIn("next_batch", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])
        self.assertEqual(
            channel.json_body["room_reports"][0]["room_id"], self.room_ids[1]
        )

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
        self.assertEqual(channel.json_body["total"], 5)
        self.assertEqual(len(channel.json_body["room_reports"]), 5)
        self.assertNotIn("next_batch", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])

        for report in channel.json_body["room_reports"]:
            self.assertEqual(report["user_id"], self.other_user)

    def test_filter_user_and_room(self) -> None:
        """
        Testing list of reported rooms with a filter of user and room
        """

        channel = self.make_request(
            "GET",
            self.url + "?user_id=%s&room_id=%s" % (self.other_user, self.room_ids[4]),
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 1)
        self.assertEqual(len(channel.json_body["room_reports"]), 1)
        self.assertNotIn("next_batch", channel.json_body)
        self._check_fields(channel.json_body["room_reports"])

        self.assertEqual(
            channel.json_body["room_reports"][0]["user_id"], self.other_user
        )
        self.assertEqual(
            channel.json_body["room_reports"][0]["room_id"], self.room_ids[4]
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

    def test_next_batch(self) -> None:
        """
        Testing that `next_batch` appears at the right place
        """

        #  `next_batch` does not appear
        # Number of results is the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=10",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["room_reports"]), 10)
        self.assertNotIn("next_batch", channel.json_body)

        # fetch ts of 2nd oldest report
        report_9 = channel.json_body["room_reports"][8]

        #  `next_batch` does not appear
        # Number of max results is larger than the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=11",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["room_reports"]), 10)
        self.assertNotIn("next_batch", channel.json_body)

        #  `next_batch` does appear
        # Number of max results is smaller than the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=9",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["room_reports"]), 9)
        self.assertEqual(channel.json_body["next_batch"], report_9["id"])

        # Check
        # Set `from` to value of `next_batch` for request remaining entries
        #  `next_batch` does not appear
        channel = self.make_request(
            "GET",
            self.url + f"?from={report_9['id']}",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["room_reports"]), 1)
        self.assertNotIn("next_batch", channel.json_body)

    def _report_room(self, room_id: str, user_tok: str) -> None:
        """Report rooms"""

        channel = self.make_request(
            "POST",
            "_matrix/client/v3/rooms/%s/report" % (room_id),
            {"reason": "this makes me sad"},
            access_token=user_tok,
            shorthand=False,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)

    def _check_fields(self, content: list[JsonDict]) -> None:
        """Checks that all attributes are present in a room report"""
        for c in content:
            self.assertIn("id", c)
            self.assertIn("received_ts", c)
            self.assertIn("room_id", c)
            self.assertIn("user_id", c)
            self.assertIn("canonical_alias", c)
            self.assertIn("name", c)
            self.assertIn("reason", c)
            self.assertIn("topic", c)

    def test_count_correct_despite_table_deletions(self) -> None:
        """
        Tests that the count matches the number of rows, even if rows in joined tables
        are missing.
        """

        # Delete rows from room_stats_state for one of our rooms.
        self.get_success(
            self.hs.get_datastores().main.db_pool.simple_delete(
                "room_stats_state", {"room_id": self.room_ids[1]}, desc="_"
            )
        )

        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        # The 'total' field is 9 because only 9 reports will actually
        # be retrievable since we deleted the row in the room_stats_state
        # table.
        self.assertEqual(channel.json_body["total"], 9)
        # This is consistent with the number of rows actually returned.
        self.assertEqual(len(channel.json_body["room_reports"]), 9)


class RoomReportDetailTestCase(unittest.HomeserverTestCase):
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

        self._report_room(self.room_id1, self.admin_user_tok)

        # first created room report gets `id`=2
        self.url = "/_synapse/admin/v1/room_reports/2"

    def test_no_auth(self) -> None:
        """
        Try to get room report without authentication.
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
        Testing get a reported room
        """

        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self._check_fields(channel.json_body)

    def test_invalid_report_id(self) -> None:
        """
        Testing that an invalid `report_id` returns a 400.
        """

        # `report_id` is a non-numerical string
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/room_reports/abcdef",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])
        self.assertEqual(
            "The report_id parameter must be a string representing a positive integer.",
            channel.json_body["error"],
        )

        # `report_id` is undefined
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/room_reports/",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])
        self.assertEqual(
            "The report_id parameter must be a string representing a positive integer.",
            channel.json_body["error"],
        )

    def test_report_id_not_found(self) -> None:
        """
        Testing that a not existing `report_id` returns a 404.
        """

        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/room_reports/123",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])
        self.assertEqual("Room report not found", channel.json_body["error"])

    def _check_fields(self, content: JsonDict) -> None:
        """Checks that all attributes are present in a room report"""
        self.assertIn("id", content)
        self.assertIn("received_ts", content)
        self.assertIn("room_id", content)
        self.assertIn("user_id", content)
        self.assertIn("canonical_alias", content)
        self.assertIn("name", content)
        self.assertIn("topic", content)
        self.assertIn("reason", content)

    def _report_room(self, room_id: str, user_tok: str) -> None:
        """Report rooms"""

        channel = self.make_request(
            "POST",
            "_matrix/client/v3/rooms/%s/report" % (room_id),
            {"reason": "this makes me sad"},
            access_token=user_tok,
            shorthand=False,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)


class DeleteRoomReportTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self._store = hs.get_datastores().main

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.other_user_tok = self.login("user", "pass")

        # create report
        room_report_id = self.get_success(
            self._store.add_room_report(
                "room_id",
                self.other_user,
                "this makes me sad",
                self.clock.time_msec(),
            )
        )

        self.url = f"/_synapse/admin/v1/room_reports/{room_report_id}"

    def test_no_auth(self) -> None:
        """
        Try to delete a room report without authentication.
        """
        channel = self.make_request("DELETE", self.url)

        self.assertEqual(401, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_no_admin(self) -> None:
        """
        If the user is not a server admin, an error 403 is returned.
        """

        channel = self.make_request(
            "DELETE",
            self.url,
            access_token=self.other_user_tok,
        )

        self.assertEqual(403, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_delete_success(self) -> None:
        """
        Testing delete a report.
        """

        channel = self.make_request(
            "DELETE",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual({}, channel.json_body)

        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        # check that report was deleted
        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    def test_invalid_report_id(self) -> None:
        """
        Testing that an invalid `report_id` returns a 400.
        """

        # `report_id` is negative
        channel = self.make_request(
            "DELETE",
            "/_synapse/admin/v1/room_reports/-123",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])
        self.assertEqual(
            "The report_id parameter must be a string representing a positive integer.",
            channel.json_body["error"],
        )

        # `report_id` is a non-numerical string
        channel = self.make_request(
            "DELETE",
            "/_synapse/admin/v1/room_reports/abcdef",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])
        self.assertEqual(
            "The report_id parameter must be a string representing a positive integer.",
            channel.json_body["error"],
        )

        # `report_id` is undefined
        channel = self.make_request(
            "DELETE",
            "/_synapse/admin/v1/room_reports/",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])
        self.assertEqual(
            "The report_id parameter must be a string representing a positive integer.",
            channel.json_body["error"],
        )

    def test_report_id_not_found(self) -> None:
        """
        Testing that a not existing `report_id` returns a 404.
        """

        channel = self.make_request(
            "DELETE",
            "/_synapse/admin/v1/room_reports/123",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])
        self.assertEqual("Room report not found", channel.json_body["error"])
