#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 Matrix.org Federation C.I.C
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

from unittest import mock
from unittest.mock import AsyncMock

import twisted.web.client
from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.api.errors import HttpResponseException
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.rest import admin
from synapse.rest.client import login, register, room, user_directory
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.storage.test_user_directory import GetUserDirectoryTables
from tests.test_utils import FakeResponse, event_injection
from tests.unittest import FederatingHomeserverTestCase


class FederationClientTest(FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        user_directory.register_servlets,
        register.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        """Create a homeserver with federation enabled and user directory enabled."""
        config = self.default_config()
        config["user_directory"] = {
            "enabled": True,
            "search_all_users": True,
        }
        return self.setup_test_homeserver(config=config)

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        super().prepare(reactor, clock, homeserver)

        # mock out the Agent used by the federation client, which is easier than
        # catching the HTTPS connection and do the TLS stuff.
        self._mock_agent = mock.create_autospec(twisted.web.client.Agent, spec_set=True)
        homeserver.get_federation_http_client().agent = self._mock_agent

        # Move clock up to somewhat realistic time so the PDU destination retry
        # works (`now` needs to be larger than `0 + PDU_RETRY_TIME_MS`).
        self.reactor.advance(1000000000)

        self.creator = f"@creator:{self.OTHER_SERVER_NAME}"
        self.test_room_id = "!room_id"
        self.federation_client = homeserver.get_federation_client()
        self.transport_layer = self.federation_client.transport_layer

    def test_get_room_state(self) -> None:
        # mock up some events to use in the response.
        # In real life, these would have things in `prev_events` and `auth_events`, but that's
        # a bit annoying to mock up, and the code under test doesn't care, so we don't bother.
        create_event_dict = self.add_hashes_and_signatures_from_other_server(
            {
                "room_id": self.test_room_id,
                "type": "m.room.create",
                "state_key": "",
                "sender": self.creator,
                "content": {"creator": self.creator},
                "prev_events": [],
                "auth_events": [],
                "depth": 1,
                "origin_server_ts": 500,
            }
        )
        member_event_dict = self.add_hashes_and_signatures_from_other_server(
            {
                "room_id": self.test_room_id,
                "type": "m.room.member",
                "sender": self.creator,
                "state_key": self.creator,
                "content": {"membership": "join"},
                "prev_events": [],
                "auth_events": [],
                "depth": 2,
                "origin_server_ts": 600,
            }
        )
        pl_event_dict = self.add_hashes_and_signatures_from_other_server(
            {
                "room_id": self.test_room_id,
                "type": "m.room.power_levels",
                "sender": self.creator,
                "state_key": "",
                "content": {},
                "prev_events": [],
                "auth_events": [],
                "depth": 3,
                "origin_server_ts": 700,
            }
        )

        # mock up the response, and have the agent return it
        self._mock_agent.request.side_effect = lambda *args, **kwargs: defer.succeed(
            FakeResponse.json(
                payload={
                    "pdus": [
                        create_event_dict,
                        member_event_dict,
                        pl_event_dict,
                    ],
                    "auth_chain": [
                        create_event_dict,
                        member_event_dict,
                    ],
                }
            )
        )

        # now fire off the request
        state_resp, auth_resp = self.get_success(
            self.hs.get_federation_client().get_room_state(
                "yet.another.server",
                self.test_room_id,
                "event_id",
                RoomVersions.V9,
            )
        )

        # check the right call got made to the agent
        self._mock_agent.request.assert_called_once_with(
            b"GET",
            b"matrix-federation://yet.another.server/_matrix/federation/v1/state/%21room_id?event_id=event_id",
            headers=mock.ANY,
            bodyProducer=None,
        )

        # ... and that the response is correct.

        # the auth_resp should be empty because all the events are also in state
        self.assertEqual(auth_resp, [])

        # all of the events should be returned in state_resp, though not necessarily
        # in the same order. We just check the type on the assumption that if the type
        # is right, so is the rest of the event.
        self.assertCountEqual(
            [e.type for e in state_resp],
            ["m.room.create", "m.room.member", "m.room.power_levels"],
        )

    def test_get_pdu_returns_nothing_when_event_does_not_exist(self) -> None:
        """No event should be returned when the event does not exist"""
        pulled_pdu_info = self.get_success(
            self.hs.get_federation_client().get_pdu(
                ["yet.another.server"],
                "event_should_not_exist",
                RoomVersions.V9,
            )
        )
        self.assertEqual(pulled_pdu_info, None)

    def test_get_pdu(self) -> None:
        """Test to make sure an event is returned by `get_pdu()`"""
        self._get_pdu_once()

    def test_get_pdu_event_from_cache_is_pristine(self) -> None:
        """Test that modifications made to events returned by `get_pdu()`
        do not propagate back to to the internal cache (events returned should
        be a copy).
        """

        # Get the PDU in the cache
        remote_pdu = self._get_pdu_once()

        # Modify the the event reference.
        # This change should not make it back to the `_get_pdu_cache`.
        remote_pdu.internal_metadata.outlier = True

        # Get the event again. This time it should read it from cache.
        pulled_pdu_info2 = self.get_success(
            self.hs.get_federation_client().get_pdu(
                ["yet.another.server"],
                remote_pdu.event_id,
                RoomVersions.V9,
            )
        )
        assert pulled_pdu_info2 is not None
        remote_pdu2 = pulled_pdu_info2.pdu

        # Sanity check that we are working against the same event
        self.assertEqual(remote_pdu.event_id, remote_pdu2.event_id)

        # Make sure the event does not include modification from earlier
        self.assertIsNotNone(remote_pdu2)
        self.assertEqual(remote_pdu2.internal_metadata.outlier, False)

    def _get_pdu_once(self) -> EventBase:
        """Retrieve an event via `get_pdu()` and assert that an event was returned.
        Also used to prime the cache for subsequent test logic.
        """
        message_event_dict = self.add_hashes_and_signatures_from_other_server(
            {
                "room_id": self.test_room_id,
                "type": "m.room.message",
                "sender": self.creator,
                "state_key": "",
                "content": {},
                "prev_events": [],
                "auth_events": [],
                "origin_server_ts": 700,
                "depth": 10,
            }
        )

        # mock up the response, and have the agent return it
        self._mock_agent.request.side_effect = lambda *args, **kwargs: defer.succeed(
            FakeResponse.json(
                payload={
                    "origin": "yet.another.server",
                    "origin_server_ts": 900,
                    "pdus": [
                        message_event_dict,
                    ],
                }
            )
        )

        pulled_pdu_info = self.get_success(
            self.hs.get_federation_client().get_pdu(
                ["yet.another.server"],
                "event_id",
                RoomVersions.V9,
            )
        )
        assert pulled_pdu_info is not None
        remote_pdu = pulled_pdu_info.pdu

        # check the right call got made to the agent
        self._mock_agent.request.assert_called_once_with(
            b"GET",
            b"matrix-federation://yet.another.server/_matrix/federation/v1/event/event_id",
            headers=mock.ANY,
            bodyProducer=None,
        )

        self.assertIsNotNone(remote_pdu)
        self.assertEqual(remote_pdu.internal_metadata.outlier, False)

        return remote_pdu

    def test_backfill_invalid_signature_records_failed_pull_attempts(
        self,
    ) -> None:
        """
        Test to make sure that events from /backfill with invalid signatures get
        recorded as failed pull attempts.
        """
        OTHER_USER = f"@user:{self.OTHER_SERVER_NAME}"
        main_store = self.hs.get_datastores().main

        # Create the room
        user_id = self.register_user("kermit", "test")
        tok = self.login("kermit", "test")
        room_id = self.helper.create_room_as(room_creator=user_id, tok=tok)

        # We purposely don't run `add_hashes_and_signatures_from_other_server`
        # over this because we want the signature check to fail.
        pulled_event, _ = self.get_success(
            event_injection.create_event(
                self.hs,
                room_id=room_id,
                sender=OTHER_USER,
                type="test_event_type",
                content={"body": "garply"},
            )
        )

        # We expect an outbound request to /backfill, so stub that out
        self._mock_agent.request.side_effect = lambda *args, **kwargs: defer.succeed(
            FakeResponse.json(
                payload={
                    "origin": "yet.another.server",
                    "origin_server_ts": 900,
                    # Mimic the other server returning our new `pulled_event`
                    "pdus": [pulled_event.get_pdu_json()],
                }
            )
        )

        self.get_success(
            self.hs.get_federation_client().backfill(
                # We use "yet.another.server" instead of
                # `self.OTHER_SERVER_NAME` because we want to see the behavior
                # from `_check_sigs_and_hash_and_fetch_one` where it tries to
                # fetch the PDU again from the origin server if the signature
                # fails. Just want to make sure that the failure is counted from
                # both code paths.
                dest="yet.another.server",
                room_id=room_id,
                limit=1,
                extremities=[pulled_event.event_id],
            ),
        )

        # Make sure our failed pull attempt was recorded
        backfill_num_attempts = self.get_success(
            main_store.db_pool.simple_select_one_onecol(
                table="event_failed_pull_attempts",
                keyvalues={"event_id": pulled_event.event_id},
                retcol="num_attempts",
            )
        )
        # This is 2 because it failed once from `self.OTHER_SERVER_NAME` and the
        # other from "yet.another.server"
        self.assertEqual(backfill_num_attempts, 2)

    def test_user_directory_search(self) -> None:
        """Test that the federation client correctly handles user directory search requests."""
        # Mock the transport layer's user_directory_search method
        mock_results = {
            "limited": False,
            "results": [
                {
                    "user_id": "@user:other.example.com",
                    "display_name": "Test User",
                    "avatar_url": "mxc://example.com/avatar",
                }
            ],
        }
        self.transport_layer.user_directory_search = AsyncMock(  # type: ignore[method-assign]
            return_value=mock_results
        )

        # Call the federation client method
        result = self.get_success(
            self.federation_client.user_directory_search("other.example.com", 2000)
        )

        # Check that the result is correct
        self.assertEqual(result, mock_results)

        # Check that user_directory_search was called with the correct arguments
        self.transport_layer.user_directory_search.assert_called_once_with(
            "other.example.com", 2000
        )

    def test_user_directory_search_endpoint_not_found(self) -> None:
        """Test that the federation client handles 404 responses correctly."""
        # Mock the transport layer to raise a 404 error
        self.transport_layer.user_directory_search = AsyncMock(  # type: ignore[method-assign]
            side_effect=HttpResponseException(
                404, "Not Found", b'{"errcode": "M_NOT_FOUND"}'
            )
        )

        # Call the federation client method
        result = self.get_success(
            self.federation_client.user_directory_search("other.example.com", 10)
        )

        # Check that the result is an empty result set
        self.assertEqual(result, {"limited": False, "results": []})


class FederatedUserDirectorySyncTestCase(FederatingHomeserverTestCase):
    """Tests the federation-side periodic user directory sync background job."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        register.register_servlets,
        user_directory.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()
        # Enable writing to the user directory on this process.
        config["update_user_directory_from_worker"] = None
        config["user_directory"] = {"enabled": True, "search_all_users": True}
        config["experimental_features"] = {
            "bwi_federated_user_dir_enabled": True,
        }
        return self.setup_test_homeserver(config=config)

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main
        self.federation_client = homeserver.get_federation_client()
        self.user_dir_helper = GetUserDirectoryTables(self.store)

    def test_sync_uses_db_destinations_and_upserts_remote_users(self) -> None:
        # Record a known destination in the DB.
        self.get_success(
            self.store.set_destination_retry_timings("remote.example.com", None, 0, 0)
        )

        self.federation_client.user_directory_search = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "limited": False,
                "results": [
                    {
                        "user_id": "@bob:remote.example.com",
                        "display_name": "Bob Remote",
                        "avatar_url": None,
                    }
                ],
            }
        )

        self.get_success(self.federation_client._sync_federated_user_directory())

        self.federation_client.user_directory_search.assert_called_once_with(
            "remote.example.com",
            self.hs.config.experimental.bwi_federated_user_dir_federation_search_timeout,
        )

        profiles = self.get_success(
            self.user_dir_helper.get_profiles_in_user_directory()
        )
        self.assertIn("@bob:remote.example.com", profiles)

    def test_sync_skips_own_server(self) -> None:
        # Our own server name should never be queried, even if present in the DB.
        self.get_success(self.store.set_destination_retry_timings("test", None, 0, 0))

        self.federation_client.user_directory_search = AsyncMock()  # type: ignore[method-assign]

        self.get_success(self.federation_client._sync_federated_user_directory())

        self.federation_client.user_directory_search.assert_not_called()
