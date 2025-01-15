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

import time
import logging
import urllib.parse
from http import HTTPStatus
from typing import Callable, Optional, Tuple, TypeVar, Union
from unittest.mock import Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.room_versions import RoomVersions
from synapse.events import make_event_from_dict, EventBase
from synapse.events.utils import strip_event
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
from synapse.util import Clock

from tests import unittest
from tests.utils import test_timeout

logger = logging.getLogger(__name__)


def required_state_json_to_state_map(required_state: JsonDict) -> StateMap[EventBase]:
    state_map: MutableStateMap[EventBase] = {}

    for state_event_dict in required_state:
        state_map[(state_event_dict["type"], state_event_dict["state_key"])] = (
            make_event_from_dict(state_event_dict)
        )

    return state_map


class OutOfBandMembershipTests(unittest.FederatingHomeserverTestCase):
    """
    Tests to make sure that interactions with out-of-band membership (outliers) works as
    expected.

     - invites received over federation, before we join the room
     - *rejections* for said invites
     - knock events for rooms that we would like to join but have not yet joined.

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
                response_body, _ = self.do_sync(sync_body, tok=user1_tok)
                if (
                    room_id in response_body["rooms"].keys()
                    # If they have `invite_state` for the room, they are invited
                    and len(response_body["rooms"][room_id].get("invite_state", [])) > 0
                ):
                    break

                # Prevent tight-looping to allow the `test_timeout` to work
                time.sleep(0.1)

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

        # Sync until the local user1 can see that they are now joined to the room
        with test_timeout(
            3,
            "Unable to find user1's join event in the room",
        ):
            while True:
                response_body, _ = self.do_sync(sync_body, tok=user1_tok)
                logger.info("response_body %s", response_body)
                if room_id in response_body["rooms"].keys():
                    required_state_map = required_state_json_to_state_map(
                        response_body["rooms"][room_id]["required_state"]
                    )
                    if (
                        required_state_map.get((EventTypes.Member, user1_id))
                        is not None
                    ):
                        break

                # Prevent tight-looping to allow the `test_timeout` to work
                time.sleep(0.1)
