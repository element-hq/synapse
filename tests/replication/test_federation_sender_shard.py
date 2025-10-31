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
from unittest.mock import AsyncMock, Mock

from netaddr import IPSet
from signedjson.key import (
    encode_verify_key_base64,
    generate_signing_key,
    get_verify_key,
)

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, Membership
from synapse.api.room_versions import RoomVersion
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.events import EventBase, make_event_from_dict
from synapse.handlers.typing import TypingWriterHandler
from synapse.http.federation.matrix_federation_agent import MatrixFederationAgent
from synapse.rest.admin import register_servlets_for_client_rest_resource
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.keys import FetchKeyResult
from synapse.types import JsonDict, UserID, create_requester
from synapse.util.clock import Clock

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.server import get_clock

logger = logging.getLogger(__name__)


class FederationSenderTestCase(BaseMultiWorkerStreamTestCase):
    """
    Various tests for federation sending on workers.

    Federation sending is disabled by default, it will be enabled in each test by
    updating 'federation_sender_instances'.
    """

    servlets = [
        login.register_servlets,
        register_servlets_for_client_rest_resource,
        room.register_servlets,
    ]

    def setUp(self) -> None:
        super().setUp()

        reactor, clock = get_clock()
        self.matrix_federation_agent = MatrixFederationAgent(
            server_name="OUR_STUB_HOMESERVER_NAME",
            reactor=reactor,
            clock=clock,
            tls_client_options_factory=None,
            user_agent=b"SynapseInTrialTest/0.0.0",
            ip_allowlist=None,
            ip_blocklist=IPSet(),
            proxy_config=None,
        )

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.storage_controllers = hs.get_storage_controllers()

    def test_send_event_single_sender(self) -> None:
        """Test that using a single federation sender worker correctly sends a
        new event.
        """
        mock_client = Mock(spec=["put_json"])
        mock_client.put_json = AsyncMock(return_value={})
        mock_client.agent = self.matrix_federation_agent
        self.make_worker_hs(
            "synapse.app.generic_worker",
            {
                "worker_name": "federation_sender1",
                "federation_sender_instances": ["federation_sender1"],
            },
            federation_http_client=mock_client,
        )

        user = self.register_user("user", "pass")
        token = self.login("user", "pass")

        room = self.create_room_with_remote_server(user, token)

        mock_client.put_json.reset_mock()

        self.create_and_send_event(room, UserID.from_string(user))
        self.replicate()

        # Assert that the event was sent out over federation.
        mock_client.put_json.assert_called()
        self.assertEqual(mock_client.put_json.call_args[0][0], "other_server")
        self.assertTrue(mock_client.put_json.call_args[1]["data"].get("pdus"))

    def test_send_event_sharded(self) -> None:
        """Test that using two federation sender workers correctly sends
        new events.
        """
        mock_client1 = Mock(spec=["put_json"])
        mock_client1.put_json = AsyncMock(return_value={})
        mock_client1.agent = self.matrix_federation_agent
        self.make_worker_hs(
            "synapse.app.generic_worker",
            {
                "worker_name": "federation_sender1",
                "federation_sender_instances": [
                    "federation_sender1",
                    "federation_sender2",
                ],
            },
            federation_http_client=mock_client1,
        )

        mock_client2 = Mock(spec=["put_json"])
        mock_client2.put_json = AsyncMock(return_value={})
        mock_client2.agent = self.matrix_federation_agent
        self.make_worker_hs(
            "synapse.app.generic_worker",
            {
                "worker_name": "federation_sender2",
                "federation_sender_instances": [
                    "federation_sender1",
                    "federation_sender2",
                ],
            },
            federation_http_client=mock_client2,
        )

        user = self.register_user("user2", "pass")
        token = self.login("user2", "pass")

        sent_on_1 = False
        sent_on_2 = False
        for i in range(20):
            server_name = "other_server_%d" % (i,)
            room = self.create_room_with_remote_server(user, token, server_name)
            mock_client1.reset_mock()
            mock_client2.reset_mock()

            self.create_and_send_event(room, UserID.from_string(user))
            self.replicate()

            if mock_client1.put_json.called:
                sent_on_1 = True
                mock_client2.put_json.assert_not_called()
                self.assertEqual(mock_client1.put_json.call_args[0][0], server_name)
                self.assertTrue(mock_client1.put_json.call_args[1]["data"].get("pdus"))
            elif mock_client2.put_json.called:
                sent_on_2 = True
                mock_client1.put_json.assert_not_called()
                self.assertEqual(mock_client2.put_json.call_args[0][0], server_name)
                self.assertTrue(mock_client2.put_json.call_args[1]["data"].get("pdus"))
            else:
                raise AssertionError(
                    "Expected send transaction from one or the other sender"
                )

            if sent_on_1 and sent_on_2:
                break

        self.assertTrue(sent_on_1)
        self.assertTrue(sent_on_2)

    def test_send_typing_sharded(self) -> None:
        """Test that using two federation sender workers correctly sends
        new typing EDUs.
        """
        mock_client1 = Mock(spec=["put_json"])
        mock_client1.put_json = AsyncMock(return_value={})
        mock_client1.agent = self.matrix_federation_agent
        self.make_worker_hs(
            "synapse.app.generic_worker",
            {
                "worker_name": "federation_sender1",
                "federation_sender_instances": [
                    "federation_sender1",
                    "federation_sender2",
                ],
            },
            federation_http_client=mock_client1,
        )

        mock_client2 = Mock(spec=["put_json"])
        mock_client2.put_json = AsyncMock(return_value={})
        mock_client2.agent = self.matrix_federation_agent
        self.make_worker_hs(
            "synapse.app.generic_worker",
            {
                "worker_name": "federation_sender2",
                "federation_sender_instances": [
                    "federation_sender1",
                    "federation_sender2",
                ],
            },
            federation_http_client=mock_client2,
        )

        user = self.register_user("user3", "pass")
        token = self.login("user3", "pass")

        typing_handler = self.hs.get_typing_handler()
        assert isinstance(typing_handler, TypingWriterHandler)

        sent_on_1 = False
        sent_on_2 = False
        for i in range(20):
            server_name = "other_server_%d" % (i,)
            room = self.create_room_with_remote_server(user, token, server_name)
            mock_client1.reset_mock()
            mock_client2.reset_mock()

            self.get_success(
                typing_handler.started_typing(
                    target_user=UserID.from_string(user),
                    requester=create_requester(user),
                    room_id=room,
                    timeout=20000,
                )
            )

            self.replicate()

            if mock_client1.put_json.called:
                sent_on_1 = True
                mock_client2.put_json.assert_not_called()
                self.assertEqual(mock_client1.put_json.call_args[0][0], server_name)
                self.assertTrue(mock_client1.put_json.call_args[1]["data"].get("edus"))
            elif mock_client2.put_json.called:
                sent_on_2 = True
                mock_client1.put_json.assert_not_called()
                self.assertEqual(mock_client2.put_json.call_args[0][0], server_name)
                self.assertTrue(mock_client2.put_json.call_args[1]["data"].get("edus"))
            else:
                raise AssertionError(
                    "Expected send transaction from one or the other sender"
                )

            if sent_on_1 and sent_on_2:
                break

        self.assertTrue(sent_on_1)
        self.assertTrue(sent_on_2)

    def create_fake_event_from_remote_server(
        self, remote_server_name: str, event_dict: JsonDict, room_version: RoomVersion
    ) -> EventBase:
        """
        This is similar to what `FederatingHomeserverTestCase` is doing but we don't
        need all of the extra baggage and we want to be able to create an event from
        many remote servers.
        """

        # poke the other server's signing key into the key store, so that we don't
        # make requests for it
        other_server_signature_key = generate_signing_key("test")
        verify_key = get_verify_key(other_server_signature_key)
        verify_key_id = "%s:%s" % (verify_key.alg, verify_key.version)

        self.get_success(
            self.hs.get_datastores().main.store_server_keys_response(
                remote_server_name,
                from_server=remote_server_name,
                ts_added_ms=self.clock.time_msec(),
                verify_keys={
                    verify_key_id: FetchKeyResult(
                        verify_key=verify_key,
                        valid_until_ts=self.clock.time_msec() + 10000,
                    ),
                },
                response_json={
                    "verify_keys": {
                        verify_key_id: {"key": encode_verify_key_base64(verify_key)}
                    }
                },
            )
        )

        add_hashes_and_signatures(
            room_version=room_version,
            event_dict=event_dict,
            signature_name=remote_server_name,
            signing_key=other_server_signature_key,
        )
        event = make_event_from_dict(
            event_dict,
            room_version=room_version,
        )

        return event

    def create_room_with_remote_server(
        self, user: str, token: str, remote_server: str = "other_server"
    ) -> str:
        room_id = self.helper.create_room_as(user, tok=token)
        store = self.hs.get_datastores().main
        federation = self.hs.get_federation_event_handler()

        room_version = self.get_success(store.get_room_version(room_id))

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )

        # Figure out what the forward extremities in the room are (the most recent
        # events that aren't tied into the DAG)
        prev_event_ids = self.get_success(store.get_latest_event_ids_in_room(room_id))

        user_id = UserID("user", remote_server).to_string()

        join_event = self.create_fake_event_from_remote_server(
            remote_server_name=remote_server,
            event_dict={
                "room_id": room_id,
                "sender": user_id,
                "type": EventTypes.Member,
                "state_key": user_id,
                "depth": 1000,
                "origin_server_ts": 1,
                "content": {"membership": Membership.JOIN},
                "auth_events": [
                    state_map[(EventTypes.Create, "")].event_id,
                    state_map[(EventTypes.JoinRules, "")].event_id,
                ],
                "prev_events": list(prev_event_ids),
            },
            room_version=room_version,
        )

        self.get_success(federation.on_send_membership_event(remote_server, join_event))
        self.replicate()

        return room_id
