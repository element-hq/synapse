#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
import urllib.parse
from http import HTTPStatus
from typing import Callable, Optional, Tuple, TypeVar, Union
from unittest.mock import Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.errors import FederationError
from synapse.api.room_versions import RoomVersions
from synapse.events import make_event_from_dict
from synapse.events.utils import strip_event
from synapse.federation.federation_base import event_from_pdu_json
from synapse.federation.transport.client import SendJoinResponse
from synapse.http.matrixfederationclient import (
    ByteParser,
)
from synapse.http.types import QueryParams
from synapse.logging.context import LoggingContext
from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest
from tests.utils import test_timeout

logger = logging.getLogger(__name__)


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

        # Figure out what the forward extremeties in the room are (the most recent
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
            data: Optional[JsonDict] = None,
            long_retries: bool = False,
            timeout: Optional[int] = None,
            ignore_backoff: bool = False,
            args: Optional[QueryParams] = None,
        ) -> Union[JsonDict, list]:
            # If it asks us for new missing events, give them NOTHING
            if path.startswith("/_matrix/federation/v1/get_missing_events/"):
                return {"events": []}
            return {}

        self.http_client.post_json = post_json

        # Figure out what the forward extremeties in the room are (the most recent
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

        with LoggingContext("test-context"):
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


class OutOfBandMembershipTests(unittest.FederatingHomeserverTestCase):
    """
    Tests to make sure that we can join remote rooms over federation.
    """

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    sync_endpoint = "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_http_client = Mock(
            # spec=MatrixFederationHttpClient
        )
        return self.setup_test_homeserver(
            federation_http_client=self.federation_http_client
        )

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.store = self.hs.get_datastores().main

    def do_sync(
        self, sync_body: JsonDict, *, since: Optional[str] = None, tok: str
    ) -> Tuple[JsonDict, str]:
        """Do a sliding sync request with given body.

        Asserts the request was successful.

        Attributes:
            sync_body: The full request body to use
            since: Optional since token
            tok: Access token to use

        Returns:
            A tuple of the response body and the `pos` field.
        """

        sync_path = self.sync_endpoint
        if since:
            sync_path += f"?pos={since}"

        channel = self.make_request(
            method="POST",
            path=sync_path,
            content=sync_body,
            access_token=tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        return channel.json_body, channel.json_body["pos"]

    def test_asdf(self) -> None:
        # Create a local room
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        # user2_id = self.register_user("user2", "pass")
        # user2_tok = self.login(user2_id, "pass")

        # Create a remote room
        room_creator_user_id = f"@user:{self.OTHER_SERVER_NAME}"
        room_id = f"!foo:{self.OTHER_SERVER_NAME}"
        room_version = RoomVersions.V10

        room_create_event = make_event_from_dict(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": room_id,
                    "sender": room_creator_user_id,
                    "depth": 1,
                    "origin_server_ts": 1,
                    "type": EventTypes.Create,
                    "state_key": "",
                    "content": {
                        # The `ROOM_CREATOR` field could be removed if we used a room
                        # version > 10 (in favor of relying on `sender`)
                        EventContentFields.ROOM_CREATOR: room_creator_user_id,
                        EventContentFields.ROOM_VERSION: room_version.identifier,
                    },
                    "auth_events": [],
                    "prev_events": [],
                }
            ),
            room_version=room_version,
        )

        creator_membership_event = make_event_from_dict(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": room_id,
                    "sender": room_creator_user_id,
                    "depth": 2,
                    "origin_server_ts": 2,
                    "type": EventTypes.Member,
                    "state_key": room_creator_user_id,
                    "content": {"membership": Membership.JOIN},
                    "auth_events": [room_create_event.event_id],
                    "prev_events": [room_create_event.event_id],
                }
            ),
            room_version=room_version,
        )

        # From the remote homeserver, invite user1 on the local homserver
        user1_invite_membership_event = make_event_from_dict(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": room_id,
                    "sender": room_creator_user_id,
                    "depth": 3,
                    "origin_server_ts": 3,
                    "type": EventTypes.Member,
                    "state_key": user1_id,
                    "content": {"membership": Membership.INVITE},
                    "auth_events": [
                        room_create_event.event_id,
                        creator_membership_event.event_id,
                    ],
                    "prev_events": [creator_membership_event.event_id],
                }
            ),
            room_version=room_version,
        )
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/invite/{room_id}/{user1_invite_membership_event.event_id}",
            content={
                "event": user1_invite_membership_event.get_dict(),
                "invite_room_state": [
                    strip_event(room_create_event),
                    strip_event(creator_membership_event),
                ],
                "room_version": room_version.identifier,
            },
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }

        # Sync until the local user1 can see the invite
        with test_timeout(5):
            while True:
                response_body, _ = self.do_sync(sync_body, tok=user1_tok)
                if room_id in response_body["rooms"].keys():
                    break

        user1_join_membership_event_template = make_event_from_dict(
            {
                "room_id": room_id,
                "sender": user1_id,
                "depth": 4,
                "origin_server_ts": 4,
                "type": EventTypes.Member,
                "state_key": user1_id,
                "content": {"membership": Membership.JOIN},
                "auth_events": [
                    room_create_event.event_id,
                    user1_invite_membership_event.event_id,
                ],
                "prev_events": [user1_invite_membership_event.event_id],
            },
            room_version=room_version,
        )

        T = TypeVar("T")

        async def get_json(
            destination: str,
            path: str,
            args: Optional[QueryParams] = None,
            retry_on_dns_fail: bool = True,
            timeout: Optional[int] = None,
            ignore_backoff: bool = False,
            try_trailing_slash_on_400: bool = False,
            parser: Optional[ByteParser[T]] = None,
        ) -> Union[JsonDict, T]:
            logger.info("asdf get_json %s %s", destination, path)

            if (
                path
                == f"/_matrix/federation/v1/make_join/{urllib.parse.quote_plus(room_id)}/{urllib.parse.quote_plus(user1_id)}"
            ):
                return {
                    "event": user1_join_membership_event_template.get_pdu_json(),
                    "room_version": room_version.identifier,
                }

            return {}

        self.federation_http_client.get_json.side_effect = get_json

        async def put_json(
            destination: str,
            path: str,
            args: Optional[QueryParams] = None,
            data: Optional[JsonDict] = None,
            json_data_callback: Optional[Callable[[], JsonDict]] = None,
            long_retries: bool = False,
            timeout: Optional[int] = None,
            ignore_backoff: bool = False,
            backoff_on_404: bool = False,
            try_trailing_slash_on_400: bool = False,
            parser: Optional[ByteParser[T]] = None,
            backoff_on_all_error_codes: bool = False,
        ) -> Union[JsonDict, T]:
            logger.info("asdf put_json %s %s parser=%s", destination, path, parser)

            if (
                path.startswith(
                    f"/_matrix/federation/v2/send_join/{urllib.parse.quote_plus(room_id)}/"
                )
                and data is not None
                and data.get("type") == EventTypes.Member
                and data.get("state_key") == user1_id
                # We're assuming this is a `ByteParser[SendJoinResponse]`
                and parser is not None
            ):
                user1_join_membership_event_signed = make_event_from_dict(
                    self.add_hashes_and_signatures_from_other_server(data),
                    room_version=room_version,
                )

                return SendJoinResponse(
                    auth_events=[
                        room_create_event,
                        user1_invite_membership_event,
                    ],
                    state=[
                        room_create_event,
                        creator_membership_event,
                        user1_invite_membership_event,
                    ],
                    event_dict=user1_join_membership_event_signed.get_pdu_json(),
                    event=user1_join_membership_event_signed,
                    members_omitted=False,
                    servers_in_room=[
                        self.OTHER_SERVER_NAME,
                    ],
                )

            return {}

        self.federation_http_client.put_json.side_effect = put_json

        # User1 joins the room
        self.helper.join(room_id, user1_id, tok=user1_tok)


class StripUnsignedFromEventsTestCase(unittest.TestCase):
    def test_strip_unauthorized_unsigned_values(self) -> None:
        event1 = {
            "sender": "@baduser:test.serv",
            "state_key": "@baduser:test.serv",
            "event_id": "$event1:test.serv",
            "depth": 1000,
            "origin_server_ts": 1,
            "type": "m.room.member",
            "origin": "test.servx",
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
            "origin": "test.servx",
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
            "origin": "test.servx",
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
