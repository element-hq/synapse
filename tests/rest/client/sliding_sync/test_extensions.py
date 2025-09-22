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
import logging
from typing import Literal

from parameterized import parameterized, parameterized_class
from typing_extensions import assert_never

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import ReceiptTypes
from synapse.rest.client import login, receipts, room, sync
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

logger = logging.getLogger(__name__)


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
class SlidingSyncExtensionsTestCase(SlidingSyncBase):
    """
    Test general extensions behavior in the Sliding Sync API. Each extension has their
    own suite of tests in their own file as well.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        receipts.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        self.account_data_handler = hs.get_account_data_handler()

        super().prepare(reactor, clock, hs)

    # Any extensions that use `lists`/`rooms` should be tested here
    @parameterized.expand([("account_data",), ("receipts",), ("typing",)])
    def test_extensions_lists_rooms_relevant_rooms(
        self,
        extension_name: Literal["account_data", "receipts", "typing"],
    ) -> None:
        """
        With various extensions, test out requesting different variations of
        `lists`/`rooms`.

        Stresses `SlidingSyncHandler.find_relevant_room_ids_for_extension(...)`
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create some rooms
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id3 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id4 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id5 = self.helper.create_room_as(user1_id, tok=user1_tok)

        room_id_to_human_name_map = {
            room_id1: "room1",
            room_id2: "room2",
            room_id3: "room3",
            room_id4: "room4",
            room_id5: "room5",
        }

        for room_id in room_id_to_human_name_map.keys():
            if extension_name == "account_data":
                # Add some account data to each room
                self.get_success(
                    self.account_data_handler.add_account_data_to_room(
                        user_id=user1_id,
                        room_id=room_id,
                        account_data_type="org.matrix.roorarraz",
                        content={"roo": "rar"},
                    )
                )
            elif extension_name == "receipts":
                event_response = self.helper.send(
                    room_id, body="new event", tok=user1_tok
                )
                # Read last event
                channel = self.make_request(
                    "POST",
                    f"/rooms/{room_id}/receipt/{ReceiptTypes.READ}/{event_response['event_id']}",
                    {},
                    access_token=user1_tok,
                )
                self.assertEqual(channel.code, 200, channel.json_body)
            elif extension_name == "typing":
                # Start a typing notification
                channel = self.make_request(
                    "PUT",
                    f"/rooms/{room_id}/typing/{user1_id}",
                    b'{"typing": true, "timeout": 30000}',
                    access_token=user1_tok,
                )
                self.assertEqual(channel.code, 200, channel.json_body)
            else:
                assert_never(extension_name)

        main_sync_body = {
            "lists": {
                # We expect this list range to include room5 and room4
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    # We set this to `1` because we're testing `receipts` which
                    # interact with the `timeline`. With receipts, when a room
                    # hasn't been sent down the connection before or it appears
                    # as `initial: true`, we only include receipts for events in
                    # the timeline to avoid bloating and blowing up the sync
                    # response as the number of users in the room increases.
                    # (this behavior is part of the spec)
                    "timeline_limit": 1,
                },
                # We expect this list range to include room5, room4, room3
                "bar-list": {
                    "ranges": [[0, 2]],
                    "required_state": [],
                    "timeline_limit": 1,
                },
            },
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    "timeline_limit": 1,
                }
            },
        }

        # Mix lists and rooms
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    "lists": ["foo-list", "non-existent-list"],
                    "rooms": [room_id1, room_id2, "!non-existent-room"],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ✅ Requested via `rooms` and a room subscription exists
        # room2: ❌ Requested via `rooms` but not in the response (from lists or room subscriptions)
        # room3: ❌ Not requested
        # room4: ✅ Shows up because requested via `lists` and list exists in the response
        # room5: ✅ Shows up because requested via `lists` and list exists in the response
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            {"room1", "room4", "room5"},
            exact=True,
        )

        # Try wildcards (this is the default)
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    # "lists": ["*"],
                    # "rooms": ["*"],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ✅ Shows up because of default `rooms` wildcard and is in one of the room subscriptions
        # room2: ❌ Not requested
        # room3: ✅ Shows up because of default `lists` wildcard and is in a list
        # room4: ✅ Shows up because of default `lists` wildcard and is in a list
        # room5: ✅ Shows up because of default `lists` wildcard and is in a list
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            {"room1", "room3", "room4", "room5"},
            exact=True,
        )

        # Empty list will return nothing
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    "lists": [],
                    "rooms": [],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ❌ Not requested
        # room2: ❌ Not requested
        # room3: ❌ Not requested
        # room4: ❌ Not requested
        # room5: ❌ Not requested
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            set(),
            exact=True,
        )

        # Try wildcard and none
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    "lists": ["*"],
                    "rooms": [],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ❌ Not requested
        # room2: ❌ Not requested
        # room3: ✅ Shows up because of default `lists` wildcard and is in a list
        # room4: ✅ Shows up because of default `lists` wildcard and is in a list
        # room5: ✅ Shows up because of default `lists` wildcard and is in a list
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            {"room3", "room4", "room5"},
            exact=True,
        )

        # Try requesting a room that is only in a list
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    "lists": [],
                    "rooms": [room_id5],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ❌ Not requested
        # room2: ❌ Not requested
        # room3: ❌ Not requested
        # room4: ❌ Not requested
        # room5: ✅ Requested via `rooms` and is in a list
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            {"room5"},
            exact=True,
        )
