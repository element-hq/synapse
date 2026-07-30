#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

import logging
import urllib.parse
from http import HTTPStatus
from typing import Callable, TypeVar
from unittest.mock import Mock

import attr

from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersion
from synapse.config.server import DEFAULT_ROOM_VERSION
from synapse.events import EventBase
from synapse.events.utils import strip_event
from synapse.federation.transport.client import SendJoinResponse
from synapse.http.matrixfederationclient import ByteParser
from synapse.http.types import QueryParams
from synapse.types import JsonDict

from tests.test_utils.event_builders import make_test_event, make_test_pdu_event
from tests.unittest import FederatingHomeserverTestCase

logger = logging.getLogger(__name__)


@attr.s(slots=True, auto_attribs=True)
class RemoteStateEvent:
    """
    A state event to insert into the remote room `/send_join` response.
    """

    type: str
    state_key: str
    content: JsonDict
    # Defaults to the remote room creator.
    sender: str | None = None


class RemoteJoinHelper:
    """
    Helps a `FederatingHomeserverTestCase` join a remote (non-resident) room
    over federation.

    Spiritually inspired by `test_federation_out_of_band_membership.py`
    but in a more reusable form.

    Constructor Args:
        test_case: the current `FederatingHomeserverTestCase`
        federation_http_client: Mocked form of the main test homeserver's federation HTTP client;
            should have method slots for `get_json` and `put_json`.
        remote_creator_user_id: User ID of the remote user creating the room
        room_version: Desired room version
        create_content: Desired `content` of the `m.room.create` event
        state_events: Extra state events to create in the mock room
            (They will be made available in the `/send_join` response)

    Usage:

        helper = RemoteJoinHelper(
            self,
            create_content={"predecessor": {"room_id": old_room_id, ...}},
        )
        # helper.room_id is available now; you can e.g. point a tombstone at it.
        helper.join(local_user_id, local_user_tok)
    """

    room_id: str
    """
    The room ID of the mock remote room that will be joined.
    """

    def __init__(
        self,
        test_case: FederatingHomeserverTestCase,
        federation_http_client: Mock,
        *,
        remote_creator_user_id: str | None = None,
        room_version: RoomVersion = KNOWN_ROOM_VERSIONS[DEFAULT_ROOM_VERSION],
        create_content: JsonDict | None = None,
        state_events: list[RemoteStateEvent] | None = None,
    ) -> None:
        if remote_creator_user_id is None:
            remote_creator_user_id = f"@remote-user:{test_case.OTHER_SERVER_NAME}"

        self._test_case = test_case
        self._federation_http_client = federation_http_client
        self._remote_creator_user_id = remote_creator_user_id
        self._room_version = room_version

        # 1. Create the room creation event
        create_content_full: JsonDict = {
            EventContentFields.ROOM_CREATOR: remote_creator_user_id,
            EventContentFields.ROOM_VERSION: room_version.identifier,
        }
        if create_content is not None:
            create_content_full.update(create_content)

        create_event_dict: JsonDict = {
            "sender": remote_creator_user_id,
            "depth": 1,
            "origin_server_ts": 1,
            "type": EventTypes.Create,
            "state_key": "",
            "content": create_content_full,
            "auth_events": [],
            "prev_events": [],
        }
        if not room_version.msc4291_room_ids_as_hashes:
            # For room versions that _don't_ derive the room ID from the content,
            # we need to set our own.
            # We could consider exposing a parameter to allow varying the localpart
            # (and perturb the create event for hashes-as-room-ID rooms)
            create_event_dict["room_id"] = f"!remote-room:{test_case.OTHER_SERVER_NAME}"

        room_create_event = make_test_event(
            test_case.add_hashes_and_signatures_from_other_server(
                create_event_dict,
                room_version=room_version,
            ),
            room_version=room_version,
        )

        self.room_id = room_create_event.room_id

        # 2. Create the room creator's membership event
        creator_membership_event = make_test_event(
            test_case.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.room_id,
                    "sender": remote_creator_user_id,
                    "depth": 2,
                    "origin_server_ts": 2,
                    "type": EventTypes.Member,
                    "state_key": remote_creator_user_id,
                    "content": {"membership": Membership.JOIN},
                    "auth_events": [room_create_event.event_id]
                    if not room_version.msc4291_room_ids_as_hashes
                    else [],
                    "prev_events": [room_create_event.event_id],
                },
                room_version=room_version,
            ),
            room_version=room_version,
        )

        # 3. Create requested extra state events (in a linear chain from the membership)
        extra_state_events: list[EventBase] = []
        prev_event = creator_membership_event
        depth = 3
        for spec in state_events or []:
            sender = spec.sender or remote_creator_user_id
            event = make_test_event(
                test_case.add_hashes_and_signatures_from_other_server(
                    {
                        "room_id": self.room_id,
                        "sender": sender,
                        "depth": depth,
                        "origin_server_ts": depth,
                        "type": spec.type,
                        "state_key": spec.state_key,
                        "content": spec.content,
                        "auth_events": [
                            room_create_event.event_id,
                            creator_membership_event.event_id,
                        ]
                        if not room_version.msc4291_room_ids_as_hashes
                        else [creator_membership_event.event_id],
                        "prev_events": [prev_event.event_id],
                    },
                    room_version=room_version,
                ),
                room_version=room_version,
            )
            extra_state_events.append(event)
            prev_event = event
            depth += 1

        self._room_create_event = room_create_event
        self._creator_membership_event = creator_membership_event
        self._extra_state_events = extra_state_events

    def join(self, local_user_id: str, local_user_tok: str) -> None:
        """
        Invite `local_user_id` and perform the federation join dance.
        """
        remote_room_id = self.room_id
        room_version = self._room_version

        room_create_event = self._room_create_event
        creator_membership_event = self._creator_membership_event
        extra_events = self._extra_state_events

        # 1. Create an invite event and make it appear on the 'real' homeserver
        depth = 3 + len(extra_events)

        invite_membership_event = make_test_event(
            self._test_case.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": remote_room_id,
                    "sender": self._remote_creator_user_id,
                    "depth": depth,
                    "origin_server_ts": depth,
                    "type": EventTypes.Member,
                    "state_key": local_user_id,
                    "content": {"membership": Membership.INVITE},
                    "auth_events": [
                        room_create_event.event_id,
                        creator_membership_event.event_id,
                    ]
                    if not room_version.msc4291_room_ids_as_hashes
                    else [creator_membership_event.event_id],
                    "prev_events": [
                        extra_events[-1].event_id
                        if extra_events
                        else creator_membership_event.event_id
                    ],
                },
                room_version=room_version,
            ),
            room_version=room_version,
        )

        channel = self._test_case.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/invite/{remote_room_id}/{invite_membership_event.event_id}",
            content={
                "event": invite_membership_event.get_dict(),
                "invite_room_state": [
                    strip_event(room_create_event),
                ],
                "room_version": room_version.identifier,
            },
        )
        assert channel.code == HTTPStatus.OK, channel.json_body

        # 2. Mock `/make_join` and `/send_join`.
        # Start by creating a join membership event.
        join_membership_event_template = make_test_event(
            {
                "room_id": remote_room_id,
                "sender": local_user_id,
                "depth": depth + 1,
                "origin_server_ts": depth + 1,
                "type": EventTypes.Member,
                "state_key": local_user_id,
                "content": {"membership": Membership.JOIN},
                "auth_events": [
                    room_create_event.event_id,
                    invite_membership_event.event_id,
                ]
                if not room_version.msc4291_room_ids_as_hashes
                else [invite_membership_event.event_id],
                "prev_events": [invite_membership_event.event_id],
            },
            room_version=room_version,
        )

        T = TypeVar("T")

        async def _get_json(
            destination: str,
            path: str,
            args: QueryParams | None = None,
            retry_on_dns_fail: bool = True,
            timeout: int | None = None,
            ignore_backoff: bool = False,
            try_trailing_slash_on_400: bool = False,
            parser: ByteParser[T] | None = None,
        ) -> JsonDict | T:
            make_join_path = (
                f"/_matrix/federation/v1/make_join/"
                f"{urllib.parse.quote_plus(remote_room_id)}/{urllib.parse.quote_plus(local_user_id)}"
            )
            if path == make_join_path:
                return {
                    "event": join_membership_event_template.get_pdu_json(),
                    "room_version": room_version.identifier,
                }
            raise NotImplementedError(
                "We have not mocked a response for `get_json(...)` for the following endpoint yet: "
                + f"{destination}{path}"
            )

        self._federation_http_client.get_json.side_effect = _get_json

        send_join_state = [
            room_create_event,
            creator_membership_event,
            *extra_events,
            invite_membership_event,
        ]

        async def _put_json(
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
                and data.get("state_key") == local_user_id
                and parser is not None
            ):
                # As the remote server, sign the join event before returning it.
                join_membership_event_signed = make_test_event(
                    self._test_case.add_hashes_and_signatures_from_other_server(
                        data, room_version=room_version
                    ),
                    room_version=room_version,
                )
                return SendJoinResponse(
                    auth_events=[
                        room_create_event,
                        invite_membership_event,
                    ],
                    state=send_join_state,
                    event_dict=join_membership_event_signed.get_pdu_json(),
                    event=join_membership_event_signed,
                    members_omitted=False,
                    servers_in_room=[
                        self._test_case.OTHER_SERVER_NAME,
                    ],
                )

            if path.startswith("/_matrix/federation/v1/send/") and data is not None:
                # Just acknowledge everything.
                return {
                    make_test_pdu_event(pdu, room_version).event_id: {}
                    for pdu in data.get("pdus", [])
                }

            raise NotImplementedError(
                "We have not mocked a response for `put_json(...)` for the following endpoint yet: "
                + f"{destination}{path} with the following body data: {data}"
            )

        self._federation_http_client.put_json.side_effect = _put_json

        # 3. Issue the client-server API request to join the room
        self._test_case.helper.join(remote_room_id, local_user_id, tok=local_user_tok)

        # 4. Reset mocks
        self._federation_http_client.get_json.side_effect = None
        self._federation_http_client.put_json.side_effect = None
