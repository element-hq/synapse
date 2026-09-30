#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Tests that out-of-band membership events (which are outliers, and so never
enter the current state delta stream) are reflected in `state_after` (MSC4222)
on `/sync`."""

import time
import urllib.parse
from http import HTTPStatus
from typing import Callable, TypeVar
from unittest.mock import Mock

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.room_versions import RoomVersions
from synapse.events.utils import strip_event
from synapse.http.matrixfederationclient import ByteParser
from synapse.http.types import QueryParams
from synapse.rest import admin
from synapse.rest.client import knock, login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.event_builders import make_test_event
from tests.utils import test_timeout

T = TypeVar("T")


class OutOfBandMembershipStateAfterTestCase(unittest.FederatingHomeserverTestCase):
    """
    Rescinding a knock made on a remote room generates a local out-of-band
    leave event. Such events are outliers: they never enter the current state
    delta stream, and the archived-room branch of sync deliberately returns no
    state for them. Clients using MSC4222's `state_after` do not apply
    timeline events to state, so the leave must be reflected in `state_after`
    or such clients never learn of the membership change.
    """

    servlets = [
        admin.register_servlets,
        knock.register_servlets,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        conf = super().default_config()
        conf["experimental_features"] = {"msc4222_enabled": True}
        return conf

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_http_client = Mock()
        return self.setup_test_homeserver(
            federation_http_client=self.federation_http_client
        )

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.store = self.hs.get_datastores().main

    def _knock_on_remote_room(self, user_id: str, user_tok: str, room_id: str) -> None:
        """Have a local user knock on a (mocked) remote room via the remote
        server."""

        room_creator_user_id = f"@remote-user:{self.OTHER_SERVER_NAME}"
        room_version = RoomVersions.V10

        room_create_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": room_id,
                    "sender": room_creator_user_id,
                    "depth": 1,
                    "origin_server_ts": 1,
                    "type": EventTypes.Create,
                    "state_key": "",
                    "content": {
                        EventContentFields.ROOM_CREATOR: room_creator_user_id,
                        EventContentFields.ROOM_VERSION: room_version.identifier,
                    },
                    "auth_events": [],
                    "prev_events": [],
                }
            ),
            room_version=room_version,
        )

        # A knock event template, as the remote server would return from
        # /make_knock.
        knock_event_template = {
            "room_id": room_id,
            "sender": user_id,
            "depth": 2,
            "origin_server_ts": 2,
            "type": EventTypes.Member,
            "state_key": user_id,
            "content": {"membership": Membership.KNOCK},
            "auth_events": [room_create_event.event_id],
            "prev_events": [room_create_event.event_id],
        }

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
                f"/_matrix/federation/v1/make_knock/{urllib.parse.quote_plus(room_id)}/{urllib.parse.quote_plus(user_id)}"
            ):
                return {
                    "event": knock_event_template,
                    "room_version": room_version.identifier,
                }

            raise NotImplementedError(
                f"Unmocked `get_json(...)` endpoint: {destination}{path}"
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
                f"/_matrix/federation/v1/send_knock/{urllib.parse.quote_plus(room_id)}/"
            ):
                return {"knock_room_state": [strip_event(room_create_event)]}

            raise NotImplementedError(
                f"Unmocked `put_json(...)` endpoint: {destination}{path} with body {data}"
            )

        self.federation_http_client.put_json.side_effect = put_json

        # The user knocks on the remote room
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/knock/{urllib.parse.quote_plus(room_id)}?server_name={self.OTHER_SERVER_NAME}",
            {},
            access_token=user_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        # Reset the mocks now that the knock has completed
        self.federation_http_client.get_json.side_effect = None
        self.federation_http_client.put_json.side_effect = None

        # Our local membership should now be an (out-of-band) knock
        membership, _ = self.get_success(
            self.store.get_local_current_membership_for_user_in_room(user_id, room_id)
        )
        self.assertEqual(membership, Membership.KNOCK)

    def test_rescinded_knock_in_state_after(self) -> None:
        """The out-of-band leave rescinding a knock is reflected in
        `state_after` (MSC4222) on sync."""
        user_id = self.register_user("user1", "pass")
        user_tok = self.login(user_id, "pass")
        remote_room_id = f"!remote-room:{self.OTHER_SERVER_NAME}"

        self._knock_on_remote_room(user_id, user_tok, remote_room_id)

        # Sync up to just after the knock.
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/sync?timeout=0&org.matrix.msc4222.use_state_after=true",
            access_token=user_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        since_token = channel.json_body["next_batch"]

        # Rescind the knock. The room is remote, so this generates a local
        # out-of-band leave event.
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{urllib.parse.quote_plus(remote_room_id)}/leave",
            {},
            access_token=user_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        with test_timeout(3, "Rescission of the knock was not processed"):
            while True:
                membership, _ = self.get_success(
                    self.store.get_local_current_membership_for_user_in_room(
                        user_id, remote_room_id
                    )
                )
                if membership == Membership.LEAVE:
                    break
                time.sleep(0.1)

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/sync?timeout=0&org.matrix.msc4222.use_state_after=true&since={since_token}",
            access_token=user_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        leave_room = channel.json_body["rooms"]["leave"][remote_room_id]
        state_after_events = leave_room["org.matrix.msc4222.state_after"]["events"]
        self.assertTrue(
            any(
                e["type"] == EventTypes.Member
                and e["state_key"] == user_id
                and e["content"]["membership"] == Membership.LEAVE
                for e in state_after_events
            ),
            leave_room,
        )
