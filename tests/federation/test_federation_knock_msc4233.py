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

"""Tests for MSC4233: remembering which server a user knocked through, so
that knocks can be rescinded and denied over federation."""

import logging
import time
import urllib.parse
from http import HTTPStatus
from typing import Any, Callable, TypeVar
from unittest.mock import Mock

import attr

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.events import EventBase, builder
from synapse.events.snapshot import EventContext
from synapse.events.utils import strip_event
from synapse.http.matrixfederationclient import ByteParser
from synapse.http.types import QueryParams
from synapse.rest import admin
from synapse.rest.client import knock, login, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.event_builders import make_test_event
from tests.utils import test_timeout

logger = logging.getLogger(__name__)

T = TypeVar("T")


@attr.s(slots=True, auto_attribs=True)
class RemoteRoomKnockResult:
    remote_room_id: str
    room_version: RoomVersion
    remote_room_creator_user_id: str
    local_user1_id: str
    local_user1_tok: str
    room_create_event: EventBase
    knock_event_id: str


class KnockViaServerTestCase(unittest.FederatingHomeserverTestCase):
    """
    Tests for the knocking server's side of MSC4233:

     - the server that fulfilled our /send_knock is remembered
     - a rescission of the knock is routed through that server
     - a denial of the knock sent to us by that server is accepted as an
       out-of-band retraction of the knock
    """

    servlets = [
        admin.register_servlets,
        knock.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        conf = super().default_config()
        conf["experimental_features"] = {"msc4233_enabled": True}
        return conf

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_http_client = Mock()
        return self.setup_test_homeserver(
            federation_http_client=self.federation_http_client
        )

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.store = self.hs.get_datastores().main

    def _knock_on_remote_room(self) -> RemoteRoomKnockResult:
        """Have a local user knock on a (mocked) remote room via the remote
        server, and return the details of the knock."""

        local_user1_id = self.register_user("user1", "pass")
        local_user1_tok = self.login(local_user1_id, "pass")

        room_creator_user_id = f"@remote-user:{self.OTHER_SERVER_NAME}"
        remote_room_id = f"!remote-room:{self.OTHER_SERVER_NAME}"
        room_version = RoomVersions.V10

        room_create_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": remote_room_id,
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
            "room_id": remote_room_id,
            "sender": local_user1_id,
            "depth": 2,
            "origin_server_ts": 2,
            "type": EventTypes.Member,
            "state_key": local_user1_id,
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
                f"/_matrix/federation/v1/make_knock/{urllib.parse.quote_plus(remote_room_id)}/{urllib.parse.quote_plus(local_user1_id)}"
            ):
                return {
                    "event": knock_event_template,
                    "room_version": room_version.identifier,
                }

            raise NotImplementedError(
                f"Unmocked `get_json(...)` endpoint: {destination}{path}"
            )

        self.federation_http_client.get_json.side_effect = get_json

        # Record which server(s) we called /send_knock on.
        send_knock_destinations: list[str] = []

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
                f"/_matrix/federation/v1/send_knock/{urllib.parse.quote_plus(remote_room_id)}/"
            ):
                send_knock_destinations.append(destination)
                return {"knock_room_state": [strip_event(room_create_event)]}

            raise NotImplementedError(
                f"Unmocked `put_json(...)` endpoint: {destination}{path} with body {data}"
            )

        self.federation_http_client.put_json.side_effect = put_json

        # User1 knocks on the remote room
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/knock/{urllib.parse.quote_plus(remote_room_id)}?server_name={self.OTHER_SERVER_NAME}",
            {},
            access_token=local_user1_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        # Reset the mocks now that the knock has completed
        self.federation_http_client.get_json.side_effect = None
        self.federation_http_client.put_json.side_effect = None

        self.assertEqual(send_knock_destinations, [self.OTHER_SERVER_NAME])

        # Our local membership should now be an (out-of-band) knock
        membership, knock_event_id = self.get_success(
            self.store.get_local_current_membership_for_user_in_room(
                local_user1_id, remote_room_id
            )
        )
        self.assertEqual(membership, Membership.KNOCK)
        assert knock_event_id is not None

        return RemoteRoomKnockResult(
            remote_room_id=remote_room_id,
            room_version=room_version,
            remote_room_creator_user_id=room_creator_user_id,
            local_user1_id=local_user1_id,
            local_user1_tok=local_user1_tok,
            room_create_event=room_create_event,
            knock_event_id=knock_event_id,
        )

    def test_knock_remembers_via_server(self) -> None:
        """The server that fulfilled our /send_knock is recorded."""
        knock_result = self._knock_on_remote_room()

        via_server = self.get_success(
            self.store.get_local_knock_via_server(
                knock_result.local_user1_id, knock_result.remote_room_id
            )
        )
        self.assertEqual(via_server, self.OTHER_SERVER_NAME)

    def test_rescind_knock_routed_through_via_server(self) -> None:
        """Rescinding a knock does the make_leave/send_leave dance through the
        server the knock was fulfilled through."""
        knock_result = self._knock_on_remote_room()
        remote_room_id = knock_result.remote_room_id
        local_user1_id = knock_result.local_user1_id

        # A leave event template, as the remote server would return from
        # /make_leave.
        leave_event_template = {
            "room_id": remote_room_id,
            "sender": local_user1_id,
            "depth": 3,
            "origin_server_ts": 3,
            "type": EventTypes.Member,
            "state_key": local_user1_id,
            "content": {"membership": Membership.LEAVE},
            "auth_events": [
                knock_result.room_create_event.event_id,
                knock_result.knock_event_id,
            ],
            "prev_events": [knock_result.knock_event_id],
        }

        make_leave_destinations: list[str] = []
        send_leave_destinations: list[str] = []

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
                f"/_matrix/federation/v1/make_leave/{urllib.parse.quote_plus(remote_room_id)}/{urllib.parse.quote_plus(local_user1_id)}"
            ):
                make_leave_destinations.append(destination)
                return {
                    "event": leave_event_template,
                    "room_version": knock_result.room_version.identifier,
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
                f"/_matrix/federation/v2/send_leave/{urllib.parse.quote_plus(remote_room_id)}/"
            ):
                send_leave_destinations.append(destination)
                return {}

            raise NotImplementedError(
                f"Unmocked `put_json(...)` endpoint: {destination}{path} with body {data}"
            )

        self.federation_http_client.put_json.side_effect = put_json

        # User1 rescinds the knock
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{urllib.parse.quote_plus(remote_room_id)}/leave",
            {},
            access_token=knock_result.local_user1_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        # The rescission should have been routed through the server we
        # knocked through.
        self.assertEqual(make_leave_destinations, [self.OTHER_SERVER_NAME])
        self.assertEqual(send_leave_destinations, [self.OTHER_SERVER_NAME])

        # And our local membership should now be leave.
        membership, _ = self.get_success(
            self.store.get_local_current_membership_for_user_in_room(
                local_user1_id, remote_room_id
            )
        )
        self.assertEqual(membership, Membership.LEAVE)

    def test_rescind_knock_falls_back_to_local_leave(self) -> None:
        """If the server we knocked through is unreachable, the knock is still
        rescinded locally."""
        knock_result = self._knock_on_remote_room()

        async def get_json(*args: Any, **kwargs: Any) -> JsonDict:
            raise RuntimeError("server unreachable")

        self.federation_http_client.get_json.side_effect = get_json

        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{urllib.parse.quote_plus(knock_result.remote_room_id)}/leave",
            {},
            access_token=knock_result.local_user1_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        membership, _ = self.get_success(
            self.store.get_local_current_membership_for_user_in_room(
                knock_result.local_user1_id, knock_result.remote_room_id
            )
        )
        self.assertEqual(membership, Membership.LEAVE)

    def _send_denial_over_federation(
        self,
        knock_result: RemoteRoomKnockResult,
        auth_events: list[str],
        membership: str = Membership.LEAVE,
    ) -> None:
        """Send a leave (or ban) event denying the knock to our server, as the
        remote server we knocked through."""
        deny_event = make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": knock_result.remote_room_id,
                    "sender": knock_result.remote_room_creator_user_id,
                    "depth": 3,
                    "origin_server_ts": 3,
                    "type": EventTypes.Member,
                    "state_key": knock_result.local_user1_id,
                    "content": {"membership": membership},
                    "auth_events": auth_events,
                    "prev_events": [knock_result.knock_event_id],
                }
            ),
            room_version=knock_result.room_version,
        )

        channel = self.make_signed_federation_request(
            "PUT",
            "/_matrix/federation/v1/send/txn_deny_knock",
            {"pdus": [deny_event.get_pdu_json()]},
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

    def test_denied_knock_accepted_from_via_server(self) -> None:
        """A leave event for our knocked user, referencing the knock in its
        auth events and sent by the server we knocked through, retracts the
        knock."""
        knock_result = self._knock_on_remote_room()

        self._send_denial_over_federation(
            knock_result,
            auth_events=[
                knock_result.room_create_event.event_id,
                knock_result.knock_event_id,
            ],
        )

        # The knock should (eventually - the PDU is processed in the
        # background) be retracted.
        leave_event_id = None
        with test_timeout(3, "Denial of the knock was not processed"):
            while True:
                membership, leave_event_id = self.get_success(
                    self.store.get_local_current_membership_for_user_in_room(
                        knock_result.local_user1_id, knock_result.remote_room_id
                    )
                )
                if membership == Membership.LEAVE:
                    break
                time.sleep(0.1)

        # The stored leave should carry the replaced knock in `unsigned`, so
        # that clients can tell a denied knock from a plain kick.
        assert leave_event_id is not None
        leave_event = self.get_success(self.store.get_event(leave_event_id))
        self.assertEqual(
            leave_event.unsigned.get("prev_content", {}).get("membership"),
            Membership.KNOCK,
        )
        self.assertEqual(
            leave_event.unsigned.get("replaces_state"),
            knock_result.knock_event_id,
        )

    def test_banned_knock_accepted_from_via_server(self) -> None:
        """A ban event for our knocked user (deny + ban, per MSC2403 a ban has
        the same retracting effect), referencing the knock in its auth events
        and sent by the server we knocked through, retracts the knock and
        leaves us banned."""
        knock_result = self._knock_on_remote_room()

        self._send_denial_over_federation(
            knock_result,
            auth_events=[
                knock_result.room_create_event.event_id,
                knock_result.knock_event_id,
            ],
            membership=Membership.BAN,
        )

        ban_event_id = None
        with test_timeout(3, "Ban of the knocked user was not processed"):
            while True:
                membership, ban_event_id = self.get_success(
                    self.store.get_local_current_membership_for_user_in_room(
                        knock_result.local_user1_id, knock_result.remote_room_id
                    )
                )
                if membership == Membership.BAN:
                    break
                time.sleep(0.1)

        # The stored ban should carry the replaced knock in `unsigned`.
        assert ban_event_id is not None
        ban_event = self.get_success(self.store.get_event(ban_event_id))
        self.assertEqual(
            ban_event.unsigned.get("prev_content", {}).get("membership"),
            Membership.KNOCK,
        )

    def test_denied_knock_ignored_without_knock_in_auth_events(self) -> None:
        """A leave event for our knocked user which does not reference the
        knock in its auth events is ignored."""
        knock_result = self._knock_on_remote_room()

        self._send_denial_over_federation(
            knock_result,
            auth_events=[knock_result.room_create_event.event_id],
        )

        # Pump the reactor to let the (ignored) PDU get processed.
        self.pump(1)

        membership, _ = self.get_success(
            self.store.get_local_current_membership_for_user_in_room(
                knock_result.local_user1_id, knock_result.remote_room_id
            )
        )
        self.assertEqual(membership, Membership.KNOCK)


class DenyKnockFederationSendTestCase(unittest.FederatingHomeserverTestCase):
    """
    Tests for the resident server's side of MSC4233: a leave event denying a
    knock is sent to the knocking user's server, which is otherwise not in
    the room.
    """

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        conf = super().default_config()
        conf["experimental_features"] = {"msc4233_enabled": True}
        # Federation sending is disabled by default in the test environment
        # so we need to enable it like this.
        conf["federation_sender_instances"] = ["master"]
        return conf

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_http_client = Mock()
        return self.setup_test_homeserver(
            federation_http_client=self.federation_http_client
        )

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main

        # We're not going to be properly signing events as our remote
        # homeserver is fake, therefore disable event signature checks.
        async def approve_all_signature_checking(
            room_version: RoomVersion,
            pdu: EventBase,
            record_failure_callback: Any = None,
        ) -> EventBase:
            return pdu

        homeserver.get_federation_server()._check_sigs_and_hash = (  # type: ignore[method-assign]
            approve_all_signature_checking
        )

        async def _check_event_auth(
            origin: str | None, event: EventBase, context: EventContext
        ) -> None:
            pass

        homeserver.get_federation_event_handler()._check_event_auth = _check_event_auth  # type: ignore[method-assign]

        return super().prepare(reactor, clock, homeserver)

    def _setup_room_with_pending_federated_knock(
        self,
    ) -> tuple[str, str, str, dict[str, list[JsonDict]]]:
        """Create a knockable room, receive a federated knock into it, and
        start collecting outbound PDUs.

        Returns (room_id, admin_token, knocking_user_id, sent_pdus_by_destination).
        """
        self.register_user("u1", "pass")
        user_token = self.login("u1", "pass")

        fake_knocking_user_id = f"@user:{self.OTHER_SERVER_NAME}"

        # Create a knockable room
        room_id = self.helper.create_room_as(
            "u1",
            is_public=False,
            room_version=RoomVersions.V10.identifier,
            tok=user_token,
        )
        self.helper.send_state(
            room_id,
            EventTypes.JoinRules,
            {"join_rule": "knock"},
            tok=user_token,
        )

        # Collect the PDUs that our server sends out, per destination.
        sent_pdus_by_destination: dict[str, list[JsonDict]] = {}

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
                pdus = data.get("pdus", [])
                sent_pdus_by_destination.setdefault(destination, []).extend(pdus)
                return {}

            raise NotImplementedError(
                f"Unmocked `put_json(...)` endpoint: {destination}{path}"
            )

        self.federation_http_client.put_json.side_effect = put_json

        # The remote user knocks on the room, over federation.
        channel = self.make_signed_federation_request(
            "GET",
            "/_matrix/federation/v1/make_knock/%s/%s?ver=%s"
            % (
                room_id,
                fake_knocking_user_id,
                RoomVersions.V10.identifier,
            ),
        )
        self.assertEqual(200, channel.code, channel.result)
        knock_event = channel.json_body["event"]

        signed_knock_event = builder.create_local_event_from_event_dict(
            self.clock,
            self.hs.hostname,
            self.hs.signing_key,
            room_version=RoomVersions.V10,
            event_dict=knock_event,
        )
        channel = self.make_signed_federation_request(
            "PUT",
            "/_matrix/federation/v1/send_knock/%s/%s"
            % (room_id, signed_knock_event.event_id),
            signed_knock_event.get_pdu_json(self.clock.time_msec()),
        )
        self.assertEqual(200, channel.code, channel.result)

        return room_id, user_token, fake_knocking_user_id, sent_pdus_by_destination

    def _expect_membership_sent_to_knocking_server(
        self,
        sent_pdus_by_destination: dict[str, list[JsonDict]],
        knocking_user_id: str,
        membership: str,
    ) -> None:
        """Wait until a membership event of the given kind for the knocking
        user has been sent to their (otherwise uninvolved) server."""
        with test_timeout(3, f"{membership} event was not sent to the knocking server"):
            while True:
                pdus = [
                    pdu
                    for pdu in sent_pdus_by_destination.get(self.OTHER_SERVER_NAME, [])
                    if pdu.get("type") == EventTypes.Member
                    and pdu.get("state_key") == knocking_user_id
                    and pdu.get("content", {}).get("membership") == membership
                ]
                if pdus:
                    break
                time.sleep(0.1)
                self.pump(0.1)

    def test_deny_sends_leave_to_knocking_server(self) -> None:
        """Kicking a remote user whose membership is knock sends the leave
        event to their (otherwise uninvolved) server."""
        (
            room_id,
            user_token,
            fake_knocking_user_id,
            sent_pdus_by_destination,
        ) = self._setup_room_with_pending_federated_knock()

        # The local user denies the knock.
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{urllib.parse.quote_plus(room_id)}/kick",
            {"user_id": fake_knocking_user_id},
            access_token=user_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        # The leave event should be sent to the knocking user's server, even
        # though it has no other users in the room.
        self._expect_membership_sent_to_knocking_server(
            sent_pdus_by_destination, fake_knocking_user_id, Membership.LEAVE
        )

    def test_ban_sends_ban_to_knocking_server(self) -> None:
        """Banning a remote user whose membership is knock (deny + ban) sends
        the ban event to their (otherwise uninvolved) server."""
        (
            room_id,
            user_token,
            fake_knocking_user_id,
            sent_pdus_by_destination,
        ) = self._setup_room_with_pending_federated_knock()

        # The local user bans the knocker.
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{urllib.parse.quote_plus(room_id)}/ban",
            {"user_id": fake_knocking_user_id},
            access_token=user_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        self._expect_membership_sent_to_knocking_server(
            sent_pdus_by_destination, fake_knocking_user_id, Membership.BAN
        )
