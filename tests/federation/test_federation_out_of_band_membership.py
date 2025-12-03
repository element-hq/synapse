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
import time
import urllib.parse
from http import HTTPStatus
from typing import Any, Callable, TypeVar
from unittest.mock import Mock

import attr
from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.events import EventBase, make_event_from_dict
from synapse.events.utils import strip_event
from synapse.federation.federation_base import (
    event_from_pdu_json,
)
from synapse.federation.transport.client import SendJoinResponse
from synapse.http.matrixfederationclient import (
    ByteParser,
)
from synapse.http.types import QueryParams
from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, MutableStateMap, StateMap
from synapse.types.handlers.sliding_sync import (
    StateValues,
)
from synapse.util.clock import Clock

from tests import unittest
from tests.utils import test_timeout

logger = logging.getLogger(__name__)


def required_state_json_to_state_map(required_state: Any) -> StateMap[EventBase]:
    state_map: MutableStateMap[EventBase] = {}

    # Scrutinize JSON values to ensure it's in the expected format
    if isinstance(required_state, list):
        for state_event_dict in required_state:
            # Yell because we're in a test and this is unexpected
            assert isinstance(state_event_dict, dict), (
                "`required_state` should be a list of event dicts"
            )

            event_type = state_event_dict["type"]
            event_state_key = state_event_dict["state_key"]

            # Yell because we're in a test and this is unexpected
            assert isinstance(event_type, str), (
                "Each event in `required_state` should have a string `type`"
            )
            assert isinstance(event_state_key, str), (
                "Each event in `required_state` should have a string `state_key`"
            )

            state_map[(event_type, event_state_key)] = make_event_from_dict(
                state_event_dict
            )
    else:
        # Yell because we're in a test and this is unexpected
        raise AssertionError("`required_state` should be a list of event dicts")

    return state_map


@attr.s(slots=True, auto_attribs=True)
class RemoteRoomJoinResult:
    remote_room_id: str
    room_version: RoomVersion
    remote_room_creator_user_id: str
    local_user1_id: str
    local_user1_tok: str
    state_map: StateMap[EventBase]


class OutOfBandMembershipTests(unittest.FederatingHomeserverTestCase):
    """
    Tests to make sure that interactions with out-of-band membership (outliers) works as
    expected.

     - invites received over federation, before we join the room
     - *rejections* for said invites

    See the "Out-of-band membership events" section in
    `docs/development/room-dag-concepts.md` for more information.
    """

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    sync_endpoint = "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"

    def default_config(self) -> JsonDict:
        conf = super().default_config()
        # Federation sending is disabled by default in the test environment
        # so we need to enable it like this.
        conf["federation_sender_instances"] = ["master"]

        return conf

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_http_client = Mock(
            # The problem with using `spec=MatrixFederationHttpClient` here is that it
            # requires everything to be mocked which is a lot of work that I don't want
            # to do when the code only uses a few methods (`get_json` and `put_json`).
        )
        return self.setup_test_homeserver(
            federation_http_client=self.federation_http_client
        )

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.store = self.hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()

    def do_sync(
        self, sync_body: JsonDict, *, since: str | None = None, tok: str
    ) -> tuple[JsonDict, str]:
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

    def _invite_local_user_to_remote_room_and_join(self) -> RemoteRoomJoinResult:
        """
        Helper to reproduce this scenario:

         1. The remote user invites our local user to a room on their remote server (which
        creates an out-of-band invite membership for user1 on our local server).
         2. The local user notices the invite from `/sync`.
         3. The local user joins the room.
         4. The local user can see that they are now joined to the room from `/sync`.
        """

        # Create a local user
        local_user1_id = self.register_user("user1", "pass")
        local_user1_tok = self.login(local_user1_id, "pass")

        # Create a remote room
        room_creator_user_id = f"@remote-user:{self.OTHER_SERVER_NAME}"
        remote_room_id = f"!remote-room:{self.OTHER_SERVER_NAME}"
        room_version = RoomVersions.V10

        room_create_event = make_event_from_dict(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": remote_room_id,
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
                    "room_id": remote_room_id,
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
                    "room_id": remote_room_id,
                    "sender": room_creator_user_id,
                    "depth": 3,
                    "origin_server_ts": 3,
                    "type": EventTypes.Member,
                    "state_key": local_user1_id,
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
            f"/_matrix/federation/v2/invite/{remote_room_id}/{user1_invite_membership_event.event_id}",
            content={
                "event": user1_invite_membership_event.get_dict(),
                "invite_room_state": [
                    strip_event(room_create_event),
                ],
                "room_version": room_version.identifier,
            },
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [(EventTypes.Member, StateValues.WILDCARD)],
                    "timeline_limit": 0,
                }
            }
        }

        # Sync until the local user1 can see the invite
        with test_timeout(
            3,
            "Unable to find user1's invite event in the room",
        ):
            while True:
                response_body, _ = self.do_sync(sync_body, tok=local_user1_tok)
                if (
                    remote_room_id in response_body["rooms"].keys()
                    # If they have `invite_state` for the room, they are invited
                    and len(
                        response_body["rooms"][remote_room_id].get("invite_state", [])
                    )
                    > 0
                ):
                    break

                # Prevent tight-looping to allow the `test_timeout` to work
                time.sleep(0.1)

        user1_join_membership_event_template = make_event_from_dict(
            {
                "room_id": remote_room_id,
                "sender": local_user1_id,
                "depth": 4,
                "origin_server_ts": 4,
                "type": EventTypes.Member,
                "state_key": local_user1_id,
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

        # Mock the remote homeserver responding to our HTTP requests
        #
        # We're going to mock the following endpoints so that user1 can join the remote room:
        # - GET /_matrix/federation/v1/make_join/{room_id}/{user_id}
        # - PUT /_matrix/federation/v2/send_join/{room_id}/{user_id}
        #
        async def get_json(
            destination: str,
            path: str,
            args: QueryParams | None = None,
            retry_on_dns_fail: bool = True,
            timeout: int | None = None,
            ignore_backoff: bool = False,
            try_trailing_slash_on_400: bool = False,
            parser: ByteParser[T] | None = None,
        ) -> JsonDict | T:
            if (
                path
                == f"/_matrix/federation/v1/make_join/{urllib.parse.quote_plus(remote_room_id)}/{urllib.parse.quote_plus(local_user1_id)}"
            ):
                return {
                    "event": user1_join_membership_event_template.get_pdu_json(),
                    "room_version": room_version.identifier,
                }

            raise NotImplementedError(
                "We have not mocked a response for `get_json(...)` for the following endpoint yet: "
                + f"{destination}{path}"
            )

        self.federation_http_client.get_json.side_effect = get_json

        # PDU's that hs1 sent to hs2
        collected_pdus_from_hs1_federation_send: set[str] = set()

        async def put_json(
            destination: str,
            path: str,
            args: QueryParams | None = None,
            data: JsonDict | None = None,
            json_data_callback: Callable[[], JsonDict] | None = None,
            long_retries: bool = False,
            timeout: int | None = None,
            ignore_backoff: bool = False,
            backoff_on_404: bool = False,
            try_trailing_slash_on_400: bool = False,
            parser: ByteParser[T] | None = None,
            backoff_on_all_error_codes: bool = False,
        ) -> JsonDict | T | SendJoinResponse:
            if (
                path.startswith(
                    f"/_matrix/federation/v2/send_join/{urllib.parse.quote_plus(remote_room_id)}/"
                )
                and data is not None
                and data.get("type") == EventTypes.Member
                and data.get("state_key") == local_user1_id
                # We're assuming this is a `ByteParser[SendJoinResponse]`
                and parser is not None
            ):
                # As the remote server, we need to sign the event before sending it back
                user1_join_membership_event_signed = make_event_from_dict(
                    self.add_hashes_and_signatures_from_other_server(data),
                    room_version=room_version,
                )

                # Since they passed in a `parser`, we need to return the type that
                # they're expecting instead of just a `JsonDict`
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

            if path.startswith("/_matrix/federation/v1/send/") and data is not None:
                for pdu in data.get("pdus", []):
                    event = event_from_pdu_json(pdu, room_version)
                    collected_pdus_from_hs1_federation_send.add(event.event_id)

                # Just acknowledge everything hs1 is trying to send hs2
                return {
                    event_from_pdu_json(pdu, room_version).event_id: {}
                    for pdu in data.get("pdus", [])
                }

            raise NotImplementedError(
                "We have not mocked a response for `put_json(...)` for the following endpoint yet: "
                + f"{destination}{path} with the following body data: {data}"
            )

        self.federation_http_client.put_json.side_effect = put_json

        # User1 joins the room
        self.helper.join(remote_room_id, local_user1_id, tok=local_user1_tok)

        # Reset the mocks now that user1 has joined the room
        self.federation_http_client.get_json.side_effect = None
        self.federation_http_client.put_json.side_effect = None

        # Sync until the local user1 can see that they are now joined to the room
        with test_timeout(
            3,
            "Unable to find user1's join event in the room",
        ):
            while True:
                response_body, _ = self.do_sync(sync_body, tok=local_user1_tok)
                if remote_room_id in response_body["rooms"].keys():
                    required_state_map = required_state_json_to_state_map(
                        response_body["rooms"][remote_room_id]["required_state"]
                    )
                    if (
                        required_state_map.get((EventTypes.Member, local_user1_id))
                        is not None
                    ):
                        break

                # Prevent tight-looping to allow the `test_timeout` to work
                time.sleep(0.1)

        # Nothing needs to be sent from hs1 to hs2 since we already let the other
        # homeserver know by doing the `/make_join` and `/send_join` dance.
        self.assertIncludes(
            collected_pdus_from_hs1_federation_send,
            set(),
            exact=True,
            message="Didn't expect any events to be sent from hs1 over federation to hs2",
        )

        return RemoteRoomJoinResult(
            remote_room_id=remote_room_id,
            room_version=room_version,
            remote_room_creator_user_id=room_creator_user_id,
            local_user1_id=local_user1_id,
            local_user1_tok=local_user1_tok,
            state_map=self.get_success(
                self.storage_controllers.state.get_current_state(remote_room_id)
            ),
        )

    def test_can_join_from_out_of_band_invite(self) -> None:
        """
        Test to make sure that we can join a room that we were invited to over
        federation; even if our server has never participated in the room before.
        """
        self._invite_local_user_to_remote_room_and_join()

    @parameterized.expand(
        [("accept invite", Membership.JOIN), ("reject invite", Membership.LEAVE)]
    )
    def test_can_x_from_out_of_band_invite_after_we_are_already_participating_in_the_room(
        self, _test_description: str, membership_action: str
    ) -> None:
        """
        Test to make sure that we can do either a) join the room (accept the invite) or
        b) reject the invite after being invited to over federation; even if we are
        already participating in the room.

        This is a regression test to make sure we stress the scenario where even though
        we are already participating in the room, local users can still react to invites
        regardless of whether the remote server has told us about the invite event (via
        a federation `/send` transaction) and we have de-outliered the invite event.
        Previously, we would mistakenly throw an error saying the user wasn't in the
        room when they tried to join or reject the invite.
        """
        remote_room_join_result = self._invite_local_user_to_remote_room_and_join()
        remote_room_id = remote_room_join_result.remote_room_id
        room_version = remote_room_join_result.room_version

        # Create another local user
        local_user2_id = self.register_user("user2", "pass")
        local_user2_tok = self.login(local_user2_id, "pass")

        T = TypeVar("T")

        # PDU's that hs1 sent to hs2
        collected_pdus_from_hs1_federation_send: set[str] = set()

        async def put_json(
            destination: str,
            path: str,
            args: QueryParams | None = None,
            data: JsonDict | None = None,
            json_data_callback: Callable[[], JsonDict] | None = None,
            long_retries: bool = False,
            timeout: int | None = None,
            ignore_backoff: bool = False,
            backoff_on_404: bool = False,
            try_trailing_slash_on_400: bool = False,
            parser: ByteParser[T] | None = None,
            backoff_on_all_error_codes: bool = False,
        ) -> JsonDict | T:
            if path.startswith("/_matrix/federation/v1/send/") and data is not None:
                for pdu in data.get("pdus", []):
                    event = event_from_pdu_json(pdu, room_version)
                    collected_pdus_from_hs1_federation_send.add(event.event_id)

                # Just acknowledge everything hs1 is trying to send hs2
                return {
                    event_from_pdu_json(pdu, room_version).event_id: {}
                    for pdu in data.get("pdus", [])
                }

            raise NotImplementedError(
                "We have not mocked a response for `put_json(...)` for the following endpoint yet: "
                + f"{destination}{path} with the following body data: {data}"
            )

        self.federation_http_client.put_json.side_effect = put_json

        # From the remote homeserver, invite user2 on the local homserver
        user2_invite_membership_event = make_event_from_dict(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": remote_room_id,
                    "sender": remote_room_join_result.remote_room_creator_user_id,
                    "depth": 5,
                    "origin_server_ts": 5,
                    "type": EventTypes.Member,
                    "state_key": local_user2_id,
                    "content": {"membership": Membership.INVITE},
                    "auth_events": [
                        remote_room_join_result.state_map[
                            (EventTypes.Create, "")
                        ].event_id,
                        remote_room_join_result.state_map[
                            (
                                EventTypes.Member,
                                remote_room_join_result.remote_room_creator_user_id,
                            )
                        ].event_id,
                    ],
                    "prev_events": [
                        remote_room_join_result.state_map[
                            (EventTypes.Member, remote_room_join_result.local_user1_id)
                        ].event_id
                    ],
                }
            ),
            room_version=room_version,
        )
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/invite/{remote_room_id}/{user2_invite_membership_event.event_id}",
            content={
                "event": user2_invite_membership_event.get_dict(),
                "invite_room_state": [
                    strip_event(
                        remote_room_join_result.state_map[(EventTypes.Create, "")]
                    ),
                ],
                "room_version": room_version.identifier,
            },
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [(EventTypes.Member, StateValues.WILDCARD)],
                    "timeline_limit": 0,
                }
            }
        }

        # Sync until the local user2 can see the invite
        with test_timeout(
            3,
            "Unable to find user2's invite event in the room",
        ):
            while True:
                response_body, _ = self.do_sync(sync_body, tok=local_user2_tok)
                if (
                    remote_room_id in response_body["rooms"].keys()
                    # If they have `invite_state` for the room, they are invited
                    and len(
                        response_body["rooms"][remote_room_id].get("invite_state", [])
                    )
                    > 0
                ):
                    break

                # Prevent tight-looping to allow the `test_timeout` to work
                time.sleep(0.1)

        if membership_action == Membership.JOIN:
            # User2 joins the room
            join_event = self.helper.join(
                remote_room_join_result.remote_room_id,
                local_user2_id,
                tok=local_user2_tok,
            )
            expected_pdu_event_id = join_event["event_id"]
        elif membership_action == Membership.LEAVE:
            # User2 rejects the invite
            leave_event = self.helper.leave(
                remote_room_join_result.remote_room_id,
                local_user2_id,
                tok=local_user2_tok,
            )
            expected_pdu_event_id = leave_event["event_id"]
        else:
            raise NotImplementedError(
                "This test does not support this membership action yet"
            )

        # Sync until the local user2 can see their new membership in the room
        with test_timeout(
            3,
            "Unable to find user2's new membership event in the room",
        ):
            while True:
                response_body, _ = self.do_sync(sync_body, tok=local_user2_tok)
                if membership_action == Membership.JOIN:
                    if remote_room_id in response_body["rooms"].keys():
                        required_state_map = required_state_json_to_state_map(
                            response_body["rooms"][remote_room_id]["required_state"]
                        )
                        if (
                            required_state_map.get((EventTypes.Member, local_user2_id))
                            is not None
                        ):
                            break
                elif membership_action == Membership.LEAVE:
                    if remote_room_id not in response_body["rooms"].keys():
                        break
                else:
                    raise NotImplementedError(
                        "This test does not support this membership action yet"
                    )

                # Prevent tight-looping to allow the `test_timeout` to work
                time.sleep(0.1)

        # Make sure that we let hs2 know about the new membership event
        self.assertIncludes(
            collected_pdus_from_hs1_federation_send,
            {expected_pdu_event_id},
            exact=True,
            message="Expected to find the event ID of the user2 membership to be sent from hs1 over federation to hs2",
        )
