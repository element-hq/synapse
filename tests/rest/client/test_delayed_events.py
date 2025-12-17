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

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.errors import Codes
from synapse.rest import admin
from synapse.rest.client import delayed_events, login, room, versions
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.server import FakeChannel
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
        self.assertListEqual([], self._get_delayed_events())

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
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        self.reactor.advance(1)
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    def test_get_delayed_events_auth(self) -> None:
        channel = self.make_request("GET", PATH_PREFIX)
        self.assertEqual(HTTPStatus.UNAUTHORIZED, channel.code, channel.result)

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
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)

    def test_update_delayed_event_without_body(self) -> None:
        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/abc",
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
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)
        self.assertEqual(
            Codes.INVALID_PARAM,
            channel.json_body["errcode"],
        )

    @parameterized.expand(
        (
            (action, action_in_path)
            for action in ("cancel", "restart", "send")
            for action_in_path in (True, False)
        )
    )
    def test_update_delayed_event_without_match(
        self, action: str, action_in_path: bool
    ) -> None:
        channel = self._update_delayed_event("abc", action, action_in_path)
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)

    @parameterized.expand((True, False))
    def test_cancel_delayed_state_event(self, action_in_path: bool) -> None:
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
        assert delay_id is not None

        self.reactor.advance(1)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self._update_delayed_event(delay_id, "cancel", action_in_path)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertListEqual([], self._get_delayed_events())

        self.reactor.advance(1)
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

    @parameterized.expand((True, False))
    @unittest.override_config(
        {"rc_delayed_event_mgmt": {"per_second": 0.5, "burst_count": 1}}
    )
    def test_cancel_delayed_event_ratelimit(self, action_in_path: bool) -> None:
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
            assert delay_id is not None
            delay_ids.append(delay_id)

        channel = self._update_delayed_event(delay_ids.pop(0), "cancel", action_in_path)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        channel = self._update_delayed_event(delay_ids.pop(0), "cancel", action_in_path)
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

    @parameterized.expand((True, False))
    def test_send_delayed_state_event(self, action_in_path: bool) -> None:
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
        assert delay_id is not None

        self.reactor.advance(1)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self._update_delayed_event(delay_id, "send", action_in_path)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    @parameterized.expand((True, False))
    @unittest.override_config({"rc_message": {"per_second": 2.5, "burst_count": 3}})
    def test_send_delayed_event_ratelimit(self, action_in_path: bool) -> None:
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
            assert delay_id is not None
            delay_ids.append(delay_id)

        channel = self._update_delayed_event(delay_ids.pop(0), "send", action_in_path)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        channel = self._update_delayed_event(delay_ids.pop(0), "send", action_in_path)
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

    @parameterized.expand((True, False))
    def test_restart_delayed_state_event(self, action_in_path: bool) -> None:
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
        assert delay_id is not None

        self.reactor.advance(1)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self._update_delayed_event(delay_id, "restart", action_in_path)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        self.reactor.advance(1)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        self.reactor.advance(1)
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    @parameterized.expand((True, False))
    @unittest.override_config(
        {"rc_delayed_event_mgmt": {"per_second": 0.5, "burst_count": 1}}
    )
    def test_restart_delayed_event_ratelimit(self, action_in_path: bool) -> None:
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
            assert delay_id is not None
            delay_ids.append(delay_id)

        channel = self._update_delayed_event(
            delay_ids.pop(0), "restart", action_in_path
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        channel = self._update_delayed_event(
            delay_ids.pop(0), "restart", action_in_path
        )
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

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
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)

        self.helper.send_state(
            self.room_id,
            _EVENT_TYPE,
            {
                setter_key: "manual",
            },
            self.user1_access_token,
            state_key=state_key,
        )
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)

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
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)

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
        self.assertListEqual([], self._get_delayed_events())

        self.reactor.advance(1)
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    def _get_delayed_events(self) -> list[JsonDict]:
        channel = self.make_request(
            "GET",
            PATH_PREFIX,
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        key = "delayed_events"
        self.assertIn(key, channel.json_body)

        events = channel.json_body[key]
        self.assertIsInstance(events, list)

        return events

    def _get_delayed_event_content(self, event: JsonDict) -> JsonDict:
        key = "content"
        self.assertIn(key, event)

        content = event[key]
        self.assertIsInstance(content, dict)

        return content

    def _update_delayed_event(
        self, delay_id: str, action: str, action_in_path: bool
    ) -> FakeChannel:
        path = f"{PATH_PREFIX}/{delay_id}"
        body = {}
        if action_in_path:
            path += f"/{action}"
        else:
            body["action"] = action
        return self.make_request("POST", path, body)


def _get_path_for_delayed_state(
    room_id: str, event_type: str, state_key: str, delay_ms: int
) -> str:
    return f"rooms/{room_id}/state/{event_type}/{state_key}?org.matrix.msc4140.delay={delay_ms}"


def _get_path_for_delayed_send(room_id: str, event_type: str, delay_ms: int) -> str:
    return f"rooms/{room_id}/send/{event_type}?org.matrix.msc4140.delay={delay_ms}"
