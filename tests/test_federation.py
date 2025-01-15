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
from http import HTTPStatus
from typing import Optional, Tuple, Union
from unittest.mock import AsyncMock, Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.errors import FederationError
from synapse.api.room_versions import RoomVersions
from synapse.events import make_event_from_dict
from synapse.events.utils import strip_event
from synapse.federation.federation_base import event_from_pdu_json
from synapse.handlers.device import DeviceListUpdater
from synapse.http.types import QueryParams
from synapse.logging.context import LoggingContext
from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock
from synapse.util.retryutils import NotRetryingDestination
from synapse.http.matrixfederationclient import (
    MatrixFederationRequest,
)

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
        self.federation_http_client = Mock()
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
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

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
            room_version=RoomVersions.V10,
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
            room_version=RoomVersions.V10,
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
            room_version=RoomVersions.V10,
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

        # User1 joins the room
        self.helper.join(room_id, user1_id, tok=user1_tok)


class DeviceListResyncTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main

    def test_retry_device_list_resync(self) -> None:
        """Tests that device lists are marked as stale if they couldn't be synced, and
        that stale device lists are retried periodically.
        """
        remote_user_id = "@john:test_remote"
        remote_origin = "test_remote"

        # Track the number of attempts to resync the user's device list.
        self.resync_attempts = 0

        # When this function is called, increment the number of resync attempts (only if
        # we're querying devices for the right user ID), then raise a
        # NotRetryingDestination error to fail the resync gracefully.
        def query_user_devices(
            destination: str, user_id: str, timeout: int = 30000
        ) -> JsonDict:
            if user_id == remote_user_id:
                self.resync_attempts += 1

            raise NotRetryingDestination(0, 0, destination)

        # Register the mock on the federation client.
        federation_client = self.hs.get_federation_client()
        federation_client.query_user_devices = Mock(side_effect=query_user_devices)  # type: ignore[method-assign]

        # Register a mock on the store so that the incoming update doesn't fail because
        # we don't share a room with the user.
        self.store.get_rooms_for_user = AsyncMock(return_value=["!someroom:test"])

        # Manually inject a fake device list update. We need this update to include at
        # least one prev_id so that the user's device list will need to be retried.
        device_list_updater = self.hs.get_device_handler().device_list_updater
        assert isinstance(device_list_updater, DeviceListUpdater)
        self.get_success(
            device_list_updater.incoming_device_list_update(
                origin=remote_origin,
                edu_content={
                    "deleted": False,
                    "device_display_name": "Mobile",
                    "device_id": "QBUAZIFURK",
                    "prev_id": [5],
                    "stream_id": 6,
                    "user_id": remote_user_id,
                },
            )
        )

        # Check that there was one resync attempt.
        self.assertEqual(self.resync_attempts, 1)

        # Check that the resync attempt failed and caused the user's device list to be
        # marked as stale.
        need_resync = self.get_success(
            self.store.get_user_ids_requiring_device_list_resync()
        )
        self.assertIn(remote_user_id, need_resync)

        # Check that waiting for 30 seconds caused Synapse to retry resyncing the device
        # list.
        self.reactor.advance(30)
        self.assertEqual(self.resync_attempts, 2)

    def test_cross_signing_keys_retry(self) -> None:
        """Tests that resyncing a device list correctly processes cross-signing keys from
        the remote server.
        """
        remote_user_id = "@john:test_remote"
        remote_master_key = "85T7JXPFBAySB/jwby4S3lBPTqY3+Zg53nYuGmu1ggY"
        remote_self_signing_key = "QeIiFEjluPBtI7WQdG365QKZcFs9kqmHir6RBD0//nQ"

        # Register mock device list retrieval on the federation client.
        federation_client = self.hs.get_federation_client()
        federation_client.query_user_devices = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "user_id": remote_user_id,
                "stream_id": 1,
                "devices": [],
                "master_key": {
                    "user_id": remote_user_id,
                    "usage": ["master"],
                    "keys": {"ed25519:" + remote_master_key: remote_master_key},
                },
                "self_signing_key": {
                    "user_id": remote_user_id,
                    "usage": ["self_signing"],
                    "keys": {
                        "ed25519:" + remote_self_signing_key: remote_self_signing_key
                    },
                },
            }
        )

        # Resync the device list.
        device_handler = self.hs.get_device_handler()
        self.get_success(
            device_handler.device_list_updater.multi_user_device_resync(
                [remote_user_id]
            ),
        )

        # Retrieve the cross-signing keys for this user.
        keys = self.get_success(
            self.store.get_e2e_cross_signing_keys_bulk(user_ids=[remote_user_id]),
        )
        self.assertIn(remote_user_id, keys)
        key = keys[remote_user_id]
        assert key is not None

        # Check that the master key is the one returned by the mock.
        master_key = key["master"]
        self.assertEqual(len(master_key["keys"]), 1)
        self.assertTrue("ed25519:" + remote_master_key in master_key["keys"].keys())
        self.assertTrue(remote_master_key in master_key["keys"].values())

        # Check that the self-signing key is the one returned by the mock.
        self_signing_key = key["self_signing"]
        self.assertEqual(len(self_signing_key["keys"]), 1)
        self.assertTrue(
            "ed25519:" + remote_self_signing_key in self_signing_key["keys"].keys(),
        )
        self.assertTrue(remote_self_signing_key in self_signing_key["keys"].values())


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
