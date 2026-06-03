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

        self.room_reports = []
        for _ in range(5):
            room_id = self.helper.create_room_as(
                self.admin_user, tok=self.admin_user_tok
            )
            report_id = self._report_room(room_id, self.other_user_tok)
            self.room_reports.append(
                {"id": report_id, "room_id": room_id, "user_id": self.other_user}
            )
        for _ in range(5, 10):
            room_id = self.helper.create_room_as(
                self.other_user, tok=self.other_user_tok
            )
            report_id = self._report_room(room_id, self.admin_user_tok)
            self.room_reports.append(
                {"id": report_id, "room_id": room_id, "user_id": self.admin_user}
            )

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
        self.assertEqual(len(channel.json_body["room_reports"]), 10)
        # No pagination token because the page was big enough to hold all of the reports
        self.assertNotIn("next_batch", channel.json_body)
        # we reverse the list of room reports to check against as they are in chronological order
        self._check_expected_room_report_fields(
            channel.json_body["room_reports"], list(reversed(self.room_reports))
        )

    def test_pagination(self) -> None:
        """
        Test pagination of the returned room reports.
        """
        # First page of results
        channel = self.make_request(
            "GET",
            self.url + "?limit=5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        first_page = channel.json_body["room_reports"]
        self.assertEqual(len(first_page), 5)
        self.assertIn("next_batch", channel.json_body)
        # endpoint should return the 5 most recent reports in reverse chronological order
        # we reverse the list of recorded room reports to check against as they are in chronological order
        self._check_expected_room_report_fields(
            first_page, list(reversed(self.room_reports[5:]))
        )

        # Request second page of results using next_batch token
        next_batch = channel.json_body["next_batch"]
        channel = self.make_request(
            "GET",
            self.url + f"?limit=5&from={next_batch}",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        second_page = channel.json_body["room_reports"]
        self.assertEqual(len(second_page), 5)
        self.assertNotIn("next_batch", channel.json_body)
        # endpoint should return the 5 oldest reports in reverse chronological order
        # we reverse the list of recorded room reports to check against as they are in chronological order
        self._check_expected_room_report_fields(
            second_page, list(reversed(self.room_reports[:5]))
        )

    def test_filter_room(self) -> None:
        """
        Testing list of reported rooms with a filter of room
        """
        room_id = self.room_reports[3]["room_id"]

        channel = self.make_request(
            "GET",
            self.url + f"?room_id={room_id}",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(len(channel.json_body["room_reports"]), 1)
        self.assertNotIn("next_batch", channel.json_body)
        self._check_expected_room_report_fields(
            channel.json_body["room_reports"], [self.room_reports[3]]
        )

    def test_filter_user(self) -> None:
        """
        Testing list of reported rooms with a filter of user
        """

        channel = self.make_request(
            "GET",
            self.url + f"?user_id={self.other_user}",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(len(channel.json_body["room_reports"]), 5)
        self.assertNotIn("next_batch", channel.json_body)
        # we reverse the list of room reports to check against as they are in chronological order
        self._check_expected_room_report_fields(
            channel.json_body["room_reports"], list(reversed(self.room_reports[:5]))
        )

        for report in channel.json_body["room_reports"]:
            self.assertEqual(report["user_id"], self.other_user)

    def test_filter_user_and_room(self) -> None:
        """
        Testing list of reported rooms with a filter of user and room
        """

        channel = self.make_request(
            "GET",
            self.url
            + f"?user_id={self.other_user}&room_id={self.room_reports[4]['room_id']}",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(len(channel.json_body["room_reports"]), 1)
        self.assertNotIn("next_batch", channel.json_body)
        self._check_expected_room_report_fields(
            channel.json_body["room_reports"], [self.room_reports[4]]
        )

        self.assertEqual(
            channel.json_body["room_reports"][0]["user_id"], self.other_user
        )
        self.assertEqual(
            channel.json_body["room_reports"][0]["room_id"],
            self.room_reports[4]["room_id"],
        )

    def _report_room(self, room_id: str, user_tok: str) -> int:
        """Report rooms"""

        channel = self.make_request(
            "POST",
            f"_matrix/client/v3/rooms/{room_id}/report",
            {"reason": "this makes me sad"},
            access_token=user_tok,
            shorthand=False,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        # there is no endpoint to get the id of a room report, so we fetch it from the db
        (id,) = self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_one(
                table="room_reports",
                keyvalues={"room_id": room_id},
                retcols=("id",),
                allow_none=False,
            )
        )
        return id

    def test_room_reports_for_deleted_rooms_are_not_returned(self) -> None:
        """
        Tests that room reports for rooms that have been deleted are not returned.
        """

        # Delete rows from room_stats_state for one of our rooms.
        self.get_success(
            self.hs.get_datastores().main.db_pool.simple_delete(
                "room_stats_state",
                {"room_id": self.room_reports[1]["room_id"]},
                desc="_",
            )
        )

        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(len(channel.json_body["room_reports"]), 9)

    def _check_expected_room_report_fields(
        self, content: list[JsonDict], expected_content: list[JsonDict]
    ) -> None:
        """Checks that all attributes are present in a room report and expected values are found"""
        for i, room_report in enumerate(content):
            self.assertIn("id", room_report)
            self.assertEqual(room_report["id"], expected_content[i]["id"])
            self.assertIn("received_ts", room_report)
            self.assertIn("room_id", room_report)
            self.assertEqual(room_report["room_id"], expected_content[i]["room_id"])
            self.assertIn("user_id", room_report)
            self.assertEqual(room_report["user_id"], expected_content[i]["user_id"])
            self.assertIn("canonical_alias", room_report)
            self.assertIn("name", room_report)
            self.assertIn("reason", room_report)
            self.assertIn("topic", room_report)


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

        id = self._report_room(self.room_id1, self.admin_user_tok)
        self.room_reports = [
            {"id": id, "room_id": self.room_id1, "user_id": self.admin_user}
        ]

        self.fetch_report_url = f"/_synapse/admin/v1/room_reports/{id}"

    def test_no_auth(self) -> None:
        """
        Try to get room report without authentication.
        """
        channel = self.make_request("GET", self.fetch_report_url, {})

        self.assertEqual(401, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_no_admin(self) -> None:
        """
        If the user is not a server admin, an error 403 is returned.
        """

        channel = self.make_request(
            "GET",
            self.fetch_report_url,
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
            self.fetch_report_url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self._check_expected_room_report_fields([channel.json_body], self.room_reports)

    def test_invalid_report_id(self) -> None:
        """
        Testing that an invalid `report_id` returns a 400.
        """

        # `report_id` is invalid (should be a numerical report ID)
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/room_reports/abcdef",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])
        self.assertEqual(
            "The report_id parameter must be a string representing a room report ID (positive integer).",
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
            "The report_id parameter must be a string representing a room report ID (positive integer).",
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

    def _check_expected_room_report_fields(
        self, content: list[JsonDict], expected_content: list[JsonDict]
    ) -> None:
        """Checks that all attributes are present in a room report and expected values are found"""
        for i, room_report in enumerate(content):
            self.assertIn("id", room_report)
            self.assertEqual(room_report["id"], expected_content[i]["id"])
            self.assertIn("received_ts", room_report)
            self.assertIn("room_id", room_report)
            self.assertEqual(room_report["room_id"], expected_content[i]["room_id"])
            self.assertIn("user_id", room_report)
            self.assertEqual(room_report["user_id"], expected_content[i]["user_id"])
            self.assertIn("canonical_alias", room_report)
            self.assertIn("name", room_report)
            self.assertIn("reason", room_report)
            self.assertIn("topic", room_report)

    def _report_room(self, room_id: str, user_tok: str) -> int:
        """Report rooms"""

        channel = self.make_request(
            "POST",
            f"_matrix/client/v3/rooms/{room_id}/report",
            {"reason": "this makes me sad"},
            access_token=user_tok,
            shorthand=False,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        # there is no endpoint to get the id of a room report, so we fetch it from the db
        (id,) = self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_one(
                table="room_reports",
                keyvalues={"room_id": room_id},
                retcols=("id",),
                allow_none=False,
            )
        )
        return id
