#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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

from typing import Collection, Optional

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import ReceiptTypes
from synapse.server import HomeServer
from synapse.types import UserID, create_requester
from synapse.util import Clock

from tests.test_utils.event_injection import create_event
from tests.unittest import HomeserverTestCase

OTHER_USER_ID = "@other:test"
OUR_USER_ID = "@our:test"


class ReceiptTestCase(HomeserverTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        super().prepare(reactor, clock, homeserver)

        self.store = homeserver.get_datastores().main

        self.room_creator = homeserver.get_room_creation_handler()
        persist_event_storage_controller = self.hs.get_storage_controllers().persistence
        assert persist_event_storage_controller is not None
        self.persist_event_storage_controller = persist_event_storage_controller

        # Create a test user
        self.ourUser = UserID.from_string(OUR_USER_ID)
        self.ourRequester = create_requester(self.ourUser)

        # Create a second test user
        self.otherUser = UserID.from_string(OTHER_USER_ID)
        self.otherRequester = create_requester(self.otherUser)

        # Create a test room
        self.room_id1, _, _ = self.get_success(
            self.room_creator.create_room(self.ourRequester, {})
        )

        # Create a second test room
        self.room_id2, _, _ = self.get_success(
            self.room_creator.create_room(self.ourRequester, {})
        )

        # Join the second user to the first room
        memberEvent, memberEventContext = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id1,
                type="m.room.member",
                sender=self.otherRequester.user.to_string(),
                state_key=self.otherRequester.user.to_string(),
                content={"membership": "join"},
            )
        )
        self.get_success(
            self.persist_event_storage_controller.persist_event(
                memberEvent, memberEventContext
            )
        )

        # Join the second user to the second room
        memberEvent, memberEventContext = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id2,
                type="m.room.member",
                sender=self.otherRequester.user.to_string(),
                state_key=self.otherRequester.user.to_string(),
                content={"membership": "join"},
            )
        )
        self.get_success(
            self.persist_event_storage_controller.persist_event(
                memberEvent, memberEventContext
            )
        )

    def get_last_unthreaded_receipt(
        self, receipt_types: Collection[str], room_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Fetch the event ID for the latest unthreaded receipt in the test room for the test user.

        Args:
            receipt_types: The receipt types to fetch.

        Returns:
            The latest receipt, if one exists.
        """
        result = self.get_success(
            self.store.db_pool.runInteraction(
                "get_last_receipt_event_id_for_user",
                self.store.get_last_unthreaded_receipt_for_user_txn,
                OUR_USER_ID,
                room_id or self.room_id1,
                receipt_types,
            )
        )
        if not result:
            return None

        event_id, _ = result
        return event_id

    def test_return_empty_with_no_data(self) -> None:
        res = self.get_success(
            self.store.get_receipts_for_user(
                OUR_USER_ID,
                [
                    ReceiptTypes.READ,
                    ReceiptTypes.READ_PRIVATE,
                ],
            )
        )
        self.assertEqual(res, {})

        res = self.get_success(
            self.store.get_receipts_for_user_with_orderings(
                OUR_USER_ID,
                [
                    ReceiptTypes.READ,
                    ReceiptTypes.READ_PRIVATE,
                ],
            )
        )
        self.assertEqual(res, {})

        res2 = self.get_last_unthreaded_receipt(
            [ReceiptTypes.READ, ReceiptTypes.READ_PRIVATE]
        )

        self.assertIsNone(res2)

    def test_get_receipts_for_user(self) -> None:
        # Send some events into the first room
        event1_1_id = self.create_and_send_event(
            self.room_id1, UserID.from_string(OTHER_USER_ID)
        )
        event1_2_id = self.create_and_send_event(
            self.room_id1, UserID.from_string(OTHER_USER_ID)
        )

        # Send public read receipt for the first event
        self.get_success(
            self.store.insert_receipt(
                self.room_id1, ReceiptTypes.READ, OUR_USER_ID, [event1_1_id], None, {}
            )
        )
        # Send private read receipt for the second event
        self.get_success(
            self.store.insert_receipt(
                self.room_id1,
                ReceiptTypes.READ_PRIVATE,
                OUR_USER_ID,
                [event1_2_id],
                None,
                {},
            )
        )

        # Test we get the latest event when we want both private and public receipts
        res = self.get_success(
            self.store.get_receipts_for_user(
                OUR_USER_ID, [ReceiptTypes.READ, ReceiptTypes.READ_PRIVATE]
            )
        )
        self.assertEqual(res, {self.room_id1: event1_2_id})

        # Test we get the older event when we want only public receipt
        res = self.get_success(
            self.store.get_receipts_for_user(OUR_USER_ID, [ReceiptTypes.READ])
        )
        self.assertEqual(res, {self.room_id1: event1_1_id})

        # Test we get the latest event when we want only the public receipt
        res = self.get_success(
            self.store.get_receipts_for_user(OUR_USER_ID, [ReceiptTypes.READ_PRIVATE])
        )
        self.assertEqual(res, {self.room_id1: event1_2_id})

        # Test receipt updating
        self.get_success(
            self.store.insert_receipt(
                self.room_id1, ReceiptTypes.READ, OUR_USER_ID, [event1_2_id], None, {}
            )
        )
        res = self.get_success(
            self.store.get_receipts_for_user(OUR_USER_ID, [ReceiptTypes.READ])
        )
        self.assertEqual(res, {self.room_id1: event1_2_id})

        # Send some events into the second room
        event2_1_id = self.create_and_send_event(
            self.room_id2, UserID.from_string(OTHER_USER_ID)
        )

        # Test new room is reflected in what the method returns
        self.get_success(
            self.store.insert_receipt(
                self.room_id2,
                ReceiptTypes.READ_PRIVATE,
                OUR_USER_ID,
                [event2_1_id],
                None,
                {},
            )
        )
        res = self.get_success(
            self.store.get_receipts_for_user(
                OUR_USER_ID, [ReceiptTypes.READ, ReceiptTypes.READ_PRIVATE]
            )
        )
        self.assertEqual(res, {self.room_id1: event1_2_id, self.room_id2: event2_1_id})

    def test_get_last_receipt_event_id_for_user(self) -> None:
        # Send some events into the first room
        event1_1_id = self.create_and_send_event(
            self.room_id1, UserID.from_string(OTHER_USER_ID)
        )
        event1_2_id = self.create_and_send_event(
            self.room_id1, UserID.from_string(OTHER_USER_ID)
        )

        # Send public read receipt for the first event
        self.get_success(
            self.store.insert_receipt(
                self.room_id1, ReceiptTypes.READ, OUR_USER_ID, [event1_1_id], None, {}
            )
        )
        # Send private read receipt for the second event
        self.get_success(
            self.store.insert_receipt(
                self.room_id1,
                ReceiptTypes.READ_PRIVATE,
                OUR_USER_ID,
                [event1_2_id],
                None,
                {},
            )
        )

        # Test we get the latest event when we want both private and public receipts
        res = self.get_last_unthreaded_receipt(
            [ReceiptTypes.READ, ReceiptTypes.READ_PRIVATE]
        )
        self.assertEqual(res, event1_2_id)

        # Test we get the older event when we want only public receipt
        res = self.get_last_unthreaded_receipt([ReceiptTypes.READ])
        self.assertEqual(res, event1_1_id)

        # Test we get the latest event when we want only the private receipt
        res = self.get_last_unthreaded_receipt([ReceiptTypes.READ_PRIVATE])
        self.assertEqual(res, event1_2_id)

        # Test receipt updating
        self.get_success(
            self.store.insert_receipt(
                self.room_id1, ReceiptTypes.READ, OUR_USER_ID, [event1_2_id], None, {}
            )
        )
        res = self.get_last_unthreaded_receipt([ReceiptTypes.READ])
        self.assertEqual(res, event1_2_id)

        # Send some events into the second room
        event2_1_id = self.create_and_send_event(
            self.room_id2, UserID.from_string(OTHER_USER_ID)
        )

        # Test new room is reflected in what the method returns
        self.get_success(
            self.store.insert_receipt(
                self.room_id2,
                ReceiptTypes.READ_PRIVATE,
                OUR_USER_ID,
                [event2_1_id],
                None,
                {},
            )
        )
        res = self.get_last_unthreaded_receipt(
            [ReceiptTypes.READ, ReceiptTypes.READ_PRIVATE], room_id=self.room_id2
        )
        self.assertEqual(res, event2_1_id)
