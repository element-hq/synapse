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
from synapse.api.errors import FederationError
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersions
from synapse.config.server import DEFAULT_ROOM_VERSION
from synapse.events import EventBase, make_event_from_dict
from synapse.federation.federation_base import event_from_pdu_json
from synapse.http.types import QueryParams
from synapse.logging.context import LoggingContext
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.controllers.state import server_acl_evaluator_from_event
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
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


def _create_acl_event(content: JsonDict) -> EventBase:
    return make_event_from_dict(
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
        self.remote_bad_user_join_event = make_event_from_dict(
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
        lying_event = make_event_from_dict(
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

    def test_send_join(self) -> None:
        """happy-path test of send_join"""
        joining_user = "@misspiggy:" + self.OTHER_SERVER_NAME
        join_result = self._make_join(joining_user)

        join_event_dict = join_result["event"]
        self.add_hashes_and_signatures_from_other_server(
            join_event_dict,
            KNOWN_ROOM_VERSIONS[DEFAULT_ROOM_VERSION],
        )
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/send_join/{self._room_id}/x",
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
                ("m.room.member", "@kermit:test"),
                ("m.room.member", "@fozzie:test"),
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
                ("m.room.member", "@kermit:test"),
                ("m.room.power_levels", ""),
                ("m.room.join_rules", ""),
            ],
        )

        # the room should show that the new user is a member
        r = self.get_success(
            self._storage_controllers.state.get_current_state(self._room_id)
        )
        self.assertEqual(r[("m.room.member", joining_user)].membership, "join")

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
        filtered_event = event_from_pdu_json(event1, RoomVersions.V1)
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

        filtered_event2 = event_from_pdu_json(event2, RoomVersions.V1)
        self.assertIn("age", filtered_event2.unsigned)
        self.assertEqual(14, filtered_event2.unsigned["age"])
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
        filtered_event3 = event_from_pdu_json(event3, RoomVersions.V1)
        self.assertIn("age", filtered_event3.unsigned)
        # Invite_room_state field is only permitted in event type m.room.member
        self.assertNotIn("invite_room_state", filtered_event3.unsigned)
        self.assertNotIn("more warez", filtered_event3.unsigned)
