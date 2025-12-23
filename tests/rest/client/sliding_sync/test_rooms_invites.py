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
from synapse.api.constants import EventTypes, HistoryVisibility
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import UserID
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
class SlidingSyncRoomsInvitesTestCase(SlidingSyncBase):
    """
    Test to make sure the `rooms` response looks good for invites in the Sliding Sync API.

    Invites behave a lot different than other rooms because we don't include the
    `timeline` (`num_live`, `limited`, `prev_batch`) or `required_state` in favor of
    some stripped state under the `invite_state` key.

    Knocks probably have the same behavior but the spec doesn't mention knocks yet.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()

        super().prepare(reactor, clock, hs)

    def test_rooms_invite_shared_history_initial_sync(self) -> None:
        """
        Test that `rooms` we are invited to have some stripped `invite_state` during an
        initial sync.

        This is an `invite` room so we should only have `stripped_state` (no `timeline`)
        but we also shouldn't see any timeline events because the history visiblity is
        `shared` and we haven't joined the room yet.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user1 = UserID.from_string(user1_id)
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user2 = UserID.from_string(user2_id)

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Ensure we're testing with a room with `shared` history visibility which means
        # history visible until you actually join the room.
        history_visibility_response = self.helper.get_state(
            room_id1, EventTypes.RoomHistoryVisibility, tok=user2_tok
        )
        self.assertEqual(
            history_visibility_response.get("history_visibility"),
            HistoryVisibility.SHARED,
        )

        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.send(room_id1, "activity before2", tok=user2_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.send(room_id1, "activity after3", tok=user2_tok)
        self.helper.send(room_id1, "activity after4", tok=user2_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # `timeline` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            response_body["rooms"][room_id1].get("timeline"),
            response_body["rooms"][room_id1],
        )
        # `num_live` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("num_live"),
            response_body["rooms"][room_id1],
        )
        # `limited` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("limited"),
            response_body["rooms"][room_id1],
        )
        # `prev_batch` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("prev_batch"),
            response_body["rooms"][room_id1],
        )
        # `required_state` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            response_body["rooms"][room_id1].get("required_state"),
            response_body["rooms"][room_id1],
        )
        # We should have some `stripped_state` so the potential joiner can identify the
        # room (we don't care about the order).
        self.assertCountEqual(
            response_body["rooms"][room_id1]["invite_state"],
            [
                {
                    "content": {"creator": user2_id, "room_version": "10"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.create",
                },
                {
                    "content": {"join_rule": "public"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.join_rules",
                },
                {
                    "content": {"displayname": user2.localpart, "membership": "join"},
                    "sender": user2_id,
                    "state_key": user2_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"displayname": user1.localpart, "membership": "invite"},
                    "sender": user2_id,
                    "state_key": user1_id,
                    "type": "m.room.member",
                },
            ],
            response_body["rooms"][room_id1]["invite_state"],
        )

    def test_rooms_invite_shared_history_incremental_sync(self) -> None:
        """
        Test that `rooms` we are invited to have some stripped `invite_state` during an
        incremental sync.

        This is an `invite` room so we should only have `stripped_state` (no `timeline`)
        but we also shouldn't see any timeline events because the history visiblity is
        `shared` and we haven't joined the room yet.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user1 = UserID.from_string(user1_id)
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user2 = UserID.from_string(user2_id)

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Ensure we're testing with a room with `shared` history visibility which means
        # history visible until you actually join the room.
        history_visibility_response = self.helper.get_state(
            room_id1, EventTypes.RoomHistoryVisibility, tok=user2_tok
        )
        self.assertEqual(
            history_visibility_response.get("history_visibility"),
            HistoryVisibility.SHARED,
        )

        self.helper.send(room_id1, "activity before invite1", tok=user2_tok)
        self.helper.send(room_id1, "activity before invite2", tok=user2_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.send(room_id1, "activity after invite3", tok=user2_tok)
        self.helper.send(room_id1, "activity after invite4", tok=user2_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 3,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        self.helper.send(room_id1, "activity after token5", tok=user2_tok)
        self.helper.send(room_id1, "activity after toekn6", tok=user2_tok)

        # Make the Sliding Sync request
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        # `timeline` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            response_body["rooms"][room_id1].get("timeline"),
            response_body["rooms"][room_id1],
        )
        # `num_live` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("num_live"),
            response_body["rooms"][room_id1],
        )
        # `limited` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("limited"),
            response_body["rooms"][room_id1],
        )
        # `prev_batch` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("prev_batch"),
            response_body["rooms"][room_id1],
        )
        # `required_state` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            response_body["rooms"][room_id1].get("required_state"),
            response_body["rooms"][room_id1],
        )
        # We should have some `stripped_state` so the potential joiner can identify the
        # room (we don't care about the order).
        self.assertCountEqual(
            response_body["rooms"][room_id1]["invite_state"],
            [
                {
                    "content": {"creator": user2_id, "room_version": "10"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.create",
                },
                {
                    "content": {"join_rule": "public"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.join_rules",
                },
                {
                    "content": {"displayname": user2.localpart, "membership": "join"},
                    "sender": user2_id,
                    "state_key": user2_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"displayname": user1.localpart, "membership": "invite"},
                    "sender": user2_id,
                    "state_key": user1_id,
                    "type": "m.room.member",
                },
            ],
            response_body["rooms"][room_id1]["invite_state"],
        )

    def test_rooms_invite_world_readable_history_initial_sync(self) -> None:
        """
        Test that `rooms` we are invited to have some stripped `invite_state` during an
        initial sync.

        This is an `invite` room so we should only have `stripped_state` (no `timeline`)
        but depending on the semantics we decide, we could potentially see some
        historical events before/after the `from_token` because the history is
        `world_readable`. Same situation for events after the `from_token` if the
        history visibility was set to `invited`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user1 = UserID.from_string(user1_id)
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user2 = UserID.from_string(user2_id)

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "preset": "public_chat",
                "initial_state": [
                    {
                        "content": {
                            "history_visibility": HistoryVisibility.WORLD_READABLE
                        },
                        "state_key": "",
                        "type": EventTypes.RoomHistoryVisibility,
                    }
                ],
            },
        )
        # Ensure we're testing with a room with `world_readable` history visibility
        # which means events are visible to anyone even without membership.
        history_visibility_response = self.helper.get_state(
            room_id1, EventTypes.RoomHistoryVisibility, tok=user2_tok
        )
        self.assertEqual(
            history_visibility_response.get("history_visibility"),
            HistoryVisibility.WORLD_READABLE,
        )

        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.send(room_id1, "activity before2", tok=user2_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.send(room_id1, "activity after3", tok=user2_tok)
        self.helper.send(room_id1, "activity after4", tok=user2_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    # Large enough to see the latest events and before the invite
                    "timeline_limit": 4,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # `timeline` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            response_body["rooms"][room_id1].get("timeline"),
            response_body["rooms"][room_id1],
        )
        # `num_live` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("num_live"),
            response_body["rooms"][room_id1],
        )
        # `limited` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("limited"),
            response_body["rooms"][room_id1],
        )
        # `prev_batch` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("prev_batch"),
            response_body["rooms"][room_id1],
        )
        # `required_state` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            response_body["rooms"][room_id1].get("required_state"),
            response_body["rooms"][room_id1],
        )
        # We should have some `stripped_state` so the potential joiner can identify the
        # room (we don't care about the order).
        self.assertCountEqual(
            response_body["rooms"][room_id1]["invite_state"],
            [
                {
                    "content": {"creator": user2_id, "room_version": "10"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.create",
                },
                {
                    "content": {"join_rule": "public"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.join_rules",
                },
                {
                    "content": {"displayname": user2.localpart, "membership": "join"},
                    "sender": user2_id,
                    "state_key": user2_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"displayname": user1.localpart, "membership": "invite"},
                    "sender": user2_id,
                    "state_key": user1_id,
                    "type": "m.room.member",
                },
            ],
            response_body["rooms"][room_id1]["invite_state"],
        )

    def test_rooms_invite_world_readable_history_incremental_sync(self) -> None:
        """
        Test that `rooms` we are invited to have some stripped `invite_state` during an
        incremental sync.

        This is an `invite` room so we should only have `stripped_state` (no `timeline`)
        but depending on the semantics we decide, we could potentially see some
        historical events before/after the `from_token` because the history is
        `world_readable`. Same situation for events after the `from_token` if the
        history visibility was set to `invited`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user1 = UserID.from_string(user1_id)
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user2 = UserID.from_string(user2_id)

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "preset": "public_chat",
                "initial_state": [
                    {
                        "content": {
                            "history_visibility": HistoryVisibility.WORLD_READABLE
                        },
                        "state_key": "",
                        "type": EventTypes.RoomHistoryVisibility,
                    }
                ],
            },
        )
        # Ensure we're testing with a room with `world_readable` history visibility
        # which means events are visible to anyone even without membership.
        history_visibility_response = self.helper.get_state(
            room_id1, EventTypes.RoomHistoryVisibility, tok=user2_tok
        )
        self.assertEqual(
            history_visibility_response.get("history_visibility"),
            HistoryVisibility.WORLD_READABLE,
        )

        self.helper.send(room_id1, "activity before invite1", tok=user2_tok)
        self.helper.send(room_id1, "activity before invite2", tok=user2_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.send(room_id1, "activity after invite3", tok=user2_tok)
        self.helper.send(room_id1, "activity after invite4", tok=user2_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    # Large enough to see the latest events and before the invite
                    "timeline_limit": 4,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        self.helper.send(room_id1, "activity after token5", tok=user2_tok)
        self.helper.send(room_id1, "activity after toekn6", tok=user2_tok)

        # Make the incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # `timeline` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            response_body["rooms"][room_id1].get("timeline"),
            response_body["rooms"][room_id1],
        )
        # `num_live` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("num_live"),
            response_body["rooms"][room_id1],
        )
        # `limited` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("limited"),
            response_body["rooms"][room_id1],
        )
        # `prev_batch` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("prev_batch"),
            response_body["rooms"][room_id1],
        )
        # `required_state` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            response_body["rooms"][room_id1].get("required_state"),
            response_body["rooms"][room_id1],
        )
        # We should have some `stripped_state` so the potential joiner can identify the
        # room (we don't care about the order).
        self.assertCountEqual(
            response_body["rooms"][room_id1]["invite_state"],
            [
                {
                    "content": {"creator": user2_id, "room_version": "10"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.create",
                },
                {
                    "content": {"join_rule": "public"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.join_rules",
                },
                {
                    "content": {"displayname": user2.localpart, "membership": "join"},
                    "sender": user2_id,
                    "state_key": user2_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"displayname": user1.localpart, "membership": "invite"},
                    "sender": user2_id,
                    "state_key": user1_id,
                    "type": "m.room.member",
                },
            ],
            response_body["rooms"][room_id1]["invite_state"],
        )
