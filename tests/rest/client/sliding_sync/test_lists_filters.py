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

from parameterized import parameterized_class

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    RoomTypes,
)
from synapse.api.room_versions import RoomVersions
from synapse.events import StrippedStateEvent
from synapse.rest.client import login, room, sync, tags
from synapse.server import HomeServer
from synapse.types import JsonDict
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
        tags.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.storage_controllers = hs.get_storage_controllers()
        self.account_data_handler = hs.get_account_data_handler()

        super().prepare(reactor, clock, hs)

    def test_multiple_filters_and_multiple_lists(self) -> None:
        """
        Test that filters apply to `lists` in various scenarios.
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
                # Absence of filters does not imply "False" values
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

        # Make sure it has the lists we requested
        self.assertIncludes(
            response_body["lists"].keys(),
            {"all", "dms", "non-dms", "room-invites"},
            exact=True,
        )

        # Make sure the lists have the correct rooms
        self.assertIncludes(
            set(response_body["lists"]["all"]["ops"][0]["room_ids"]),
            {
                invite_room_id,
                room_id,
                invited_dm_room_id,
                joined_dm_room_id,
            },
            exact=True,
        )
        self.assertIncludes(
            set(response_body["lists"]["dms"]["ops"][0]["room_ids"]),
            {invited_dm_room_id, joined_dm_room_id},
            exact=True,
        )
        self.assertIncludes(
            set(response_body["lists"]["non-dms"]["ops"][0]["room_ids"]),
            {invite_room_id, room_id},
            exact=True,
        )
        self.assertIncludes(
            set(response_body["lists"]["room-invites"]["ops"][0]["room_ids"]),
            {invite_room_id},
            exact=True,
        )

    def test_filters_regardless_of_membership_server_left_room(self) -> None:
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
        self.assertIncludes(
            response_body["lists"].keys(),
            {"all-list", "foo-list"},
        )

        # Make sure the lists have the correct rooms
        self.assertIncludes(
            set(response_body["lists"]["all-list"]["ops"][0]["room_ids"]),
            {space_room_id, room_id},
            exact=True,
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id},
            exact=True,
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

        # Make sure the response has the lists we requested
        self.assertIncludes(
            response_body["lists"].keys(),
            {"all-list", "foo-list"},
            exact=True,
        )

        # Make sure the lists have the correct rooms even though we `newly_left`
        self.assertIncludes(
            set(response_body["lists"]["all-list"]["ops"][0]["room_ids"]),
            {space_room_id, room_id},
            exact=True,
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id},
            exact=True,
        )

    def test_filters_is_dm(self) -> None:
        """
        Test `filter.is_dm` for DM rooms
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a normal room
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create a DM room
        dm_room_id = self._create_dm_room(
            inviter_user_id=user1_id,
            inviter_tok=user1_tok,
            invitee_user_id=user2_id,
            invitee_tok=user2_tok,
        )

        # Try with `is_dm=True`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_dm": True,
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {dm_room_id},
            exact=True,
        )

        # Try with `is_dm=False`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_dm": False,
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

    def test_filters_is_encrypted(self) -> None:
        """
        Test `filters.is_encrypted` for encrypted rooms
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create an unencrypted room
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create an encrypted room
        encrypted_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.send_state(
            encrypted_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )

        # Try with `is_encrypted=True`
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
        # No rooms are encrypted yet
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {encrypted_room_id},
            exact=True,
        )

        # Try with `is_encrypted=False`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_encrypted": False,
                    },
                },
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        # No rooms are encrypted yet
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

    def test_filters_is_encrypted_server_left_room(self) -> None:
        """
        Test that we can apply a `filters.is_encrypted` against a room that everyone has left.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Get a token before we create any rooms
        sync_body: JsonDict = {
            "lists": {},
        }
        response_body, before_rooms_token = self.do_sync(sync_body, tok=user1_tok)

        # Create an unencrypted room
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Leave the room
        self.helper.leave(room_id, user1_id, tok=user1_tok)

        # Create an encrypted room
        encrypted_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.send_state(
            encrypted_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )
        # Leave the room
        self.helper.leave(encrypted_room_id, user1_id, tok=user1_tok)

        # Try with `is_encrypted=True`
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
        # Use an incremental sync so that the room is considered `newly_left` and shows
        # up down sync
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {encrypted_room_id},
            exact=True,
        )

        # Try with `is_encrypted=False`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_encrypted": False,
                    },
                },
            }
        }
        # Use an incremental sync so that the room is considered `newly_left` and shows
        # up down sync
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

    def test_filters_is_encrypted_server_left_room2(self) -> None:
        """
        Test that we can apply a `filters.is_encrypted` against a room that everyone has
        left.

        There is still someone local who is invited to the rooms but that doesn't affect
        whether the server is participating in the room (users need to be joined).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        _user2_tok = self.login(user2_id, "pass")

        # Get a token before we create any rooms
        sync_body: JsonDict = {
            "lists": {},
        }
        response_body, before_rooms_token = self.do_sync(sync_body, tok=user1_tok)

        # Create an unencrypted room
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Invite user2
        self.helper.invite(room_id, targ=user2_id, tok=user1_tok)
        # User1 leaves the room
        self.helper.leave(room_id, user1_id, tok=user1_tok)

        # Create an encrypted room
        encrypted_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.send_state(
            encrypted_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )
        # Invite user2
        self.helper.invite(encrypted_room_id, targ=user2_id, tok=user1_tok)
        # User1 leaves the room
        self.helper.leave(encrypted_room_id, user1_id, tok=user1_tok)

        # Try with `is_encrypted=True`
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
        # Use an incremental sync so that the room is considered `newly_left` and shows
        # up down sync
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {encrypted_room_id},
            exact=True,
        )

        # Try with `is_encrypted=False`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_encrypted": False,
                    },
                },
            }
        }
        # Use an incremental sync so that the room is considered `newly_left` and shows
        # up down sync
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

    def test_filters_is_encrypted_after_we_left(self) -> None:
        """
        Test that we can apply a `filters.is_encrypted` against a room that was encrypted
        after we left the room (make sure we don't just use the current state)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Get a token before we create any rooms
        sync_body: JsonDict = {
            "lists": {},
        }
        response_body, before_rooms_token = self.do_sync(sync_body, tok=user1_tok)

        # Create an unencrypted room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Leave the room
        self.helper.join(room_id, user1_id, tok=user1_tok)
        self.helper.leave(room_id, user1_id, tok=user1_tok)

        # Create a room that will be encrypted
        encrypted_after_we_left_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok
        )
        # Leave the room
        self.helper.join(encrypted_after_we_left_room_id, user1_id, tok=user1_tok)
        self.helper.leave(encrypted_after_we_left_room_id, user1_id, tok=user1_tok)

        # Encrypt the room after we've left
        self.helper.send_state(
            encrypted_after_we_left_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )

        # Try with `is_encrypted=True`
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
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        if self.use_new_tables:
            self.assertIncludes(
                set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
                set(),
                exact=True,
            )
        else:
            # Even though we left the room before it was encrypted, we still see it because
            # someone else on our server is still participating in the room and we "leak"
            # the current state to the left user. But we consider the room encryption status
            # to not be a secret given it's often set at the start of the room and it's one
            # of the stripped state events that is normally handed out.
            self.assertIncludes(
                set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
                {encrypted_after_we_left_room_id},
                exact=True,
            )

        # Try with `is_encrypted=False`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_encrypted": False,
                    },
                },
            }
        }
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        if self.use_new_tables:
            self.assertIncludes(
                set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
                {room_id, encrypted_after_we_left_room_id},
                exact=True,
            )
        else:
            # Even though we left the room before it was encrypted... (see comment above)
            self.assertIncludes(
                set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
                {room_id},
                exact=True,
            )

    def test_filters_is_encrypted_with_remote_invite_room_no_stripped_state(
        self,
    ) -> None:
        """
        Test that we can apply a `filters.is_encrypted` filter against a remote invite
        room without any `unsigned.invite_room_state` (stripped state).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room without any `unsigned.invite_room_state`
        _remote_invite_room_id = self._create_remote_invite_room_for_user(
            user1_id, None
        )

        # Create an unencrypted room
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create an encrypted room
        encrypted_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.send_state(
            encrypted_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )

        # Try with `is_encrypted=True`
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
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # `remote_invite_room_id` should not appear because we can't figure out whether
        # it is encrypted or not (no stripped state, `unsigned.invite_room_state`).
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {encrypted_room_id},
            exact=True,
        )

        # Try with `is_encrypted=False`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_encrypted": False,
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # `remote_invite_room_id` should not appear because we can't figure out whether
        # it is encrypted or not (no stripped state, `unsigned.invite_room_state`).
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

    def test_filters_is_encrypted_with_remote_invite_encrypted_room(self) -> None:
        """
        Test that we can apply a `filters.is_encrypted` filter against a remote invite
        encrypted room with some `unsigned.invite_room_state` (stripped state).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room with some `unsigned.invite_room_state`
        # indicating that the room is encrypted.
        remote_invite_room_id = self._create_remote_invite_room_for_user(
            user1_id,
            [
                StrippedStateEvent(
                    type=EventTypes.Create,
                    state_key="",
                    sender="@inviter:remote_server",
                    content={
                        EventContentFields.ROOM_CREATOR: "@inviter:remote_server",
                        EventContentFields.ROOM_VERSION: RoomVersions.V10.identifier,
                    },
                ),
                StrippedStateEvent(
                    type=EventTypes.RoomEncryption,
                    state_key="",
                    sender="@inviter:remote_server",
                    content={
                        EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2",
                    },
                ),
            ],
        )

        # Create an unencrypted room
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create an encrypted room
        encrypted_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.send_state(
            encrypted_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )

        # Try with `is_encrypted=True`
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
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # `remote_invite_room_id` should appear here because it is encrypted
        # according to the stripped state
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {encrypted_room_id, remote_invite_room_id},
            exact=True,
        )

        # Try with `is_encrypted=False`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_encrypted": False,
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # `remote_invite_room_id` should not appear here because it is encrypted
        # according to the stripped state
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

    def test_filters_is_encrypted_with_remote_invite_unencrypted_room(self) -> None:
        """
        Test that we can apply a `filters.is_encrypted` filter against a remote invite
        unencrypted room with some `unsigned.invite_room_state` (stripped state).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room with some `unsigned.invite_room_state`
        # but don't set any room encryption event.
        remote_invite_room_id = self._create_remote_invite_room_for_user(
            user1_id,
            [
                StrippedStateEvent(
                    type=EventTypes.Create,
                    state_key="",
                    sender="@inviter:remote_server",
                    content={
                        EventContentFields.ROOM_CREATOR: "@inviter:remote_server",
                        EventContentFields.ROOM_VERSION: RoomVersions.V10.identifier,
                    },
                ),
                # No room encryption event
            ],
        )

        # Create an unencrypted room
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create an encrypted room
        encrypted_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.send_state(
            encrypted_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )

        # Try with `is_encrypted=True`
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
        # `remote_invite_room_id` should not appear here because it is unencrypted
        # according to the stripped state
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {encrypted_room_id},
            exact=True,
        )

        # Try with `is_encrypted=False`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_encrypted": False,
                    },
                },
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        # `remote_invite_room_id` should appear because it is unencrypted according to
        # the stripped state
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id, remote_invite_room_id},
            exact=True,
        )

    def test_filters_is_encrypted_updated(self) -> None:
        """
        Make sure we get rooms if the encrypted room status is updated for a joined room
        (`filters.is_encrypted`)
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

        # No rooms are encrypted yet
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

    def test_filters_is_invite_rooms(self) -> None:
        """
        Test `filters.is_invite` for rooms that the user has been invited to
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a normal room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Create a room that user1 is invited to
        invite_room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.invite(invite_room_id, src=user2_id, targ=user1_id, tok=user2_tok)

        # Try with `is_invite=True`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_invite": True,
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {invite_room_id},
            exact=True,
        )

        # Try with `is_invite=False`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "is_invite": False,
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

    def test_filters_room_types(self) -> None:
        """
        Test `filters.room_types` for different room types
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a normal room (no room type)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create a space room
        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )

        # Create an arbitrarily typed room
        foo_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {
                    EventContentFields.ROOM_TYPE: "org.matrix.foobarbaz"
                }
            },
        )

        # Try finding only normal rooms
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [None],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

        # Try finding only spaces
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [RoomTypes.SPACE],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id},
            exact=True,
        )

        # Try finding normal rooms and spaces
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [None, RoomTypes.SPACE],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id, space_room_id},
            exact=True,
        )

        # Try finding an arbitrary room type
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": ["org.matrix.foobarbaz"],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {foo_room_id},
            exact=True,
        )

        # Just make sure we know what happens when you specify an empty list of room_types
        # (we should find nothing)
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            set(),
            exact=True,
        )

    def test_filters_not_room_types(self) -> None:
        """
        Test `filters.not_room_types` for different room types
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a normal room (no room type)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create a space room
        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )

        # Create an arbitrarily typed room
        foo_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {
                    EventContentFields.ROOM_TYPE: "org.matrix.foobarbaz"
                }
            },
        )

        # Try finding *NOT* normal rooms
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "not_room_types": [None],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id, foo_room_id},
            exact=True,
        )

        # Try finding *NOT* spaces
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "not_room_types": [RoomTypes.SPACE],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id, foo_room_id},
            exact=True,
        )

        # Try finding *NOT* normal rooms or spaces
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "not_room_types": [None, RoomTypes.SPACE],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {foo_room_id},
            exact=True,
        )

        # Test how it behaves when we have both `room_types` and `not_room_types`.
        # `not_room_types` should win.
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [None],
                        "not_room_types": [None],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # Nothing matches because nothing is both a normal room and not a normal room
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            set(),
            exact=True,
        )

        # Test how it behaves when we have both `room_types` and `not_room_types`.
        # `not_room_types` should win.
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [None, RoomTypes.SPACE],
                        "not_room_types": [None],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id},
            exact=True,
        )

        # Just make sure we know what happens when you specify an empty list of not_room_types
        # (we should find all of the rooms)
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "not_room_types": [],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id, foo_room_id, space_room_id},
            exact=True,
        )

    def test_filters_room_types_server_left_room(self) -> None:
        """
        Test that we can apply a `filters.room_types` against a room that everyone has left.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Get a token before we create any rooms
        sync_body: JsonDict = {
            "lists": {},
        }
        response_body, before_rooms_token = self.do_sync(sync_body, tok=user1_tok)

        # Create a normal room (no room type)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Leave the room
        self.helper.leave(room_id, user1_id, tok=user1_tok)

        # Create a space room
        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Leave the room
        self.helper.leave(space_room_id, user1_id, tok=user1_tok)

        # Try finding only normal rooms
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [None],
                    },
                },
            }
        }
        # Use an incremental sync so that the room is considered `newly_left` and shows
        # up down sync
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

        # Try finding only spaces
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [RoomTypes.SPACE],
                    },
                },
            }
        }
        # Use an incremental sync so that the room is considered `newly_left` and shows
        # up down sync
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id},
            exact=True,
        )

    def test_filter_room_types_server_left_room2(self) -> None:
        """
        Test that we can apply a `filter.room_types` against a room that everyone has left.

        There is still someone local who is invited to the rooms but that doesn't affect
        whether the server is participating in the room (users need to be joined).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        _user2_tok = self.login(user2_id, "pass")

        # Get a token before we create any rooms
        sync_body: JsonDict = {
            "lists": {},
        }
        response_body, before_rooms_token = self.do_sync(sync_body, tok=user1_tok)

        # Create a normal room (no room type)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Invite user2
        self.helper.invite(room_id, targ=user2_id, tok=user1_tok)
        # User1 leaves the room
        self.helper.leave(room_id, user1_id, tok=user1_tok)

        # Create a space room
        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Invite user2
        self.helper.invite(space_room_id, targ=user2_id, tok=user1_tok)
        # User1 leaves the room
        self.helper.leave(space_room_id, user1_id, tok=user1_tok)

        # Try finding only normal rooms
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [None],
                    },
                },
            }
        }
        # Use an incremental sync so that the room is considered `newly_left` and shows
        # up down sync
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

        # Try finding only spaces
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [RoomTypes.SPACE],
                    },
                },
            }
        }
        # Use an incremental sync so that the room is considered `newly_left` and shows
        # up down sync
        response_body, _ = self.do_sync(
            sync_body, since=before_rooms_token, tok=user1_tok
        )
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id},
            exact=True,
        )

    def test_filters_room_types_with_remote_invite_room_no_stripped_state(self) -> None:
        """
        Test that we can apply a `filters.room_types` filter against a remote invite
        room without any `unsigned.invite_room_state` (stripped state).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room without any `unsigned.invite_room_state`
        _remote_invite_room_id = self._create_remote_invite_room_for_user(
            user1_id, None
        )

        # Create a normal room (no room type)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create a space room
        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )

        # Try finding only normal rooms
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [None],
                    },
                },
            }
        }
        # `remote_invite_room_id` should not appear because we can't figure out what
        # room type it is (no stripped state, `unsigned.invite_room_state`)
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

        # Try finding only spaces
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [RoomTypes.SPACE],
                    },
                },
            }
        }
        # `remote_invite_room_id` should not appear because we can't figure out what
        # room type it is (no stripped state, `unsigned.invite_room_state`)
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id},
            exact=True,
        )

    def test_filters_room_types_with_remote_invite_space(self) -> None:
        """
        Test that we can apply a `filters.room_types` filter against a remote invite
        to a space room with some `unsigned.invite_room_state` (stripped state).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room with some `unsigned.invite_room_state` indicating
        # that it is a space room
        remote_invite_room_id = self._create_remote_invite_room_for_user(
            user1_id,
            [
                StrippedStateEvent(
                    type=EventTypes.Create,
                    state_key="",
                    sender="@inviter:remote_server",
                    content={
                        EventContentFields.ROOM_CREATOR: "@inviter:remote_server",
                        EventContentFields.ROOM_VERSION: RoomVersions.V10.identifier,
                        # Specify that it is a space room
                        EventContentFields.ROOM_TYPE: RoomTypes.SPACE,
                    },
                ),
            ],
        )

        # Create a normal room (no room type)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create a space room
        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )

        # Try finding only normal rooms
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [None],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # `remote_invite_room_id` should not appear here because it is a space room
        # according to the stripped state
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

        # Try finding only spaces
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [RoomTypes.SPACE],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # `remote_invite_room_id` should appear here because it is a space room
        # according to the stripped state
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id, remote_invite_room_id},
            exact=True,
        )

    def test_filters_room_types_with_remote_invite_normal_room(self) -> None:
        """
        Test that we can apply a `filters.room_types` filter against a remote invite
        to a normal room with some `unsigned.invite_room_state` (stripped state).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room with some `unsigned.invite_room_state`
        # but the create event does not specify a room type (normal room)
        remote_invite_room_id = self._create_remote_invite_room_for_user(
            user1_id,
            [
                StrippedStateEvent(
                    type=EventTypes.Create,
                    state_key="",
                    sender="@inviter:remote_server",
                    content={
                        EventContentFields.ROOM_CREATOR: "@inviter:remote_server",
                        EventContentFields.ROOM_VERSION: RoomVersions.V10.identifier,
                        # No room type means this is a normal room
                    },
                ),
            ],
        )

        # Create a normal room (no room type)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create a space room
        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )

        # Try finding only normal rooms
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [None],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # `remote_invite_room_id` should appear here because it is a normal room
        # according to the stripped state (no room type)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id, remote_invite_room_id},
            exact=True,
        )

        # Try finding only spaces
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "room_types": [RoomTypes.SPACE],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # `remote_invite_room_id` should not appear here because it is a normal room
        # according to the stripped state (no room type)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {space_room_id},
            exact=True,
        )

    def _add_tag_to_room(
        self, *, room_id: str, user_id: str, access_token: str, tag_name: str
    ) -> None:
        channel = self.make_request(
            method="PUT",
            path=f"/user/{user_id}/rooms/{room_id}/tags/{tag_name}",
            content={},
            access_token=access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def test_filters_tags(self) -> None:
        """
        Test `filters.tags` for rooms with given tags
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room with no tags
        self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create some rooms with tags
        foo_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        bar_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Create a room without multiple tags
        foobar_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Add the "foo" tag to the foo room
        self._add_tag_to_room(
            room_id=foo_room_id,
            user_id=user1_id,
            access_token=user1_tok,
            tag_name="foo",
        )
        # Add the "bar" tag to the bar room
        self._add_tag_to_room(
            room_id=bar_room_id,
            user_id=user1_id,
            access_token=user1_tok,
            tag_name="bar",
        )
        # Add both "foo" and "bar" tags to the foobar room
        self._add_tag_to_room(
            room_id=foobar_room_id,
            user_id=user1_id,
            access_token=user1_tok,
            tag_name="foo",
        )
        self._add_tag_to_room(
            room_id=foobar_room_id,
            user_id=user1_id,
            access_token=user1_tok,
            tag_name="bar",
        )

        # Try finding rooms with the "foo" tag
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "tags": ["foo"],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {foo_room_id, foobar_room_id},
            exact=True,
        )

        # Try finding rooms with either "foo" or "bar" tags
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "tags": ["foo", "bar"],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {foo_room_id, bar_room_id, foobar_room_id},
            exact=True,
        )

        # Try with a random tag we didn't add
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "tags": ["flomp"],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # No rooms should match
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            set(),
            exact=True,
        )

        # Just make sure we know what happens when you specify an empty list of tags
        # (we should find nothing)
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "tags": [],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            set(),
            exact=True,
        )

    def test_filters_not_tags(self) -> None:
        """
        Test `filters.not_tags` for excluding rooms with given tags
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room with no tags
        untagged_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create some rooms with tags
        foo_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        bar_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Create a room without multiple tags
        foobar_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Add the "foo" tag to the foo room
        self._add_tag_to_room(
            room_id=foo_room_id,
            user_id=user1_id,
            access_token=user1_tok,
            tag_name="foo",
        )
        # Add the "bar" tag to the bar room
        self._add_tag_to_room(
            room_id=bar_room_id,
            user_id=user1_id,
            access_token=user1_tok,
            tag_name="bar",
        )
        # Add both "foo" and "bar" tags to the foobar room
        self._add_tag_to_room(
            room_id=foobar_room_id,
            user_id=user1_id,
            access_token=user1_tok,
            tag_name="foo",
        )
        self._add_tag_to_room(
            room_id=foobar_room_id,
            user_id=user1_id,
            access_token=user1_tok,
            tag_name="bar",
        )

        # Try finding rooms without the "foo" tag
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "not_tags": ["foo"],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {untagged_room_id, bar_room_id},
            exact=True,
        )

        # Try finding rooms without either "foo" or "bar" tags
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "not_tags": ["foo", "bar"],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {untagged_room_id},
            exact=True,
        )

        # Test how it behaves when we have both `tags` and `not_tags`.
        # `not_tags` should win.
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "tags": ["foo"],
                        "not_tags": ["foo"],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # Nothing matches because nothing is both tagged with "foo" and not tagged with "foo"
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            set(),
            exact=True,
        )

        # Just make sure we know what happens when you specify an empty list of not_tags
        # (we should find all of the rooms)
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {
                        "not_tags": [],
                    },
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {untagged_room_id, foo_room_id, bar_room_id, foobar_room_id},
            exact=True,
        )
