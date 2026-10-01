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


class UserReportsTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        reporting.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.users = {}
        for i in range(10):
            self.users[i] = self.register_user(f"user{i}", "pass")

        # users 1 and 2 report all other users
        reporter_1_tok = self.login(self.users[0], "pass")
        reporter_2_tok = self.login(self.users[1], "pass")
        for num, user in self.users.items():
            if num <= 1:
                continue
            if num % 2 == 0:
                self._report_user(user, reporter_1_tok)
            else:
                self._report_user(user, reporter_2_tok)

        self.url = "/_synapse/admin/v1/user_reports"

    def test_no_auth(self) -> None:
        """
        Try to get a user report without authentication.
        """
        channel = self.make_request("GET", self.url, {})

        self.assertEqual(401, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_no_admin(self) -> None:
        """
        If the user is not a server admin, an error 403 is returned.
        """
        rando_tok = self.login(self.users[4], "pass")
        channel = self.make_request(
            "GET",
            self.url,
            access_token=rando_tok,
        )

        self.assertEqual(403, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_default_success(self) -> None:
        """
        Testing list of reported users
        """

        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(len(channel.json_body["user_reports"]), 8)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["user_reports"])

    def test_limit(self) -> None:
        """
        Testing list of reported users with limit
        """

        channel = self.make_request(
            "GET",
            self.url + "?limit=5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(len(channel.json_body["user_reports"]), 5)
        self.assertEqual(channel.json_body["next_token"], 5)
        self._check_fields(channel.json_body["user_reports"])

    def test_from(self) -> None:
        """
        Testing list of reported users with a defined starting point (from)
        """

        channel = self.make_request(
            "GET",
            self.url + "?from=5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(len(channel.json_body["user_reports"]), 3)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["user_reports"])

    def test_limit_and_from(self) -> None:
        """
        Testing list of reported users with a defined starting point and limit
        """

        channel = self.make_request(
            "GET",
            self.url + "?from=2&limit=5",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(channel.json_body["next_token"], 7)
        self.assertEqual(len(channel.json_body["user_reports"]), 5)
        self._check_fields(channel.json_body["user_reports"])

    def test_filter_by_target_user_id(self) -> None:
        """
        Testing list of reported users with a filter of target_user_id
        """

        channel = self.make_request(
            "GET",
            self.url + "?target_user_id=%s" % self.users[3],
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 1)
        self.assertEqual(len(channel.json_body["user_reports"]), 1)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["user_reports"])

        for report in channel.json_body["user_reports"]:
            self.assertEqual(report["target_user_id"], self.users[3])

    def test_filter_user(self) -> None:
        """
        Testing list of reported users with a filter of reporting user
        """

        channel = self.make_request(
            "GET",
            self.url + "?user_id=%s" % self.users[0],
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 4)
        self.assertEqual(len(channel.json_body["user_reports"]), 4)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["user_reports"])

        for report in channel.json_body["user_reports"]:
            self.assertEqual(report["user_id"], self.users[0])

    def test_filter_user_and_target_user(self) -> None:
        """
        Testing list of reported users with a filter of reporting user and target_user
        """

        channel = self.make_request(
            "GET",
            self.url + "?user_id=%s&target_user_id=%s" % (self.users[1], self.users[7]),
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 1)
        self.assertEqual(len(channel.json_body["user_reports"]), 1)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["user_reports"])

        for report in channel.json_body["user_reports"]:
            self.assertEqual(report["user_id"], self.users[1])
            self.assertEqual(report["target_user_id"], self.users[7])

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
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(len(channel.json_body["user_reports"]), 8)
        report = 1
        while report < len(channel.json_body["user_reports"]):
            self.assertGreaterEqual(
                channel.json_body["user_reports"][report - 1]["received_ts"],
                channel.json_body["user_reports"][report]["received_ts"],
            )
            report += 1

        # fetch the oldest first, smallest timestamp
        channel = self.make_request(
            "GET",
            self.url + "?dir=f",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(len(channel.json_body["user_reports"]), 8)
        report = 1
        while report < len(channel.json_body["user_reports"]):
            self.assertLessEqual(
                channel.json_body["user_reports"][report - 1]["received_ts"],
                channel.json_body["user_reports"][report]["received_ts"],
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
            self.url + "?limit=8",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(len(channel.json_body["user_reports"]), 8)
        self.assertNotIn("next_token", channel.json_body)

        #  `next_token` does not appear
        # Number of max results is larger than the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=10",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(len(channel.json_body["user_reports"]), 8)
        self.assertNotIn("next_token", channel.json_body)

        #  `next_token` does appear
        # Number of max results is smaller than the number of entries
        channel = self.make_request(
            "GET",
            self.url + "?limit=6",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(len(channel.json_body["user_reports"]), 6)
        self.assertEqual(channel.json_body["next_token"], 6)

        # Check
        # Set `from` to value of `next_token` for request remaining entries
        #  `next_token` does not appear
        channel = self.make_request(
            "GET",
            self.url + "?from=6",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["total"], 8)
        self.assertEqual(len(channel.json_body["user_reports"]), 2)
        self.assertNotIn("next_token", channel.json_body)

    def _report_user(self, target_user: str, reporter_tok: str) -> None:
        """Report a user"""
        channel = self.make_request(
            "POST",
            "_matrix/client/v3/users/%s/report" % (target_user),
            {"reason": "stone-cold bummer"},
            access_token=reporter_tok,
            shorthand=False,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)

    def _check_fields(self, content: list[JsonDict]) -> None:
        """Checks that all attributes are present in a user report"""
        for c in content:
            self.assertIn("id", c)
            self.assertIn("received_ts", c)
            self.assertIn("user_id", c)
            self.assertIn("target_user_id", c)
            self.assertIn("reason", c)


class UserReportDetailTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        reporting.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.bad_user = self.register_user("user", "pass")
        self.bad_user_tok = self.login("user", "pass")

        channel = self.make_request(
            "POST",
            "_matrix/client/v3/users/%s/report" % (self.bad_user),
            {"reason": "stone-cold bummer"},
            access_token=self.admin_user_tok,
            shorthand=False,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)

        # first created user report gets `id`=2
        self.url = "/_synapse/admin/v1/user_reports/2"

    def test_no_auth(self) -> None:
        """
        Try to get user report without authentication.
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
            access_token=self.bad_user_tok,
        )

        self.assertEqual(403, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_default_success(self) -> None:
        """
        Testing get a reported user
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
            "/_synapse/admin/v1/user_reports/-123",
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
            "/_synapse/admin/v1/user_reports/abcdef",
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
            "/_synapse/admin/v1/user_reports/",
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
            "/_synapse/admin/v1/user_reports/123",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])
        self.assertEqual("User report not found", channel.json_body["error"])

    def _check_fields(self, content: JsonDict) -> None:
        """Checks that all attributes are present in a user report"""
        self.assertIn("id", content)
        self.assertIn("received_ts", content)
        self.assertIn("target_user_id", content)
        self.assertIn("user_id", content)
        self.assertIn("reason", content)


class DeleteUserReportTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self._store = hs.get_datastores().main

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.bad_user = self.register_user("user", "pass")
        self.other_user_tok = self.login("user", "pass")

        # create report
        self.get_success(
            self._store.add_user_report(
                self.bad_user,
                self.admin_user,
                "super bummer",
                self.clock.time_msec(),
            )
        )

        self.url = "/_synapse/admin/v1/user_reports/2"

    def test_no_auth(self) -> None:
        """
        Try to delete user report without authentication.
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
            "/_synapse/admin/v1/user_reports/-123",
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
            "/_synapse/admin/v1/user_reports/abcdef",
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
            "/_synapse/admin/v1/user_reports/",
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
            "/_synapse/admin/v1/user_reports/123",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])
        self.assertEqual("User report not found", channel.json_body["error"])
