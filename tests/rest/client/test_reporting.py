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
from synapse.handlers.reports import is_valid_report_identifier
from synapse.rest.client import directory, login, register, reporting, room
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.util.clock import Clock

from tests import unittest
from tests.server import FakeChannel
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

    @override_config({"rc_reports": {"per_second": 0.5, "burst_count": 1}})
    def test_ratelimit(self) -> None:
        """
        Tests that the room report endpoint is rate limited.
        """
        data = {"reason": "this makes me sad"}

        self._assert_status(200, data)
        self._assert_status(429, data)

        # Add the current user to the ratelimit overrides, allowing them no ratelimiting.
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(self.other_user, 0, 0)
        )

        # Test that the request isn't ratelimited anymore.
        self._assert_status(200, data)

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


class SafetyReportTestCase(unittest.HomeserverTestCase):
    """Tests for the MSC4457 generic reporting endpoint."""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        register.register_servlets,
        reporting.register_servlets,
        room.register_servlets,
        directory.register_servlets,
    ]

    PATH = "/_matrix/client/unstable/org.matrix.msc4457/safety/report"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable the endpoint, unless a test has set the flag via `override_config`.
        config.setdefault("experimental_features", {}).setdefault(
            "msc4457_enabled", True
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.user_id = self.register_user("user", "pass")
        self.user_tok = self.login("user", "pass")
        self.txn_counter = 0

        # Many tests here file several reports in a row, which would otherwise hit
        # the reports rate limit.
        self.get_success(self.store.set_ratelimit_for_user(self.user_id, 0, 0))

    def test_complaint_is_stored(self) -> None:
        """A valid complaint is stored and its ID returned as a string."""
        channel = self._report(
            {
                "type": "complaint",
                "regarding": "@bad:example.org",
                "harm": "org.matrix.msc4456.spam.fraud",
                "description": "they tried to sell me a bridge",
            }
        )
        self.assertEqual(200, channel.code, msg=channel.result["body"])
        report_id = channel.json_body["report_id"]
        self.assertIsInstance(report_id, str)

        row = self.get_success(
            self.store.db_pool.simple_select_one(
                table="safety_report_complaints",
                keyvalues={"id": int(report_id)},
                retcols=("regarding", "harm", "description", "user_id"),
                desc="get_safety_report_complaint",
            )
        )
        self.assertEqual(
            row,
            (
                "@bad:example.org",
                "org.matrix.msc4456.spam.fraud",
                "they tried to sell me a bridge",
                self.user_id,
            ),
        )

    def test_description_optional_for_specific_harm(self) -> None:
        """A specific (non-General/Other) harm doesn't need a description."""
        self._assert_status(
            200,
            {
                "type": "complaint",
                "regarding": "@bad:example.org",
                "harm": "org.matrix.msc4456.harassment.trolling",
            },
        )

    def test_valid_regarding(self) -> None:
        """Every identifier other than namespaced gramma in MSC4457 is syntactically accepted.

        This checks the format only: whether the thing referred to exists is
        covered by the `test_report_*` tests below.
        """
        for regarding in (
            "@bad:example.org",
            # Room v1/v2 event ID.
            "$event:example.org",
            # Room v3 event ID (standard base64).
            "$" + "a+/" * 14 + "a",
            # Room v4+ event ID (URL-safe base64), including room v12.
            "$" + "a-_" * 14 + "a",
            "!room:example.org",
            # MSC4291 (room v12) room ID.
            "!" + "a" * 43,
            "#alias:example.org",
            "server:example.org",
            "server:example.org:8448",
            "server:" + "a" * 230,
            "mxc://example.org/abcdef",
            "m.system",
        ):
            with self.subTest(regarding=regarding):
                self.assertTrue(is_valid_report_identifier(regarding))

    def test_report_system(self) -> None:
        self._assert_status(200, self._complaint("m.system"))

    def test_report_user(self) -> None:
        """Existing local users can be reported; unknown local users can't.
        Remote users can't be checked, so are accepted."""
        other_user_id = self.register_user("other", "pass")
        self._assert_status(200, self._complaint(other_user_id))
        self._assert_status(200, self._complaint("@someone:example.org"))
        self._assert_error(
            404, "M_NOT_FOUND", self._complaint(f"@nobody:{self.hs.hostname}")
        )

    def test_report_event(self) -> None:
        """Events the reporter can see can be reported; unknown events, and events
        the reporter can't see, can't."""
        room_id = self.helper.create_room_as(self.user_id, tok=self.user_tok)
        event_id = self.helper.send(room_id, "spam", tok=self.user_tok)["event_id"]
        self._assert_status(200, self._complaint(event_id))

        # An event in a private room the reporter isn't in.
        other_user_id = self.register_user("other", "pass")
        other_tok = self.login("other", "pass")
        private_room_id = self.helper.create_room_as(
            other_user_id, is_public=False, tok=other_tok
        )
        hidden_event_id = self.helper.send(private_room_id, "secret", tok=other_tok)[
            "event_id"
        ]

        for regarding in (
            hidden_event_id,
            # Room v4+ event ID (URL-safe base64).
            "$" + "a-_" * 14 + "a",
            # Room v1/v2 event ID.
            "$event:example.org",
        ):
            with self.subTest(regarding=regarding):
                self._assert_error(404, "M_NOT_FOUND", self._complaint(regarding))

    def test_report_room(self) -> None:
        """Rooms known to the server can be reported; unknown rooms can't."""
        room_id = self.helper.create_room_as(self.user_id, tok=self.user_tok)
        self._assert_status(200, self._complaint(room_id))

        for regarding in (
            "!room:example.org",
            # MSC4291 (room v12) room ID.
            "!" + "a" * 43,
        ):
            with self.subTest(regarding=regarding):
                self._assert_error(404, "M_NOT_FOUND", self._complaint(regarding))

    def test_report_alias(self) -> None:
        """An existing alias can be reported, and is stored as the room ID it
        resolves to; an unknown alias can't be reported."""
        room_id = self.helper.create_room_as(self.user_id, tok=self.user_tok)
        alias = f"#alias:{self.hs.hostname}"
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/directory/room/{alias}",
            {"room_id": room_id},
            access_token=self.user_tok,
            shorthand=False,
        )
        self.assertEqual(200, channel.code, msg=channel.result["body"])

        channel = self._report(self._complaint(alias))
        self.assertEqual(200, channel.code, msg=channel.result["body"])
        stored = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="safety_report_complaints",
                keyvalues={"id": int(channel.json_body["report_id"])},
                retcol="regarding",
                desc="get_safety_report_complaint",
            )
        )
        self.assertEqual(stored, room_id)

        self._assert_error(
            404, "M_NOT_FOUND", self._complaint(f"#nope:{self.hs.hostname}")
        )

    def test_report_server(self) -> None:
        """Servers known to this homeserver can be reported; unknown servers
        can't."""
        self.get_success(
            self.store.set_destination_retry_timings("known.example.org", None, 0, 0)
        )
        self._assert_status(200, self._complaint("server:known.example.org"))
        self._assert_error(
            404, "M_NOT_FOUND", self._complaint("server:unknown.example.org")
        )

    def test_report_media(self) -> None:
        """Existing local media can be reported; unknown local media can't.
        Remote media can't be checked, so is accepted."""
        self.get_success(
            self.store.store_local_media(
                media_id="abcdef",
                media_type="text/plain",
                time_now_ms=self.clock.time_msec(),
                upload_name=None,
                media_length=1,
                user_id=UserID.from_string(self.user_id),
            )
        )
        self._assert_status(200, self._complaint(f"mxc://{self.hs.hostname}/abcdef"))
        self._assert_status(200, self._complaint("mxc://example.org/abcdef"))
        self._assert_error(
            404, "M_NOT_FOUND", self._complaint(f"mxc://{self.hs.hostname}/nope")
        )

    @override_config(
        {
            "rc_reports": {"per_second": 1000, "burst_count": 1000},
            "rc_reports_failed": {"per_second": 0.1, "burst_count": 2},
        }
    )
    def test_failed_reports_ratelimited(self) -> None:
        """Only reports rejected as not found count towards `rc_reports_failed`.
        Once it is exceeded, all reports are rejected until it recovers."""
        # `prepare` disables ratelimiting for `self.user_id`, so use a fresh user.
        reporter_id = self.register_user("reporter", "pass")
        reporter_tok = self.login("reporter", "pass")
        existing = self._complaint(reporter_id)
        missing = self._complaint(f"@nobody:{self.hs.hostname}")

        # Successful and malformed reports don't count towards the limit.
        for _ in range(5):
            channel = self._report(existing, tok=reporter_tok)
            self.assertEqual(200, channel.code, msg=channel.result["body"])
            channel = self._report(self._complaint("NotNamespaced"), tok=reporter_tok)
            self.assertEqual(400, channel.code, msg=channel.result["body"])

        # Use up the burst of not-found reports.
        for _ in range(2):
            channel = self._report(missing, tok=reporter_tok)
            self.assertEqual(404, channel.code, msg=channel.result["body"])

        # Now even a report about something which exists is rejected.
        channel = self._report(existing, tok=reporter_tok)
        self.assertEqual(429, channel.code, msg=channel.result["body"])
        self.assertEqual("M_LIMIT_EXCEEDED", channel.json_body["errcode"])

        # After waiting for the limit to recover, reports are accepted again.
        self.reactor.advance(10)
        channel = self._report(existing, tok=reporter_tok)
        self.assertEqual(200, channel.code, msg=channel.result["body"])

    def test_invalid_regarding(self) -> None:
        """Identifiers outside the forms listed in MSC4457 are rejected."""
        for regarding in (
            "",
            "@nodomain",
            "@" + "a" * 250 + ":example.org",
            "$",
            # Mixes the standard and URL-safe base64 alphabets.
            "$" + "a+_" * 14 + "a",
            "$" + "a" * 250 + ":example.org",
            "!nodomain",
            "#nodomain",
            "server:",
            "server:not a server",
            "server:" + "a" * 231,
            "mxc://example.org",
            "m.not_system",
            "NotNamespaced",
            # Namespaced identifiers are allowed by MSC4457 but not supported.
            "com.example.thing",
        ):
            with self.subTest(regarding=regarding):
                self._assert_error(
                    400,
                    "M_INVALID_PARAM",
                    {
                        "type": "complaint",
                        "regarding": regarding,
                        "harm": "org.matrix.msc4456.spam.fraud",
                    },
                )

    def test_missing_or_non_string_fields(self) -> None:
        """Missing required fields, or fields of the wrong type, are M_BAD_JSON."""
        valid = {
            "type": "complaint",
            "regarding": "@bad:example.org",
            "harm": "org.matrix.msc4456.spam.fraud",
            "description": "spam",
        }
        for field in ("type", "regarding", "harm"):
            with self.subTest(missing=field):
                body = dict(valid)
                del body[field]
                self._assert_error(400, "M_BAD_JSON", body)
        for field in ("type", "regarding", "harm", "description"):
            with self.subTest(non_string=field):
                self._assert_error(400, "M_BAD_JSON", {**valid, field: 42})

    def test_unknown_type(self) -> None:
        self._assert_error(
            400,
            "M_INVALID_PARAM",
            {
                "type": "appeal",
                "regarding": "@bad:example.org",
                "harm": "org.matrix.msc4456.spam.fraud",
            },
        )

    def test_unknown_harm(self) -> None:
        """Harms not defined by MSC4456, including the stable `m.` forms while the
        MSC is unstable, are rejected."""
        for harm in ("com.example.custom", "m.spam", "org.matrix.msc4456.nope"):
            with self.subTest(harm=harm):
                self._assert_error(
                    400,
                    "M_INVALID_PARAM",
                    {
                        "type": "complaint",
                        "regarding": "@bad:example.org",
                        "harm": harm,
                        "description": "spam",
                    },
                )

    def test_description_required_for_general_harms(self) -> None:
        """General/Other harms require a description."""
        for suffix in (
            "spam",
            "adult",
            "harassment",
            "violence",
            "child_safety",
            "danger",
            "tos",
            "other",
        ):
            harm = f"org.matrix.msc4456.{suffix}"
            with self.subTest(harm=harm):
                body = {
                    "type": "complaint",
                    "regarding": "@bad:example.org",
                    "harm": harm,
                }
                self._assert_error(400, "M_BAD_JSON", body)
                self._assert_status(200, {**body, "description": "details"})

    def test_description_length(self) -> None:
        """The description is limited to 1024 bytes, not characters."""
        body = {
            "type": "complaint",
            "regarding": "@bad:example.org",
            "harm": "org.matrix.msc4456.other",
        }
        self._assert_status(200, {**body, "description": "a" * 1024})
        self._assert_error(400, "M_BAD_JSON", {**body, "description": "a" * 1025})
        # three-byte character
        self._assert_error(400, "M_BAD_JSON", {**body, "description": "€" * 1024})

    def test_description_blank(self) -> None:
        """A description that is empty after trimming whitespace is rejected."""
        body = {
            "type": "complaint",
            "regarding": "@bad:example.org",
            "harm": "org.matrix.msc4456.spam.fraud",
        }
        for description in ("", "   ", "\n\t"):
            with self.subTest(description=description):
                self._assert_error(
                    400, "M_BAD_JSON", {**body, "description": description}
                )

    @override_config({"allow_guest_access": True})
    def test_guest_can_report(self) -> None:
        channel = self.make_request("POST", "/register?kind=guest", {})
        self.assertEqual(200, channel.code, msg=channel.result["body"])
        guest_tok = channel.json_body["access_token"]

        channel = self._report(
            {
                "type": "complaint",
                "regarding": "@bad:example.org",
                "harm": "org.matrix.msc4456.spam.fraud",
            },
            tok=guest_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.result["body"])

    @override_config({"experimental_features": {"msc4457_enabled": False}})
    def test_disabled(self) -> None:
        """The endpoint isn't registered unless MSC4457 is enabled."""
        channel = self._report(
            {
                "type": "complaint",
                "regarding": "@bad:example.org",
                "harm": "org.matrix.msc4456.spam.fraud",
            }
        )
        self.assertEqual(404, channel.code, msg=channel.result["body"])
        self.assertEqual("M_UNRECOGNIZED", channel.json_body["errcode"])

    def test_requires_auth(self) -> None:
        channel = self.make_request(
            "POST",
            f"{self.PATH}/txn",
            {
                "type": "complaint",
                "regarding": "@bad:example.org",
                "harm": "org.matrix.msc4456.spam.fraud",
            },
            shorthand=False,
        )
        self.assertEqual(401, channel.code, msg=channel.result["body"])

    def _complaint(self, regarding: str) -> JsonDict:
        return {
            "type": "complaint",
            "regarding": regarding,
            "harm": "org.matrix.msc4456.spam.fraud",
        }

    def _report(self, data: JsonDict, tok: str | None = None) -> FakeChannel:
        # Use a fresh transaction ID for each request.
        self.txn_counter += 1
        return self.make_request(
            "POST",
            f"{self.PATH}/txn{self.txn_counter}",
            data,
            access_token=tok or self.user_tok,
            shorthand=False,
        )

    def _assert_status(self, response_status: int, data: JsonDict) -> None:
        channel = self._report(data)
        self.assertEqual(response_status, channel.code, msg=channel.result["body"])

    def _assert_error(self, response_status: int, errcode: str, data: JsonDict) -> None:
        channel = self._report(data)
        self.assertEqual(response_status, channel.code, msg=channel.result["body"])
        self.assertEqual(errcode, channel.json_body["errcode"], msg=channel.json_body)
