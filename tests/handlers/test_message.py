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
from typing import Tuple

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.api.errors import SynapseError
from synapse.events import EventBase
from synapse.events.snapshot import EventContext, UnpersistedEventContextBase
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import create_requester
from synapse.util import Clock
from synapse.util.stringutils import random_string

from tests import unittest
from tests.test_utils.event_injection import create_event

logger = logging.getLogger(__name__)


class EventCreationTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.handler = self.hs.get_event_creation_handler()
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None
        self._persist_event_storage_controller = persistence
        self.store = self.hs.get_datastores().main

        self.user_id = self.register_user("tester", "foobar")
        device_id = "dev-1"
        access_token = self.login("tester", "foobar", device_id=device_id)
        self.room_id = self.helper.create_room_as(self.user_id, tok=access_token)
        self.private_room_id = self.helper.create_room_as(
            self.user_id, tok=access_token, extra_content={"preset": "private_chat"}
        )

        self.requester = create_requester(self.user_id, device_id=device_id)

    def _create_and_persist_member_event(self) -> Tuple[EventBase, EventContext]:
        # Create a member event we can use as an auth_event
        memberEvent, memberEventContext = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.room.member",
                sender=self.requester.user.to_string(),
                state_key=self.requester.user.to_string(),
                content={"membership": "join"},
            )
        )
        self.get_success(
            self._persist_event_storage_controller.persist_event(
                memberEvent, memberEventContext
            )
        )

        return memberEvent, memberEventContext

    def _create_duplicate_event(
        self, txn_id: str
    ) -> Tuple[EventBase, UnpersistedEventContextBase]:
        """Create a new event with the given transaction ID. All events produced
        by this method will be considered duplicates.
        """

        # We create a new event with a random body, as otherwise we'll produce
        # *exactly* the same event with the same hash, and so same event ID.
        return self.get_success(
            self.handler.create_event(
                self.requester,
                {
                    "type": EventTypes.Message,
                    "room_id": self.room_id,
                    "sender": self.requester.user.to_string(),
                    "content": {"msgtype": "m.text", "body": random_string(5)},
                },
                txn_id=txn_id,
            )
        )

    def test_duplicated_txn_id(self) -> None:
        """Test that attempting to handle/persist an event with a transaction ID
        that has already been persisted correctly returns the old event and does
        *not* produce duplicate messages.
        """

        txn_id = "something_suitably_random"

        event1, unpersisted_context = self._create_duplicate_event(txn_id)
        context = self.get_success(unpersisted_context.persist(event1))

        ret_event1 = self.get_success(
            self.handler.handle_new_client_event(
                self.requester,
                events_and_context=[(event1, context)],
            )
        )
        stream_id1 = ret_event1.internal_metadata.stream_ordering

        self.assertEqual(event1.event_id, ret_event1.event_id)

        event2, unpersisted_context = self._create_duplicate_event(txn_id)
        context = self.get_success(unpersisted_context.persist(event2))

        # We want to test that the deduplication at the persit event end works,
        # so we want to make sure we test with different events.
        self.assertNotEqual(event1.event_id, event2.event_id)

        ret_event2 = self.get_success(
            self.handler.handle_new_client_event(
                self.requester,
                events_and_context=[(event2, context)],
            )
        )
        stream_id2 = ret_event2.internal_metadata.stream_ordering

        # Assert that the returned values match those from the initial event
        # rather than the new one.
        self.assertEqual(ret_event1.event_id, ret_event2.event_id)
        self.assertEqual(stream_id1, stream_id2)

        # Let's test that calling `persist_event` directly also does the right
        # thing.
        event3, unpersisted_context = self._create_duplicate_event(txn_id)
        context = self.get_success(unpersisted_context.persist(event3))

        self.assertNotEqual(event1.event_id, event3.event_id)

        ret_event3, event_pos3, _ = self.get_success(
            self._persist_event_storage_controller.persist_event(event3, context)
        )

        # Assert that the returned values match those from the initial event
        # rather than the new one.
        self.assertEqual(ret_event1.event_id, ret_event3.event_id)
        self.assertEqual(stream_id1, event_pos3.stream)

        # Let's test that calling `persist_events` directly also does the right
        # thing.
        event4, unpersisted_context = self._create_duplicate_event(txn_id)
        context = self.get_success(unpersisted_context.persist(event4))
        self.assertNotEqual(event1.event_id, event3.event_id)

        events, _ = self.get_success(
            self._persist_event_storage_controller.persist_events([(event3, context)])
        )
        ret_event4 = events[0]

        # Assert that the returned values match those from the initial event
        # rather than the new one.
        self.assertEqual(ret_event1.event_id, ret_event4.event_id)

    def test_duplicated_txn_id_one_call(self) -> None:
        """Test that we correctly handle duplicates that we try and persist at
        the same time.
        """

        txn_id = "something_else_suitably_random"

        # Create two duplicate events to persist at the same time
        event1, unpersisted_context1 = self._create_duplicate_event(txn_id)
        context1 = self.get_success(unpersisted_context1.persist(event1))
        event2, unpersisted_context2 = self._create_duplicate_event(txn_id)
        context2 = self.get_success(unpersisted_context2.persist(event2))

        # Ensure their event IDs are different to start with
        self.assertNotEqual(event1.event_id, event2.event_id)

        events, _ = self.get_success(
            self._persist_event_storage_controller.persist_events(
                [(event1, context1), (event2, context2)]
            )
        )

        # Check that we've deduplicated the events.
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_id, events[1].event_id)

    def test_reject_event_with_empty_prev_events(
        self,
    ) -> None:
        """
        Shouldn't be able to create an event without any `prev_events` even if it has
        `auth_events`. Expect an exception to be raised.
        """
        # Create a member event we can use as an auth_event
        memberEvent, _ = self._create_and_persist_member_event()

        # Try to create the event with empty prev_events but with some auth_events
        #
        # We expect the test to fail because empty prev_events are not allowed
        self.get_failure(
            self.handler.create_event(
                self.requester,
                {
                    "type": EventTypes.Message,
                    "room_id": self.room_id,
                    "sender": self.requester.user.to_string(),
                    "content": {"msgtype": "m.text", "body": random_string(5)},
                },
                # Empty prev_events is the key thing we're testing here
                prev_event_ids=[],
                # But with some auth_events
                auth_event_ids=[memberEvent.event_id],
            ),
            AssertionError,
        )

    def test_call_invite_event_creation_fails_in_public_room(self) -> None:
        # get prev_events for room
        prev_events = self.get_success(
            self.store.get_prev_events_for_room(self.room_id)
        )

        # the invite in a public room should fail
        self.get_failure(
            self.handler.create_event(
                self.requester,
                {
                    "type": EventTypes.CallInvite,
                    "room_id": self.room_id,
                    "sender": self.requester.user.to_string(),
                },
                prev_event_ids=prev_events,
                auth_event_ids=prev_events,
            ),
            SynapseError,
        )

        # but a call invite in a private room should succeed
        self.get_success(
            self.handler.create_event(
                self.requester,
                {
                    "type": EventTypes.CallInvite,
                    "room_id": self.private_room_id,
                    "sender": self.requester.user.to_string(),
                },
                prev_event_ids=prev_events,
                auth_event_ids=prev_events,
            )
        )


class ServerAclValidationTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user_id = self.register_user("tester", "foobar")
        self.access_token = self.login("tester", "foobar")
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.access_token)

    def test_allow_server_acl(self) -> None:
        """Test that sending an ACL that blocks everyone but ourselves works."""

        self.helper.send_state(
            self.room_id,
            EventTypes.ServerACL,
            body={"allow": [self.hs.hostname]},
            tok=self.access_token,
            expect_code=200,
        )

    def test_deny_server_acl_block_outselves(self) -> None:
        """Test that sending an ACL that blocks ourselves does not work."""
        self.helper.send_state(
            self.room_id,
            EventTypes.ServerACL,
            body={},
            tok=self.access_token,
            expect_code=400,
        )

    def test_deny_redact_server_acl(self) -> None:
        """Test that attempting to redact an ACL is blocked."""

        body = self.helper.send_state(
            self.room_id,
            EventTypes.ServerACL,
            body={"allow": [self.hs.hostname]},
            tok=self.access_token,
            expect_code=200,
        )
        event_id = body["event_id"]

        # Redaction of event should fail.
        path = "/_matrix/client/r0/rooms/%s/redact/%s" % (self.room_id, event_id)
        channel = self.make_request(
            "POST", path, content={}, access_token=self.access_token
        )
        self.assertEqual(channel.code, 403)
