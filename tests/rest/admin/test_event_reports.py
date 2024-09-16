#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Dirk Klimpel
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
from typing import List

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.errors import Codes
from synapse.rest.client import login, reporting, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest


class EventReportsTestCase(unittest.HomeserverTestCase):
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

        # Two rooms and two users. Every user sends and reports every room event
        for _ in range(5):
            self._create_event_and_report(
                room_id=self.room_id1,
                user_tok=self.other_user_tok,
            )
        for _ in range(5):
            self._create_event_and_report(
                room_id=self.room_id2,
                user_tok=self.other_user_tok,
            )
        for _ in range(5):
            self._create_event_and_report(
                room_id=self.room_id1,
                user_tok=self.admin_user_tok,
            )
        for _ in range(5):
            self._create_event_and_report_without_parameters(
                room_id=self.room_id2,
                user_tok=self.admin_user_tok,
            )

        self.url = "/_synapse/admin/v1/event_reports"

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
        Testing list of reported events
        """

        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 20)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

    def test_limit(self) -> None:
        """
        Testing list of reported events with limit
        """

        channel = self.make_request(
            "GET",
            self.url + "?limit=5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 5)
        self.assertEqual(channel.json_body["next_token"], 5)
        self._check_fields(channel.json_body["event_reports"])

    def test_from(self) -> None:
        """
        Testing list of reported events with a defined starting point (from)
        """

        channel = self.make_request(
            "GET",
            self.url + "?from=5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 15)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

    def test_limit_and_from(self) -> None:
        """
        Testing list of reported events with a defined starting point and limit
        """

        channel = self.make_request(
            "GET",
            self.url + "?from=5&limit=10",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(channel.json_body["next_token"], 15)
        self.assertEqual(len(channel.json_body["event_reports"]), 10)
        self._check_fields(channel.json_body["event_reports"])

    def test_filter_room(self) -> None:
        """
        Testing list of reported events with a filter of room
        """

        channel = self.make_request(
            "GET",
            self.url + "?room_id=%s" % self.room_id1,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["event_reports"]), 10)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

        for report in channel.json_body["event_reports"]:
            self.assertEqual(report["room_id"], self.room_id1)

    def test_filter_user(self) -> None:
        """
        Testing list of reported events with a filter of user
        """

        channel = self.make_request(
            "GET",
            self.url + "?user_id=%s" % self.other_user,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["event_reports"]), 10)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

        for report in channel.json_body["event_reports"]:
            self.assertEqual(report["user_id"], self.other_user)

    def test_filter_user_and_room(self) -> None:
        """
        Testing list of reported events with a filter of user and room
        """

        channel = self.make_request(
            "GET",
            self.url + "?user_id=%s&room_id=%s" % (self.other_user, self.room_id1),
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 5)
        self.assertEqual(len(channel.json_body["event_reports"]), 5)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

        for report in channel.json_body["event_reports"]:
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
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 20)
        report = 1
        while report < len(channel.json_body["event_reports"]):
            self.assertGreaterEqual(
                channel.json_body["event_reports"][report - 1]["received_ts"],
                channel.json_body["event_reports"][report]["received_ts"],
            )
            report += 1

        # fetch the oldest first, smallest timestamp
        channel = self.make_request(
            "GET",
            self.url + "?dir=f",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 20)
        report = 1
        while report < len(channel.json_body["event_reports"]):
            self.assertLessEqual(
                channel.json_body["event_reports"][report - 1]["received_ts"],
                channel.json_body["event_reports"][report]["received_ts"],
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
            self.url + "?limit=20",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 20)
        self.assertNotIn("next_token", channel.json_body)

        #  `next_token` does not appear
        # Number of max results is larger than the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=21",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 20)
        self.assertNotIn("next_token", channel.json_body)

        #  `next_token` does appear
        # Number of max results is smaller than the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=19",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 19)
        self.assertEqual(channel.json_body["next_token"], 19)

        # Check
        # Set `from` to value of `next_token` for request remaining entries
        #  `next_token` does not appear
        channel = self.make_request(
            "GET",
            self.url + "?from=19",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 1)
        self.assertNotIn("next_token", channel.json_body)

    def _create_event_and_report(self, room_id: str, user_tok: str) -> None:
        """Create and report events"""
        resp = self.helper.send(room_id, tok=user_tok)
        event_id = resp["event_id"]

        channel = self.make_request(
            "POST",
            "rooms/%s/report/%s" % (room_id, event_id),
            {"score": -100, "reason": "this makes me sad"},
            access_token=user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)

    def _create_event_and_report_without_parameters(
        self, room_id: str, user_tok: str
    ) -> None:
        """Create and report an event, but omit reason and score"""
        resp = self.helper.send(room_id, tok=user_tok)
        event_id = resp["event_id"]

        channel = self.make_request(
            "POST",
            "rooms/%s/report/%s" % (room_id, event_id),
            {},
            access_token=user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)

    def _check_fields(self, content: List[JsonDict]) -> None:
        """Checks that all attributes are present in an event report"""
        for c in content:
            self.assertIn("id", c)
            self.assertIn("received_ts", c)
            self.assertIn("room_id", c)
            self.assertIn("event_id", c)
            self.assertIn("user_id", c)
            self.assertIn("sender", c)
            self.assertIn("canonical_alias", c)
            self.assertIn("name", c)
            self.assertIn("score", c)
            self.assertIn("reason", c)

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
        self.assertEqual(channel.json_body["total"], 10)
        # This is consistent with the number of rows actually returned.
        self.assertEqual(len(channel.json_body["event_reports"]), 10)


class EventReportDetailTestCase(unittest.HomeserverTestCase):
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

        self._create_event_and_report(
            room_id=self.room_id1,
            user_tok=self.other_user_tok,
        )

        # first created event report gets `id`=2
        self.url = "/_synapse/admin/v1/event_reports/2"

    def test_no_auth(self) -> None:
        """
        Try to get event report without authentication.
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
        Testing get a reported event
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

        # `report_id` is negative
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/event_reports/-123",
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
            "GET",
            "/_synapse/admin/v1/event_reports/abcdef",
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
            "/_synapse/admin/v1/event_reports/",
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
            "/_synapse/admin/v1/event_reports/123",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])
        self.assertEqual("Event report not found", channel.json_body["error"])

    def _create_event_and_report(self, room_id: str, user_tok: str) -> None:
        """Create and report events"""
        resp = self.helper.send(room_id, tok=user_tok)
        event_id = resp["event_id"]

        channel = self.make_request(
            "POST",
            "rooms/%s/report/%s" % (room_id, event_id),
            {"score": -100, "reason": "this makes me sad"},
            access_token=user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)

    def _check_fields(self, content: JsonDict) -> None:
        """Checks that all attributes are present in a event report"""
        self.assertIn("id", content)
        self.assertIn("received_ts", content)
        self.assertIn("room_id", content)
        self.assertIn("event_id", content)
        self.assertIn("user_id", content)
        self.assertIn("sender", content)
        self.assertIn("canonical_alias", content)
        self.assertIn("name", content)
        self.assertIn("event_json", content)
        self.assertIn("score", content)
        self.assertIn("reason", content)
        self.assertIn("auth_events", content["event_json"])
        self.assertIn("type", content["event_json"])
        self.assertIn("room_id", content["event_json"])
        self.assertIn("sender", content["event_json"])
        self.assertIn("content", content["event_json"])


class DeleteEventReportTestCase(unittest.HomeserverTestCase):
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
        event_id = self.get_success(
            self._store.add_event_report(
                "room_id",
                "event_id",
                self.other_user,
                "this makes me sad",
                {},
                self.clock.time_msec(),
            )
        )

        self.url = f"/_synapse/admin/v1/event_reports/{event_id}"

    def test_no_auth(self) -> None:
        """
        Try to delete event report without authentication.
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
            "/_synapse/admin/v1/event_reports/-123",
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
            "/_synapse/admin/v1/event_reports/abcdef",
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
            "/_synapse/admin/v1/event_reports/",
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
            "/_synapse/admin/v1/event_reports/123",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])
        self.assertEqual("Event report not found", channel.json_body["error"])
