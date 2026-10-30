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
from synapse.events import EventBase
from synapse.events.utils import strip_event
from synapse.federation.transport.client import SendJoinResponse
from synapse.http.matrixfederationclient import (
    ByteParser,
)
from synapse.http.types import QueryParams
from synapse.rest import admin
from synapse.rest.client import knock, login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, MutableStateMap, StateMap
from synapse.types.handlers.sliding_sync import (
    StateValues,
)
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.event_builders import make_test_event, make_test_pdu_event
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

            state_map[(event_type, event_state_key)] = make_test_event(state_event_dict)
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
        room_version = self.hs.config.server.default_room_version

        room_create_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "sender": room_creator_user_id,
                    "depth": 1,
                    "origin_server_ts": 1,
                    "type": EventTypes.Create,
                    "state_key": "",
                    "content": {
                        EventContentFields.ROOM_VERSION: room_version.identifier,
                    },
                    "auth_events": [],
                    "prev_events": [],
                }
            ),
            room_version=room_version,
        )

        remote_room_id = room_create_event.room_id

        creator_membership_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": remote_room_id,
                    "sender": room_creator_user_id,
                    "depth": 2,
                    "origin_server_ts": 2,
                    "type": EventTypes.Member,
                    "state_key": room_creator_user_id,
                    "content": {"membership": Membership.JOIN},
                    "auth_events": [],
                    "prev_events": [room_create_event.event_id],
                }
            ),
            room_version=room_version,
        )

        # From the remote homeserver, invite user1 on the local homserver
        user1_invite_membership_event = make_test_event(
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

        user1_join_membership_event_template = make_test_event(
            {
                "room_id": remote_room_id,
                "sender": local_user1_id,
                "depth": 4,
                "origin_server_ts": 4,
                "type": EventTypes.Member,
                "state_key": local_user1_id,
                "content": {"membership": Membership.JOIN},
                "auth_events": [
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
                user1_join_membership_event_signed = make_test_event(
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
                    event = make_test_pdu_event(pdu, room_version)
                    collected_pdus_from_hs1_federation_send.add(event.event_id)

                # Just acknowledge everything hs1 is trying to send hs2
                return {
                    make_test_pdu_event(pdu, room_version).event_id: {}
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
                    event = make_test_pdu_event(pdu, room_version)
                    collected_pdus_from_hs1_federation_send.add(event.event_id)

                # Just acknowledge everything hs1 is trying to send hs2
                return {
                    make_test_pdu_event(pdu, room_version).event_id: {}
                    for pdu in data.get("pdus", [])
                }

            raise NotImplementedError(
                "We have not mocked a response for `put_json(...)` for the following endpoint yet: "
                + f"{destination}{path} with the following body data: {data}"
            )

        self.federation_http_client.put_json.side_effect = put_json

        # From the remote homeserver, invite user2 on the local homserver
        user2_invite_membership_event = make_test_event(
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


class OutOfBandMembershipStateAfterTests(unittest.FederatingHomeserverTestCase):
    """
    Out-of-band leaves (rejecting a remote invite, rescinding a knock on a remote
    room) are outliers, so they never enter the current state delta stream. Check
    that MSC4222 `state_after` reports them anyway.
    """

    servlets = [
        admin.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_http_client = Mock()
        return self.setup_test_homeserver(
            federation_http_client=self.federation_http_client
        )

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.store = self.hs.get_datastores().main

        self.local_user1_id = self.register_user("user1", "pass")
        self.local_user1_tok = self.login(self.local_user1_id, "pass")

        self.remote_user_id = f"@remote-user:{self.OTHER_SERVER_NAME}"
        self.remote_room_id = f"!remote-room:{self.OTHER_SERVER_NAME}"
        self.room_version = RoomVersions.V11

        self.room_create_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.remote_room_id,
                    "sender": self.remote_user_id,
                    "depth": 1,
                    "origin_server_ts": 1,
                    "type": EventTypes.Create,
                    "state_key": "",
                    "content": {
                        EventContentFields.ROOM_VERSION: self.room_version.identifier,
                    },
                    "auth_events": [],
                    "prev_events": [],
                }
            ),
            room_version=self.room_version,
        )

        self.creator_membership_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.remote_room_id,
                    "sender": self.remote_user_id,
                    "depth": 2,
                    "origin_server_ts": 2,
                    "type": EventTypes.Member,
                    "state_key": self.remote_user_id,
                    "content": {"membership": Membership.JOIN},
                    "auth_events": [self.room_create_event.event_id],
                    "prev_events": [self.room_create_event.event_id],
                }
            ),
            room_version=self.room_version,
        )

    def sync(self, since: str | None = None) -> JsonDict:
        """Do a `/sync` with MSC4222 `state_after` enabled.

        Asserts the request was successful.

        Args:
            since: Optional since token

        Returns:
            The response body.
        """
        sync_path = "/_matrix/client/v3/sync?timeout=0&use_state_after=true"
        if since:
            sync_path += f"&since={since}"

        channel = self.make_request("GET", sync_path, access_token=self.local_user1_tok)
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        return channel.json_body

    def assert_leave_in_state_after(self, since: str) -> None:
        """Assert that user1's leave is the whole of `state_after` for the remote
        room in an incremental sync from `since`."""
        response_body = self.sync(since=since)

        leave_room = response_body["rooms"]["leave"][self.remote_room_id]
        state_after_events = leave_room["state_after"]["events"]
        self.assertIncludes(
            {
                (event["type"], event["state_key"], event["content"]["membership"])
                for event in state_after_events
            },
            {(EventTypes.Member, self.local_user1_id, Membership.LEAVE)},
            exact=True,
            message=str(leave_room),
        )

    def _invite_local_user_to_remote_room(self) -> EventBase:
        """Have the remote server invite user1, giving them an out-of-band invite
        membership in a room our server is not in.

        Returns:
            The invite event.
        """
        invite_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.remote_room_id,
                    "sender": self.remote_user_id,
                    "depth": 3,
                    "origin_server_ts": 3,
                    "type": EventTypes.Member,
                    "state_key": self.local_user1_id,
                    "content": {"membership": Membership.INVITE},
                    "auth_events": [
                        self.room_create_event.event_id,
                        self.creator_membership_event.event_id,
                    ],
                    "prev_events": [self.creator_membership_event.event_id],
                }
            ),
            room_version=self.room_version,
        )

        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/invite/{self.remote_room_id}/{invite_event.event_id}",
            content={
                "event": invite_event.get_dict(),
                "invite_room_state": [strip_event(self.room_create_event)],
                "room_version": self.room_version.identifier,
            },
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        return invite_event

    def _knock_local_user_on_remote_room(self) -> None:
        """Have user1 knock on the remote room through the remote server, giving
        them an out-of-band knock membership."""
        knock_event_template = {
            "room_id": self.remote_room_id,
            "sender": self.local_user1_id,
            "depth": 3,
            "origin_server_ts": 3,
            "type": EventTypes.Member,
            "state_key": self.local_user1_id,
            "content": {"membership": Membership.KNOCK},
            "auth_events": [self.room_create_event.event_id],
            "prev_events": [self.creator_membership_event.event_id],
        }

        T = TypeVar("T")

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
            if path.startswith(
                f"/_matrix/federation/v1/make_knock/{urllib.parse.quote_plus(self.remote_room_id)}/{urllib.parse.quote_plus(self.local_user1_id)}"
            ):
                return {
                    "event": knock_event_template,
                    "room_version": self.room_version.identifier,
                }

            raise NotImplementedError(
                "We have not mocked a response for `get_json(...)` for the following endpoint yet: "
                + f"{destination}{path}"
            )

        self.federation_http_client.get_json.side_effect = get_json

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
            if path.startswith(
                f"/_matrix/federation/v1/send_knock/{urllib.parse.quote_plus(self.remote_room_id)}/"
            ):
                return {"knock_room_state": [strip_event(self.room_create_event)]}

            raise NotImplementedError(
                "We have not mocked a response for `put_json(...)` for the following endpoint yet: "
                + f"{destination}{path} with the following body data: {data}"
            )

        self.federation_http_client.put_json.side_effect = put_json

        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/knock/{urllib.parse.quote_plus(self.remote_room_id)}?server_name={self.OTHER_SERVER_NAME}",
            {},
            access_token=self.local_user1_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        # Reset the mocks now that the knock has completed
        self.federation_http_client.get_json.side_effect = None
        self.federation_http_client.put_json.side_effect = None

        membership, _ = self.get_success(
            self.store.get_local_current_membership_for_user_in_room(
                self.local_user1_id, self.remote_room_id
            )
        )
        self.assertEqual(membership, Membership.KNOCK)

    def test_rejected_invite_in_state_after(self) -> None:
        """Rejecting a remote invite through the inviting server is reflected in
        `state_after` (MSC4222) on sync."""
        invite_event = self._invite_local_user_to_remote_room()
        since_token = self.sync()["next_batch"]

        leave_event_template = make_test_event(
            {
                "room_id": self.remote_room_id,
                "sender": self.local_user1_id,
                "depth": 4,
                "origin_server_ts": 4,
                "type": EventTypes.Member,
                "state_key": self.local_user1_id,
                "content": {"membership": Membership.LEAVE},
                "auth_events": [
                    self.room_create_event.event_id,
                    invite_event.event_id,
                ],
                "prev_events": [invite_event.event_id],
            },
            room_version=self.room_version,
        )

        T = TypeVar("T")

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
                == f"/_matrix/federation/v1/make_leave/{urllib.parse.quote_plus(self.remote_room_id)}/{urllib.parse.quote_plus(self.local_user1_id)}"
            ):
                return {
                    "event": leave_event_template.get_pdu_json(),
                    "room_version": self.room_version.identifier,
                }

            raise NotImplementedError(
                "We have not mocked a response for `get_json(...)` for the following endpoint yet: "
                + f"{destination}{path}"
            )

        self.federation_http_client.get_json.side_effect = get_json

        # Leave events that hs1 sent to hs2 via /send_leave
        collected_send_leave_event_ids: set[str] = set()

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
            if (
                path.startswith(
                    f"/_matrix/federation/v2/send_leave/{urllib.parse.quote_plus(self.remote_room_id)}/"
                )
                and data is not None
            ):
                collected_send_leave_event_ids.add(
                    make_test_pdu_event(data, self.room_version).event_id
                )
                return {}

            raise NotImplementedError(
                "We have not mocked a response for `put_json(...)` for the following endpoint yet: "
                + f"{destination}{path} with the following body data: {data}"
            )

        self.federation_http_client.put_json.side_effect = put_json

        leave_event = self.helper.leave(
            self.remote_room_id, self.local_user1_id, tok=self.local_user1_tok
        )

        # If the rejection had not reached the inviting server, `remote_reject_invite`
        # would have fallen back to generating the leave locally, which is the path
        # `test_rescinded_knock_in_state_after` covers.
        self.assertIncludes(
            collected_send_leave_event_ids,
            {leave_event["event_id"]},
            exact=True,
            message="Expected user1's leave to be sent to the inviting server",
        )

        self.assert_leave_in_state_after(since_token)

    def test_retracted_invite_in_state_after(self) -> None:
        """An invite retracted by the remote server is reflected in `state_after`
        (MSC4222) on sync."""
        invite_event = self._invite_local_user_to_remote_room()
        since_token = self.sync()["next_batch"]

        retraction_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.remote_room_id,
                    "sender": self.remote_user_id,
                    "depth": 4,
                    "origin_server_ts": 4,
                    "type": EventTypes.Member,
                    "state_key": self.local_user1_id,
                    "content": {"membership": Membership.LEAVE},
                    "auth_events": [
                        self.room_create_event.event_id,
                        self.creator_membership_event.event_id,
                        invite_event.event_id,
                    ],
                    "prev_events": [invite_event.event_id],
                }
            ),
            room_version=self.room_version,
        )

        channel = self.make_signed_federation_request(
            "PUT",
            "/_matrix/federation/v1/send/1000",
            content={
                "origin": self.OTHER_SERVER_NAME,
                "origin_server_ts": 4,
                "pdus": [retraction_event.get_pdu_json()],
                "edus": [],
            },
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        membership, _ = self.get_success(
            self.store.get_local_current_membership_for_user_in_room(
                self.local_user1_id, self.remote_room_id
            )
        )
        self.assertEqual(membership, Membership.LEAVE)

        self.assert_leave_in_state_after(since_token)

    def test_rescinded_knock_in_state_after(self) -> None:
        """The out-of-band leave rescinding a knock is reflected in `state_after`
        (MSC4222) on sync."""
        self._knock_local_user_on_remote_room()
        since_token = self.sync()["next_batch"]

        # The room is remote, so rescinding the knock generates a local
        # out-of-band leave event.
        self.helper.leave(
            self.remote_room_id, self.local_user1_id, tok=self.local_user1_tok
        )

        membership, _ = self.get_success(
            self.store.get_local_current_membership_for_user_in_room(
                self.local_user1_id, self.remote_room_id
            )
        )
        self.assertEqual(membership, Membership.LEAVE)

        self.assert_leave_in_state_after(since_token)
