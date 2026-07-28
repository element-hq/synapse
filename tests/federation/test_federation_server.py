#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 Matrix.org Federation C.I.C
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
import logging
from http import HTTPStatus
from unittest.mock import Mock

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import Codes, FederationError
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersions
from synapse.config.server import DEFAULT_ROOM_VERSION
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.events import EventBase
from synapse.http.types import QueryParams
from synapse.logging.context import LoggingContext
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.controllers.state import server_acl_evaluator_from_event
from synapse.types import JsonDict, UserID
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.event_builders import make_test_event, make_test_pdu_event
from tests.unittest import override_config

logger = logging.getLogger(__name__)


class FederationServerTests(unittest.FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    @parameterized.expand([(b"",), (b"foo",), (b'{"limit": Infinity}',)])
    def test_bad_request(self, query_content: bytes) -> None:
        """
        Querying with bad data returns a reasonable error code.
        """
        u1 = self.register_user("u1", "pass")
        u1_token = self.login("u1", "pass")

        room_1 = self.helper.create_room_as(u1, tok=u1_token)
        self.inject_room_member(room_1, "@user:other.example.com", "join")

        "/get_missing_events/(?P<room_id>[^/]*)/?"

        channel = self.make_request(
            "POST",
            "/_matrix/federation/v1/get_missing_events/%s" % (room_1,),
            query_content,
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_JSON")

    def test_failed_edu_causes_500(self) -> None:
        """If the EDU handler fails, /send should return a 500."""

        async def failing_handler(_origin: str, _content: JsonDict) -> None:
            raise Exception("bleh")

        self.hs.get_federation_registry().register_edu_handler(
            "FAIL_EDU_TYPE", failing_handler
        )

        channel = self.make_signed_federation_request(
            "PUT",
            "/_matrix/federation/v1/send/txn",
            {"edus": [{"edu_type": "FAIL_EDU_TYPE", "content": {}}]},
        )
        self.assertEqual(500, channel.code, channel.result)


class GetMissingEventsRoomCheckTests(unittest.FederatingHomeserverTestCase):
    """
    Regression tests for room confusion in /get_missing_events
    https://github.com/element-hq/synapse/security/advisories/GHSA-27p5-4f45-gx76
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        # Local user
        self.local_user_id = self.register_user("alice", "pass")
        self.local_user_token = self.login("alice", "pass")
        self.local_user = UserID.from_string(self.local_user_id)

        # Create 2 rooms (one with the remote server, one without).
        # - The remote server will be in this room
        self.room_allowed = self.helper.create_room_as(
            self.local_user_id, tok=self.local_user_token
        )
        self.inject_room_member(
            self.room_allowed, f"@remote:{self.OTHER_SERVER_NAME}", "join"
        )
        # - The remote server will _not_ be in this room
        self.room_blocked = self.helper.create_room_as(
            self.local_user_id, tok=self.local_user_token
        )

        # Insert a linear chain of events in both rooms
        self.room_allowed_event_ids = self.helper.send_messages(
            self.room_allowed, num_events=5, tok=self.local_user_token
        )
        self.room_blocked_event_ids = self.helper.send_messages(
            self.room_blocked, num_events=5, tok=self.local_user_token
        )

    def _extract_returned_event_ids(self, json_body: JsonDict) -> set[str]:
        """
        Given the response body of `/get_missing_events`, return the event IDs
        of the events that were returned in the response.
        This only includes event IDs from `self.room_allowed_event_ids` and
        `self.room_blocked_event_ids`; other events are ignored.

        As the federation PDU format doesn't include event IDs
        (at least not for every room version), we match on the
        `(room_id, content.body, prev_events)` triple against the events
        we sent in the setup.
        """
        store = self.hs.get_datastores().main
        events = self.get_success(
            store.get_events_as_list(
                list(self.room_allowed_event_ids) + list(self.room_blocked_event_ids)
            )
        )
        # (room_id, content.body, prev_events) -> event ID
        event_lookup: dict[tuple[str, str, tuple[str, ...]], str] = {}
        for event in events:
            key = (
                event.room_id,
                event.content["body"],
                tuple(event.prev_event_ids()),
            )
            event_lookup[key] = event.event_id

        returned_event_ids: set[str] = set()
        for pdu in json_body["events"]:
            key = (
                pdu.get("room_id"),
                pdu.get("content", {}).get("body"),
                tuple(pdu.get("prev_events", [])),
            )
            event_id = event_lookup.get(key)
            if event_id is None:
                # Not one of the events we created; ignore it.
                continue
            returned_event_ids.add(event_id)
        return returned_event_ids

    def test_get_missing_events_returns_events_from_correct_room(self) -> None:
        """
        Tests the happy path when `latest_events` and `earliest_events`
        are both in the correct room.

                returned
                  |
                  v
            e1 <- e2 <- e3 <- e4 <- e5
            ^           ^
            |           |
            earliest    latest

        Not a regression test; I'm just filling a gap in our (in-repo) testing
        as far as I can tell.
        """
        channel = self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/v1/get_missing_events/{self.room_allowed}",
            content={
                "earliest_events": [self.room_allowed_event_ids[1]],
                "latest_events": [self.room_allowed_event_ids[3]],
                "limit": 10,
            },
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertEqual(
            self._extract_returned_event_ids(channel.json_body),
            {self.room_allowed_event_ids[2]},
        )

    def test_get_missing_events_with_empty_earliest_events(self) -> None:
        """
        Tests that `/get_missing_events`, when given no `earliest_events`,
        walks back to the start of the room, capped at `limit`.

        (Not a regression test; documents pre-existing behaviour)
        """
        channel = self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/v1/get_missing_events/{self.room_allowed}",
            content={
                "earliest_events": [],
                "latest_events": [self.room_allowed_event_ids[-1]],
                "limit": 10,
            },
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertEqual(
            self._extract_returned_event_ids(channel.json_body),
            set(self.room_allowed_event_ids[:-1]),
        )

    def test_get_missing_events_with_unknown_earliest_event(self) -> None:
        """
        Tests that `/get_missing_events` ignores unknown event IDs given in
        `earliest_events`.

        This makes sense as the `earliest_events` are intuitively
        'events to stop at' when walking backwards.
        Since we don't know about those events, we don't use them as stopping conditions.
        (In other words, this falls back to the same behaviour as
        `test_get_missing_events_with_empty_earliest_events`.)

        (Not a regression test; documents pre-existing behaviour)
        """
        channel = self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/v1/get_missing_events/{self.room_allowed}",
            content={
                "earliest_events": ["$someUnknownEventId"],
                "latest_events": [self.room_allowed_event_ids[-1]],
                "limit": 10,
            },
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertEqual(
            self._extract_returned_event_ids(channel.json_body),
            set(self.room_allowed_event_ids[:-1]),
        )

    def test_get_missing_events_with_no_latest_event(self) -> None:
        """
        Tests that when the `/get_missing_events` request references
        no events in `latest_events`, the response is 200 OK
        with an empty `events` list.

        (Not a regression test; documents pre-existing behaviour)
        """
        channel = self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/v1/get_missing_events/{self.room_allowed}",
            content={
                "earliest_events": ["$someOtherUnknownEventId"],
                "latest_events": [],
                "limit": 10,
            },
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        self.assertEqual(channel.json_body, {"events": []})

    def test_get_missing_events_with_unknown_latest_event(self) -> None:
        """
        Tests that when the `/get_missing_events` request references
        unknown events in `latest_events`, the response is 200 OK
        with an empty `events` list.

        I imagine this makes sense as you might request several events
        in `latest_events` to start walking back from and we need to be
        tolerant of the fact that servers don't always know about every event.

        (Not a regression test; documents pre-existing behaviour)
        """
        channel = self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/v1/get_missing_events/{self.room_allowed}",
            content={
                "earliest_events": ["$someOtherUnknownEventId"],
                "latest_events": ["$someUnknownEventId"],
                "limit": 10,
            },
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        self.assertEqual(channel.json_body, {"events": []})

    def test_get_missing_events_ignores_events_from_other_room(self) -> None:
        """
        Tests that providing `earliest_events` and `latest_events` from the wrong room
        treats them the same as being unknown.

        From `test_get_missing_events_with_unknown_latest_event` we established that
        unknown events in `latest_events` get skipped (to the point of returning an empty
        `events: []` response)

        From `test_get_missing_events_with_unknown_earliest_event` we established that
        unknown events in `earliest_events` get ignored as stopping conditions.

        This regression test previously failed.
        """
        channel = self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/v1/get_missing_events/{self.room_allowed}",
            content={
                "earliest_events": [self.room_blocked_event_ids[0]],
                "latest_events": [self.room_blocked_event_ids[-1]],
                "limit": 10,
            },
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        self.assertEqual(channel.json_body, {"events": []})

    def test_get_missing_events_skips_latest_events_from_other_room(self) -> None:
        """
        Tests that providing `latest_events` from the wrong room
        treats it as being unknown, even if `earliest_events` are from the correct
        room.

        From `test_get_missing_events_with_unknown_latest_event` we established that
        unknown events in `latest_events` get skipped (to the point of returning an empty
        `events: []` response)

        This regression test previously failed.
        """
        channel = self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/v1/get_missing_events/{self.room_allowed}",
            content={
                "earliest_events": [self.room_allowed_event_ids[0]],
                "latest_events": [self.room_blocked_event_ids[-1]],
                "limit": 10,
            },
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        self.assertEqual(channel.json_body, {"events": []})

    def test_get_missing_events_ignores_earliest_events_from_other_room(self) -> None:
        """
        Tests that providing `earliest_events` from the wrong room causes those
        events to be ignored as stopping conditions,
        even though `latest_events` are from the correct room.

        From `test_get_missing_events_with_unknown_earliest_event` we established that
        unknown events in `earliest_events` get ignored as stopping conditions.

        This test was previously fine, but is an obvious extra case.
        """
        channel = self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/v1/get_missing_events/{self.room_allowed}",
            content={
                # Use [-3] here as we want to see if the walk-back algorithm
                # confuses depth (topological ordering) across the two rooms.
                "earliest_events": [self.room_blocked_event_ids[-3]],
                "latest_events": [self.room_allowed_event_ids[-1]],
                "limit": 10,
            },
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertEqual(
            self._extract_returned_event_ids(channel.json_body),
            set(self.room_allowed_event_ids[:-1]),
        )


def _create_acl_event(content: JsonDict) -> EventBase:
    return make_test_event(
        {
            "room_id": "!a:b",
            "event_id": "$a:b",
            "type": "m.room.server_acls",
            "sender": "@a:b",
            "content": content,
        }
    )


class MessageAcceptTests(unittest.FederatingHomeserverTestCase):
    """
    Tests to make sure that we don't accept flawed events from federation (incoming).
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.http_client = Mock()
        return self.setup_test_homeserver(federation_http_client=self.http_client)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.store = self.hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        self.federation_event_handler = self.hs.get_federation_event_handler()

        # Create a local room
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        self.room_id = self.helper.create_room_as(
            user1_id, tok=user1_tok, is_public=True
        )

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(self.room_id)
        )

        # Figure out what the forward extremities in the room are (the most recent
        # events that aren't tied into the DAG)
        forward_extremity_event_ids = self.get_success(
            self.hs.get_datastores().main.get_latest_event_ids_in_room(self.room_id)
        )

        # Join a remote user to the room that will attempt to send bad events
        self.remote_bad_user_id = f"@baduser:{self.OTHER_SERVER_NAME}"
        self.remote_bad_user_join_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.room_id,
                    "sender": self.remote_bad_user_id,
                    "state_key": self.remote_bad_user_id,
                    "depth": 1000,
                    "origin_server_ts": 1,
                    "type": EventTypes.Member,
                    "content": {"membership": Membership.JOIN},
                    "auth_events": [
                        state_map[(EventTypes.Create, "")].event_id,
                        state_map[(EventTypes.JoinRules, "")].event_id,
                    ],
                    "prev_events": list(forward_extremity_event_ids),
                }
            ),
            room_version=RoomVersions.V10,
        )

        # Send the join, it should return None (which is not an error)
        self.assertEqual(
            self.get_success(
                self.federation_event_handler.on_receive_pdu(
                    self.OTHER_SERVER_NAME, self.remote_bad_user_join_event
                )
            ),
            None,
        )

        # Make sure we actually joined the room
        self.assertEqual(
            self.get_success(self.store.get_latest_event_ids_in_room(self.room_id)),
            {self.remote_bad_user_join_event.event_id},
        )

    def test_cant_hide_direct_ancestors(self) -> None:
        """
        If you send a message, you must be able to provide the direct
        prev_events that said event references.
        """

        async def post_json(
            destination: str,
            path: str,
            data: JsonDict | None = None,
            long_retries: bool = False,
            timeout: int | None = None,
            ignore_backoff: bool = False,
            args: QueryParams | None = None,
        ) -> JsonDict | list:
            # If it asks us for new missing events, give them NOTHING
            if path.startswith("/_matrix/federation/v1/get_missing_events/"):
                return {"events": []}
            return {}

        self.http_client.post_json = post_json

        # Figure out what the forward extremities in the room are (the most recent
        # events that aren't tied into the DAG)
        forward_extremity_event_ids = self.get_success(
            self.hs.get_datastores().main.get_latest_event_ids_in_room(self.room_id)
        )

        # Now lie about an event's prev_events
        lying_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.room_id,
                    "sender": self.remote_bad_user_id,
                    "depth": 1000,
                    "origin_server_ts": 1,
                    "type": "m.room.message",
                    "content": {"body": "hewwo?"},
                    "auth_events": [],
                    "prev_events": ["$missing_prev_event"]
                    + list(forward_extremity_event_ids),
                }
            ),
            room_version=RoomVersions.V10,
        )

        with LoggingContext(
            name="test-context",
            server_name=self.hs.hostname,
        ):
            failure = self.get_failure(
                self.federation_event_handler.on_receive_pdu(
                    self.OTHER_SERVER_NAME, lying_event
                ),
                FederationError,
            )

        # on_receive_pdu should throw an error
        self.assertEqual(
            failure.value.args[0],
            (
                "ERROR 403: Your server isn't divulging details about prev_events "
                "referenced in this event."
            ),
        )

        # Make sure the invalid event isn't there
        extrem = self.get_success(self.store.get_latest_event_ids_in_room(self.room_id))
        self.assertEqual(extrem, {self.remote_bad_user_join_event.event_id})


class ServerACLsTestCase(unittest.TestCase):
    def test_blocked_server(self) -> None:
        e = _create_acl_event({"allow": ["*"], "deny": ["evil.com"]})
        logger.info("ACL event: %s", e.content)

        server_acl_evalutor = server_acl_evaluator_from_event(e)

        self.assertFalse(server_acl_evalutor.server_matches_acl_event("evil.com"))
        self.assertFalse(server_acl_evalutor.server_matches_acl_event("EVIL.COM"))

        self.assertTrue(server_acl_evalutor.server_matches_acl_event("evil.com.au"))
        self.assertTrue(
            server_acl_evalutor.server_matches_acl_event("honestly.not.evil.com")
        )

    def test_block_ip_literals(self) -> None:
        e = _create_acl_event({"allow_ip_literals": False, "allow": ["*"]})
        logger.info("ACL event: %s", e.content)

        server_acl_evalutor = server_acl_evaluator_from_event(e)

        self.assertFalse(server_acl_evalutor.server_matches_acl_event("1.2.3.4"))
        self.assertTrue(server_acl_evalutor.server_matches_acl_event("1a.2.3.4"))
        self.assertFalse(server_acl_evalutor.server_matches_acl_event("[1:2::]"))
        self.assertTrue(server_acl_evalutor.server_matches_acl_event("1:2:3:4"))

    def test_wildcard_matching(self) -> None:
        e = _create_acl_event({"allow": ["good*.com"]})

        server_acl_evalutor = server_acl_evaluator_from_event(e)

        self.assertTrue(
            server_acl_evalutor.server_matches_acl_event("good.com"),
            "* matches 0 characters",
        )
        self.assertTrue(
            server_acl_evalutor.server_matches_acl_event("GOOD.COM"),
            "pattern is case-insensitive",
        )
        self.assertTrue(
            server_acl_evalutor.server_matches_acl_event("good.aa.com"),
            "* matches several characters, including '.'",
        )
        self.assertFalse(
            server_acl_evalutor.server_matches_acl_event("ishgood.com"),
            "pattern does not allow prefixes",
        )


class StateQueryTests(unittest.FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def test_needs_to_be_in_room(self) -> None:
        """/v1/state/<room_id> requires the server to be in the room"""
        u1 = self.register_user("u1", "pass")
        u1_token = self.login("u1", "pass")

        room_1 = self.helper.create_room_as(u1, tok=u1_token)

        channel = self.make_signed_federation_request(
            "GET", "/_matrix/federation/v1/state/%s?event_id=xyz" % (room_1,)
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, channel.code, channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")


class TimestampToEventTests(unittest.FederatingHomeserverTestCase):
    """Tests for `GET /_matrix/federation/v1/timestamp_to_event/<roomID>`."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Create a room and join the remote server so it's allowed to query
        user = self.register_user("u1", "pass")
        tok = self.login("u1", "pass")
        self.room_id = self.helper.create_room_as(user, tok=tok)
        # Send one event at time = 1000s
        self.reactor.advance(1000)
        self.event_at_1000 = self.helper.send_messages(self.room_id, 1, tok=tok)[0]

        # Send another event at time = 4000s
        self.reactor.advance(3000)
        self.event_at_4000 = self.helper.send_messages(self.room_id, 1, tok=tok)[0]

        # Send another event at time = 8000s
        self.reactor.advance(4000)
        self.event_at_8000 = self.helper.send_messages(self.room_id, 1, tok=tok)[0]

        super().prepare(reactor, clock, hs)

    @parameterized.expand(
        [
            # Query backwards from 5000s, should find the event at 4000s
            (5000000, "b"),
            # Query forwards from 1100s, should find the event at 4000s
            (1100000, "f"),
        ]
    )
    def test_happy_path(self, ts: int, dir: str) -> None:
        """
        Tests that a server in the room gets 200 OK
        with the closest event IDs as requested for a given timestamp,
        in both forward and backward directions.
        """
        # Join the remote server to the room
        self.inject_room_member(self.room_id, "@user:" + self.OTHER_SERVER_NAME, "join")

        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/timestamp_to_event/{self.room_id}?ts={ts}&dir={dir}",
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        self.assertEqual(channel.json_body["event_id"], self.event_at_4000)

    @parameterized.expand(
        [
            # Query backwards at 0s, no events to be found.
            (0, "b"),
            # Query forwards from 8100s, no events to be found.
            (8100000, "f"),
        ]
    )
    def test_no_matching_event(self, ts: int, dir: str) -> None:
        """
        Tests that a 404 / M_NOT_FOUND is returned when no event occurs
        in the requested direction of a timestamp.
        """
        # Join the remote server to the room
        self.inject_room_member(self.room_id, "@user:" + self.OTHER_SERVER_NAME, "join")

        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/timestamp_to_event/{self.room_id}?ts={ts}&dir={dir}",
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    def test_requires_server_in_room(self) -> None:
        """
        Tests that a server not in the room is rejected with 403 / M_FORBIDDEN.
        """
        # Notably: _don't_ join the remote server to the room

        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/timestamp_to_event/{self.room_id}?ts=2000000&dir=b",
        )
        self.assertEqual(channel.code, HTTPStatus.FORBIDDEN, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")


class UnstableGetExtremitiesTests(unittest.FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self._storage_controllers = hs.get_storage_controllers()

    def _make_endpoint_path(self, room_id: str) -> str:
        return f"/_matrix/federation/unstable/org.matrix.msc4370/extremities/{room_id}"

    def _remote_join(self, room_id: str, room_version: str) -> str:
        # Note: other tests ensure the called endpoints in this function return useful
        # and proper data.

        # make_join first
        joining_user = "@misspiggy:" + self.OTHER_SERVER_NAME
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/make_join/{room_id}/{joining_user}?ver={room_version}",
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        join_result = channel.json_body

        # Sign/populate the join
        join_event_dict = join_result["event"]
        self.add_hashes_and_signatures_from_other_server(
            join_event_dict,
            KNOWN_ROOM_VERSIONS[room_version],
        )
        if room_version in ["1", "2"]:
            add_hashes_and_signatures(
                KNOWN_ROOM_VERSIONS[room_version],
                join_event_dict,
                signature_name=self.hs.hostname,
                signing_key=self.hs.signing_key,
            )

        # Send the join
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/send_join/{room_id}/x",
            content=join_event_dict,
        )

        # Check that things went okay so the test doesn't become a total train wreck
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        r = self.get_success(self._storage_controllers.state.get_current_state(room_id))
        self.assertEqual(r[("m.room.member", joining_user)].membership, "join")

        return r[("m.room.member", joining_user)].event_id

    def _test_get_extremities_common(self, room_version: str) -> None:
        # Create a room to test with
        creator_user_id = self.register_user("kermit", "test")
        tok = self.login("kermit", "test")
        room_id = self.helper.create_room_as(
            room_creator=creator_user_id,
            tok=tok,
            room_version=room_version,
            extra_content={
                # Public preset uses `shared` history visibility, but makes joins
                # easier in our tests.
                # https://spec.matrix.org/v1.16/client-server-api/#post_matrixclientv3createroom
                "preset": "public_chat"
            },
        )

        # At this stage we should fail to get the extremities because we're not joined
        # and therefore can't see the events (`shared` history visibility).
        channel = self.make_signed_federation_request(
            "GET", self._make_endpoint_path(room_id)
        )
        self.assertEqual(channel.code, HTTPStatus.FORBIDDEN, channel.json_body)
        self.assertEqual(channel.json_body["error"], "Host not in room.")
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")

        # Now join the room and try again
        # Note: there should be just one extremity: the join
        join_event_id = self._remote_join(room_id, room_version)
        channel = self.make_signed_federation_request(
            "GET", self._make_endpoint_path(room_id)
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        self.assertEqual(channel.json_body["prev_events"], [join_event_id])

        # ACL the calling server and try again. This should cause an error getting extremities.
        self.helper.send_state(
            room_id,
            "m.room.server_acl",
            {
                "allow": ["*"],
                "allow_ip_literals": False,
                "deny": [self.OTHER_SERVER_NAME],
            },
            tok=tok,
            expect_code=HTTPStatus.OK,
        )
        channel = self.make_signed_federation_request(
            "GET", self._make_endpoint_path(room_id)
        )
        self.assertEqual(channel.code, HTTPStatus.FORBIDDEN, channel.json_body)
        self.assertEqual(channel.json_body["error"], "Server is banned from room")
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")

    # FIXME: Exclude MSC4242 room versions whilst it lacks federation support
    @parameterized.expand(
        [
            (k,)
            for k in KNOWN_ROOM_VERSIONS.keys()
            if k != RoomVersions.MSC4242v12.identifier
        ]
    )
    @override_config(
        {"use_frozen_dicts": True, "experimental_features": {"msc4370_enabled": True}}
    )
    def test_get_extremities_with_frozen_dicts(self, room_version: str) -> None:
        """Test GET /extremities with USE_FROZEN_DICTS=True"""
        self._test_get_extremities_common(room_version)

    # FIXME: Exclude MSC4242 room versions whilst it lacks federation support
    @parameterized.expand(
        [
            (k,)
            for k in KNOWN_ROOM_VERSIONS.keys()
            if k != RoomVersions.MSC4242v12.identifier
        ]
    )
    @override_config(
        {"use_frozen_dicts": False, "experimental_features": {"msc4370_enabled": True}}
    )
    def test_get_extremities_without_frozen_dicts(self, room_version: str) -> None:
        """Test GET /extremities with USE_FROZEN_DICTS=False"""
        self._test_get_extremities_common(room_version)

    # note the lack of config-setting stuff on this test.
    def test_get_extremities_unstable_not_enabled(self) -> None:
        """Test that GET /extremities returns M_UNRECOGNIZED when MSC4370 is not enabled"""
        # We shouldn't even have to create a room - the endpoint should just fail.
        channel = self.make_signed_federation_request(
            "GET", self._make_endpoint_path("!room:example.org")
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_UNRECOGNIZED")


class EventAuthFederationTests(unittest.FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Create a local user
        self.user_id = self.register_user("alice", "password")
        self.user_tok = self.login("alice", "password")

        # Set up a room and join the remote server to it
        self.room_id = self.helper.create_room_as(
            self.user_id,
            is_public=True,
            room_version=RoomVersions.V10.identifier,
            tok=self.user_tok,
        )
        self.inject_room_member(
            self.room_id, f"@remote:{self.OTHER_SERVER_NAME}", Membership.JOIN
        )

        # Create a known event whose auth chain we can request back.
        self.event_id = self.helper.send_messages(
            self.room_id, num_events=1, tok=self.user_tok
        )[0]

        return super().prepare(reactor, clock, hs)

    def test_event_auth_unknown_event_returns_404(self) -> None:
        """
        Tests that requesting the auth chain of an unknown event
        returns 404 / M_NOT_FOUND.
        """

        # Request an event that doesn't exist in self.room_id.
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/event_auth/{self.room_id}/$unknownevent",
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND, channel.result)
        self.assertEqual(
            channel.json_body["errcode"], Codes.NOT_FOUND, channel.json_body
        )

    def test_event_auth_wrong_room_returns_404(self) -> None:
        """
        Tests that a request whose `room_id` is wrong for the event
        acts the same as though it were an unknown event.

        Regression test for https://github.com/element-hq/synapse/security/advisories/GHSA-qcjr-46gf-7f4r
        """

        # Create a second room with its own event.
        other_room_id = self.helper.create_room_as(
            self.user_id,
            is_public=True,
            room_version=RoomVersions.V10.identifier,
            tok=self.user_tok,
        )
        other_room_event_id = self.helper.send_messages(
            other_room_id, num_events=1, tok=self.user_tok
        )[0]

        # Request the chain of other_room_id's event, but pretend it's part of the room
        # we are in.
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/event_auth/{self.room_id}/{other_room_event_id}",
        )

        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND, channel.result)
        self.assertEqual(
            channel.json_body["errcode"], Codes.NOT_FOUND, channel.json_body
        )


class SendJoinFederationTests(unittest.FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self._storage_controllers = hs.get_storage_controllers()

        # create the room
        creator_user_id = self.register_user("kermit", "test")
        tok = self.login("kermit", "test")
        self._room_id = self.helper.create_room_as(
            room_creator=creator_user_id, tok=tok
        )

        # a second member on the orgin HS
        second_member_user_id = self.register_user("fozzie", "bear")
        tok2 = self.login("fozzie", "bear")
        self.helper.join(self._room_id, second_member_user_id, tok=tok2)

    def _make_join(self, user_id: str) -> JsonDict:
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/make_join/{self._room_id}/{user_id}"
            f"?ver={DEFAULT_ROOM_VERSION}",
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        return channel.json_body

    def _test_send_join_common(self, room_version: str) -> None:
        """happy-path test of send_join"""
        creator_user_id = self.register_user(f"kermit_v{room_version}", "test")
        tok = self.login(f"kermit_v{room_version}", "test")
        room_id = self.helper.create_room_as(
            room_creator=creator_user_id, tok=tok, room_version=room_version
        )

        # Second member joins
        second_member_user_id = self.register_user(f"fozzie_v{room_version}", "bear")
        tok2 = self.login(f"fozzie_v{room_version}", "bear")
        self.helper.join(room_id, second_member_user_id, tok=tok2)

        # Make join for remote user
        joining_user = "@misspiggy:" + self.OTHER_SERVER_NAME
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/make_join/{room_id}/{joining_user}?ver={room_version}",
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        join_result = channel.json_body

        # Sign and send the join
        join_event_dict = join_result["event"]
        self.add_hashes_and_signatures_from_other_server(
            join_event_dict,
            KNOWN_ROOM_VERSIONS[room_version],
        )
        if room_version in ["1", "2"]:
            add_hashes_and_signatures(
                KNOWN_ROOM_VERSIONS[room_version],
                join_event_dict,
                signature_name=self.hs.hostname,
                signing_key=self.hs.signing_key,
            )
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/send_join/{room_id}/x",
            content=join_event_dict,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        # we should get complete room state back
        returned_state = [
            (ev["type"], ev["state_key"]) for ev in channel.json_body["state"]
        ]
        self.assertCountEqual(
            returned_state,
            [
                ("m.room.create", ""),
                ("m.room.power_levels", ""),
                ("m.room.join_rules", ""),
                ("m.room.history_visibility", ""),
                ("m.room.member", f"@kermit_v{room_version}:test"),
                ("m.room.member", f"@fozzie_v{room_version}:test"),
                # nb: *not* the joining user
            ],
        )

        # also check the auth chain
        returned_auth_chain_events = [
            (ev["type"], ev["state_key"]) for ev in channel.json_body["auth_chain"]
        ]
        self.assertCountEqual(
            returned_auth_chain_events,
            [
                ("m.room.create", ""),
                ("m.room.member", f"@kermit_v{room_version}:test"),
                ("m.room.power_levels", ""),
                ("m.room.join_rules", ""),
            ],
        )

        # the room should show that the new user is a member
        r = self.get_success(self._storage_controllers.state.get_current_state(room_id))
        self.assertEqual(r[("m.room.member", joining_user)].membership, "join")

    @parameterized.expand([(k,) for k in KNOWN_ROOM_VERSIONS.keys()])
    @override_config({"use_frozen_dicts": True})
    def test_send_join_with_frozen_dicts(self, room_version: str) -> None:
        """Test send_join with USE_FROZEN_DICTS=True"""
        if room_version == RoomVersions.MSC4242v12.identifier:
            # TODO: This room version doesn't work over federation in this PR.
            return
        self._test_send_join_common(room_version)

    @parameterized.expand([(k,) for k in KNOWN_ROOM_VERSIONS.keys()])
    @override_config({"use_frozen_dicts": False})
    def test_send_join_without_frozen_dicts(self, room_version: str) -> None:
        """Test send_join with USE_FROZEN_DICTS=False"""
        if room_version == RoomVersions.MSC4242v12.identifier:
            # TODO: This room version doesn't work over federation in this PR.
            return
        self._test_send_join_common(room_version)

    def test_send_join_partial_state(self) -> None:
        """/send_join should return partial state, if requested"""
        joining_user = "@misspiggy:" + self.OTHER_SERVER_NAME
        join_result = self._make_join(joining_user)

        join_event_dict = join_result["event"]
        self.add_hashes_and_signatures_from_other_server(
            join_event_dict,
            KNOWN_ROOM_VERSIONS[DEFAULT_ROOM_VERSION],
        )
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/send_join/{self._room_id}/x?omit_members=true",
            content=join_event_dict,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        # expect a reduced room state
        returned_state = [
            (ev["type"], ev["state_key"]) for ev in channel.json_body["state"]
        ]
        self.assertCountEqual(
            returned_state,
            [
                ("m.room.create", ""),
                ("m.room.power_levels", ""),
                ("m.room.join_rules", ""),
                ("m.room.history_visibility", ""),
                # Users included here because they're heroes.
                ("m.room.member", "@kermit:test"),
                ("m.room.member", "@fozzie:test"),
            ],
        )

        # the auth chain should not include anything already in "state"
        returned_auth_chain_events = [
            (ev["type"], ev["state_key"]) for ev in channel.json_body["auth_chain"]
        ]
        self.assertCountEqual(
            returned_auth_chain_events,
            # TODO: change the test so that we get at least one event in the auth chain
            #   here.
            [],
        )

        # the room should show that the new user is a member
        r = self.get_success(
            self._storage_controllers.state.get_current_state(self._room_id)
        )
        self.assertEqual(r[("m.room.member", joining_user)].membership, "join")

    @override_config({"rc_joins_per_room": {"per_second": 0.1, "burst_count": 3}})
    def test_make_join_respects_room_join_rate_limit(self) -> None:
        # In the test setup, two users join the room. Since the rate limiter burst
        # count is 3, a new make_join request to the room should be accepted.

        joining_user = "@ronniecorbett:" + self.OTHER_SERVER_NAME
        self._make_join(joining_user)

        # Now have a new local user join the room. This saturates the rate limiter
        # bucket, so the next make_join should be denied.
        new_local_user = self.register_user("animal", "animal")
        token = self.login("animal", "animal")
        self.helper.join(self._room_id, new_local_user, tok=token)

        joining_user = "@ronniebarker:" + self.OTHER_SERVER_NAME
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/make_join/{self._room_id}/{joining_user}"
            f"?ver={DEFAULT_ROOM_VERSION}",
        )
        self.assertEqual(channel.code, HTTPStatus.TOO_MANY_REQUESTS, channel.json_body)

    @override_config({"rc_joins_per_room": {"per_second": 0.1, "burst_count": 3}})
    def test_send_join_contributes_to_room_join_rate_limit_and_is_limited(self) -> None:
        # Make two make_join requests up front. (These are rate limited, but do not
        # contribute to the rate limit.)
        join_event_dicts = []
        for i in range(2):
            joining_user = f"@misspiggy{i}:{self.OTHER_SERVER_NAME}"
            join_result = self._make_join(joining_user)
            join_event_dict = join_result["event"]
            self.add_hashes_and_signatures_from_other_server(
                join_event_dict,
                KNOWN_ROOM_VERSIONS[DEFAULT_ROOM_VERSION],
            )
            join_event_dicts.append(join_event_dict)

        # In the test setup, two users join the room. Since the rate limiter burst
        # count is 3, the first send_join should be accepted...
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/send_join/{self._room_id}/join0",
            content=join_event_dicts[0],
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # ... but the second should be denied.
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/send_join/{self._room_id}/join1",
            content=join_event_dicts[1],
        )
        self.assertEqual(channel.code, HTTPStatus.TOO_MANY_REQUESTS, channel.json_body)

    # NB: we could write a test which checks that the send_join event is seen
    #   by other workers over replication, and that they update their rate limit
    #   buckets accordingly. I'm going to assume that the join event gets sent over
    #   replication, at which point the tests.handlers.room_member test
    #       test_local_users_joining_on_another_worker_contribute_to_rate_limit
    #   is probably sufficient to reassure that the bucket is updated.


class StripUnsignedFromEventsTestCase(unittest.TestCase):
    """
    Test to make sure that we handle the raw JSON events from federation carefully and
    strip anything that shouldn't be there.
    """

    def test_strip_unauthorized_unsigned_values(self) -> None:
        event1 = {
            "sender": "@baduser:test.serv",
            "state_key": "@baduser:test.serv",
            "event_id": "$event1:test.serv",
            "depth": 1000,
            "origin_server_ts": 1,
            "type": "m.room.member",
            "content": {"membership": "join"},
            "auth_events": [],
            "unsigned": {"malicious garbage": "hackz", "more warez": "more hackz"},
        }
        filtered_event = make_test_pdu_event(event1, RoomVersions.V1)
        # Make sure unauthorized fields are stripped from unsigned
        self.assertNotIn("more warez", filtered_event.unsigned)

    def test_strip_event_maintains_allowed_fields(self) -> None:
        event2 = {
            "sender": "@baduser:test.serv",
            "state_key": "@baduser:test.serv",
            "event_id": "$event2:test.serv",
            "depth": 1000,
            "origin_server_ts": 1,
            "type": "m.room.member",
            "auth_events": [],
            "content": {"membership": "join"},
            "unsigned": {
                "malicious garbage": "hackz",
                "more warez": "more hackz",
                "age": 14,
                "invite_room_state": [],
            },
        }

        filtered_event2 = make_test_pdu_event(event2, RoomVersions.V1, received_time=20)
        self.assertIn("age_ts", filtered_event2.unsigned)
        self.assertEqual(6, filtered_event2.unsigned["age_ts"])
        self.assertNotIn("more warez", filtered_event2.unsigned)
        # Invite_room_state is allowed in events of type m.room.member
        self.assertIn("invite_room_state", filtered_event2.unsigned)
        self.assertEqual([], filtered_event2.unsigned["invite_room_state"])

    def test_strip_event_removes_fields_based_on_event_type(self) -> None:
        event3 = {
            "sender": "@baduser:test.serv",
            "state_key": "@baduser:test.serv",
            "event_id": "$event3:test.serv",
            "depth": 1000,
            "origin_server_ts": 1,
            "type": "m.room.power_levels",
            "content": {},
            "auth_events": [],
            "unsigned": {
                "malicious garbage": "hackz",
                "more warez": "more hackz",
                "age": 14,
                "invite_room_state": [],
            },
        }
        filtered_event3 = make_test_pdu_event(event3, RoomVersions.V1, received_time=20)
        self.assertIn("age_ts", filtered_event3.unsigned)
        # Invite_room_state field is only permitted in event type m.room.member
        self.assertNotIn("invite_room_state", filtered_event3.unsigned)
        self.assertNotIn("more warez", filtered_event3.unsigned)
