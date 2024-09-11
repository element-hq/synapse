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
from typing import Dict, List

from parameterized import parameterized_class

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import (
    AccountDataTypes,
    EventContentFields,
    EventTypes,
    RoomTypes,
)
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.util import Clock

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
class SlidingSyncFiltersTestCase(SlidingSyncBase):
    """
    Test `filters` in the Sliding Sync API to make sure it includes/excludes rooms
    correctly.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.storage_controllers = hs.get_storage_controllers()
        self.account_data_handler = hs.get_account_data_handler()

        super().prepare(reactor, clock, hs)

    def _add_new_dm_to_global_account_data(
        self, source_user_id: str, target_user_id: str, target_room_id: str
    ) -> None:
        """
        Helper to handle inserting a new DM for the source user into global account data
        (handles all of the list merging).

        Args:
            source_user_id: The user ID of the DM mapping we're going to update
            target_user_id: User ID of the person the DM is with
            target_room_id: Room ID of the DM
        """

        # Get the current DM map
        existing_dm_map = self.get_success(
            self.store.get_global_account_data_by_type_for_user(
                source_user_id, AccountDataTypes.DIRECT
            )
        )
        # Scrutinize the account data since it has no concrete type. We're just copying
        # everything into a known type. It should be a mapping from user ID to a list of
        # room IDs. Ignore anything else.
        new_dm_map: Dict[str, List[str]] = {}
        if isinstance(existing_dm_map, dict):
            for user_id, room_ids in existing_dm_map.items():
                if isinstance(user_id, str) and isinstance(room_ids, list):
                    for room_id in room_ids:
                        if isinstance(room_id, str):
                            new_dm_map[user_id] = new_dm_map.get(user_id, []) + [
                                room_id
                            ]

        # Add the new DM to the map
        new_dm_map[target_user_id] = new_dm_map.get(target_user_id, []) + [
            target_room_id
        ]
        # Save the DM map to global account data
        self.get_success(
            self.store.add_account_data_for_user(
                source_user_id,
                AccountDataTypes.DIRECT,
                new_dm_map,
            )
        )

    def _create_dm_room(
        self,
        inviter_user_id: str,
        inviter_tok: str,
        invitee_user_id: str,
        invitee_tok: str,
        should_join_room: bool = True,
    ) -> str:
        """
        Helper to create a DM room as the "inviter" and invite the "invitee" user to the
        room. The "invitee" user also will join the room. The `m.direct` account data
        will be set for both users.
        """

        # Create a room and send an invite the other user
        room_id = self.helper.create_room_as(
            inviter_user_id,
            is_public=False,
            tok=inviter_tok,
        )
        self.helper.invite(
            room_id,
            src=inviter_user_id,
            targ=invitee_user_id,
            tok=inviter_tok,
            extra_data={"is_direct": True},
        )
        if should_join_room:
            # Person that was invited joins the room
            self.helper.join(room_id, invitee_user_id, tok=invitee_tok)

        # Mimic the client setting the room as a direct message in the global account
        # data for both users.
        self._add_new_dm_to_global_account_data(
            invitee_user_id, inviter_user_id, room_id
        )
        self._add_new_dm_to_global_account_data(
            inviter_user_id, invitee_user_id, room_id
        )

        return room_id

    def test_filter_list(self) -> None:
        """
        Test that filters apply to `lists`
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a DM room
        joined_dm_room_id = self._create_dm_room(
            inviter_user_id=user1_id,
            inviter_tok=user1_tok,
            invitee_user_id=user2_id,
            invitee_tok=user2_tok,
            should_join_room=True,
        )
        invited_dm_room_id = self._create_dm_room(
            inviter_user_id=user1_id,
            inviter_tok=user1_tok,
            invitee_user_id=user2_id,
            invitee_tok=user2_tok,
            should_join_room=False,
        )

        # Create a normal room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Create a room that user1 is invited to
        invite_room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.invite(invite_room_id, src=user2_id, targ=user1_id, tok=user2_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                # Absense of filters does not imply "False" values
                "all": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 1,
                    "filters": {},
                },
                # Test single truthy filter
                "dms": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 1,
                    "filters": {"is_dm": True},
                },
                # Test single falsy filter
                "non-dms": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 1,
                    "filters": {"is_dm": False},
                },
                # Test how multiple filters should stack (AND'd together)
                "room-invites": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 1,
                    "filters": {"is_dm": False, "is_invite": True},
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(response_body["lists"].keys()),
            ["all", "dms", "non-dms", "room-invites"],
            response_body["lists"].keys(),
        )

        # Make sure the lists have the correct rooms
        self.assertListEqual(
            list(response_body["lists"]["all"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [
                        invite_room_id,
                        room_id,
                        invited_dm_room_id,
                        joined_dm_room_id,
                    ],
                }
            ],
            list(response_body["lists"]["all"]),
        )
        self.assertListEqual(
            list(response_body["lists"]["dms"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [invited_dm_room_id, joined_dm_room_id],
                }
            ],
            list(response_body["lists"]["dms"]),
        )
        self.assertListEqual(
            list(response_body["lists"]["non-dms"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [invite_room_id, room_id],
                }
            ],
            list(response_body["lists"]["non-dms"]),
        )
        self.assertListEqual(
            list(response_body["lists"]["room-invites"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [invite_room_id],
                }
            ],
            list(response_body["lists"]["room-invites"]),
        )

        # Ensure DM's are correctly marked
        self.assertDictEqual(
            {
                room_id: room.get("is_dm")
                for room_id, room in response_body["rooms"].items()
            },
            {
                invite_room_id: None,
                room_id: None,
                invited_dm_room_id: True,
                joined_dm_room_id: True,
            },
        )

    def test_filter_regardless_of_membership_server_left_room(self) -> None:
        """
        Test that filters apply to rooms regardless of membership. We're also
        compounding the problem by having all of the local users leave the room causing
        our server to leave the room.

        We want to make sure that if someone is filtering rooms, and leaves, you still
        get that final update down sync that you left.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a normal room
        room_id = self.helper.create_room_as(user1_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Create an encrypted space room
        space_room_id = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        self.helper.send_state(
            space_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )
        self.helper.join(space_room_id, user1_id, tok=user1_tok)

        # Make an initial Sliding Sync request
        sync_body = {
            "lists": {
                "all-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {},
                },
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 1,
                    "filters": {
                        "is_encrypted": True,
                        "room_types": [RoomTypes.SPACE],
                    },
                },
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make sure the response has the lists we requested
        self.assertListEqual(
            list(response_body["lists"].keys()),
            ["all-list", "foo-list"],
            response_body["lists"].keys(),
        )

        # Make sure the lists have the correct rooms
        self.assertListEqual(
            list(response_body["lists"]["all-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [space_room_id, room_id],
                }
            ],
        )
        self.assertListEqual(
            list(response_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [space_room_id],
                }
            ],
        )

        # Everyone leaves the encrypted space room
        self.helper.leave(space_room_id, user1_id, tok=user1_tok)
        self.helper.leave(space_room_id, user2_id, tok=user2_tok)

        # Make an incremental Sliding Sync request
        sync_body = {
            "lists": {
                "all-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {},
                },
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 1,
                    "filters": {
                        "is_encrypted": True,
                        "room_types": [RoomTypes.SPACE],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Make sure the lists have the correct rooms even though we `newly_left`
        self.assertListEqual(
            list(response_body["lists"]["all-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [space_room_id, room_id],
                }
            ],
        )
        self.assertListEqual(
            list(response_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [space_room_id],
                }
            ],
        )

    def test_filter_is_encrypted_up_to_date(self) -> None:
        """
        Make sure we get up-to-date `is_encrypted` status for a joined room
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_encrypted": True,
                    },
                },
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            set(),
            exact=True,
        )

        # Update the encryption status
        self.helper.send_state(
            room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )

        # We should see the room now because it's encrypted
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )
