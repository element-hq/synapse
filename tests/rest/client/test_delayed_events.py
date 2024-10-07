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
from typing import List

from parameterized import parameterized

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.errors import Codes
from synapse.rest.client import delayed_events, room, versions
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest
from tests.unittest import HomeserverTestCase

PATH_PREFIX = "/_matrix/client/unstable/org.matrix.msc4140/delayed_events"

_HS_NAME = "red"
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

    servlets = [delayed_events.register_servlets, room.register_servlets]
    user_id = f"@sid1:{_HS_NAME}"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["server_name"] = _HS_NAME
        config["max_event_delay_duration"] = "24h"
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.room_id = self.helper.create_room_as(
            self.user_id,
            extra_content={
                "preset": "trusted_private_chat",
            },
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
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        self.reactor.advance(1)
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

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

    @parameterized.expand(["cancel", "restart", "send"])
    def test_update_delayed_event_without_match(self, action: str) -> None:
        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/abc",
            {"action": action},
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
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        self.assertIsNotNone(delay_id)

        self.reactor.advance(1)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/{delay_id}",
            {"action": "cancel"},
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertListEqual([], self._get_delayed_events())

        self.reactor.advance(1)
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

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
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        self.assertIsNotNone(delay_id)

        self.reactor.advance(1)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/{delay_id}",
            {"action": "send"},
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

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
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        self.assertIsNotNone(delay_id)

        self.reactor.advance(1)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self.make_request(
            "POST",
            f"{PATH_PREFIX}/{delay_id}",
            {"action": "restart"},
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        self.reactor.advance(1)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(setter_expected, content.get(setter_key), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        self.reactor.advance(1)
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    def test_delayed_state_events_are_cancelled_by_more_recent_state(self) -> None:
        state_key = "to_be_cancelled"

        setter_key = "setter"
        channel = self.make_request(
            "PUT",
            _get_path_for_delayed_state(self.room_id, _EVENT_TYPE, state_key, 900),
            {
                setter_key: "on_timeout",
            },
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)

        setter_expected = "manual"
        self.helper.send_state(
            self.room_id,
            _EVENT_TYPE,
            {
                setter_key: setter_expected,
            },
            None,
            state_key=state_key,
        )
        self.assertListEqual([], self._get_delayed_events())

        self.reactor.advance(1)
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            "",
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

    def _get_delayed_events(self) -> List[JsonDict]:
        channel = self.make_request(
            "GET",
            PATH_PREFIX,
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


def _get_path_for_delayed_state(
    room_id: str, event_type: str, state_key: str, delay_ms: int
) -> str:
    return f"rooms/{room_id}/state/{event_type}/{state_key}?org.matrix.msc4140.delay={delay_ms}"
