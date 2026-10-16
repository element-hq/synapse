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

import sqlite3
from http import HTTPStatus
from typing import Any, Literal, overload
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    EventUnsignedContentFields,
    GuestAccess,
    StickyEvent,
)
from synapse.api.errors import Codes, NotFoundError
from synapse.appservice import ApplicationService
from synapse.rest import admin
from synapse.rest.client import (
    account,
    delayed_events,
    login,
    register,
    room,
    sync,
    versions,
)
from synapse.rest.client.delayed_events import _UpdateDelayedEventAction
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID, create_requester
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.server import FakeChannel, make_request
from tests.unittest import HomeserverTestCase
from tests.utils import USE_POSTGRES_FOR_TESTS

_MANAGEMENT_PATH_PREFIX = "/_matrix/client/unstable/org.matrix.msc4140/delayed_events"

_EVENT_TYPE = "com.example.test"


class DelayedEventsUnstableSupportTestCase(HomeserverTestCase):
    servlets = [versions.register_servlets]

    def tearDown(self) -> None:
        # MemoryReactor doesn't trigger the shutdown phases, and we want the
        # Tokio thread pool to be stopped
        # XXX: This logic should probably get moved somewhere else
        shutdown_triggers = self.reactor.triggers.get("shutdown", {})
        for phase in ["before", "during", "after"]:
            triggers = shutdown_triggers.get(phase, [])
            for callbable, args, kwargs in triggers:
                callbable(*args, **kwargs)

    def test_false_by_default(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertFalse(channel.json_body["unstable_features"]["org.matrix.msc4140"])

    @unittest.override_config({"max_event_delay_duration": "24h"})
    def test_true_if_enabled(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertTrue(channel.json_body["unstable_features"]["org.matrix.msc4140"])


class DelayedEventsHelperMixin(HomeserverTestCase):
    room_id: str

    def _make_delayed_event_request(
        self,
        *,
        room_id: str,
        delay: Duration,
        event_type: str,
        state_key: str | None = None,
        content: JsonDict,
        txn_id: str | None = None,
        access_token: str,
    ) -> FakeChannel:
        """
        Build and send a request for scheduling a delayed event via the dedicated endpoint.

        Args:
            room_id: The room to send the event to.
            delay: How long to wait before sending the event.
            event_type: The type of the event to send.
            state_key: The state key of the event, or None for a message event.
            content: The content of the event.
            txn_id: The transaction ID for a delayed message event request.
                Ignored for a delayed state event request.
                Otherwise, generated if not given.
            access_token: The access token of the user to make the request for.

        Returns:
            The `FakeChannel` object which stores the result of the request.
        """
        return self.make_request(
            "PUT",
            self.helper.build_delayed_event_request_path(
                room_id=room_id,
                event_type=event_type,
                txn_id=txn_id,
            ),
            self.helper.build_delayed_event_request_body(
                delay=delay,
                state_key=state_key,
                content=content,
            ),
            access_token,
        )

    @overload
    def _check_for_delayed_event_in_sync(
        self, access_token: str, delay_id: str, should_find: Literal[True]
    ) -> JsonDict: ...

    @overload
    def _check_for_delayed_event_in_sync(
        self, access_token: str, delay_id: str, should_find: Literal[False]
    ) -> None: ...

    def _check_for_delayed_event_in_sync(
        self, access_token: str, delay_id: str, should_find: bool
    ) -> JsonDict | None:
        """Call /sync and look for a synced event with a specified delay_id.
        At most one event will ever have a matching delay_id.

        Args:
            access_token: The access token of the user to call /sync for.
            delay_id: The delay_id to search for in synced events.
            should_find: Whether /sync should include an event with a matching delay_id.

        Returns:
            The synced event with the matching delay_id, if any.
        """
        channel = self.make_request("GET", "/sync", access_token=access_token)
        self.assertEqual(HTTPStatus.OK, channel.code)

        rooms = channel.json_body.get("rooms", {})
        events = []
        for membership in "join", "leave":
            room = rooms.get(membership, {}).get(self.room_id)
            if room is not None:
                events += room["timeline"]["events"]

        found: JsonDict | None = None
        for event in events:
            if event["unsigned"].get("org.matrix.msc4140.delay_id") == delay_id:
                if not should_find:
                    self.fail(
                        f"Found event {event['event_id']} with matching delay_id {delay_id}, but expected to not find one"
                    )
                if found is not None:
                    self.fail(
                        f"Events {found['event_id']} & {event['event_id']} have matching delay_id {delay_id}, but delay_id should be unique"
                    )
                found = event
        if should_find and found is None:
            self.fail(f"Did not find any event with matching delay_id {delay_id}")
        return found


class DelayedEventsTestCaseBase(DelayedEventsHelperMixin):
    """Room and user fixtures, and request helpers, for the delayed events tests."""

    servlets = [
        admin.register_servlets,
        account.register_servlets,
        delayed_events.register_servlets,
        login.register_servlets,
        register.register_servlets,
        room.register_servlets,
        sync.register_servlets,
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

        # Advance enough time where any requests we made during `prepare(...)` doesn't
        # affect the rate-limits in the test itself
        self.reactor.advance(Duration(days=1).as_secs())

    def _get_delayed_events(self) -> list[JsonDict]:
        channel = self.make_request(
            "GET",
            _MANAGEMENT_PATH_PREFIX,
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        events = channel.json_body["delayed_events"]
        self.assertIsInstance(events, list)

        return events

    def _get_delayed_event(
        self, delay_id: str, access_token: str | None = None
    ) -> JsonDict:
        """Look up a single delayed event, which must exist.

        Args:
            delay_id: The ID of the delayed event to look up.
            access_token: The access token of the user who scheduled it. Defaults to user1's.
        """
        channel = self.make_request(
            "GET",
            f"{_MANAGEMENT_PATH_PREFIX}/{delay_id}",
            access_token=access_token or self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        return channel.json_body

    def _assert_scheduled(
        self, delay_id: str, access_token: str | None = None
    ) -> JsonDict:
        """Assert that a delayed event is still scheduled, and return it.

        Args:
            delay_id: The ID of the delayed event to look up.
            access_token: The access token of the user who scheduled it. Defaults to user1's.
        """
        delayed_event = self._get_delayed_event(delay_id, access_token)
        self.assertNotIn("finalised", delayed_event)
        return delayed_event

    def _assert_finalised(
        self,
        delay_id: str,
        *,
        event_id: str | None = None,
        errcode: str | None = None,
        access_token: str | None = None,
    ) -> JsonDict:
        """Assert that a delayed event has been finalised, and return
        its `finalised` object.

        Args:
            delay_id: The ID of the delayed event to look up.
            event_id: The ID of the event that the delayed event was sent as, if it was sent.
            errcode: The error code that prevented the delayed event from being sent, if any.
                If neither this nor `event_id` is set, the delayed event must have been
                cancelled by the user.
            access_token: The access token of the user who scheduled it. Defaults to user1's.
        """
        delayed_event = self._get_delayed_event(delay_id, access_token)
        finalised = delayed_event.get("finalised")
        self.assertIsInstance(finalised, dict, delayed_event)
        assert finalised is not None

        finalised_ts = finalised.get("finalised_ts")
        self.assertIsInstance(finalised_ts, int, finalised)
        self.assertLessEqual(finalised_ts, self.clock.time_msec())
        self.assertGreaterEqual(finalised_ts, delayed_event["delayed_since_ts"])

        if event_id is not None:
            self.assertDictEqual(
                {"finalised_ts": finalised_ts, "event_id": event_id}, finalised
            )
        elif errcode is not None:
            self.assertNotIn("event_id", finalised)
            self.assertEqual(
                errcode, finalised.get("error", {}).get("errcode"), finalised
            )
        else:
            self.assertDictEqual({"finalised_ts": finalised_ts}, finalised)

        return finalised

    def _get_delayed_event_content(self, event: JsonDict) -> JsonDict:
        content = event["content"]
        self.assertIsInstance(content, dict)

        return content

    def _schedule_delayed_event(self, access_token: str) -> str:
        """Schedule a delayed message event with a short delay, returning its delay_id."""
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=900),
            event_type=_EVENT_TYPE,
            content={},
            access_token=access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None
        return delay_id

    def _update_delayed_event(
        self,
        delay_id: str,
        action: _UpdateDelayedEventAction,
        action_in_path: bool,
        access_token: str | None = None,
        appservice_user_id: str | None = None,
    ) -> FakeChannel:
        """
        Call the endpoint to cancel/restart/send a scheduled delayed event.

        Args:
            delay_id: The ID of the delayed event to manage.
            action: The management action to take on the target delayed event.
            action_in_path: If False, uses the deprecated version of this endpoint
                that expects the action in the request body instead of the path.
            access_token: The access token of the user to call the endpoint for.
            appservice_user_id: The user an appservice asserts with `?user_id=`,
                when `access_token` is an appservice token.
        """
        path = f"{_MANAGEMENT_PATH_PREFIX}/{delay_id}"
        body = {}
        if action_in_path:
            path += f"/{action.value}"
        else:
            body["action"] = action.value
        if appservice_user_id is not None:
            path += "?" + urlencode({"user_id": appservice_user_id})
        return self.make_request("POST", path, body, access_token)


class DelayedEventsTestCase(DelayedEventsTestCaseBase):
    """Tests getting and managing delayed events."""

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # An appservice that may act on behalf of any user of the homeserver.
        self.appservice = ApplicationService(
            token="i_am_an_app_service",
            id="1234",
            namespaces={"users": [{"regex": "@.*", "exclusive": False}]},
            sender=UserID.from_string("@as_main:test"),
        )

        mock_load_appservices = Mock(return_value=[self.appservice])
        with patch(
            "synapse.storage.databases.main.appservice.load_appservices",
            mock_load_appservices,
        ):
            return self.setup_test_homeserver()

    def test_delayed_events_empty_on_startup(self) -> None:
        self.assertListEqual([], self._get_delayed_events())

    def test_delayed_event_lookup(self) -> None:
        # Schedule a message event
        delay = Duration(milliseconds=100000)
        content: JsonDict = {"message": "hello"}
        delayed_since_ts = self.hs.get_clock().time_msec()
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=delay,
            event_type=_EVENT_TYPE,
            content=content,
            access_token=self.user1_access_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        delay_id = channel.json_body["delay_id"]

        # Test that the scheduled delayed event can be retrieved
        channel = self.make_request(
            "GET",
            f"{_MANAGEMENT_PATH_PREFIX}/{delay_id}",
            access_token=self.user1_access_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

        # Assert the stored properties of the delayed event
        event = channel.json_body
        self.assertDictEqual(
            event,
            {
                "delay_id": delay_id,
                "room_id": self.room_id,
                "type": _EVENT_TYPE,
                "delay_ms": delay.as_millis(),
                "delayed_since_ts": delayed_since_ts,
                "content": content,
            },
        )

        # Test that a non-existent delayed event cannot be found
        channel = self.make_request(
            "GET",
            f"{_MANAGEMENT_PATH_PREFIX}/{delay_id}-fake",
            access_token=self.user1_access_token,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND, channel.result)

        # Test that other users cannot access this delayed event
        channel = self.make_request(
            "GET",
            f"{_MANAGEMENT_PATH_PREFIX}/{delay_id}",
            access_token=self.user2_access_token,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND, channel.result)

        # Now schedule a state event.
        # Do it in this test, as opposed to a new one, to confirm that the correct delayed event
        # is retrieved when multiple delayed events have been scheduled.
        delay += Duration(milliseconds=2000)
        state_key = ""
        state_event_type = _EVENT_TYPE + "_state"
        content = {"state_message": "greetings"}
        delayed_since_ts = self.hs.get_clock().time_msec()
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=delay,
            event_type=state_event_type,
            state_key=state_key,
            content=content,
            access_token=self.user1_access_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        delay_id_2 = channel.json_body["delay_id"]

        # Test that the new delayed event has a different ID from the previous one
        self.assertNotEqual(delay_id, delay_id_2)

        # Test that the scheduled delayed event can be retrieved
        channel = self.make_request(
            "GET",
            f"{_MANAGEMENT_PATH_PREFIX}/{delay_id_2}",
            access_token=self.user1_access_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

        # Assert the stored properties of the delayed event
        state_event = channel.json_body
        self.assertDictEqual(
            state_event,
            {
                "delay_id": delay_id_2,
                "room_id": self.room_id,
                "type": state_event_type,
                "state_key": state_key,
                "delay_ms": delay.as_millis(),
                "delayed_since_ts": delayed_since_ts,
                "content": content,
            },
        )

        # Test that the list lookup retrieves the same items (with legacy fields included)
        self.assertEqual(
            self._get_delayed_events(),
            [
                event
                | {
                    "delay": event["delay_ms"],
                    "running_since": event["delayed_since_ts"],
                },
                state_event
                | {
                    "delay": state_event["delay_ms"],
                    "running_since": state_event["delayed_since_ts"],
                },
            ],
        )

    def test_delayed_state_events_are_sent_on_timeout(self) -> None:
        state_key = "to_send_on_timeout"

        setter_key = "setter"
        setter_expected = "on_timeout"
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=900),
            event_type=_EVENT_TYPE,
            state_key=state_key,
            content={
                setter_key: setter_expected,
            },
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

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

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_id, True
        )
        self._check_for_delayed_event_in_sync(self.user2_access_token, delay_id, False)

        self._assert_finalised(delay_id, event_id=event["event_id"])

    @unittest.override_config({"allow_guest_access": True})
    def test_guest_can_schedule_delayed_event(self) -> None:
        """Test that a guest user can schedule a delayed event."""
        channel = self.make_request("POST", "/register?kind=guest", {})
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        guest_user_id = channel.json_body["user_id"]
        guest_access_token = channel.json_body["access_token"]

        self.helper.send_state(
            self.room_id,
            EventTypes.GuestAccess,
            {EventContentFields.GUEST_ACCESS: GuestAccess.CAN_JOIN},
            tok=self.user1_access_token,
        )
        self.helper.join(room=self.room_id, user=guest_user_id, tok=guest_access_token)

        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=900),
            event_type=_EVENT_TYPE,
            content={"body": "from a guest"},
            access_token=guest_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        event = self._check_for_delayed_event_in_sync(
            guest_access_token, delay_id, True
        )
        self.assertEqual(guest_user_id, event["sender"], event)

        self._assert_finalised(
            delay_id, event_id=event["event_id"], access_token=guest_access_token
        )

    def test_delayed_member_events_are_sent_on_timeout(self) -> None:
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=900),
            event_type="m.room.member",
            state_key=self.user2_user_id,
            content={
                "membership": "leave",
                "reason": "Delayed kick",
            },
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual("leave", content.get("membership"), content)
        self.assertEqual("Delayed kick", content.get("reason"), content)

        content = self.helper.get_state(
            self.room_id,
            "m.room.member",
            self.user1_access_token,
            state_key=self.user2_user_id,
        )
        self.assertEqual("join", content.get("membership"), content)

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            "m.room.member",
            self.user1_access_token,
            state_key=self.user2_user_id,
        )
        self.assertEqual("leave", content.get("membership"), content)
        self.assertEqual("Delayed kick", content.get("reason"), content)

        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_id, True
        )
        self._check_for_delayed_event_in_sync(self.user2_access_token, delay_id, False)

        self._assert_finalised(delay_id, event_id=event["event_id"])

    def test_delayed_event_is_finalised_with_error_if_timeout_send_fails(
        self,
    ) -> None:
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=900),
            event_type=_EVENT_TYPE,
            content={},
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Leave the room so that the delayed event can no longer be sent
        self.helper.leave(self.room_id, self.user1_user_id, tok=self.user1_access_token)

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        self.assertListEqual([], self._get_delayed_events())
        self._check_for_delayed_event_in_sync(self.user1_access_token, delay_id, False)
        self._check_for_delayed_event_in_sync(self.user2_access_token, delay_id, False)

        self._assert_finalised(delay_id, errcode=Codes.FORBIDDEN)

    @unittest.override_config({"rc_message": {"per_second": 0.1, "burst_count": 3}})
    def test_delayed_event_is_finalised_with_error_if_timeout_send_is_ratelimited(
        self,
    ) -> None:
        delay_ids = []
        for delay in Duration(milliseconds=800), Duration(milliseconds=900):
            channel = self._make_delayed_event_request(
                room_id=self.room_id,
                delay=delay,
                event_type=_EVENT_TYPE,
                content={},
                access_token=self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            assert delay_id is not None
            delay_ids.append(delay_id)

        # Both delayed events are due, but only the first fits in the ratelimit.
        # The second must not be retried, and its failure must be recorded.
        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        self.assertListEqual([], self._get_delayed_events())

        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_ids[0], True
        )
        self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_ids[1], False
        )

        self._assert_finalised(delay_ids[0], event_id=event["event_id"])
        self._assert_finalised(delay_ids[1], errcode=Codes.LIMIT_EXCEEDED)

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=100).as_secs())
        self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_ids[1], False
        )

    @unittest.override_config(
        {"experimental_features": {"msc4140_max_delayed_events_per_user": 2}}
    )
    def test_finalised_delayed_events_do_not_count_towards_user_limit(self) -> None:
        # Disable rate-limits for this user, to test only the limit on scheduled events
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(
                self.user1_user_id, 0, 0
            )
        )

        def schedule(delay: Duration) -> FakeChannel:
            return self._make_delayed_event_request(
                room_id=self.room_id,
                delay=delay,
                event_type=_EVENT_TYPE,
                content={},
                access_token=self.user1_access_token,
            )

        # Finalise more delayed events than the limit allows to be scheduled at once:
        # one cancelled, one sent manually, one sent on timeout
        finalised_delay_ids = []
        for delay, action in (
            (Duration(milliseconds=1000), _UpdateDelayedEventAction.CANCEL),
            (Duration(milliseconds=1000), _UpdateDelayedEventAction.SEND),
            (Duration(milliseconds=900), None),
        ):
            channel = schedule(delay)
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body["delay_id"]
            finalised_delay_ids.append(delay_id)
            if action is not None:
                channel = self._update_delayed_event(
                    delay_id, action, True, self.user1_access_token
                )
                self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        for delay_id in finalised_delay_ids:
            self.assertIn("finalised", self._get_delayed_event(delay_id))
        self.assertListEqual([], self._get_delayed_events())

        # The finalised delayed events must not count towards the limit
        for delay in Duration(seconds=5), Duration(seconds=6):
            channel = schedule(delay)
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertEqual(2, len(self._get_delayed_events()))

        # ...nor towards the time to wait until the limit is no longer reached,
        # which must be computed from the scheduled delayed events only
        channel = schedule(Duration(seconds=7))
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)
        self.assertEqual(
            Codes.LIMIT_EXCEEDED, channel.json_body["errcode"], channel.json_body
        )
        retry_after_headers = channel.headers.getRawHeaders("Retry-After")
        assert retry_after_headers
        self.assertEqual(5, int(retry_after_headers[0]))

    @unittest.override_config(
        {"experimental_features": {"msc4140_finalised_retention_period": "1m"}}
    )
    def test_finalised_delayed_events_are_pruned_after_retention_period(
        self,
    ) -> None:
        # Disable rate-limits for this user, as this test makes many lookups
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(
                self.user1_user_id, 0, 0
            )
        )

        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=900),
            event_type=_EVENT_TYPE,
            content={},
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_id, True
        )
        self._assert_finalised(delay_id, event_id=event["event_id"])

        # The finalised delayed event must be discoverable within its retention period
        self.reactor.advance(Duration(seconds=30).as_secs())
        self._assert_finalised(delay_id, event_id=event["event_id"])

        # ...but not after it
        self.reactor.advance(Duration(seconds=30).as_secs())
        channel = self.make_request(
            "GET",
            f"{_MANAGEMENT_PATH_PREFIX}/{delay_id}",
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"], channel.result)

        # The pruning scheduler should have no work left
        self.assertIsNone(
            self.hs.get_delayed_events_handler()._next_prune_finalised_call
        )

    @unittest.override_config(
        {"experimental_features": {"msc4140_finalised_retention_limit_per_user": 2}}
    )
    def test_finalised_delayed_events_are_pruned_beyond_retention_limit(
        self,
    ) -> None:
        # Disable rate-limits for these users, as this test makes many lookups
        for user_id in (self.user1_user_id, self.user2_user_id):
            self.get_success(
                self.hs.get_datastores().main.set_ratelimit_for_user(user_id, 0, 0)
            )

        def schedule_and_cancel(access_token: str) -> str:
            channel = self._make_delayed_event_request(
                room_id=self.room_id,
                delay=Duration(seconds=100),
                event_type=_EVENT_TYPE,
                content={},
                access_token=access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            assert delay_id is not None
            channel = self._update_delayed_event(
                delay_id, _UpdateDelayedEventAction.CANCEL, True, access_token
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            # Space out the finalisation times, so that their order is unambiguous
            self.reactor.advance(Duration(seconds=1).as_secs())
            return delay_id

        # Finalise one more delayed event than the limit for user1,
        # and one for user2 to check that the limit applies per user
        user2_delay_id = schedule_and_cancel(self.user2_access_token)
        user1_delay_ids = [
            schedule_and_cancel(self.user1_access_token) for _ in range(3)
        ]

        # Only the oldest finalised delayed event of user1 is pruned
        channel = self.make_request(
            "GET",
            f"{_MANAGEMENT_PATH_PREFIX}/{user1_delay_ids[0]}",
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"], channel.result)
        for delay_id in user1_delay_ids[1:]:
            self._assert_finalised(delay_id)
        self._assert_finalised(user2_delay_id, access_token=self.user2_access_token)

        # The pruning scheduler should have work left to do
        self.assertIsNotNone(
            self.hs.get_delayed_events_handler()._next_prune_finalised_call
        )

    def test_get_delayed_events_auth(self) -> None:
        channel = self.make_request("GET", _MANAGEMENT_PATH_PREFIX)
        self.assertEqual(HTTPStatus.UNAUTHORIZED, channel.code, channel.result)
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    @unittest.override_config(
        {"rc_delayed_event_mgmt": {"per_second": 0.5, "burst_count": 1}}
    )
    def test_get_delayed_events_ratelimit(self) -> None:
        args = ("GET", _MANAGEMENT_PATH_PREFIX, b"", self.user1_access_token)

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
            f"{_MANAGEMENT_PATH_PREFIX}/",
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)

    def test_update_delayed_event_without_body(self) -> None:
        channel = self.make_request(
            "POST",
            f"{_MANAGEMENT_PATH_PREFIX}/abc",
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
            f"{_MANAGEMENT_PATH_PREFIX}/abc",
            {},
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)
        self.assertEqual(
            Codes.MISSING_PARAM,
            channel.json_body["errcode"],
        )

    def test_update_delayed_event_with_invalid_action(self) -> None:
        channel = self.make_request(
            "POST",
            f"{_MANAGEMENT_PATH_PREFIX}/abc",
            {"action": "oops"},
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)
        self.assertEqual(
            Codes.INVALID_PARAM,
            channel.json_body["errcode"],
        )

    @parameterized.expand(
        (
            (action, action_in_path)
            for action in _UpdateDelayedEventAction
            for action_in_path in (True, False)
        )
    )
    def test_update_delayed_event_without_match(
        self,
        action: _UpdateDelayedEventAction,
        action_in_path: bool,
    ) -> None:
        channel = self._update_delayed_event(
            "abc", action, action_in_path, self.user1_access_token
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    @parameterized.expand(
        (
            (action, action_in_path)
            for action in _UpdateDelayedEventAction
            for action_in_path in (True, False)
        )
    )
    def test_update_delayed_event_without_auth(
        self,
        action: _UpdateDelayedEventAction,
        action_in_path: bool,
    ) -> None:
        """Delayed events cannot be managed without an access token."""
        delay_id = self._schedule_delayed_event(self.user1_access_token)

        channel = self._update_delayed_event(delay_id, action, action_in_path)
        self.assertEqual(HTTPStatus.UNAUTHORIZED, channel.code, channel.result)
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

        # The event is still scheduled, and is sent on its original timeout.
        self.assertEqual(1, len(self._get_delayed_events()))
        self._check_for_delayed_event_in_sync(self.user1_access_token, delay_id, False)
        self.reactor.advance(1)
        self._check_for_delayed_event_in_sync(self.user1_access_token, delay_id, True)

    @parameterized.expand(
        (
            (action, action_in_path)
            for action in _UpdateDelayedEventAction
            for action_in_path in (True, False)
        )
    )
    def test_update_delayed_event_of_other_user(
        self,
        action: _UpdateDelayedEventAction,
        action_in_path: bool,
    ) -> None:
        """Delayed events can only be managed by the user who scheduled them."""
        delay_id = self._schedule_delayed_event(self.user1_access_token)

        channel = self._update_delayed_event(
            delay_id, action, action_in_path, self.user2_access_token
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

        # The event is still scheduled, and is sent on its original timeout.
        self.assertEqual(1, len(self._get_delayed_events()))
        self._check_for_delayed_event_in_sync(self.user1_access_token, delay_id, False)
        self.reactor.advance(1)
        self._check_for_delayed_event_in_sync(self.user1_access_token, delay_id, True)

    @parameterized.expand(
        (
            (action, action_in_path)
            for action in _UpdateDelayedEventAction
            for action_in_path in (True, False)
        )
    )
    def test_update_delayed_event_as_appservice(
        self,
        action: _UpdateDelayedEventAction,
        action_in_path: bool,
    ) -> None:
        """An appservice can manage the delayed events of the user it asserts."""
        delay_id = self._schedule_delayed_event(self.user1_access_token)

        # Asserting a different user gives no access to user1's delayed event.
        channel = self._update_delayed_event(
            delay_id,
            action,
            action_in_path,
            self.appservice.token,
            appservice_user_id=self.user2_user_id,
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])
        self.assertEqual(1, len(self._get_delayed_events()))

        channel = self._update_delayed_event(
            delay_id,
            action,
            action_in_path,
            self.appservice.token,
            appservice_user_id=self.user1_user_id,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        if action == _UpdateDelayedEventAction.CANCEL:
            self.assertListEqual([], self._get_delayed_events())
            self.reactor.advance(1)
            self._check_for_delayed_event_in_sync(
                self.user1_access_token, delay_id, False
            )
        elif action == _UpdateDelayedEventAction.RESTART:
            self.assertEqual(1, len(self._get_delayed_events()))
            self.reactor.advance(1)
            self._check_for_delayed_event_in_sync(
                self.user1_access_token, delay_id, True
            )
        else:
            self.assertListEqual([], self._get_delayed_events())
            self._check_for_delayed_event_in_sync(
                self.user1_access_token, delay_id, True
            )

    @unittest.override_config(
        {
            "allow_guest_access": True,
            # This test makes more management requests than the default burst allows.
            "rc_delayed_event_mgmt": {"per_second": 100, "burst_count": 100},
        }
    )
    def test_guest_can_manage_delayed_events(self) -> None:
        """All delayed event endpoints are available to guest accounts."""
        self.helper.send_state(
            self.room_id,
            EventTypes.GuestAccess,
            {EventContentFields.GUEST_ACCESS: GuestAccess.CAN_JOIN},
            tok=self.user1_access_token,
        )

        channel = self.make_request("POST", "/register?kind=guest", {})
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        guest_user_id = channel.json_body["user_id"]
        guest_access_token = channel.json_body["access_token"]

        self.helper.join(self.room_id, user=guest_user_id, tok=guest_access_token)

        delay_id = self._schedule_delayed_event(guest_access_token)

        channel = self.make_request(
            "GET", _MANAGEMENT_PATH_PREFIX, access_token=guest_access_token
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertEqual(
            [delay_id],
            [event["delay_id"] for event in channel.json_body["delayed_events"]],
        )

        channel = self.make_request(
            "GET",
            f"{_MANAGEMENT_PATH_PREFIX}/{delay_id}",
            access_token=guest_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        for action in (
            _UpdateDelayedEventAction.RESTART,
            _UpdateDelayedEventAction.CANCEL,
        ):
            channel = self._update_delayed_event(
                delay_id, action, True, guest_access_token
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        channel = self.make_request(
            "GET", _MANAGEMENT_PATH_PREFIX, access_token=guest_access_token
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertListEqual([], channel.json_body["delayed_events"])

        delay_id = self._schedule_delayed_event(guest_access_token)

        channel = self._update_delayed_event(
            delay_id, _UpdateDelayedEventAction.SEND, True, guest_access_token
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self._check_for_delayed_event_in_sync(guest_access_token, delay_id, True)

    @parameterized.expand((True, False))
    def test_cancel_delayed_state_event(self, action_in_path: bool) -> None:
        state_key = "to_never_send"

        setter_key = "setter"
        setter_expected = "none"
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=1500),
            event_type=_EVENT_TYPE,
            state_key=state_key,
            content={
                setter_key: setter_expected,
            },
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
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

        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.CANCEL,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertListEqual([], self._get_delayed_events())
        self._assert_finalised(delay_id)

        # Other users must not be able to look up the finalised delayed event either
        channel = self.make_request(
            "GET",
            f"{_MANAGEMENT_PATH_PREFIX}/{delay_id}",
            access_token=self.user2_access_token,
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, channel.code, channel.result)

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        self._check_for_delayed_event_in_sync(self.user1_access_token, delay_id, False)
        self._check_for_delayed_event_in_sync(self.user2_access_token, delay_id, False)
        self._assert_finalised(delay_id)

    @parameterized.expand((True, False))
    @unittest.override_config(
        {"rc_delayed_event_mgmt": {"per_second": 0.5, "burst_count": 1}}
    )
    def test_cancel_delayed_event_ratelimit(self, action_in_path: bool) -> None:
        delay_ids = []
        for _ in range(2):
            channel = self._make_delayed_event_request(
                room_id=self.room_id,
                delay=Duration(milliseconds=100000),
                event_type=_EVENT_TYPE,
                content={},
                access_token=self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            assert delay_id is not None
            delay_ids.append(delay_id)

        delay_id = delay_ids.pop(0)
        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.CANCEL,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        delay_id = delay_ids.pop(0)
        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.CANCEL,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

        # Add the current user to the ratelimit overrides, allowing them no ratelimiting.
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(
                self.user1_user_id, 0, 0
            )
        )

        # Test that the request isn't ratelimited anymore.
        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.CANCEL,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

    @parameterized.expand(
        (
            (content_property_value, action_in_path)
            for content_property_value in ("test", "tест")
            for action_in_path in (True, False)
        )
    )
    def test_send_delayed_state_event(
        self, content_value: str, action_in_path: bool
    ) -> None:
        state_key = "to_send_on_request"

        content_property_name = "key"
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=100000),
            event_type=_EVENT_TYPE,
            state_key=state_key,
            content={
                content_property_name: content_value,
            },
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        events = self._get_delayed_events()
        self.assertEqual(1, len(events), events)
        content = self._get_delayed_event_content(events[0])
        self.assertEqual(content_value, content.get(content_property_name), content)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.SEND,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(content_value, content.get(content_property_name), content)

        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_id, True
        )
        self._check_for_delayed_event_in_sync(self.user2_access_token, delay_id, False)

        self._assert_finalised(delay_id, event_id=event["event_id"])

    @parameterized.expand((True, False))
    @unittest.override_config({"rc_message": {"per_second": 2.5, "burst_count": 3}})
    def test_send_delayed_event_ratelimit(self, action_in_path: bool) -> None:
        delay_ids = []
        for _ in range(2):
            channel = self._make_delayed_event_request(
                room_id=self.room_id,
                delay=Duration(milliseconds=100000),
                event_type=_EVENT_TYPE,
                content={},
                access_token=self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            assert delay_id is not None
            delay_ids.append(delay_id)

        channel = self._update_delayed_event(
            delay_ids.pop(0),
            _UpdateDelayedEventAction.SEND,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        delay_id = delay_ids.pop(0)
        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.SEND,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

        # The delayed event that could not be sent must stay scheduled
        self._assert_scheduled(delay_id)
        self.assertEqual(1, len(self._get_delayed_events()))

    def test_send_delayed_event_fails_and_stays_scheduled_until_retried(
        self,
    ) -> None:
        # Disable rate-limits for this user, which this test is not concerned with
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(
                self.user1_user_id, 0, 0
            )
        )

        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(seconds=100),
            event_type=_EVENT_TYPE,
            content={},
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Leave the room, so that the delayed event cannot be sent
        self.helper.leave(self.room_id, self.user1_user_id, tok=self.user1_access_token)

        # The response must be the error that sending the event would have given
        channel = self._update_delayed_event(
            delay_id, _UpdateDelayedEventAction.SEND, True, self.user1_access_token
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, channel.code, channel.result)
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"], channel.result)

        # ...and the delayed event must stay scheduled
        self._assert_scheduled(delay_id)
        self.assertEqual(1, len(self._get_delayed_events()))
        self._check_for_delayed_event_in_sync(self.user1_access_token, delay_id, False)

        # Once the cause of the error is fixed, sending must succeed
        self.helper.join(self.room_id, self.user1_user_id, tok=self.user1_access_token)
        channel = self._update_delayed_event(
            delay_id, _UpdateDelayedEventAction.SEND, True, self.user1_access_token
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        self.assertListEqual([], self._get_delayed_events())
        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_id, True
        )
        self._assert_finalised(delay_id, event_id=event["event_id"])

    def test_send_delayed_state_event_fails_and_stays_scheduled_until_timeout(
        self,
    ) -> None:
        state_key = "to_send_on_timeout_after_failed_send"
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(seconds=2),
            event_type=_EVENT_TYPE,
            state_key=state_key,
            content={},
            access_token=self.user2_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Raise the power level required to send the state event,
        # so that user2 is no longer allowed to send it
        self.helper.send_state(
            self.room_id,
            "m.room.power_levels",
            {"events": {_EVENT_TYPE: 50}},
            self.user1_access_token,
        )

        # The response must be the error that sending the state event would have given
        channel = self._update_delayed_event(
            delay_id, _UpdateDelayedEventAction.SEND, True, self.user2_access_token
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, channel.code, channel.result)
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"], channel.result)

        # ...and the delayed event must stay scheduled
        self._assert_scheduled(delay_id, self.user2_access_token)
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user2_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        # Once the cause of the error is fixed, the delayed event must be sent on timeout
        self.helper.send_state(
            self.room_id,
            "m.room.power_levels",
            {"events": {_EVENT_TYPE: 0}},
            self.user1_access_token,
        )
        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=2).as_secs())
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user2_access_token,
            state_key=state_key,
        )
        event = self._check_for_delayed_event_in_sync(
            self.user2_access_token, delay_id, True
        )
        self._assert_finalised(
            delay_id, event_id=event["event_id"], access_token=self.user2_access_token
        )

    def test_repeated_action_on_cancelled_delayed_event(self) -> None:
        delay_ids = []
        for delay in Duration(milliseconds=900), Duration(seconds=100):
            channel = self._make_delayed_event_request(
                room_id=self.room_id,
                delay=delay,
                event_type=_EVENT_TYPE,
                content={},
                access_token=self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            assert delay_id is not None
            delay_ids.append(delay_id)
        delay_id = delay_ids[1]

        channel = self._update_delayed_event(
            delay_id, _UpdateDelayedEventAction.CANCEL, True, self.user1_access_token
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        # Repeating the action that matches the outcome must succeed
        channel = self._update_delayed_event(
            delay_id, _UpdateDelayedEventAction.CANCEL, True, self.user1_access_token
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertDictEqual({}, channel.json_body)

        # Actions that conflict with the outcome must fail
        for action in _UpdateDelayedEventAction.SEND, _UpdateDelayedEventAction.RESTART:
            channel = self._update_delayed_event(
                delay_id, action, True, self.user1_access_token
            )
            self.assertEqual(HTTPStatus.CONFLICT, channel.code, channel.result)
            self.assertEqual(
                Codes.UNKNOWN, channel.json_body["errcode"], channel.result
            )

        self._assert_finalised(delay_id)

        # None of this may have disturbed the other scheduled delayed event
        self.reactor.advance(Duration(seconds=1).as_secs())
        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_ids[0], True
        )
        self._assert_finalised(delay_ids[0], event_id=event["event_id"])
        self._check_for_delayed_event_in_sync(self.user1_access_token, delay_id, False)

    def test_repeated_action_on_sent_delayed_event(self) -> None:
        delay_ids = []
        for delay in Duration(milliseconds=900), Duration(seconds=100):
            channel = self._make_delayed_event_request(
                room_id=self.room_id,
                delay=delay,
                event_type=_EVENT_TYPE,
                content={},
                access_token=self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            assert delay_id is not None
            delay_ids.append(delay_id)
        delay_id = delay_ids[1]

        channel = self._update_delayed_event(
            delay_id, _UpdateDelayedEventAction.SEND, True, self.user1_access_token
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_id, True
        )

        # Repeating the action that matches the outcome must succeed, without resending
        channel = self._update_delayed_event(
            delay_id, _UpdateDelayedEventAction.SEND, True, self.user1_access_token
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertDictEqual({}, channel.json_body)
        self.assertEqual(
            event["event_id"],
            self._check_for_delayed_event_in_sync(
                self.user1_access_token, delay_id, True
            )["event_id"],
        )

        # Actions that conflict with the outcome must fail
        for action in (
            _UpdateDelayedEventAction.CANCEL,
            _UpdateDelayedEventAction.RESTART,
        ):
            channel = self._update_delayed_event(
                delay_id, action, True, self.user1_access_token
            )
            self.assertEqual(HTTPStatus.CONFLICT, channel.code, channel.result)
            self.assertEqual(
                Codes.UNKNOWN, channel.json_body["errcode"], channel.result
            )

        self._assert_finalised(delay_id, event_id=event["event_id"])

        # None of this may have disturbed the other scheduled delayed event
        self.reactor.advance(Duration(seconds=1).as_secs())
        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_ids[0], True
        )
        self._assert_finalised(delay_ids[0], event_id=event["event_id"])

    @parameterized.expand((True, False))
    def test_restart_delayed_state_event(self, action_in_path: bool) -> None:
        state_key = "to_send_on_restarted_timeout"

        setter_key = "setter"
        setter_expected = "on_timeout"
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=1500),
            event_type=_EVENT_TYPE,
            state_key=state_key,
            content={
                setter_key: setter_expected,
            },
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
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

        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.RESTART,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
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

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        self.assertListEqual([], self._get_delayed_events())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_id, True
        )
        self._check_for_delayed_event_in_sync(self.user2_access_token, delay_id, False)

        self._assert_finalised(delay_id, event_id=event["event_id"])

    @parameterized.expand((True, False))
    @unittest.override_config(
        {"rc_delayed_event_mgmt": {"per_second": 0.5, "burst_count": 1}}
    )
    def test_restart_delayed_event_ratelimit(self, action_in_path: bool) -> None:
        delay_ids = []
        for _ in range(2):
            channel = self._make_delayed_event_request(
                room_id=self.room_id,
                delay=Duration(milliseconds=100000),
                event_type=_EVENT_TYPE,
                content={},
                access_token=self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_id = channel.json_body.get("delay_id")
            assert delay_id is not None
            delay_ids.append(delay_id)

        delay_id = delay_ids.pop(0)
        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.RESTART,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        delay_id = delay_ids.pop(0)
        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.RESTART,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, channel.code, channel.result)

        # Add the current user to the ratelimit overrides, allowing them no ratelimiting.
        self.get_success(
            self.hs.get_datastores().main.set_ratelimit_for_user(
                self.user1_user_id, 0, 0
            )
        )

        # Test that the request isn't ratelimited anymore.
        channel = self._update_delayed_event(
            delay_id,
            _UpdateDelayedEventAction.RESTART,
            action_in_path,
            self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

    @parameterized.expand(
        (
            (action, action_in_path)
            for action in _UpdateDelayedEventAction
            for action_in_path in (True, False)
        )
    )
    def test_update_delayed_event_being_processed(
        self,
        action: _UpdateDelayedEventAction,
        action_in_path: bool,
    ) -> None:
        """Test managing a delayed event that is in the process of being sent."""
        send_after = Duration(seconds=1)
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=send_after,
            event_type=_EVENT_TYPE,
            content={},
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body["delay_id"]

        # Simulate the server taking a long time to persist delayed events
        simulated_send_lag = Duration(seconds=5)
        event_creation_handler = self.hs.get_event_creation_handler()
        orig_send_fn = event_creation_handler.create_and_send_nonmember_event

        async def slow_send_fn(*args: Any, **kwargs: Any) -> Any:
            await self.clock.sleep(simulated_send_lag)
            return await orig_send_fn(*args, **kwargs)

        with patch.object(event_creation_handler, orig_send_fn.__name__, slow_send_fn):
            # Advance time enough so the delayed event starts being sent,
            # but not enough for it to finish the sending process
            self.reactor.advance(send_after.as_secs())
            channel = self._update_delayed_event(
                delay_id, action, action_in_path, self.user1_access_token
            )
            self.assertEqual(
                HTTPStatus.SERVICE_UNAVAILABLE, channel.code, channel.result
            )

            # Advance time enough so the sending process finishes
            self.reactor.advance(simulated_send_lag.as_secs())
            channel = self._update_delayed_event(
                delay_id, action, action_in_path, self.user1_access_token
            )
            self.assertEqual(
                HTTPStatus.OK
                if action == _UpdateDelayedEventAction.SEND
                else HTTPStatus.CONFLICT,
                channel.code,
                channel.result,
            )

    def test_delayed_state_is_not_cancelled_by_new_state_from_same_user(
        self,
    ) -> None:
        state_key = "to_not_be_cancelled_by_same_user"

        setter_key = "setter"
        setter_expected = "on_timeout"
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=900),
            event_type=_EVENT_TYPE,
            state_key=state_key,
            content={
                setter_key: setter_expected,
            },
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None
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

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        content = self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user1_access_token,
            state_key=state_key,
        )
        self.assertEqual(setter_expected, content.get(setter_key), content)

        self._check_for_delayed_event_in_sync(self.user1_access_token, delay_id, True)
        self._check_for_delayed_event_in_sync(self.user2_access_token, delay_id, False)

    @parameterized.expand(
        (
            (
                f"{'admin' if by_admin else 'client'}_api_{'erase' if erase else 'preserve'}",
                by_admin,
                erase,
            )
            for by_admin in (False, True)
            for erase in (False, True)
        )
    )
    @unittest.override_config(
        {"experimental_features": {"msc4140_finalised_retention_limit_per_user": 1}}
    )
    def test_delayed_events_are_cancelled_on_deactivation(
        self, _name: str, by_admin: bool, erase: bool
    ) -> None:
        store = self.hs.get_datastores().main
        handler = self.hs.get_delayed_events_handler()

        # user1 schedules a delayed message event and a delayed state event.
        user1_delay_ids = []
        state_key = "to_cancel_on_deactivation"
        for state_event in True, False:
            channel = self._make_delayed_event_request(
                room_id=self.room_id,
                delay=Duration(milliseconds=900),
                event_type=_EVENT_TYPE,
                state_key=state_key if state_event else None,
                content={"key": "value"},
                access_token=self.user1_access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            user1_delay_ids.append(channel.json_body["delay_id"])

        # user2 schedules a delayed event to be sent later than user1's.
        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=2000),
            event_type=_EVENT_TYPE,
            content={"key": "value"},
            access_token=self.user2_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        user2_delay_id = channel.json_body["delay_id"]

        self.assertEqual(2, len(self._get_delayed_events()))

        # The next send is scheduled for user1's delayed events.
        assert handler._next_delayed_event_call is not None
        self.assertLess(
            handler._next_delayed_event_call.getTime() - self.reactor.seconds(), 1
        )

        if by_admin:
            self.register_user("admin", "pass", admin=True)
            channel = self.make_request(
                "POST",
                f"/_synapse/admin/v1/deactivate/{self.user1_user_id}",
                {"erase": erase},
                self.login("admin", "pass"),
            )
        else:
            channel = self.make_request(
                "POST",
                "account/deactivate",
                {
                    "auth": {
                        "type": "m.login.password",
                        "user": self.user1_user_id,
                        "password": "pass",
                    },
                    "erase": erase,
                },
                self.user1_access_token,
            )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        # Assert that user1's delayed events are gone.
        self.assertListEqual(
            [], self.get_success(store.get_all_delayed_events_for_user("user1"))
        )

        if erase:
            # Assert that both delayed events are pruned.
            for delay_id in user1_delay_ids:
                self.get_failure(
                    store.get_delayed_event_for_user(delay_id, "user1"),
                    NotFoundError,
                )
        else:
            # Assert that the oldest of user1's delayed events are pruned.
            self.get_failure(
                store.get_delayed_event_for_user(user1_delay_ids[0], "user1"),
                NotFoundError,
            )

            # Assert that the newest of user1's delayed events are finalised.
            self.assertIsNotNone(
                self.get_success(
                    store.get_delayed_event_for_user(user1_delay_ids[1], "user1")
                ).finalised
            )

        # Assert that the next send has been rescheduled for user2's delayed event.
        assert handler._next_delayed_event_call is not None
        self.assertGreater(
            handler._next_delayed_event_call.getTime() - self.reactor.seconds(), 1
        )

        # Nothing gets sent when user1's delayed events would have timed out.
        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        for delay_id in user1_delay_ids:
            self._check_for_delayed_event_in_sync(
                self.user2_access_token, delay_id, False
            )
        self.helper.get_state(
            self.room_id,
            _EVENT_TYPE,
            self.user2_access_token,
            state_key=state_key,
            expect_code=HTTPStatus.NOT_FOUND,
        )

        # user2's delayed event is unaffected, and still gets sent.
        self._check_for_delayed_event_in_sync(
            self.user2_access_token, user2_delay_id, False
        )
        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        for delay_id in user1_delay_ids:
            self._check_for_delayed_event_in_sync(
                self.user2_access_token, delay_id, False
            )
        self._check_for_delayed_event_in_sync(
            self.user2_access_token, user2_delay_id, True
        )

    @parameterized.expand((("preserve", False), ("erase", True)))
    def test_deactivation_cancels_the_timer_when_no_delayed_events_remain(
        self, _name: str, erase: bool
    ) -> None:
        handler = self.hs.get_delayed_events_handler()

        channel = self._make_delayed_event_request(
            room_id=self.room_id,
            delay=Duration(milliseconds=900),
            event_type=_EVENT_TYPE,
            content={},
            access_token=self.user1_access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertIsNotNone(handler._next_delayed_event_call)
        self.assertIsNone(handler._next_prune_finalised_call)

        self.get_success(
            self.hs.get_deactivate_account_handler().deactivate_account(
                self.user1_user_id,
                erase_data=erase,
                requester=create_requester(self.user1_user_id),
            )
        )
        self.assertIsNone(handler._next_delayed_event_call)
        if erase:
            self.assertIsNone(handler._next_prune_finalised_call)
        else:
            self.assertIsNotNone(handler._next_prune_finalised_call)


class DelayedStickyEventsTestCase(DelayedEventsTestCaseBase):
    """Tests scheduling delayed sticky events (MSC4354)."""

    if not USE_POSTGRES_FOR_TESTS and sqlite3.sqlite_version_info < (3, 40, 0):
        # We need the JSON functionality in SQLite
        # (the sticky events store refuses to start without it, so this has to be
        # a class-level skip: the homeserver is built before the test body runs)
        skip = f"SQLite version is too old to support sticky events: {sqlite3.sqlite_version_info} (See https://github.com/element-hq/synapse/issues/19428)"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4354_enabled": True}
        return config

    @parameterized.expand(
        (
            (False, True),
            (True, False),
            (True, True),
        )
    )
    def test_delayed_sticky_event_is_sent_with_sticky_duration(
        self, param_in_body: bool, param_in_query: bool
    ) -> None:
        """Test that the sticky duration given when scheduling a delayed event
        is applied to the event once it is sent."""
        path = self.helper.build_delayed_event_request_path(
            room_id=self.room_id,
            event_type=_EVENT_TYPE,
        )
        body = self.helper.build_delayed_event_request_body(
            delay=Duration(milliseconds=900),
            content={"body": "sticky"},
        )
        sticky_duration = Duration(minutes=1)
        if param_in_body:
            body[StickyEvent.REQUEST_PARAM_NAME] = sticky_duration.as_millis()
        if param_in_query:
            path += f"?{StickyEvent.REQUEST_PARAM_NAME}={sticky_duration.as_millis()}"
        channel = self.make_request("PUT", path, body, self.user1_access_token)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        self.assertListEqual([], self._get_delayed_events())

        event = self._check_for_delayed_event_in_sync(
            self.user1_access_token, delay_id, True
        )
        self.assertGreater(
            event["unsigned"].get(EventUnsignedContentFields.STICKY_TTL, 0),
            0,
            event,
        )

    @parameterized.expand((False, True))
    def test_delayed_sticky_state_event(self, param_in_body: bool) -> None:
        """Test that delayed event requests forbid scheduling a sticky state event."""
        path = self.helper.build_delayed_event_request_path(
            room_id=self.room_id,
            event_type="m.room.topic",
        )
        body = self.helper.build_delayed_event_request_body(
            delay=Duration(milliseconds=900),
            content={"topic": "This is a topic"},
            state_key="",
        )
        sticky_duration = Duration(minutes=1)
        if param_in_body:
            body[StickyEvent.REQUEST_PARAM_NAME] = sticky_duration.as_millis()
        else:
            path += f"?{StickyEvent.REQUEST_PARAM_NAME}={sticky_duration.as_millis()}"
        channel = self.make_request("PUT", path, body, self.user1_access_token)
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])

    @parameterized.expand(
        (
            (sticky_duration, param_in_body)
            for sticky_duration in (
                Duration(),
                Duration(seconds=-2000),
                Duration(days=1),
            )
            for param_in_body in (False, True)
        )
    )
    def test_delayed_sticky_event_with_extreme_duration(
        self, sticky_duration: Duration, param_in_body: bool
    ) -> None:
        """Test that delayed event requests permit a zero, negative, or very long sticky duration.
        (The actual sticky duration is clamped at event send time.)"""
        path = self.helper.build_delayed_event_request_path(
            room_id=self.room_id,
            event_type=_EVENT_TYPE,
        )
        body = self.helper.build_delayed_event_request_body(
            delay=Duration(milliseconds=900),
            content={"body": "sticky"},
        )
        if param_in_body:
            body[StickyEvent.REQUEST_PARAM_NAME] = sticky_duration.as_millis()
        else:
            path += f"?{StickyEvent.REQUEST_PARAM_NAME}={sticky_duration.as_millis()}"
        channel = self.make_request("PUT", path, body, self.user1_access_token)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

    def test_delayed_sticky_event_with_conflicting_sticky_durations(self) -> None:
        """Test that delayed event requests reject having a different sticky duration
        in the query parameter & request body."""
        path = self.helper.build_delayed_event_request_path(
            room_id=self.room_id,
            event_type=_EVENT_TYPE,
        )
        body = self.helper.build_delayed_event_request_body(
            delay=Duration(milliseconds=900),
            content={"body": "sticky"},
        )
        path += f"?{StickyEvent.REQUEST_PARAM_NAME}=10000"
        body[StickyEvent.REQUEST_PARAM_NAME] = 20000
        channel = self.make_request("PUT", path, body, self.user1_access_token)
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])


class DelayedEventsWorkerTestCase(
    BaseMultiWorkerStreamTestCase, DelayedEventsHelperMixin
):
    """Tests delayed events when some of the work happens off the main process."""

    servlets = [
        admin.register_servlets,
        delayed_events.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["max_event_delay_duration"] = "24h"
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user_id = self.register_user("user", "pass")
        self.access_token = self.login("user", "pass")
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.access_token)

    def test_delayed_event_scheduled_on_worker_is_sent(self) -> None:
        """Test that a delayed event scheduled on a worker is sent by the main process."""
        worker_hs = self.make_worker_hs("synapse.app.generic_worker")
        worker_site = self._hs_to_site[worker_hs]

        channel = make_request(
            self.reactor,
            worker_site,
            "PUT",
            self.helper.build_delayed_event_request_path(
                room_id=self.room_id,
                event_type=_EVENT_TYPE,
            ),
            self.helper.build_delayed_event_request_body(
                delay=Duration(milliseconds=900),
                content={"body": "from a worker"},
            ),
            self.access_token,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        delay_id = channel.json_body.get("delay_id")
        assert delay_id is not None

        # Advance time enough so the delayed event is sent
        self.reactor.advance(Duration(seconds=1).as_secs())
        self._check_for_delayed_event_in_sync(self.access_token, delay_id, True)

    @unittest.override_config(
        {"experimental_features": {"msc4140_finalised_retention_limit_per_user": 1}}
    )
    def test_delayed_events_are_cancelled_on_deactivation_from_worker(self) -> None:
        worker_hs = self.make_worker_hs("synapse.app.generic_worker")
        store = self.hs.get_datastores().main
        handler = self.hs.get_delayed_events_handler()

        delay_ids = []
        for _ in range(2):
            channel = self._make_delayed_event_request(
                room_id=self.room_id,
                delay=Duration(milliseconds=900),
                event_type=_EVENT_TYPE,
                content={},
                access_token=self.access_token,
            )
            self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
            delay_ids.append(channel.json_body["delay_id"])
        self.assertIsNotNone(handler._next_delayed_event_call)

        # Deactivate the account on the worker. The main process, which is the
        # one to send delayed events, must get to know about it.
        self.get_success(
            worker_hs.get_deactivate_account_handler().deactivate_account(
                self.user_id,
                erase_data=False,
                requester=create_requester(self.user_id),
            )
        )

        # Assert that user1's delayed events are gone.
        self.assertListEqual(
            [], self.get_success(store.get_all_delayed_events_for_user("user"))
        )

        # Assert that the oldest of user1's delayed events are pruned.
        self.get_failure(
            store.get_delayed_event_for_user(delay_ids[0], "user"), NotFoundError
        )

        # Assert that the newest of user1's delayed events are finalised.
        self.assertIsNotNone(
            self.get_success(
                store.get_delayed_event_for_user(delay_ids[1], "user")
            ).finalised
        )

        self.assertIsNone(handler._next_delayed_event_call)
