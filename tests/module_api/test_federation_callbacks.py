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
from http import HTTPStatus
from unittest.mock import AsyncMock, Mock

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.config.server import DEFAULT_ROOM_VERSION
from synapse.federation.sender.transaction_manager import TransactionManager
from synapse.module_api.callbacks.federation import (
    FederatedEventDeliveryMethod,
    FederationEventDeliveryEvent,
)
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest


class FederationDeliveryCallbackTests(unittest.FederatingHomeserverTestCase):
    """
    Tests for `on_event_delivered_over_federation` module callbacks.
    """

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # Mock out the calls over federation.
        self.fed_transport_client = Mock(spec=["send_transaction"])
        self.fed_transport_client.send_transaction = AsyncMock(return_value={})

        hs = self.setup_test_homeserver(
            federation_transport_client=self.fed_transport_client,
        )

        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        # Record every delivery the module callback is told about.
        self._deliveries: list[FederationEventDeliveryEvent] = []

        async def record(delivery: FederationEventDeliveryEvent) -> None:
            self._deliveries.append(delivery)

        hs.get_module_api().register_federation_callbacks(
            on_event_delivered_over_federation=record
        )

        # Create a public room with the remote server joined
        self.creator = self.register_user("creator", "pass")
        self.creator_tok = self.login("creator", "pass")
        self.room_id = self.helper.create_room_as(
            self.creator, tok=self.creator_tok, is_public=True
        )
        self.remote_user = f"@remote:{self.OTHER_SERVER_NAME}"
        self.inject_room_member(self.room_id, self.remote_user, "join")

    def _assert_only_delivery(
        self,
        method: FederatedEventDeliveryMethod,
    ) -> FederationEventDeliveryEvent:
        """
        Assert that exactly one delivery, with the given `method`, is currently recorded
        (since the tracker was last cleared) and return it.

        This clears the tracker.
        """
        self.assertEqual(
            len(self._deliveries),
            1,
            f"expected exactly one delivery; saw {self._deliveries!r}",
        )

        delivery = self._deliveries[0]
        self._deliveries.clear()

        self.assertEqual(delivery.method, method, delivery)

        return delivery

    def test_backfill(self) -> None:
        """
        Tests that the callback is triggered for incoming `/backfill` requests.
        """
        (
            message_event_id1,
            message_event_id2,
            message_event_id3,
        ) = self.helper.send_messages(self.room_id, 3, tok=self.creator_tok)

        # Call the endpoint twice to make sure that it doesn't forget to
        # trigger the callback a second time, for example because it has
        # a `ResponseCache` that bypasses the logic that triggers the
        # callback.
        for _ in range(2):
            channel = self.make_signed_federation_request(
                "GET",
                f"/_matrix/federation/v1/backfill/{self.room_id}"
                f"?v={message_event_id3}&limit=3",
            )
            self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
            delivery = self._assert_only_delivery(FederatedEventDeliveryMethod.BACKFILL)
            self.assertEqual(delivery.server_name, self.OTHER_SERVER_NAME)
            self.assertEqual(
                {e.event_id for e in delivery.events},
                {message_event_id1, message_event_id2, message_event_id3},
            )

    def test_event(self) -> None:
        """
        Tests that the callback is triggered for incoming `/event` requests.
        """
        (message_event_id,) = self.helper.send_messages(
            self.room_id, 1, tok=self.creator_tok
        )

        # Call the endpoint twice to make sure that it doesn't forget to
        # trigger the callback a second time, for example because it has
        # a `ResponseCache` that bypasses the logic that triggers the
        # callback.
        for _ in range(2):
            channel = self.make_signed_federation_request(
                "GET", f"/_matrix/federation/v1/event/{message_event_id}"
            )
            self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
            delivery = self._assert_only_delivery(FederatedEventDeliveryMethod.EVENT)
            self.assertEqual({e.event_id for e in delivery.events}, {message_event_id})

    def test_event_auth(self) -> None:
        """
        Tests that the callback is triggered for incoming `/event_auth` requests.
        """
        (message_event_id,) = self.helper.send_messages(
            self.room_id, 1, tok=self.creator_tok
        )

        # Call the endpoint twice to make sure that it doesn't forget to
        # trigger the callback a second time, for example because it has
        # a `ResponseCache` that bypasses the logic that triggers the
        # callback.
        for _ in range(2):
            channel = self.make_signed_federation_request(
                "GET",
                f"/_matrix/federation/v1/event_auth/{self.room_id}/{message_event_id}",
            )
            self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
            delivery = self._assert_only_delivery(
                FederatedEventDeliveryMethod.EVENT_AUTH
            )

            state_key_pairs_included = {(e.type, e.state_key) for e in delivery.events}
            self.assertEqual(
                state_key_pairs_included,
                {
                    (EventTypes.Create, ""),
                    (EventTypes.PowerLevels, ""),
                    (EventTypes.Member, self.creator),
                },
            )

    def test_state(self) -> None:
        """
        Tests that the callback is triggered for incoming `/state` requests.
        """
        (message_event_id,) = self.helper.send_messages(
            self.room_id, 1, tok=self.creator_tok
        )

        # Call the endpoint twice to make sure that it doesn't forget to
        # trigger the callback a second time, for example because it has
        # a `ResponseCache` that bypasses the logic that triggers the
        # callback.
        for _ in range(2):
            channel = self.make_signed_federation_request(
                "GET",
                f"/_matrix/federation/v1/state/{self.room_id}?event_id={message_event_id}",
            )
            self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
            delivery = self._assert_only_delivery(FederatedEventDeliveryMethod.STATE)

            # Check that we got notified about delivery for all the expected state events
            # included in a `/state` response (all the state in the room at the event)
            state_key_pairs_included = {(e.type, e.state_key) for e in delivery.events}
            self.assertEqual(
                state_key_pairs_included,
                {
                    (EventTypes.Create, ""),
                    (EventTypes.JoinRules, ""),
                    (EventTypes.PowerLevels, ""),
                    (EventTypes.RoomHistoryVisibility, ""),
                    (EventTypes.Member, self.creator),
                    (EventTypes.Member, self.remote_user),
                },
            )

    def test_get_missing_events(self) -> None:
        """
        Tests that the callback is triggered for incoming `/get_missing_events` requests.
        """
        (
            message_event_id1,
            message_event_id2,
            message_event_id3,
        ) = self.helper.send_messages(self.room_id, 3, tok=self.creator_tok)

        # Call the endpoint twice to make sure that it doesn't forget to
        # trigger the callback a second time, for example because it has
        # a `ResponseCache` that bypasses the logic that triggers the
        # callback.
        for _ in range(2):
            channel = self.make_signed_federation_request(
                "POST",
                f"/_matrix/federation/v1/get_missing_events/{self.room_id}",
                {
                    "earliest_events": [message_event_id1],
                    "latest_events": [message_event_id3],
                    "limit": 10,
                },
            )
            self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
            delivery = self._assert_only_delivery(
                FederatedEventDeliveryMethod.GET_MISSING_EVENTS
            )
            self.assertEqual({e.event_id for e in delivery.events}, {message_event_id2})

    def test_send_join(self) -> None:
        """
        Tests that the callback is triggered for incoming `/send_join` requests,
        including both the state events and the newly-created join event.
        """
        joining_user = f"@joiner:{self.OTHER_SERVER_NAME}"
        make_join = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/make_join/{self.room_id}/{joining_user}"
            f"?ver={DEFAULT_ROOM_VERSION}",
        )
        self.assertEqual(make_join.code, HTTPStatus.OK, make_join.json_body)

        join_event_dict = make_join.json_body["event"]
        self.add_hashes_and_signatures_from_other_server(
            join_event_dict, KNOWN_ROOM_VERSIONS[DEFAULT_ROOM_VERSION]
        )
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/send_join/{self.room_id}/x",
            content=join_event_dict,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        delivery = self._assert_only_delivery(FederatedEventDeliveryMethod.SEND_JOIN)

        # Check that we got notified about delivery for all the expected state events
        # included in a `/send_join` response (the join itself, the room state
        # and the auth chain events)
        state_key_pairs_included = {(e.type, e.state_key) for e in delivery.events}
        self.assertIncludes(
            state_key_pairs_included,
            {
                (EventTypes.Create, ""),
                (EventTypes.JoinRules, ""),
                (EventTypes.PowerLevels, ""),
                (EventTypes.RoomHistoryVisibility, ""),
                (EventTypes.Member, self.creator),
                (EventTypes.Member, self.remote_user),
                (EventTypes.Member, joining_user),
            },
            exact=True,
        )

    def test_send_outbound_transaction(self) -> None:
        """
        Tests that the callback is triggered for outgoing `/send` transactions
        when the remote acknowledges the PDU.
        """
        (message_event_id,) = self.helper.send_messages(
            self.room_id, 1, tok=self.creator_tok
        )

        self.fed_transport_client.send_transaction = AsyncMock(
            return_value={"pdus": {message_event_id: {}}}
        )

        event = self.get_success(
            self.hs.get_datastores().main.get_event(message_event_id)
        )
        txn_manager = TransactionManager(self.hs)
        self.get_success(
            txn_manager.send_new_transaction(self.OTHER_SERVER_NAME, [event], [])
        )
        delivery = self._assert_only_delivery(
            FederatedEventDeliveryMethod.SEND,
        )
        self.assertEqual([e.event_id for e in delivery.events], [message_event_id])

    def test_send_outbound_excludes_rejected_pdus(self) -> None:
        """
        Tests that the event is NOT triggered for outgoing `/send` transactions
        when the remote marks the PDU as failed.
        """
        (message_event_id,) = self.helper.send_messages(
            self.room_id, 1, tok=self.creator_tok
        )

        self.fed_transport_client.send_transaction = AsyncMock(
            return_value={"pdus": {message_event_id: {"error": "failed"}}}
        )

        event = self.get_success(
            self.hs.get_datastores().main.get_event(message_event_id)
        )
        txn_manager = TransactionManager(self.hs)
        self.get_success(
            txn_manager.send_new_transaction(self.OTHER_SERVER_NAME, [event], [])
        )
        self.assertEqual(self._deliveries, [])
