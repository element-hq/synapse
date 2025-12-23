#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
import enum
import logging

from parameterized import parameterized, parameterized_class
from typing_extensions import assert_never

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import AccountDataTypes
from synapse.rest.client import login, room, sendtodevice, sync
from synapse.server import HomeServer
from synapse.types import StreamKeyType
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import TimedOutException

logger = logging.getLogger(__name__)


class TagAction(enum.Enum):
    ADD = enum.auto()
    REMOVE = enum.auto()


# FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
# foreground update for
# `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
# https://github.com/element-hq/synapse/issues/17623)
@parameterized_class(
    ("use_new_tables",),
    [
        (True,),
        (False,),
    ],
    class_name_func=lambda cls,
    num,
    params_dict: f"{cls.__name__}_{'new' if params_dict['use_new_tables'] else 'fallback'}",
)
class SlidingSyncAccountDataExtensionTestCase(SlidingSyncBase):
    """Tests for the account_data sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        sendtodevice.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.account_data_handler = hs.get_account_data_handler()

        super().prepare(reactor, clock, hs)

    def test_no_data_initial_sync(self) -> None:
        """
        Test that enabling the account_data extension works during an intitial sync,
        even if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Make an initial Sliding Sync request with the account_data extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        global_account_data_map = {
            global_event["type"]: global_event["content"]
            for global_event in response_body["extensions"]["account_data"].get(
                "global"
            )
        }
        self.assertIncludes(
            global_account_data_map.keys(),
            # Even though we don't have any global account data set, Synapse saves some
            # default push rules for us.
            {AccountDataTypes.PUSH_RULES},
            exact=True,
        )
        # Push rules are a giant chunk of JSON data so we will just assume the value is correct if they key is here.
        # global_account_data_map[AccountDataTypes.PUSH_RULES]

        # No room account data for this test
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_no_data_incremental_sync(self) -> None:
        """
        Test that enabling account_data extension works during an incremental sync, even
        if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the account_data extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # There has been no account data changes since the `from_token` so we shouldn't
        # see any account data here.
        global_account_data_map = {
            global_event["type"]: global_event["content"]
            for global_event in response_body["extensions"]["account_data"].get(
                "global"
            )
        }
        self.assertIncludes(
            global_account_data_map.keys(),
            set(),
            exact=True,
        )

        # No room account data for this test
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_global_account_data_initial_sync(self) -> None:
        """
        On initial sync, we should return all global account data on initial sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Update the global account data
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user_id=user1_id,
                account_data_type="org.matrix.foobarbaz",
                content={"foo": "bar"},
            )
        )

        # Make an initial Sliding Sync request with the account_data extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # It should show us all of the global account data
        global_account_data_map = {
            global_event["type"]: global_event["content"]
            for global_event in response_body["extensions"]["account_data"].get(
                "global"
            )
        }
        self.assertIncludes(
            global_account_data_map.keys(),
            {AccountDataTypes.PUSH_RULES, "org.matrix.foobarbaz"},
            exact=True,
        )
        # Push rules are a giant chunk of JSON data so we will just assume the value is correct if they key is here.
        # global_account_data_map[AccountDataTypes.PUSH_RULES]
        self.assertEqual(
            global_account_data_map["org.matrix.foobarbaz"], {"foo": "bar"}
        )

        # No room account data for this test
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_global_account_data_incremental_sync(self) -> None:
        """
        On incremental sync, we should only account data that has changed since the
        `from_token`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Add some global account data
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user_id=user1_id,
                account_data_type="org.matrix.foobarbaz",
                content={"foo": "bar"},
            )
        )

        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Add some other global account data
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user_id=user1_id,
                account_data_type="org.matrix.doodardaz",
                content={"doo": "dar"},
            )
        )

        # Make an incremental Sliding Sync request with the account_data extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        global_account_data_map = {
            global_event["type"]: global_event["content"]
            for global_event in response_body["extensions"]["account_data"].get(
                "global"
            )
        }
        self.assertIncludes(
            global_account_data_map.keys(),
            # We should only see the new global account data that happened after the `from_token`
            {"org.matrix.doodardaz"},
            exact=True,
        )
        self.assertEqual(
            global_account_data_map["org.matrix.doodardaz"], {"doo": "dar"}
        )

        # No room account data for this test
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_room_account_data_initial_sync(self) -> None:
        """
        On initial sync, we return all account data for a given room but only for
        rooms that we request and are being returned in the Sliding Sync response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room and add some room account data
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )
        # Add a room tag to mark the room as a favourite
        self.get_success(
            self.account_data_handler.add_tag_to_room(
                user_id=user1_id,
                room_id=room_id1,
                tag="m.favourite",
                content={},
            )
        )

        # Create another room with some room account data
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )
        # Add a room tag to mark the room as a favourite
        self.get_success(
            self.account_data_handler.add_tag_to_room(
                user_id=user1_id,
                room_id=room_id2,
                tag="m.favourite",
                content={},
            )
        )

        # Make an initial Sliding Sync request with the account_data extension enabled
        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                "account_data": {
                    "enabled": True,
                    "rooms": [room_id1, room_id2],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        self.assertIsNotNone(response_body["extensions"]["account_data"].get("global"))
        # Even though we requested room2, we only expect room1 to show up because that's
        # the only room in the Sliding Sync response (room2 is not one of our room
        # subscriptions or in a sliding window list).
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id1},
            exact=True,
        )
        account_data_map = {
            event["type"]: event["content"]
            for event in response_body["extensions"]["account_data"]
            .get("rooms")
            .get(room_id1)
        }
        self.assertIncludes(
            account_data_map.keys(),
            {"org.matrix.roorarraz", AccountDataTypes.TAG},
            exact=True,
        )
        self.assertEqual(account_data_map["org.matrix.roorarraz"], {"roo": "rar"})
        self.assertEqual(
            account_data_map[AccountDataTypes.TAG], {"tags": {"m.favourite": {}}}
        )

    @parameterized.expand(
        [
            ("add tags", TagAction.ADD),
            ("remove tags", TagAction.REMOVE),
        ]
    )
    def test_room_account_data_incremental_sync(
        self, test_description: str, tag_action: TagAction
    ) -> None:
        """
        On incremental sync, we return all account data for a given room but only for
        rooms that we request and are being returned in the Sliding Sync response.

        (HaveSentRoomFlag.LIVE)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room and add some room account data
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )
        # Add a room tag to mark the room as a favourite
        self.get_success(
            self.account_data_handler.add_tag_to_room(
                user_id=user1_id,
                room_id=room_id1,
                tag="m.favourite",
                content={},
            )
        )

        # Create another room with some room account data
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )
        # Add a room tag to mark the room as a favourite
        self.get_success(
            self.account_data_handler.add_tag_to_room(
                user_id=user1_id,
                room_id=room_id2,
                tag="m.favourite",
                content={},
            )
        )

        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                "account_data": {
                    "enabled": True,
                    "rooms": [room_id1, room_id2],
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Add some other room account data
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz2",
                content={"roo": "rar"},
            )
        )
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz2",
                content={"roo": "rar"},
            )
        )
        if tag_action == TagAction.ADD:
            # Add another room tag
            self.get_success(
                self.account_data_handler.add_tag_to_room(
                    user_id=user1_id,
                    room_id=room_id1,
                    tag="m.server_notice",
                    content={},
                )
            )
            self.get_success(
                self.account_data_handler.add_tag_to_room(
                    user_id=user1_id,
                    room_id=room_id2,
                    tag="m.server_notice",
                    content={},
                )
            )
        elif tag_action == TagAction.REMOVE:
            # Remove the room tag
            self.get_success(
                self.account_data_handler.remove_tag_from_room(
                    user_id=user1_id,
                    room_id=room_id1,
                    tag="m.favourite",
                )
            )
            self.get_success(
                self.account_data_handler.remove_tag_from_room(
                    user_id=user1_id,
                    room_id=room_id2,
                    tag="m.favourite",
                )
            )
        else:
            assert_never(tag_action)

        # Make an incremental Sliding Sync request with the account_data extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertIsNotNone(response_body["extensions"]["account_data"].get("global"))
        # Even though we requested room2, we only expect room1 to show up because that's
        # the only room in the Sliding Sync response (room2 is not one of our room
        # subscriptions or in a sliding window list).
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id1},
            exact=True,
        )
        # We should only see the new room account data that happened after the `from_token`
        account_data_map = {
            event["type"]: event["content"]
            for event in response_body["extensions"]["account_data"]
            .get("rooms")
            .get(room_id1)
        }
        self.assertIncludes(
            account_data_map.keys(),
            {"org.matrix.roorarraz2", AccountDataTypes.TAG},
            exact=True,
        )
        self.assertEqual(account_data_map["org.matrix.roorarraz2"], {"roo": "rar"})
        if tag_action == TagAction.ADD:
            self.assertEqual(
                account_data_map[AccountDataTypes.TAG],
                {"tags": {"m.favourite": {}, "m.server_notice": {}}},
            )
        elif tag_action == TagAction.REMOVE:
            # If we previously showed the client that the room has tags, when it no
            # longer has tags, we need to show them an empty map.
            self.assertEqual(
                account_data_map[AccountDataTypes.TAG],
                {"tags": {}},
            )
        else:
            assert_never(tag_action)

    @parameterized.expand(
        [
            ("add tags", TagAction.ADD),
            ("remove tags", TagAction.REMOVE),
        ]
    )
    def test_room_account_data_incremental_sync_out_of_range_never(
        self, test_description: str, tag_action: TagAction
    ) -> None:
        """Tests that we don't return account data for rooms that are out of
        range, but then do send all account data once they're in range.

        (initial/HaveSentRoomFlag.NEVER)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room and add some room account data
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )
        # Add a room tag to mark the room as a favourite
        self.get_success(
            self.account_data_handler.add_tag_to_room(
                user_id=user1_id,
                room_id=room_id1,
                tag="m.favourite",
                content={},
            )
        )

        # Create another room with some room account data
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )
        # Add a room tag to mark the room as a favourite
        self.get_success(
            self.account_data_handler.add_tag_to_room(
                user_id=user1_id,
                room_id=room_id2,
                tag="m.favourite",
                content={},
            )
        )

        # Now send a message into room1 so that it is at the top of the list
        self.helper.send(room_id1, body="new event", tok=user1_tok)

        # Make a SS request for only the top room.
        sync_body = {
            "lists": {
                "main": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                "account_data": {
                    "enabled": True,
                    "lists": ["main"],
                }
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Only room1 should be in the response since it's the latest room with activity
        # and our range only includes 1 room.
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id1},
            exact=True,
        )

        # Add some other room account data
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz2",
                content={"roo": "rar"},
            )
        )
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz2",
                content={"roo": "rar"},
            )
        )
        if tag_action == TagAction.ADD:
            # Add another room tag
            self.get_success(
                self.account_data_handler.add_tag_to_room(
                    user_id=user1_id,
                    room_id=room_id1,
                    tag="m.server_notice",
                    content={},
                )
            )
            self.get_success(
                self.account_data_handler.add_tag_to_room(
                    user_id=user1_id,
                    room_id=room_id2,
                    tag="m.server_notice",
                    content={},
                )
            )
        elif tag_action == TagAction.REMOVE:
            # Remove the room tag
            self.get_success(
                self.account_data_handler.remove_tag_from_room(
                    user_id=user1_id,
                    room_id=room_id1,
                    tag="m.favourite",
                )
            )
            self.get_success(
                self.account_data_handler.remove_tag_from_room(
                    user_id=user1_id,
                    room_id=room_id2,
                    tag="m.favourite",
                )
            )
        else:
            assert_never(tag_action)

        # Move room2 into range.
        self.helper.send(room_id2, body="new event", tok=user1_tok)

        # Make an incremental Sliding Sync request with the account_data extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertIsNotNone(response_body["extensions"]["account_data"].get("global"))
        # We expect to see the account data of room2, as that has the most
        # recent update.
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id2},
            exact=True,
        )
        # Since this is the first time we're seeing room2 down sync, we should see all
        # room account data for it.
        account_data_map = {
            event["type"]: event["content"]
            for event in response_body["extensions"]["account_data"]
            .get("rooms")
            .get(room_id2)
        }
        expected_account_data_keys = {
            "org.matrix.roorarraz",
            "org.matrix.roorarraz2",
        }
        if tag_action == TagAction.ADD:
            expected_account_data_keys.add(AccountDataTypes.TAG)
        self.assertIncludes(
            account_data_map.keys(),
            expected_account_data_keys,
            exact=True,
        )
        self.assertEqual(account_data_map["org.matrix.roorarraz"], {"roo": "rar"})
        self.assertEqual(account_data_map["org.matrix.roorarraz2"], {"roo": "rar"})
        if tag_action == TagAction.ADD:
            self.assertEqual(
                account_data_map[AccountDataTypes.TAG],
                {"tags": {"m.favourite": {}, "m.server_notice": {}}},
            )
        elif tag_action == TagAction.REMOVE:
            # Since we never told the client about the room tags, we don't need to say
            # anything if there are no tags now (the client doesn't need an update).
            self.assertIsNone(
                account_data_map.get(AccountDataTypes.TAG),
                account_data_map,
            )
        else:
            assert_never(tag_action)

    @parameterized.expand(
        [
            ("add tags", TagAction.ADD),
            ("remove tags", TagAction.REMOVE),
        ]
    )
    def test_room_account_data_incremental_sync_out_of_range_previously(
        self, test_description: str, tag_action: TagAction
    ) -> None:
        """Tests that we don't return account data for rooms that fall out of
        range, but then do send all account data that has changed they're back in range.

        (HaveSentRoomFlag.PREVIOUSLY)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room and add some room account data
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )
        # Add a room tag to mark the room as a favourite
        self.get_success(
            self.account_data_handler.add_tag_to_room(
                user_id=user1_id,
                room_id=room_id1,
                tag="m.favourite",
                content={},
            )
        )

        # Create another room with some room account data
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz",
                content={"roo": "rar"},
            )
        )
        # Add a room tag to mark the room as a favourite
        self.get_success(
            self.account_data_handler.add_tag_to_room(
                user_id=user1_id,
                room_id=room_id2,
                tag="m.favourite",
                content={},
            )
        )

        # Make an initial Sliding Sync request for only room1 and room2.
        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    "timeline_limit": 0,
                },
                room_id2: {
                    "required_state": [],
                    "timeline_limit": 0,
                },
            },
            "extensions": {
                "account_data": {
                    "enabled": True,
                    "rooms": [room_id1, room_id2],
                }
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Both rooms show up because we have a room subscription for each and they're
        # requested in the `account_data` extension.
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id1, room_id2},
            exact=True,
        )

        # Add some other room account data
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id1,
                account_data_type="org.matrix.roorarraz2",
                content={"roo": "rar"},
            )
        )
        self.get_success(
            self.account_data_handler.add_account_data_to_room(
                user_id=user1_id,
                room_id=room_id2,
                account_data_type="org.matrix.roorarraz2",
                content={"roo": "rar"},
            )
        )
        if tag_action == TagAction.ADD:
            # Add another room tag
            self.get_success(
                self.account_data_handler.add_tag_to_room(
                    user_id=user1_id,
                    room_id=room_id1,
                    tag="m.server_notice",
                    content={},
                )
            )
            self.get_success(
                self.account_data_handler.add_tag_to_room(
                    user_id=user1_id,
                    room_id=room_id2,
                    tag="m.server_notice",
                    content={},
                )
            )
        elif tag_action == TagAction.REMOVE:
            # Remove the room tag
            self.get_success(
                self.account_data_handler.remove_tag_from_room(
                    user_id=user1_id,
                    room_id=room_id1,
                    tag="m.favourite",
                )
            )
            self.get_success(
                self.account_data_handler.remove_tag_from_room(
                    user_id=user1_id,
                    room_id=room_id2,
                    tag="m.favourite",
                )
            )
        else:
            assert_never(tag_action)

        # Make an incremental Sliding Sync request for just room1
        response_body, from_token = self.do_sync(
            {
                **sync_body,
                "room_subscriptions": {
                    room_id1: {
                        "required_state": [],
                        "timeline_limit": 0,
                    },
                },
            },
            since=from_token,
            tok=user1_tok,
        )

        # Only room1 shows up because we only have a room subscription for room1 now.
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id1},
            exact=True,
        )

        # Make an incremental Sliding Sync request for just room2 now
        response_body, from_token = self.do_sync(
            {
                **sync_body,
                "room_subscriptions": {
                    room_id2: {
                        "required_state": [],
                        "timeline_limit": 0,
                    },
                },
            },
            since=from_token,
            tok=user1_tok,
        )

        # Only room2 shows up because we only have a room subscription for room2 now.
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id2},
            exact=True,
        )

        self.assertIsNotNone(response_body["extensions"]["account_data"].get("global"))
        # Check for room account data for room2
        self.assertIncludes(
            response_body["extensions"]["account_data"].get("rooms").keys(),
            {room_id2},
            exact=True,
        )
        # We should see any room account data updates for room2 since the last
        # time we saw it down sync
        account_data_map = {
            event["type"]: event["content"]
            for event in response_body["extensions"]["account_data"]
            .get("rooms")
            .get(room_id2)
        }
        self.assertIncludes(
            account_data_map.keys(),
            {"org.matrix.roorarraz2", AccountDataTypes.TAG},
            exact=True,
        )
        self.assertEqual(account_data_map["org.matrix.roorarraz2"], {"roo": "rar"})
        if tag_action == TagAction.ADD:
            self.assertEqual(
                account_data_map[AccountDataTypes.TAG],
                {"tags": {"m.favourite": {}, "m.server_notice": {}}},
            )
        elif tag_action == TagAction.REMOVE:
            # If we previously showed the client that the room has tags, when it no
            # longer has tags, we need to show them an empty map.
            self.assertEqual(
                account_data_map[AccountDataTypes.TAG],
                {"tags": {}},
            )
        else:
            assert_never(tag_action)

    def test_wait_for_new_data(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive.

        (Only applies to incremental syncs with a `timeout` specified)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the account_data extension enabled
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?timeout=10000&pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Bump the global account data to trigger new results
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user1_id,
                "org.matrix.foobarbaz",
                {"foo": "bar"},
            )
        )
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see the global account data update
        self.assertIncludes(
            {
                global_event["type"]
                for global_event in channel.json_body["extensions"]["account_data"].get(
                    "global"
                )
            },
            {"org.matrix.foobarbaz"},
            exact=True,
        )
        self.assertIncludes(
            channel.json_body["extensions"]["account_data"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        from the account_data extension doesn't trigger a false-positive for new data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "account_data": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?timeout=10000&pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Wake-up `notifier.wait_for_events(...)` that will cause us test
        # `SlidingSyncResult.__bool__` for new results.
        self._bump_notifier_wait_for_events(
            user1_id,
            # We choose `StreamKeyType.PRESENCE` because we're testing for account data
            # and don't want to contaminate the account data results using
            # `StreamKeyType.ACCOUNT_DATA`.
            wake_stream_key=StreamKeyType.PRESENCE,
        )
        # Block for a little bit more to ensure we don't see any new results.
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=4000)
        # Wait for the sync to complete (wait for the rest of the 10 second timeout,
        # 5000 + 4000 + 1200 > 10000)
        channel.await_result(timeout_ms=1200)
        self.assertEqual(channel.code, 200, channel.json_body)

        self.assertIsNotNone(
            channel.json_body["extensions"]["account_data"].get("global")
        )
        self.assertIsNotNone(
            channel.json_body["extensions"]["account_data"].get("rooms")
        )
