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
#

"""Tests REST events for /delayed_events paths."""

from http import HTTPStatus
from typing import List, Tuple

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.errors import Codes
from synapse.rest import admin
from synapse.rest.client import delayed_events, login, room, versions
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import HomeserverTestCase

PATH_PREFIX = "/_matrix/client/unstable/org.matrix.msc4140/delayed_events"

_EVENT_TYPE = "com.example.test"


class DelayedEventsUnstableSupportTestCase(HomeserverTestCase):
    servlets = [versions.register_servlets]

    def test_false_by_default(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertFalse(channel.json_body["unstable_features"]["org.matrix.msc4140"])

    @unittest.override_config({"max_event_delay_duration": "24h"})
    def test_true_if_enabled(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertTrue(channel.json_body["unstable_features"]["org.matrix.msc4140"])


class DelayedEventsTestCase(HomeserverTestCase):
    """Tests getting and managing delayed events."""

    servlets = [
        admin.register_servlets,
        delayed_events.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["max_event_delay_duration"] = "24h"
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user1_user_id = self.register_user("user1", "pass")
        self.user1_access_token = self.login("user1", "pass")
        self.user2_user_id = self.register_user("user2", "pass")
        self.user2_access_token = self.login("user2", "pass")

        self.room_id = self.helper.create_room_as(
            self.user1_user_id,
            tok=self.user1_access_token,
            extra_content={
                "preset": "public_chat",
                "power_level_content_override": {
                    "events": {
                        _EVENT_TYPE: 0,
                    }
                },
            },
        )

        self.helper.join(
            room=self.room_id, user=self.user2_user_id, tok=self.user2_access_token
        )

    def test_delayed_events_empty_on_startup(self) -> None:
        scheduled, finalised = self._get_delayed_events()
        self.assertListEqual([], scheduled)
        self.assertListEqual([], finalised)

    def test_delayed_state_events_are_sent_on_timeout(self) -> None:
        state_key = "to_send_on_timeout"

        setter_key = "setter"
        setter_expected = "on_timeout"
        channel = self.make_request(
            "PUT",
            _get_path_for_delayed_state(self.room_id, _EVENT_TYPE, state_key, 900),
            {
                setter_key: setter_expected,
            },
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        scheduled, finalised = self._get_delayed_events()
        self.assertEqual(1, len(scheduled), scheduled)
        self.assertListEqual([], finalised)

        scheduled_event = scheduled[0]
        content = self._get_delayed_event_content(scheduled_event)

        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        self.reactor.advance(1)
        scheduled, finalised = self._get_delayed_events()
        self.assertListEqual([], scheduled)
        self.assertEqual(1, len(finalised), finalised)

        finalised_event_info = finalised[0]
        self.assertDictEqual(scheduled_event, finalised_event_info["delayed_event"])
        self.assertEqual("send", finalised_event_info["outcome"])
        self.assertEqual("delay", finalised_event_info["reason"])
        self.assertNotIn("error", finalised_event_info)
        self.assertIsNotNone(finalised_event_info["event_id"])

        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    @unittest.override_config(
        {"rc_delayed_event_mgmt": {"per_second": 0.5, "burst_count": 1}}
    )
    def test_get_delayed_events_ratelimit(self) -> None:
        args = ("GET", PATH_PREFIX, b"", self.user1_access_token)

        channel = self.make_request(*args)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        channel = self.make_request(*args)
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

        # Add the current user to the ratelimit overrides, allowing them no ratelimiting.
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(
                self.user1_user_id, 0, 0
            )
        )

        # Test that the request isn't ratelimited anymore.
        channel = self.make_request(*args)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

    def test_update_delayed_event_without_id(self) -> None:
        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/",
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)

    def test_update_delayed_event_without_body(self) -> None:
        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/abc",
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)
        self.assertEqual(
            Codes.NOT_JSON,
            channel.json_body["errcode"],
        )

    def test_update_delayed_event_without_action(self) -> None:
        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/abc",
            {},
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)
        self.assertEqual(
            Codes.MISSING_PARAM,
            channel.json_body["errcode"],
        )

    def test_update_delayed_event_with_invalid_action(self) -> None:
        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/abc",
            {"action": "oops"},
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)
        self.assertEqual(
            Codes.INVALID_PARAM,
            channel.json_body["errcode"],
        )

    @parameterized.expand(["cancel", "restart", "send"])
    def test_update_delayed_event_without_match(self, action: str) -> None:
        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/abc",
            {"action": action},
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)

    def test_cancel_delayed_state_event(self) -> None:
        state_key = "to_never_send"

        setter_key = "setter"
        setter_expected = "none"
        channel = self.make_request(
            "PUT",
            _get_path_for_delayed_state(self.room_id, _EVENT_TYPE, state_key, 1500),
            {
                setter_key: setter_expected,
            },
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        self.assertIsNotNone(delay_id)

        self.reactor.advance(1)
        scheduled = self._get_scheduled_delayed_events()
        self.assertEqual(1, len(scheduled), scheduled)

        scheduled_event = scheduled[0]
        content = self._get_delayed_event_content(scheduled_event)
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/{delay_id}",
            {"action": "cancel"},
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        scheduled, finalised = self._get_delayed_events()
        self.assertListEqual([], scheduled)
        self.assertEqual(1, len(finalised), finalised)

        finalised_event_info = finalised[0]
        self.assertDictEqual(scheduled_event, finalised_event_info["delayed_event"])
        self.assertEqual("cancel", finalised_event_info["outcome"])
        self.assertEqual("action", finalised_event_info["reason"])
        self.assertNotIn("error", finalised_event_info)
        self.assertNotIn("event_id", finalised_event_info)

        self.reactor.advance(1)
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

    @unittest.override_config(
        {"rc_delayed_event_mgmt": {"per_second": 0.5, "burst_count": 1}}
    )
    def test_cancel_delayed_event_ratelimit(self) -> None:
        delay_ids = []
        for _ in range(2):
            channel = self.make_request(
                "POST",
                _get_path_for_delayed_send(self.room_id, _EVENT_TYPE, 100000),
                {},
                self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            self.assertIsNotNone(delay_id)
            delay_ids.append(delay_id)

        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/{delay_ids.pop(0)}",
            {"action": "cancel"},
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        args = (
            "POST",
            f"{PATH_PREFIX}/{delay_ids.pop(0)}",
            {"action": "cancel"},
            self.user1_access_token,
        )
        channel = self.make_request(*args)
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

        # Add the current user to the ratelimit overrides, allowing them no ratelimiting.
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(
                self.user1_user_id, 0, 0
            )
        )

        # Test that the request isn't ratelimited anymore.
        channel = self.make_request(*args)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

    def test_send_delayed_state_event(self) -> None:
        state_key = "to_send_on_request"

        setter_key = "setter"
        setter_expected = "on_send"
        channel = self.make_request(
            "PUT",
            _get_path_for_delayed_state(self.room_id, _EVENT_TYPE, state_key, 100000),
            {
                setter_key: setter_expected,
            },
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        self.assertIsNotNone(delay_id)

        self.reactor.advance(1)
        scheduled = self._get_scheduled_delayed_events()
        self.assertEqual(1, len(scheduled), scheduled)

        scheduled_event = scheduled[0]
        content = self._get_delayed_event_content(scheduled_event)
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/{delay_id}",
            {"action": "send"},
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        scheduled, finalised = self._get_delayed_events()
        self.assertListEqual([], scheduled)
        self.assertEqual(1, len(finalised), finalised)

        finalised_event_info = finalised[0]
        self.assertDictEqual(scheduled_event, finalised_event_info["delayed_event"])
        self.assertEqual("send", finalised_event_info["outcome"])
        self.assertEqual("action", finalised_event_info["reason"])
        self.assertNotIn("error", finalised_event_info)
        self.assertIsNotNone(finalised_event_info["event_id"])

        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    @unittest.override_config({"rc_message": {"per_second": 3.5, "burst_count": 4}})
    def test_send_delayed_event_ratelimit(self) -> None:
        delay_ids = []
        for _ in range(2):
            channel = self.make_request(
                "POST",
                _get_path_for_delayed_send(self.room_id, _EVENT_TYPE, 100000),
                {},
                self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            self.assertIsNotNone(delay_id)
            delay_ids.append(delay_id)

        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/{delay_ids.pop(0)}",
            {"action": "send"},
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        args = (
            "POST",
            f"{PATH_PREFIX}/{delay_ids.pop(0)}",
            {"action": "send"},
            self.user1_access_token,
        )
        channel = self.make_request(*args)
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

        # Add the current user to the ratelimit overrides, allowing them no ratelimiting.
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(
                self.user1_user_id, 0, 0
            )
        )

        # Test that the request isn't ratelimited anymore.
        channel = self.make_request(*args)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

    def test_restart_delayed_state_event(self) -> None:
        state_key = "to_send_on_restarted_timeout"

        setter_key = "setter"
        setter_expected = "on_timeout"
        channel = self.make_request(
            "PUT",
            _get_path_for_delayed_state(self.room_id, _EVENT_TYPE, state_key, 1500),
            {
                setter_key: setter_expected,
            },
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        self.assertIsNotNone(delay_id)

        self.reactor.advance(1)
        scheduled = self._get_scheduled_delayed_events()
        self.assertEqual(1, len(scheduled), scheduled)

        scheduled_event = scheduled[0]
        content = self._get_delayed_event_content(scheduled_event)
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/{delay_id}",
            {"action": "restart"},
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        self.reactor.advance(1)

        scheduled, finalised = self._get_delayed_events()
        self.assertEqual(1, len(scheduled), scheduled)
        self.assertListEqual([], finalised)

        content = self._get_delayed_event_content(scheduled[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        self.reactor.advance(1)
        scheduled, finalised = self._get_delayed_events()
        self.assertListEqual([], scheduled)
        self.assertEqual(1, len(finalised), finalised)

        finalised_event_info = finalised[0]
        self.assertEqual("send", finalised_event_info["outcome"])
        self.assertEqual("delay", finalised_event_info["reason"])
        self.assertNotIn("error", finalised_event_info)
        self.assertIsNotNone(finalised_event_info["event_id"])

        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    @unittest.override_config(
        {"rc_delayed_event_mgmt": {"per_second": 0.5, "burst_count": 1}}
    )
    def test_restart_delayed_event_ratelimit(self) -> None:
        delay_ids = []
        for _ in range(2):
            channel = self.make_request(
                "POST",
                _get_path_for_delayed_send(self.room_id, _EVENT_TYPE, 100000),
                {},
                self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            self.assertIsNotNone(delay_id)
            delay_ids.append(delay_id)

        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/{delay_ids.pop(0)}",
            {"action": "restart"},
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        args = (
            "POST",
            f"{PATH_PREFIX}/{delay_ids.pop(0)}",
            {"action": "restart"},
            self.user1_access_token,
        )
        channel = self.make_request(*args)
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

        # Add the current user to the ratelimit overrides, allowing them no ratelimiting.
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(
                self.user1_user_id, 0, 0
            )
        )

        # Test that the request isn't ratelimited anymore.
        channel = self.make_request(*args)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

    def test_delayed_state_is_not_cancelled_by_new_state_from_same_user(
        self,
    ) -> None:
        state_key = "to_not_be_cancelled_by_same_user"

        setter_key = "setter"
        setter_expected = "on_timeout"
        channel = self.make_request(
            "PUT",
            _get_path_for_delayed_state(self.room_id, _EVENT_TYPE, state_key, 900),
            {
                setter_key: setter_expected,
            },
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        scheduled = self._get_scheduled_delayed_events()
        self.assertEqual(1, len(scheduled), scheduled)

        self.helper.send_state(
            self.room_id,
            _EVENT_TYPE,
            {
                setter_key: "manual",
            },
            self.user1_access_token,
            state_key=state_key,
        )
        scheduled = self._get_scheduled_delayed_events()
        self.assertEqual(1, len(scheduled), scheduled)

        self.reactor.advance(1)
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    def test_delayed_state_is_cancelled_by_new_state_from_other_user(
        self,
    ) -> None:
        state_key = "to_be_cancelled_by_other_user"

        setter_key = "setter"
        channel = self.make_request(
            "PUT",
            _get_path_for_delayed_state(self.room_id, _EVENT_TYPE, state_key, 900),
            {
                setter_key: "on_timeout",
            },
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        scheduled = self._get_scheduled_delayed_events()
        self.assertEqual(1, len(scheduled), scheduled)
        scheduled_event = scheduled[0]

        setter_expected = "other_user"
        self.helper.send_state(
            self.room_id,
            _EVENT_TYPE,
            {
                setter_key: setter_expected,
            },
            self.user2_access_token,
            state_key=state_key,
        )

        scheduled, finalised = self._get_delayed_events()
        self.assertListEqual([], scheduled)
        self.assertEqual(1, len(finalised), finalised)

        finalised_event_info = finalised[0]
        self.assertDictEqual(scheduled_event, finalised_event_info["delayed_event"])
        self.assertEqual("cancel", finalised_event_info["outcome"])
        self.assertEqual("error", finalised_event_info["reason"])
        self.assert_dict(
            {
                "errcode": "M_UNKNOWN",
                "org.matrix.msc4140.errcode": "M_CANCELLED_BY_STATE_UPDATE",
            },
            finalised_event_info["error"],
        )
        self.assertNotIn("event_id", finalised_event_info)

        self.reactor.advance(1)
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    def _get_delayed_events(self) -> Tuple[List[JsonDict], List[JsonDict]]:
        channel = self.make_request(
            "GET",
            PATH_PREFIX,
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        scheduled = self._validate_scheduled_delayed_events(channel.json_body)
        finalised = self._validate_finalised_delayed_events(channel.json_body)

        return scheduled, finalised

    def _get_scheduled_delayed_events(self) -> List[JsonDict]:
        channel = self.make_request(
            "GET",
            PATH_PREFIX + "?status=scheduled",
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        scheduled = self._validate_scheduled_delayed_events(channel.json_body)

        return scheduled

    def _get_finalised_delayed_events(self) -> List[JsonDict]:
        channel = self.make_request(
            "GET",
            PATH_PREFIX + "?status=finalised",
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        finalised = self._validate_finalised_delayed_events(channel.json_body)

        return finalised

    def _validate_scheduled_delayed_events(self, json_body: JsonDict) -> List[JsonDict]:
        key = "scheduled"
        self.assertIn(key, json_body)

        scheduled = json_body[key]
        self.assertIsInstance(scheduled, list)

        return scheduled

    def _validate_finalised_delayed_events(self, json_body: JsonDict) -> List[JsonDict]:
        key = "finalised"
        self.assertIn(key, json_body)

        finalised = json_body[key]
        self.assertIsInstance(finalised, list)

        for item in finalised:
            for key in ("delayed_event", "outcome", "reason", "origin_server_ts"):
                self.assertIsNotNone(item.get(key))

        return finalised

    def _get_delayed_event_content(self, event: JsonDict) -> JsonDict:
        key = "content"
        self.assertIn(key, event)

        content = event[key]
        self.assertIsInstance(content, dict)

        return content


def _get_path_for_delayed_state(
    room_id: str, event_type: str, state_key: str, delay_ms: int
) -> str:
    return f"rooms/{room_id}/state/{event_type}/{state_key}?org.matrix.msc4140.delay={delay_ms}"


def _get_path_for_delayed_send(room_id: str, event_type: str, delay_ms: int) -> str:
    return f"rooms/{room_id}/send/{event_type}?org.matrix.msc4140.delay={delay_ms}"
