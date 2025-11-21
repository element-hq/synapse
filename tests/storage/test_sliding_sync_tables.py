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
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#
import logging
from typing import cast

import attr
from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, Membership, RoomTypes
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase, StrippedStateEvent, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.databases.main.events import DeltaState
from synapse.storage.databases.main.events_bg_updates import (
    _resolve_stale_data_in_sliding_sync_joined_rooms_table,
    _resolve_stale_data_in_sliding_sync_membership_snapshots_table,
)
from synapse.types import create_requester
from synapse.types.storage import _BackgroundUpdates
from synapse.util.clock import Clock

from tests.test_utils.event_injection import create_event
from tests.unittest import HomeserverTestCase

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _SlidingSyncJoinedRoomResult:
    room_id: str
    # `event_stream_ordering` is only optional to allow easier semantics when we make
    # expected objects from `event.internal_metadata.stream_ordering`. in the tests.
    # `event.internal_metadata.stream_ordering` is marked optional because it only
    # exists for persisted events but in the context of these tests, we're only working
    # with persisted events and we're making comparisons so we will find any mismatch.
    event_stream_ordering: int | None
    bump_stamp: int | None
    room_type: str | None
    room_name: str | None
    is_encrypted: bool
    tombstone_successor_room_id: str | None


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _SlidingSyncMembershipSnapshotResult:
    room_id: str
    user_id: str
    sender: str
    membership_event_id: str
    membership: str
    # `event_stream_ordering` is only optional to allow easier semantics when we make
    # expected objects from `event.internal_metadata.stream_ordering`. in the tests.
    # `event.internal_metadata.stream_ordering` is marked optional because it only
    # exists for persisted events but in the context of these tests, we're only working
    # with persisted events and we're making comparisons so we will find any mismatch.
    event_stream_ordering: int | None
    has_known_state: bool
    room_type: str | None
    room_name: str | None
    is_encrypted: bool
    tombstone_successor_room_id: str | None
    # Make this default to "not forgotten" because it doesn't apply to many tests and we
    # don't want to force all of the tests to deal with it.
    forgotten: bool = False


class SlidingSyncTablesTestCaseBase(HomeserverTestCase):
    """
    Helpers to deal with testing that the
    `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` database tables are
    populated correctly.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        persist_events_store = self.hs.get_datastores().persist_events
        assert persist_events_store is not None
        self.persist_events_store = persist_events_store

        persist_controller = self.hs.get_storage_controllers().persistence
        assert persist_controller is not None
        self.persist_controller = persist_controller

        self.state_handler = self.hs.get_state_handler()

    def _get_sliding_sync_joined_rooms(self) -> dict[str, _SlidingSyncJoinedRoomResult]:
        """
        Return the rows from the `sliding_sync_joined_rooms` table.

        Returns:
            Mapping from room_id to _SlidingSyncJoinedRoomResult.
        """
        rows = cast(
            list[tuple[str, int, int, str, str, bool, str]],
            self.get_success(
                self.store.db_pool.simple_select_list(
                    "sliding_sync_joined_rooms",
                    None,
                    retcols=(
                        "room_id",
                        "event_stream_ordering",
                        "bump_stamp",
                        "room_type",
                        "room_name",
                        "is_encrypted",
                        "tombstone_successor_room_id",
                    ),
                ),
            ),
        )

        return {
            row[0]: _SlidingSyncJoinedRoomResult(
                room_id=row[0],
                event_stream_ordering=row[1],
                bump_stamp=row[2],
                room_type=row[3],
                room_name=row[4],
                is_encrypted=bool(row[5]),
                tombstone_successor_room_id=row[6],
            )
            for row in rows
        }

    def _get_sliding_sync_membership_snapshots(
        self,
    ) -> dict[tuple[str, str], _SlidingSyncMembershipSnapshotResult]:
        """
        Return the rows from the `sliding_sync_membership_snapshots` table.

        Returns:
            Mapping from the (room_id, user_id) to _SlidingSyncMembershipSnapshotResult.
        """
        rows = cast(
            list[tuple[str, str, str, str, str, int, int, bool, str, str, bool, str]],
            self.get_success(
                self.store.db_pool.simple_select_list(
                    "sliding_sync_membership_snapshots",
                    None,
                    retcols=(
                        "room_id",
                        "user_id",
                        "sender",
                        "membership_event_id",
                        "membership",
                        "forgotten",
                        "event_stream_ordering",
                        "has_known_state",
                        "room_type",
                        "room_name",
                        "is_encrypted",
                        "tombstone_successor_room_id",
                    ),
                ),
            ),
        )

        return {
            (row[0], row[1]): _SlidingSyncMembershipSnapshotResult(
                room_id=row[0],
                user_id=row[1],
                sender=row[2],
                membership_event_id=row[3],
                membership=row[4],
                forgotten=bool(row[5]),
                event_stream_ordering=row[6],
                has_known_state=bool(row[7]),
                room_type=row[8],
                room_name=row[9],
                is_encrypted=bool(row[10]),
                tombstone_successor_room_id=row[11],
            )
            for row in rows
        }

    _remote_invite_count: int = 0

    def _create_remote_invite_room_for_user(
        self,
        invitee_user_id: str,
        unsigned_invite_room_state: list[StrippedStateEvent] | None,
    ) -> tuple[str, EventBase]:
        """
        Create a fake invite for a remote room and persist it.

        We don't have any state for these kind of rooms and can only rely on the
        stripped state included in the unsigned portion of the invite event to identify
        the room.

        Args:
            invitee_user_id: The person being invited
            unsigned_invite_room_state: List of stripped state events to assist the
                receiver in identifying the room.

        Returns:
            The room ID of the remote invite room and the persisted remote invite event.
        """
        invite_room_id = f"!test_room{self._remote_invite_count}:remote_server"

        invite_event_dict = {
            "room_id": invite_room_id,
            "sender": "@inviter:remote_server",
            "state_key": invitee_user_id,
            "depth": 1,
            "origin_server_ts": 1,
            "type": EventTypes.Member,
            "content": {"membership": Membership.INVITE},
            "auth_events": [],
            "prev_events": [],
        }
        if unsigned_invite_room_state is not None:
            serialized_stripped_state_events = []
            for stripped_event in unsigned_invite_room_state:
                serialized_stripped_state_events.append(
                    {
                        "type": stripped_event.type,
                        "state_key": stripped_event.state_key,
                        "sender": stripped_event.sender,
                        "content": stripped_event.content,
                    }
                )

            invite_event_dict["unsigned"] = {
                "invite_room_state": serialized_stripped_state_events
            }

        invite_event = make_event_from_dict(
            invite_event_dict,
            room_version=RoomVersions.V10,
        )
        invite_event.internal_metadata.outlier = True
        invite_event.internal_metadata.out_of_band_membership = True

        self.get_success(
            self.store.maybe_store_room_on_outlier_membership(
                room_id=invite_room_id, room_version=invite_event.room_version
            )
        )
        context = EventContext.for_outlier(self.hs.get_storage_controllers())
        persisted_event, _, _ = self.get_success(
            self.persist_controller.persist_event(invite_event, context)
        )

        self._remote_invite_count += 1

        return invite_room_id, persisted_event

    def _retract_remote_invite_for_user(
        self,
        user_id: str,
        remote_room_id: str,
    ) -> EventBase:
        """
        Create a fake invite retraction for a remote room and persist it.

        Retracting an invite just means the person is no longer invited to the room.
        This is done by someone with proper power levels kicking the user from the room.
        A kick shows up as a leave event for a given person with a different `sender`.

        Args:
            user_id: The person who was invited and we're going to retract the
                invite for.
            remote_room_id: The room ID that the invite was for.

        Returns:
            The persisted leave (kick) event.
        """

        kick_event_dict = {
            "room_id": remote_room_id,
            "sender": "@inviter:remote_server",
            "state_key": user_id,
            "depth": 1,
            "origin_server_ts": 1,
            "type": EventTypes.Member,
            "content": {"membership": Membership.LEAVE},
            "auth_events": [],
            "prev_events": [],
        }

        kick_event = make_event_from_dict(
            kick_event_dict,
            room_version=RoomVersions.V10,
        )
        kick_event.internal_metadata.outlier = True
        kick_event.internal_metadata.out_of_band_membership = True

        self.get_success(
            self.store.maybe_store_room_on_outlier_membership(
                room_id=remote_room_id, room_version=kick_event.room_version
            )
        )
        context = EventContext.for_outlier(self.hs.get_storage_controllers())
        persisted_event, _, _ = self.get_success(
            self.persist_controller.persist_event(kick_event, context)
        )

        return persisted_event


class SlidingSyncTablesTestCase(SlidingSyncTablesTestCaseBase):
    """
    Tests to make sure the
    `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` database tables are
    populated and updated correctly as new events are sent.
    """

    def test_joined_room_with_no_info(self) -> None:
        """
        Test joined room that doesn't have a room type, encryption, or name shows up in
        `sliding_sync_joined_rooms`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # History visibility just happens to be the last event sent in the room
                event_stream_ordering=state_map[
                    (EventTypes.RoomHistoryVisibility, "")
                ].internal_metadata.stream_ordering,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_joined_room_with_info(self) -> None:
        """
        Test joined encrypted room with name shows up in `sliding_sync_joined_rooms`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Add a room name
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            room_id1,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )
        # Add a tombstone
        self.helper.send_state(
            room_id1,
            EventTypes.Tombstone,
            {EventContentFields.TOMBSTONE_SUCCESSOR_ROOM: "another_room"},
            tok=user2_tok,
        )

        # User1 joins the room
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # This should be whatever is the last event in the room
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
                tombstone_successor_room_id="another_room",
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
                tombstone_successor_room_id="another_room",
            ),
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user2_id,
                sender=user2_id,
                membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user2_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                # Even though this room does have a name, is encrypted, and has a
                # tombstone, user2 is the room creator and joined at the room creation
                # time which didn't have this state set yet.
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_joined_space_room_with_info(self) -> None:
        """
        Test joined space room with name shows up in `sliding_sync_joined_rooms`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        space_room_id = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Add a room name
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space"},
            tok=user2_tok,
        )

        # User1 joins the room
        user1_join_response = self.helper.join(space_room_id, user1_id, tok=user1_tok)
        user1_join_event_pos = self.get_success(
            self.store.get_position_for_event(user1_join_response["event_id"])
        )

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(space_room_id)
        )

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {space_room_id},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[space_room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=space_room_id,
                event_stream_ordering=user1_join_event_pos.stream,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (space_room_id, user1_id),
                (space_room_id, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((space_room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=space_room_id,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((space_room_id, user2_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=space_room_id,
                user_id=user2_id,
                sender=user2_id,
                membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user2_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                # Even though this room does have a name, user2 is the room creator and
                # joined at the room creation time which didn't have this state set yet.
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_joined_room_with_state_updated(self) -> None:
        """
        Test state derived info in `sliding_sync_joined_rooms` is updated when the
        current state is updated.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Add a room name
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )

        # User1 joins the room
        user1_join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        user1_join_event_pos = self.get_success(
            self.store.get_position_for_event(user1_join_response["event_id"])
        )

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                event_stream_ordering=user1_join_event_pos.stream,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )

        # Update the room name
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room was renamed"},
            tok=user2_tok,
        )
        # Encrypt the room
        encrypt_room_response = self.helper.send_state(
            room_id1,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )
        encrypt_room_event_pos = self.get_success(
            self.store.get_position_for_event(encrypt_room_response["event_id"])
        )

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        # Make sure we see the new room name
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                event_stream_ordering=encrypt_room_event_pos.stream,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room was renamed",
                is_encrypted=True,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user2_id,
                sender=user2_id,
                membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user2_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_joined_room_is_bumped(self) -> None:
        """
        Test that `event_stream_ordering` and `bump_stamp` is updated when a new bump
        event is sent (`sliding_sync_joined_rooms`).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Add a room name
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )

        # User1 joins the room
        user1_join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        user1_join_event_pos = self.get_success(
            self.store.get_position_for_event(user1_join_response["event_id"])
        )

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                event_stream_ordering=user1_join_event_pos.stream,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user joined
        user1_snapshot = _SlidingSyncMembershipSnapshotResult(
            room_id=room_id1,
            user_id=user1_id,
            sender=user1_id,
            membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
            membership=Membership.JOIN,
            event_stream_ordering=state_map[
                (EventTypes.Member, user1_id)
            ].internal_metadata.stream_ordering,
            has_known_state=True,
            room_type=None,
            room_name="my super duper room",
            is_encrypted=False,
            tombstone_successor_room_id=None,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            user1_snapshot,
        )
        # Holds the info according to the current state when the user joined
        user2_snapshot = _SlidingSyncMembershipSnapshotResult(
            room_id=room_id1,
            user_id=user2_id,
            sender=user2_id,
            membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
            membership=Membership.JOIN,
            event_stream_ordering=state_map[
                (EventTypes.Member, user2_id)
            ].internal_metadata.stream_ordering,
            has_known_state=True,
            room_type=None,
            room_name=None,
            is_encrypted=False,
            tombstone_successor_room_id=None,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            user2_snapshot,
        )

        # Send a new message to bump the room
        event_response = self.helper.send(room_id1, "some message", tok=user1_tok)
        event_pos = self.get_success(
            self.store.get_position_for_event(event_response["event_id"])
        )

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        # Make sure we see the new room name
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # Updated `event_stream_ordering`
                event_stream_ordering=event_pos.stream,
                # And since the event was a bump event, the `bump_stamp` should be updated
                bump_stamp=event_pos.stream,
                # The state is still the same (it didn't change)
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            user1_snapshot,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            user2_snapshot,
        )

    def test_joined_room_bump_stamp_backfill(self) -> None:
        """
        Test that `bump_stamp` ignores backfilled events, i.e. events with a
        negative stream ordering.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")

        # Create a remote room
        creator = "@user:other"
        room_id = "!foo:other"
        room_version = RoomVersions.V10
        shared_kwargs = {
            "room_id": room_id,
            "room_version": room_version.identifier,
        }

        create_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[],
                type=EventTypes.Create,
                state_key="",
                content={
                    # The `ROOM_CREATOR` field could be removed if we used a room
                    # version > 10 (in favor of relying on `sender`)
                    EventContentFields.ROOM_CREATOR: creator,
                    EventContentFields.ROOM_VERSION: room_version.identifier,
                },
                sender=creator,
                **shared_kwargs,
            )
        )
        creator_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[create_tuple[0].event_id],
                auth_event_ids=[create_tuple[0].event_id],
                type=EventTypes.Member,
                state_key=creator,
                content={"membership": Membership.JOIN},
                sender=creator,
                **shared_kwargs,
            )
        )
        room_name_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[creator_tuple[0].event_id],
                auth_event_ids=[create_tuple[0].event_id, creator_tuple[0].event_id],
                type=EventTypes.Name,
                state_key="",
                content={
                    EventContentFields.ROOM_NAME: "my super duper room",
                },
                sender=creator,
                **shared_kwargs,
            )
        )
        # We add a message event as a valid "bump type"
        msg_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[room_name_tuple[0].event_id],
                auth_event_ids=[create_tuple[0].event_id, creator_tuple[0].event_id],
                type=EventTypes.Message,
                content={"body": "foo", "msgtype": "m.text"},
                sender=creator,
                **shared_kwargs,
            )
        )
        invite_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[msg_tuple[0].event_id],
                auth_event_ids=[create_tuple[0].event_id, creator_tuple[0].event_id],
                type=EventTypes.Member,
                state_key=user1_id,
                content={"membership": Membership.INVITE},
                sender=creator,
                **shared_kwargs,
            )
        )

        remote_events_and_contexts = [
            create_tuple,
            creator_tuple,
            room_name_tuple,
            msg_tuple,
            invite_tuple,
        ]

        # Ensure the local HS knows the room version
        self.get_success(self.store.store_room(room_id, creator, False, room_version))

        # Persist these events as backfilled events.
        for event, context in remote_events_and_contexts:
            self.get_success(
                self.persist_controller.persist_event(event, context, backfilled=True)
            )

        # Now we join the local user to the room. We want to make this feel as close to
        # the real `process_remote_join()` as possible but we'd like to avoid some of
        # the auth checks that would be done in the real code.
        #
        # FIXME: The test was originally written using this less-real
        # `persist_event(...)` shortcut but it would be nice to use the real remote join
        # process in a `FederatingHomeserverTestCase`.
        flawed_join_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[invite_tuple[0].event_id],
                # This doesn't work correctly to create an `EventContext` that includes
                # both of these state events. I assume it's because we're working on our
                # local homeserver which has the remote state set as `outlier`. We have
                # to create our own EventContext below to get this right.
                auth_event_ids=[create_tuple[0].event_id, invite_tuple[0].event_id],
                type=EventTypes.Member,
                state_key=user1_id,
                content={"membership": Membership.JOIN},
                sender=user1_id,
                **shared_kwargs,
            )
        )
        # We have to create our own context to get the state set correctly. If we use
        # the `EventContext` from the `flawed_join_tuple`, the `current_state_events`
        # table will only have the join event in it which should never happen in our
        # real server.
        join_event = flawed_join_tuple[0]
        join_context = self.get_success(
            self.state_handler.compute_event_context(
                join_event,
                state_ids_before_event={
                    (e.type, e.state_key): e.event_id
                    for e in [create_tuple[0], invite_tuple[0], room_name_tuple[0]]
                },
                partial_state=False,
            )
        )
        join_event, _join_event_pos, _room_token = self.get_success(
            self.persist_controller.persist_event(join_event, join_context)
        )

        # Make sure the tables are populated correctly
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id,
                # This should be the last event in the room (the join membership)
                event_stream_ordering=join_event.internal_metadata.stream_ordering,
                # Since all of the bump events are backfilled, the `bump_stamp` should
                # still be `None`. (and we will fallback to the users membership event
                # position in the Sliding Sync API)
                bump_stamp=None,
                room_type=None,
                # We still pick up state of the room even if it's backfilled
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=join_event.event_id,
                membership=Membership.JOIN,
                event_stream_ordering=join_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    @parameterized.expand(
        # Test both an insert an upsert into the
        # `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` to exercise
        # more possibilities of things going wrong.
        [
            ("insert", True),
            ("upsert", False),
        ]
    )
    def test_joined_room_outlier_and_deoutlier(
        self, description: str, should_insert: bool
    ) -> None:
        """
        This is a regression test.

        This is to simulate the case where an event is first persisted as an outlier
        (like a remote invite) and then later persisted again to de-outlier it. The
        first the time, the `outlier` is persisted with one `stream_ordering` but when
        persisted again and de-outliered, it is assigned a different `stream_ordering`
        that won't end up being used. Since we call
        `_calculate_sliding_sync_table_changes()` before `_update_outliers_txn()` which
        fixes this discrepancy (always use the `stream_ordering` from the first time it
        was persisted), make sure we're not using an unreliable `stream_ordering` values
        that will cause `FOREIGN KEY constraint failed` in the
        `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` tables.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_version = RoomVersions.V10
        room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, room_version=room_version.identifier
        )

        if should_insert:
            # Clear these out so we always insert
            self.get_success(
                self.store.db_pool.simple_delete(
                    table="sliding_sync_joined_rooms",
                    keyvalues={"room_id": room_id},
                    desc="TODO",
                )
            )
            self.get_success(
                self.store.db_pool.simple_delete(
                    table="sliding_sync_membership_snapshots",
                    keyvalues={"room_id": room_id},
                    desc="TODO",
                )
            )

        # Create a membership event (which triggers an insert into
        # `sliding_sync_membership_snapshots`)
        membership_event_dict = {
            "type": EventTypes.Member,
            "state_key": user1_id,
            "sender": user1_id,
            "room_id": room_id,
            "content": {EventContentFields.MEMBERSHIP: Membership.JOIN},
        }
        # Create a relevant state event (which triggers an insert into
        # `sliding_sync_joined_rooms`)
        state_event_dict = {
            "type": EventTypes.Name,
            "state_key": "",
            "sender": user2_id,
            "room_id": room_id,
            "content": {EventContentFields.ROOM_NAME: "my super room"},
        }
        event_dicts_to_persist = [
            membership_event_dict,
            state_event_dict,
        ]

        for event_dict in event_dicts_to_persist:
            events_to_persist = []

            # Create the events as an outliers
            (
                event,
                unpersisted_context,
            ) = self.get_success(
                self.hs.get_event_creation_handler().create_event(
                    requester=create_requester(user1_id),
                    event_dict=event_dict,
                    outlier=True,
                )
            )
            # FIXME: Should we use an `EventContext.for_outlier(...)` here?
            # Doesn't seem to matter for this test.
            context = self.get_success(unpersisted_context.persist(event))
            events_to_persist.append((event, context))

            # Create the event again but as an non-outlier. This will de-outlier the event
            # when we persist it.
            (
                event,
                unpersisted_context,
            ) = self.get_success(
                self.hs.get_event_creation_handler().create_event(
                    requester=create_requester(user1_id),
                    event_dict=event_dict,
                    outlier=False,
                )
            )
            context = self.get_success(unpersisted_context.persist(event))
            events_to_persist.append((event, context))

            for event, context in events_to_persist:
                self.get_success(
                    self.persist_controller.persist_event(
                        event,
                        context,
                    )
                )

        # We're just testing that it does not explode

    def test_joined_room_meta_state_reset(self) -> None:
        """
        Test that a state reset on the room name is reflected in the
        `sliding_sync_joined_rooms` table.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Add a room name
        self.helper.send_state(
            room_id,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )

        # User1 joins the room
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Make sure we see the new room name
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id},
            exact=True,
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id,
                # This should be whatever is the last event in the room
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )
        user1_snapshot = _SlidingSyncMembershipSnapshotResult(
            room_id=room_id,
            user_id=user1_id,
            sender=user1_id,
            membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
            membership=Membership.JOIN,
            event_stream_ordering=state_map[
                (EventTypes.Member, user1_id)
            ].internal_metadata.stream_ordering,
            has_known_state=True,
            room_type=None,
            room_name="my super duper room",
            is_encrypted=False,
            tombstone_successor_room_id=None,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user1_id)),
            user1_snapshot,
        )
        # Holds the info according to the current state when the user joined (no room
        # name when the room creator joined)
        user2_snapshot = _SlidingSyncMembershipSnapshotResult(
            room_id=room_id,
            user_id=user2_id,
            sender=user2_id,
            membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
            membership=Membership.JOIN,
            event_stream_ordering=state_map[
                (EventTypes.Member, user2_id)
            ].internal_metadata.stream_ordering,
            has_known_state=True,
            room_type=None,
            room_name=None,
            is_encrypted=False,
            tombstone_successor_room_id=None,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user2_id)),
            user2_snapshot,
        )

        # Mock a state reset removing the room name state from the current state
        message_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[state_map[(EventTypes.Name, "")].event_id],
                auth_event_ids=[
                    state_map[(EventTypes.Create, "")].event_id,
                    state_map[(EventTypes.Member, user1_id)].event_id,
                ],
                type=EventTypes.Message,
                content={"body": "foo", "msgtype": "m.text"},
                sender=user1_id,
                room_id=room_id,
                room_version=RoomVersions.V10.identifier,
            )
        )
        event_chunk = [message_tuple]
        self.get_success(
            self.persist_events_store._persist_events_and_state_updates(
                room_id,
                event_chunk,
                state_delta_for_room=DeltaState(
                    # This is the state reset part. We're removing the room name state.
                    to_delete=[(EventTypes.Name, "")],
                    to_insert={},
                ),
                new_forward_extremities={message_tuple[0].event_id},
                use_negative_stream_ordering=False,
                inhibit_local_membership_updates=False,
                new_event_links={},
            )
        )

        # Make sure the state reset is reflected in the `sliding_sync_joined_rooms` table
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id},
            exact=True,
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id,
                # This should be whatever is the last event in the room
                event_stream_ordering=message_tuple[
                    0
                ].internal_metadata.stream_ordering,
                bump_stamp=message_tuple[0].internal_metadata.stream_ordering,
                room_type=None,
                # This was state reset back to None
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        # State reset shouldn't be reflected in the `sliding_sync_membership_snapshots`
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )
        # Snapshots haven't changed
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user1_id)),
            user1_snapshot,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user2_id)),
            user2_snapshot,
        )

    def test_joined_room_fully_insert_on_state_update(self) -> None:
        """
        Test that when an existing room updates it's state and we don't have a
        corresponding row in `sliding_sync_joined_rooms` yet, we fully-insert the row
        even though only a tiny piece of state changed.

        FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
        foreground update for
        `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
        https://github.com/element-hq/synapse/issues/17623)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Add a room name
        self.helper.send_state(
            room_id,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user1_tok,
        )

        # Clean-up the `sliding_sync_joined_rooms` table as if the the room never made
        # it into the table. This is to simulate an existing room (before we event added
        # the sliding sync tables) not being in the `sliding_sync_joined_rooms` table
        # yet.
        self.get_success(
            self.store.db_pool.simple_delete(
                table="sliding_sync_joined_rooms",
                keyvalues={"room_id": room_id},
                desc="simulate existing room not being in the sliding_sync_joined_rooms table yet",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        # Encrypt the room
        self.helper.send_state(
            room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )

        # The room should now be in the `sliding_sync_joined_rooms` table
        # (fully-inserted with all of the state values).
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id},
            exact=True,
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id,
                # This should be whatever is the last event in the room
                event_stream_ordering=state_map[
                    (EventTypes.RoomEncryption, "")
                ].internal_metadata.stream_ordering,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
                tombstone_successor_room_id=None,
            ),
        )

    def test_joined_room_nothing_if_not_in_table_when_bumped(self) -> None:
        """
        Test a new message being sent in an existing room when we don't have a
        corresponding row in `sliding_sync_joined_rooms` yet; either nothing should
        happen or we should fully-insert the row. We currently do nothing.

        FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
        foreground update for
        `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
        https://github.com/element-hq/synapse/issues/17623)
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Add a room name
        self.helper.send_state(
            room_id,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user1_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )

        # Clean-up the `sliding_sync_joined_rooms` table as if the the room never made
        # it into the table. This is to simulate an existing room (before we event added
        # the sliding sync tables) not being in the `sliding_sync_joined_rooms` table
        # yet.
        self.get_success(
            self.store.db_pool.simple_delete(
                table="sliding_sync_joined_rooms",
                keyvalues={"room_id": room_id},
                desc="simulate existing room not being in the sliding_sync_joined_rooms table yet",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        # Send a new message to bump the room
        self.helper.send(room_id, "some message", tok=user1_tok)

        # Either nothing should happen or we should fully-insert the row. We currently
        # do nothing for non-state events.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

    def test_non_join_space_room_with_info(self) -> None:
        """
        Test users who was invited shows up in `sliding_sync_membership_snapshots`.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        space_room_id = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Add a room name
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space"},
            tok=user2_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            space_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )
        # Add a tombstone
        self.helper.send_state(
            space_room_id,
            EventTypes.Tombstone,
            {EventContentFields.TOMBSTONE_SUCCESSOR_ROOM: "another_room"},
            tok=user2_tok,
        )

        # User1 is invited to the room
        user1_invited_response = self.helper.invite(
            space_room_id, src=user2_id, targ=user1_id, tok=user2_tok
        )
        user1_invited_event_pos = self.get_success(
            self.store.get_position_for_event(user1_invited_response["event_id"])
        )

        # Update the room name after we are invited just to make sure
        # we don't update non-join memberships when the room name changes.
        rename_response = self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space was renamed"},
            tok=user2_tok,
        )
        rename_event_pos = self.get_success(
            self.store.get_position_for_event(rename_response["event_id"])
        )

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(space_room_id)
        )

        # User2 is still joined to the room so we should still have an entry in the
        # `sliding_sync_joined_rooms` table.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {space_room_id},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[space_room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=space_room_id,
                event_stream_ordering=rename_event_pos.stream,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space was renamed",
                is_encrypted=True,
                tombstone_successor_room_id="another_room",
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (space_room_id, user1_id),
                (space_room_id, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user was invited
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((space_room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=space_room_id,
                user_id=user1_id,
                sender=user2_id,
                membership_event_id=user1_invited_response["event_id"],
                membership=Membership.INVITE,
                event_stream_ordering=user1_invited_event_pos.stream,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=True,
                tombstone_successor_room_id="another_room",
            ),
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((space_room_id, user2_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=space_room_id,
                user_id=user2_id,
                sender=user2_id,
                membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user2_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_non_join_invite_ban(self) -> None:
        """
        Test users who have invite/ban membership in room shows up in
        `sliding_sync_membership_snapshots`.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 is invited to the room
        user1_invited_response = self.helper.invite(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )
        user1_invited_event_pos = self.get_success(
            self.store.get_position_for_event(user1_invited_response["event_id"])
        )

        # User3 joins the room
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        # User3 is banned from the room
        user3_ban_response = self.helper.ban(
            room_id1, src=user2_id, targ=user3_id, tok=user2_tok
        )
        user3_ban_event_pos = self.get_success(
            self.store.get_position_for_event(user3_ban_response["event_id"])
        )

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # User2 is still joined to the room so we should still have an entry
        # in the `sliding_sync_joined_rooms` table.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                event_stream_ordering=user3_ban_event_pos.stream,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
                (room_id1, user3_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user was invited
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user1_id,
                sender=user2_id,
                membership_event_id=user1_invited_response["event_id"],
                membership=Membership.INVITE,
                event_stream_ordering=user1_invited_event_pos.stream,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user2_id,
                sender=user2_id,
                membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user2_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        # Holds the info according to the current state when the user was banned
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user3_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user3_id,
                sender=user2_id,
                membership_event_id=user3_ban_response["event_id"],
                membership=Membership.BAN,
                event_stream_ordering=user3_ban_event_pos.stream,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_non_join_reject_invite_empty_room(self) -> None:
        """
        In a room where no one is joined (`no_longer_in_room`), test rejecting an invite.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 is invited to the room
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        # User2 leaves the room
        user2_leave_response = self.helper.leave(room_id1, user2_id, tok=user2_tok)
        user2_leave_event_pos = self.get_success(
            self.store.get_position_for_event(user2_leave_response["event_id"])
        )

        # User1 rejects the invite
        user1_leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        user1_leave_event_pos = self.get_success(
            self.store.get_position_for_event(user1_leave_response["event_id"])
        )

        # No one is joined to the room
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user left
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=user1_leave_response["event_id"],
                membership=Membership.LEAVE,
                event_stream_ordering=user1_leave_event_pos.stream,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        # Holds the info according to the current state when the left
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user2_id,
                sender=user2_id,
                membership_event_id=user2_leave_response["event_id"],
                membership=Membership.LEAVE,
                event_stream_ordering=user2_leave_event_pos.stream,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_membership_changing(self) -> None:
        """
        Test latest snapshot evolves when membership changes (`sliding_sync_membership_snapshots`).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 is invited to the room
        # ======================================================
        user1_invited_response = self.helper.invite(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )
        user1_invited_event_pos = self.get_success(
            self.store.get_position_for_event(user1_invited_response["event_id"])
        )

        # Update the room name after the user was invited
        room_name_update_response = self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )
        room_name_update_event_pos = self.get_success(
            self.store.get_position_for_event(room_name_update_response["event_id"])
        )

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Assert joined room status
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # Latest event in the room
                event_stream_ordering=room_name_update_event_pos.stream,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        # Assert membership snapshots
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user was invited
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user1_id,
                sender=user2_id,
                membership_event_id=user1_invited_response["event_id"],
                membership=Membership.INVITE,
                event_stream_ordering=user1_invited_event_pos.stream,
                has_known_state=True,
                room_type=None,
                # Room name was updated after the user was invited so we should still
                # see it unset here
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        # Holds the info according to the current state when the user joined
        user2_snapshot = _SlidingSyncMembershipSnapshotResult(
            room_id=room_id1,
            user_id=user2_id,
            sender=user2_id,
            membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
            membership=Membership.JOIN,
            event_stream_ordering=state_map[
                (EventTypes.Member, user2_id)
            ].internal_metadata.stream_ordering,
            has_known_state=True,
            room_type=None,
            room_name=None,
            is_encrypted=False,
            tombstone_successor_room_id=None,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            user2_snapshot,
        )

        # User1 joins the room
        # ======================================================
        user1_joined_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        user1_joined_event_pos = self.get_success(
            self.store.get_position_for_event(user1_joined_response["event_id"])
        )

        # Assert joined room status
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # Latest event in the room
                event_stream_ordering=user1_joined_event_pos.stream,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        # Assert membership snapshots
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=user1_joined_response["event_id"],
                membership=Membership.JOIN,
                event_stream_ordering=user1_joined_event_pos.stream,
                has_known_state=True,
                room_type=None,
                # We see the update state because the user joined after the room name
                # change
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            user2_snapshot,
        )

        # User1 is banned from the room
        # ======================================================
        user1_ban_response = self.helper.ban(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )
        user1_ban_event_pos = self.get_success(
            self.store.get_position_for_event(user1_ban_response["event_id"])
        )

        # Assert joined room status
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # Latest event in the room
                event_stream_ordering=user1_ban_event_pos.stream,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        # Assert membership snapshots
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user was banned
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user1_id,
                sender=user2_id,
                membership_event_id=user1_ban_response["event_id"],
                membership=Membership.BAN,
                event_stream_ordering=user1_ban_event_pos.stream,
                has_known_state=True,
                room_type=None,
                # We see the update state because the user joined after the room name
                # change
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            user2_snapshot,
        )

    def test_non_join_server_left_room(self) -> None:
        """
        Test everyone local leaves the room but their leave membership still shows up in
        `sliding_sync_membership_snapshots`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 joins the room
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # User2 leaves the room
        user2_leave_response = self.helper.leave(room_id1, user2_id, tok=user2_tok)
        user2_leave_event_pos = self.get_success(
            self.store.get_position_for_event(user2_leave_response["event_id"])
        )

        # User1 leaves the room
        user1_leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        user1_leave_event_pos = self.get_success(
            self.store.get_position_for_event(user1_leave_response["event_id"])
        )

        # No one is joined to the room anymore so we shouldn't have an entry in the
        # `sliding_sync_joined_rooms` table.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        # We should still see rows for the leave events (non-joins)
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=user1_leave_response["event_id"],
                membership=Membership.LEAVE,
                event_stream_ordering=user1_leave_event_pos.stream,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id1, user2_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id1,
                user_id=user2_id,
                sender=user2_id,
                membership_event_id=user2_leave_response["event_id"],
                membership=Membership.LEAVE,
                event_stream_ordering=user2_leave_event_pos.stream,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    @parameterized.expand(
        [
            # No stripped state provided
            ("none", None),
            # Empty stripped state provided
            ("empty", []),
        ]
    )
    def test_non_join_remote_invite_no_stripped_state(
        self, _description: str, stripped_state: list[StrippedStateEvent] | None
    ) -> None:
        """
        Test remote invite with no stripped state provided shows up in
        `sliding_sync_membership_snapshots` with `has_known_state=False`.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room without any `unsigned.invite_room_state`
        remote_invite_room_id, remote_invite_event = (
            self._create_remote_invite_room_for_user(user1_id, stripped_state)
        )

        # No one local is joined to the remote room
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (remote_invite_room_id, user1_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (remote_invite_room_id, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=remote_invite_room_id,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=remote_invite_event.event_id,
                membership=Membership.INVITE,
                event_stream_ordering=remote_invite_event.internal_metadata.stream_ordering,
                # No stripped state provided
                has_known_state=False,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_non_join_remote_invite_unencrypted_room(self) -> None:
        """
        Test remote invite with stripped state (unencrypted room) shows up in
        `sliding_sync_membership_snapshots`.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room with some `unsigned.invite_room_state`
        # indicating that the room is encrypted.
        remote_invite_room_id, remote_invite_event = (
            self._create_remote_invite_room_for_user(
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
                        type=EventTypes.Name,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.ROOM_NAME: "my super duper room",
                        },
                    ),
                ],
            )
        )

        # No one local is joined to the remote room
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (remote_invite_room_id, user1_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (remote_invite_room_id, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=remote_invite_room_id,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=remote_invite_event.event_id,
                membership=Membership.INVITE,
                event_stream_ordering=remote_invite_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_non_join_remote_invite_encrypted_room(self) -> None:
        """
        Test remote invite with stripped state (encrypted room) shows up in
        `sliding_sync_membership_snapshots`.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room with some `unsigned.invite_room_state`
        # indicating that the room is encrypted.
        remote_invite_room_id, remote_invite_event = (
            self._create_remote_invite_room_for_user(
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
                    # This is not one of the stripped state events according to the state
                    # but we still handle it.
                    StrippedStateEvent(
                        type=EventTypes.Tombstone,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.TOMBSTONE_SUCCESSOR_ROOM: "another_room",
                        },
                    ),
                    # Also test a random event that we don't care about
                    StrippedStateEvent(
                        type="org.matrix.foo_state",
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            "foo": "qux",
                        },
                    ),
                ],
            )
        )

        # No one local is joined to the remote room
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (remote_invite_room_id, user1_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (remote_invite_room_id, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=remote_invite_room_id,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=remote_invite_event.event_id,
                membership=Membership.INVITE,
                event_stream_ordering=remote_invite_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=True,
                tombstone_successor_room_id="another_room",
            ),
        )

    def test_non_join_remote_invite_space_room(self) -> None:
        """
        Test remote invite with stripped state (encrypted space room with name) shows up in
        `sliding_sync_membership_snapshots`.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room with some `unsigned.invite_room_state`
        # indicating that the room is encrypted.
        remote_invite_room_id, remote_invite_event = (
            self._create_remote_invite_room_for_user(
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
                    StrippedStateEvent(
                        type=EventTypes.RoomEncryption,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2",
                        },
                    ),
                    StrippedStateEvent(
                        type=EventTypes.Name,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.ROOM_NAME: "my super duper space",
                        },
                    ),
                ],
            )
        )

        # No one local is joined to the remote room
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (remote_invite_room_id, user1_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (remote_invite_room_id, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=remote_invite_room_id,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=remote_invite_event.event_id,
                membership=Membership.INVITE,
                event_stream_ordering=remote_invite_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=True,
                tombstone_successor_room_id=None,
            ),
        )

    def test_non_join_reject_remote_invite(self) -> None:
        """
        Test rejected remote invite (user decided to leave the room) inherits meta data
        from when the remote invite stripped state and shows up in
        `sliding_sync_membership_snapshots`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room with some `unsigned.invite_room_state`
        # indicating that the room is encrypted.
        remote_invite_room_id, remote_invite_event = (
            self._create_remote_invite_room_for_user(
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
        )

        # User1 decides to leave the room (reject the invite)
        user1_leave_response = self.helper.leave(
            remote_invite_room_id, user1_id, tok=user1_tok
        )
        user1_leave_pos = self.get_success(
            self.store.get_position_for_event(user1_leave_response["event_id"])
        )

        # No one local is joined to the remote room
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (remote_invite_room_id, user1_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (remote_invite_room_id, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=remote_invite_room_id,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=user1_leave_response["event_id"],
                membership=Membership.LEAVE,
                event_stream_ordering=user1_leave_pos.stream,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=True,
                tombstone_successor_room_id=None,
            ),
        )

    def test_non_join_retracted_remote_invite(self) -> None:
        """
        Test retracted remote invite (Remote inviter kicks the person who was invited)
        inherits meta data from when the remote invite stripped state and shows up in
        `sliding_sync_membership_snapshots`.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")

        # Create a remote invite room with some `unsigned.invite_room_state`
        # indicating that the room is encrypted.
        remote_invite_room_id, remote_invite_event = (
            self._create_remote_invite_room_for_user(
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
        )

        # `@inviter:remote_server` decides to retract the invite (kicks the user).
        # (Note: A kick is just a leave event with a different sender)
        remote_invite_retraction_event = self._retract_remote_invite_for_user(
            user_id=user1_id,
            remote_room_id=remote_invite_room_id,
        )

        # No one local is joined to the remote room
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (remote_invite_room_id, user1_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (remote_invite_room_id, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=remote_invite_room_id,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=remote_invite_retraction_event.event_id,
                membership=Membership.LEAVE,
                event_stream_ordering=remote_invite_retraction_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=True,
                tombstone_successor_room_id=None,
            ),
        )

    def test_non_join_state_reset(self) -> None:
        """
        Test a state reset that removes someone from the room.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Add a room name
        self.helper.send_state(
            room_id,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )

        # User1 joins the room
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Make sure we see the new room name
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id},
            exact=True,
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id,
                # This should be whatever is the last event in the room
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )
        user1_snapshot = _SlidingSyncMembershipSnapshotResult(
            room_id=room_id,
            user_id=user1_id,
            sender=user1_id,
            membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
            membership=Membership.JOIN,
            event_stream_ordering=state_map[
                (EventTypes.Member, user1_id)
            ].internal_metadata.stream_ordering,
            has_known_state=True,
            room_type=None,
            room_name="my super duper room",
            is_encrypted=False,
            tombstone_successor_room_id=None,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user1_id)),
            user1_snapshot,
        )
        # Holds the info according to the current state when the user joined (no room
        # name when the room creator joined)
        user2_snapshot = _SlidingSyncMembershipSnapshotResult(
            room_id=room_id,
            user_id=user2_id,
            sender=user2_id,
            membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
            membership=Membership.JOIN,
            event_stream_ordering=state_map[
                (EventTypes.Member, user2_id)
            ].internal_metadata.stream_ordering,
            has_known_state=True,
            room_type=None,
            room_name=None,
            is_encrypted=False,
            tombstone_successor_room_id=None,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user2_id)),
            user2_snapshot,
        )

        # Mock a state reset removing the membership for user1 in the current state
        message_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[state_map[(EventTypes.Name, "")].event_id],
                auth_event_ids=[
                    state_map[(EventTypes.Create, "")].event_id,
                    state_map[(EventTypes.Member, user1_id)].event_id,
                ],
                type=EventTypes.Message,
                content={"body": "foo", "msgtype": "m.text"},
                sender=user1_id,
                room_id=room_id,
                room_version=RoomVersions.V10.identifier,
            )
        )
        event_chunk = [message_tuple]
        self.get_success(
            self.persist_events_store._persist_events_and_state_updates(
                room_id,
                event_chunk,
                state_delta_for_room=DeltaState(
                    # This is the state reset part. We're removing the room name state.
                    to_delete=[(EventTypes.Member, user1_id)],
                    to_insert={},
                ),
                new_forward_extremities={message_tuple[0].event_id},
                use_negative_stream_ordering=False,
                inhibit_local_membership_updates=False,
                new_event_links={},
            )
        )

        # State reset on membership doesn't affect the`sliding_sync_joined_rooms` table
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id},
            exact=True,
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id,
                # This should be whatever is the last event in the room
                event_stream_ordering=message_tuple[
                    0
                ].internal_metadata.stream_ordering,
                bump_stamp=message_tuple[0].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

        # State reset on membership should remove the user's snapshot
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                # We shouldn't see user1 in the snapshots table anymore
                (room_id, user2_id),
            },
            exact=True,
        )
        # Snapshot for user2 hasn't changed
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user2_id)),
            user2_snapshot,
        )

    def test_membership_snapshot_forget(self) -> None:
        """
        Test forgetting a room will update `sliding_sync_membership_snapshots`
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 joins the room
        self.helper.join(room_id, user1_id, tok=user1_tok)
        # User1 leaves the room (we have to leave in order to forget the room)
        self.helper.leave(room_id, user1_id, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )

        # Check on the `sliding_sync_membership_snapshots` table (nothing should be
        # forgotten yet)
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user joined
        user1_snapshot = _SlidingSyncMembershipSnapshotResult(
            room_id=room_id,
            user_id=user1_id,
            sender=user1_id,
            membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
            membership=Membership.LEAVE,
            event_stream_ordering=state_map[
                (EventTypes.Member, user1_id)
            ].internal_metadata.stream_ordering,
            has_known_state=True,
            room_type=None,
            room_name=None,
            is_encrypted=False,
            tombstone_successor_room_id=None,
            # Room is not forgotten
            forgotten=False,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user1_id)),
            user1_snapshot,
        )
        # Holds the info according to the current state when the user joined
        user2_snapshot = _SlidingSyncMembershipSnapshotResult(
            room_id=room_id,
            user_id=user2_id,
            sender=user2_id,
            membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
            membership=Membership.JOIN,
            event_stream_ordering=state_map[
                (EventTypes.Member, user2_id)
            ].internal_metadata.stream_ordering,
            has_known_state=True,
            room_type=None,
            room_name=None,
            is_encrypted=False,
            tombstone_successor_room_id=None,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user2_id)),
            user2_snapshot,
        )

        # Forget the room
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Check on the `sliding_sync_membership_snapshots` table
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )
        # Room is now forgotten for user1
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user1_id)),
            attr.evolve(user1_snapshot, forgotten=True),
        )
        # Nothing changed for user2
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user2_id)),
            user2_snapshot,
        )

    def test_membership_snapshot_missing_forget(
        self,
    ) -> None:
        """
        Test forgetting a room with no existing row in `sliding_sync_membership_snapshots`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 joins the room
        self.helper.join(room_id, user1_id, tok=user1_tok)
        # User1 leaves the room (we have to leave in order to forget the room)
        self.helper.leave(room_id, user1_id, tok=user1_tok)

        # Clean-up the `sliding_sync_membership_snapshots` table as if the inserts did not
        # happen during event creation.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_membership_snapshots",
                column="room_id",
                iterable=(room_id,),
                keyvalues={},
                desc="sliding_sync_membership_snapshots.test_membership_snapshots_background_update_forgotten_missing",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )

        # Forget the room
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # It doesn't explode

        # We still shouldn't find anything in the table because nothing has re-created them
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )


class SlidingSyncTablesBackgroundUpdatesTestCase(SlidingSyncTablesTestCaseBase):
    """
    Test the background updates that populate the `sliding_sync_joined_rooms` and
    `sliding_sync_membership_snapshots` tables.
    """

    def test_joined_background_update_missing(self) -> None:
        """
        Test that the background update for `sliding_sync_joined_rooms` populates missing rows
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create rooms with various levels of state that should appear in the table
        #
        room_id_no_info = self.helper.create_room_as(user1_id, tok=user1_tok)

        room_id_with_info = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Add a room name
        self.helper.send_state(
            room_id_with_info,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user1_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            room_id_with_info,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )

        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Add a room name
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space"},
            tok=user1_tok,
        )

        # Clean-up the `sliding_sync_joined_rooms` table as if the inserts did not
        # happen during event creation.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_joined_rooms",
                column="room_id",
                iterable=(room_id_no_info, room_id_with_info, space_room_id),
                keyvalues={},
                desc="sliding_sync_joined_rooms.test_joined_background_update_missing",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        # Insert and run the background updates.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.SLIDING_SYNC_PREFILL_JOINED_ROOMS_TO_RECALCULATE_TABLE_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.SLIDING_SYNC_JOINED_ROOMS_BG_UPDATE,
                    "progress_json": "{}",
                    "depends_on": _BackgroundUpdates.SLIDING_SYNC_PREFILL_JOINED_ROOMS_TO_RECALCULATE_TABLE_BG_UPDATE,
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Make sure the table is populated
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id_no_info, room_id_with_info, space_room_id},
            exact=True,
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id_no_info)
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id_no_info],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id_no_info,
                # History visibility just happens to be the last event sent in the room
                event_stream_ordering=state_map[
                    (EventTypes.RoomHistoryVisibility, "")
                ].internal_metadata.stream_ordering,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id_with_info)
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id_with_info],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id_with_info,
                # Lastest event sent in the room
                event_stream_ordering=state_map[
                    (EventTypes.RoomEncryption, "")
                ].internal_metadata.stream_ordering,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
                tombstone_successor_room_id=None,
            ),
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(space_room_id)
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[space_room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=space_room_id,
                # Lastest event sent in the room
                event_stream_ordering=state_map[
                    (EventTypes.Name, "")
                ].internal_metadata.stream_ordering,
                bump_stamp=state_map[
                    (EventTypes.Create, "")
                ].internal_metadata.stream_ordering,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_membership_snapshots_background_update_joined(self) -> None:
        """
        Test that the background update for `sliding_sync_membership_snapshots`
        populates missing rows for join memberships.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create rooms with various levels of state that should appear in the table
        #
        room_id_no_info = self.helper.create_room_as(user1_id, tok=user1_tok)

        room_id_with_info = self.helper.create_room_as(user1_id, tok=user1_tok)
        # Add a room name
        self.helper.send_state(
            room_id_with_info,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user1_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            room_id_with_info,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user1_tok,
        )
        # Add a tombstone
        self.helper.send_state(
            room_id_with_info,
            EventTypes.Tombstone,
            {EventContentFields.TOMBSTONE_SUCCESSOR_ROOM: "another_room"},
            tok=user1_tok,
        )

        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Add a room name
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space"},
            tok=user1_tok,
        )

        # Clean-up the `sliding_sync_membership_snapshots` table as if the inserts did not
        # happen during event creation.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_membership_snapshots",
                column="room_id",
                iterable=(room_id_no_info, room_id_with_info, space_room_id),
                keyvalues={},
                desc="sliding_sync_membership_snapshots.test_membership_snapshots_background_update_joined",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Make sure the table is populated
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id_no_info, user1_id),
                (room_id_with_info, user1_id),
                (space_room_id, user1_id),
            },
            exact=True,
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id_no_info)
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id_no_info, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_no_info,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id_with_info)
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (room_id_with_info, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_with_info,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
                tombstone_successor_room_id="another_room",
            ),
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(space_room_id)
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((space_room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=space_room_id,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_membership_snapshots_background_update_local_invite(self) -> None:
        """
        Test that the background update for `sliding_sync_membership_snapshots`
        populates missing rows for invite memberships.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create rooms with various levels of state that should appear in the table
        #
        room_id_no_info = self.helper.create_room_as(user2_id, tok=user2_tok)

        room_id_with_info = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Add a room name
        self.helper.send_state(
            room_id_with_info,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            room_id_with_info,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )
        # Add a tombstone
        self.helper.send_state(
            room_id_with_info,
            EventTypes.Tombstone,
            {EventContentFields.TOMBSTONE_SUCCESSOR_ROOM: "another_room"},
            tok=user2_tok,
        )

        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Add a room name
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space"},
            tok=user2_tok,
        )

        # Invite user1 to the rooms
        user1_invite_room_id_no_info_response = self.helper.invite(
            room_id_no_info, src=user2_id, targ=user1_id, tok=user2_tok
        )
        user1_invite_room_id_with_info_response = self.helper.invite(
            room_id_with_info, src=user2_id, targ=user1_id, tok=user2_tok
        )
        user1_invite_space_room_id_response = self.helper.invite(
            space_room_id, src=user2_id, targ=user1_id, tok=user2_tok
        )

        # Have user2 leave the rooms to make sure that our background update is not just
        # reading from `current_state_events`. For invite/knock memberships, we should
        # be reading from the stripped state on the invite/knock event itself.
        self.helper.leave(room_id_no_info, user2_id, tok=user2_tok)
        self.helper.leave(room_id_with_info, user2_id, tok=user2_tok)
        self.helper.leave(space_room_id, user2_id, tok=user2_tok)
        # Check to make sure we actually don't have any `current_state_events` for the rooms
        current_state_check_rows = self.get_success(
            self.store.db_pool.simple_select_many_batch(
                table="current_state_events",
                column="room_id",
                iterable=[room_id_no_info, room_id_with_info, space_room_id],
                retcols=("event_id",),
                keyvalues={},
                desc="check current_state_events in test",
            )
        )
        self.assertEqual(len(current_state_check_rows), 0)

        # Clean-up the `sliding_sync_membership_snapshots` table as if the inserts did not
        # happen during event creation.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_membership_snapshots",
                column="room_id",
                iterable=(room_id_no_info, room_id_with_info, space_room_id),
                keyvalues={},
                desc="sliding_sync_membership_snapshots.test_membership_snapshots_background_update_local_invite",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Make sure the table is populated
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                # The invite memberships for user1
                (room_id_no_info, user1_id),
                (room_id_with_info, user1_id),
                (space_room_id, user1_id),
                # The leave memberships for user2
                (room_id_no_info, user2_id),
                (room_id_with_info, user2_id),
                (space_room_id, user2_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id_no_info, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_no_info,
                user_id=user1_id,
                sender=user2_id,
                membership_event_id=user1_invite_room_id_no_info_response["event_id"],
                membership=Membership.INVITE,
                event_stream_ordering=self.get_success(
                    self.store.get_position_for_event(
                        user1_invite_room_id_no_info_response["event_id"]
                    )
                ).stream,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (room_id_with_info, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_with_info,
                user_id=user1_id,
                sender=user2_id,
                membership_event_id=user1_invite_room_id_with_info_response["event_id"],
                membership=Membership.INVITE,
                event_stream_ordering=self.get_success(
                    self.store.get_position_for_event(
                        user1_invite_room_id_with_info_response["event_id"]
                    )
                ).stream,
                has_known_state=True,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
                # The tombstone isn't showing here ("another_room") because it's not one
                # of the stripped events that we hand out as part of the invite event.
                # Even though we handle this scenario from other remote homservers,
                # Synapse does not include the tombstone in the invite event.
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((space_room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=space_room_id,
                user_id=user1_id,
                sender=user2_id,
                membership_event_id=user1_invite_space_room_id_response["event_id"],
                membership=Membership.INVITE,
                event_stream_ordering=self.get_success(
                    self.store.get_position_for_event(
                        user1_invite_space_room_id_response["event_id"]
                    )
                ).stream,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_membership_snapshots_background_update_remote_invite(
        self,
    ) -> None:
        """
        Test that the background update for `sliding_sync_membership_snapshots`
        populates missing rows for remote invites (out-of-band memberships).
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")

        # Create rooms with various levels of state that should appear in the table
        #
        room_id_unknown_state, room_id_unknown_state_invite_event = (
            self._create_remote_invite_room_for_user(user1_id, None)
        )

        room_id_no_info, room_id_no_info_invite_event = (
            self._create_remote_invite_room_for_user(
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
                ],
            )
        )

        room_id_with_info, room_id_with_info_invite_event = (
            self._create_remote_invite_room_for_user(
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
                        type=EventTypes.Name,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.ROOM_NAME: "my super duper room",
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
        )

        space_room_id, space_room_id_invite_event = (
            self._create_remote_invite_room_for_user(
                user1_id,
                [
                    StrippedStateEvent(
                        type=EventTypes.Create,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.ROOM_CREATOR: "@inviter:remote_server",
                            EventContentFields.ROOM_VERSION: RoomVersions.V10.identifier,
                            EventContentFields.ROOM_TYPE: RoomTypes.SPACE,
                        },
                    ),
                    StrippedStateEvent(
                        type=EventTypes.Name,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.ROOM_NAME: "my super duper space",
                        },
                    ),
                ],
            )
        )

        # Clean-up the `sliding_sync_membership_snapshots` table as if the inserts did not
        # happen during event creation.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_membership_snapshots",
                column="room_id",
                iterable=(
                    room_id_unknown_state,
                    room_id_no_info,
                    room_id_with_info,
                    space_room_id,
                ),
                keyvalues={},
                desc="sliding_sync_membership_snapshots.test_membership_snapshots_background_update_remote_invite",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Make sure the table is populated
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                # The invite memberships for user1
                (room_id_unknown_state, user1_id),
                (room_id_no_info, user1_id),
                (room_id_with_info, user1_id),
                (space_room_id, user1_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (room_id_unknown_state, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_unknown_state,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=room_id_unknown_state_invite_event.event_id,
                membership=Membership.INVITE,
                event_stream_ordering=room_id_unknown_state_invite_event.internal_metadata.stream_ordering,
                has_known_state=False,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id_no_info, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_no_info,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=room_id_no_info_invite_event.event_id,
                membership=Membership.INVITE,
                event_stream_ordering=room_id_no_info_invite_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (room_id_with_info, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_with_info,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=room_id_with_info_invite_event.event_id,
                membership=Membership.INVITE,
                event_stream_ordering=room_id_with_info_invite_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((space_room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=space_room_id,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=space_room_id_invite_event.event_id,
                membership=Membership.INVITE,
                event_stream_ordering=space_room_id_invite_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_membership_snapshots_background_update_remote_invite_rejections_and_retractions(
        self,
    ) -> None:
        """
        Test that the background update for `sliding_sync_membership_snapshots`
        populates missing rows for remote invite rejections/retractions (out-of-band memberships).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create rooms with various levels of state that should appear in the table
        #
        room_id_unknown_state, room_id_unknown_state_invite_event = (
            self._create_remote_invite_room_for_user(user1_id, None)
        )

        room_id_no_info, room_id_no_info_invite_event = (
            self._create_remote_invite_room_for_user(
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
                ],
            )
        )

        room_id_with_info, room_id_with_info_invite_event = (
            self._create_remote_invite_room_for_user(
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
                        type=EventTypes.Name,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.ROOM_NAME: "my super duper room",
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
        )

        space_room_id, space_room_id_invite_event = (
            self._create_remote_invite_room_for_user(
                user1_id,
                [
                    StrippedStateEvent(
                        type=EventTypes.Create,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.ROOM_CREATOR: "@inviter:remote_server",
                            EventContentFields.ROOM_VERSION: RoomVersions.V10.identifier,
                            EventContentFields.ROOM_TYPE: RoomTypes.SPACE,
                        },
                    ),
                    StrippedStateEvent(
                        type=EventTypes.Name,
                        state_key="",
                        sender="@inviter:remote_server",
                        content={
                            EventContentFields.ROOM_NAME: "my super duper space",
                        },
                    ),
                ],
            )
        )

        # Reject the remote invites.
        # Also try retracting a remote invite.
        room_id_unknown_state_leave_event_response = self.helper.leave(
            room_id_unknown_state, user1_id, tok=user1_tok
        )
        room_id_no_info_leave_event = self._retract_remote_invite_for_user(
            user_id=user1_id,
            remote_room_id=room_id_no_info,
        )
        room_id_with_info_leave_event_response = self.helper.leave(
            room_id_with_info, user1_id, tok=user1_tok
        )
        space_room_id_leave_event = self._retract_remote_invite_for_user(
            user_id=user1_id,
            remote_room_id=space_room_id,
        )

        # Clean-up the `sliding_sync_membership_snapshots` table as if the inserts did not
        # happen during event creation.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_membership_snapshots",
                column="room_id",
                iterable=(
                    room_id_unknown_state,
                    room_id_no_info,
                    room_id_with_info,
                    space_room_id,
                ),
                keyvalues={},
                desc="sliding_sync_membership_snapshots.test_membership_snapshots_background_update_remote_invite_rejections_and_retractions",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Make sure the table is populated
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                # The invite memberships for user1
                (room_id_unknown_state, user1_id),
                (room_id_no_info, user1_id),
                (room_id_with_info, user1_id),
                (space_room_id, user1_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (room_id_unknown_state, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_unknown_state,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=room_id_unknown_state_leave_event_response[
                    "event_id"
                ],
                membership=Membership.LEAVE,
                event_stream_ordering=self.get_success(
                    self.store.get_position_for_event(
                        room_id_unknown_state_leave_event_response["event_id"]
                    )
                ).stream,
                has_known_state=False,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id_no_info, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_no_info,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=room_id_no_info_leave_event.event_id,
                membership=Membership.LEAVE,
                event_stream_ordering=room_id_no_info_leave_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (room_id_with_info, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_with_info,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=room_id_with_info_leave_event_response["event_id"],
                membership=Membership.LEAVE,
                event_stream_ordering=self.get_success(
                    self.store.get_position_for_event(
                        room_id_with_info_leave_event_response["event_id"]
                    )
                ).stream,
                has_known_state=True,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((space_room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=space_room_id,
                user_id=user1_id,
                sender="@inviter:remote_server",
                membership_event_id=space_room_id_leave_event.event_id,
                membership=Membership.LEAVE,
                event_stream_ordering=space_room_id_leave_event.internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    @parameterized.expand(
        [
            # We'll do a kick for this
            (Membership.LEAVE,),
            (Membership.BAN,),
        ]
    )
    def test_membership_snapshots_background_update_historical_state(
        self, test_membership: str
    ) -> None:
        """
        Test that the background update for `sliding_sync_membership_snapshots`
        populates missing rows for leave memberships.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create rooms with various levels of state that should appear in the table
        #
        room_id_no_info = self.helper.create_room_as(user2_id, tok=user2_tok)

        room_id_with_info = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Add a room name
        self.helper.send_state(
            room_id_with_info,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            room_id_with_info,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )
        # Add a tombstone
        self.helper.send_state(
            room_id_with_info,
            EventTypes.Tombstone,
            {EventContentFields.TOMBSTONE_SUCCESSOR_ROOM: "another_room"},
            tok=user2_tok,
        )

        space_room_id = self.helper.create_room_as(
            user1_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Add a room name
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space"},
            tok=user2_tok,
        )

        # Join the room in preparation for our test_membership
        self.helper.join(room_id_no_info, user1_id, tok=user1_tok)
        self.helper.join(room_id_with_info, user1_id, tok=user1_tok)
        self.helper.join(space_room_id, user1_id, tok=user1_tok)

        if test_membership == Membership.LEAVE:
            # Kick user1 from the rooms
            user1_membership_room_id_no_info_response = self.helper.change_membership(
                room=room_id_no_info,
                src=user2_id,
                targ=user1_id,
                tok=user2_tok,
                membership=Membership.LEAVE,
                extra_data={
                    "reason": "Bad manners",
                },
            )
            user1_membership_room_id_with_info_response = self.helper.change_membership(
                room=room_id_with_info,
                src=user2_id,
                targ=user1_id,
                tok=user2_tok,
                membership=Membership.LEAVE,
                extra_data={
                    "reason": "Bad manners",
                },
            )
            user1_membership_space_room_id_response = self.helper.change_membership(
                room=space_room_id,
                src=user2_id,
                targ=user1_id,
                tok=user2_tok,
                membership=Membership.LEAVE,
                extra_data={
                    "reason": "Bad manners",
                },
            )
        elif test_membership == Membership.BAN:
            # Ban user1 from the rooms
            user1_membership_room_id_no_info_response = self.helper.ban(
                room_id_no_info, src=user2_id, targ=user1_id, tok=user2_tok
            )
            user1_membership_room_id_with_info_response = self.helper.ban(
                room_id_with_info, src=user2_id, targ=user1_id, tok=user2_tok
            )
            user1_membership_space_room_id_response = self.helper.ban(
                space_room_id, src=user2_id, targ=user1_id, tok=user2_tok
            )
        else:
            raise AssertionError("Unknown test_membership")

        # Have user2 leave the rooms to make sure that our background update is not just
        # reading from `current_state_events`. For leave memberships, we should be
        # reading from the historical state.
        self.helper.leave(room_id_no_info, user2_id, tok=user2_tok)
        self.helper.leave(room_id_with_info, user2_id, tok=user2_tok)
        self.helper.leave(space_room_id, user2_id, tok=user2_tok)
        # Check to make sure we actually don't have any `current_state_events` for the rooms
        current_state_check_rows = self.get_success(
            self.store.db_pool.simple_select_many_batch(
                table="current_state_events",
                column="room_id",
                iterable=[room_id_no_info, room_id_with_info, space_room_id],
                retcols=("event_id",),
                keyvalues={},
                desc="check current_state_events in test",
            )
        )
        self.assertEqual(len(current_state_check_rows), 0)

        # Clean-up the `sliding_sync_membership_snapshots` table as if the inserts did not
        # happen during event creation.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_membership_snapshots",
                column="room_id",
                iterable=(room_id_no_info, room_id_with_info, space_room_id),
                keyvalues={},
                desc="sliding_sync_membership_snapshots.test_membership_snapshots_background_update_historical_state",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Make sure the table is populated
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                # The memberships for user1
                (room_id_no_info, user1_id),
                (room_id_with_info, user1_id),
                (space_room_id, user1_id),
                # The leave memberships for user2
                (room_id_no_info, user2_id),
                (room_id_with_info, user2_id),
                (space_room_id, user2_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id_no_info, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_no_info,
                user_id=user1_id,
                # Because user2 kicked/banned user1 from the room
                sender=user2_id,
                membership_event_id=user1_membership_room_id_no_info_response[
                    "event_id"
                ],
                membership=test_membership,
                event_stream_ordering=self.get_success(
                    self.store.get_position_for_event(
                        user1_membership_room_id_no_info_response["event_id"]
                    )
                ).stream,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get(
                (room_id_with_info, user1_id)
            ),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id_with_info,
                user_id=user1_id,
                # Because user2 kicked/banned user1 from the room
                sender=user2_id,
                membership_event_id=user1_membership_room_id_with_info_response[
                    "event_id"
                ],
                membership=test_membership,
                event_stream_ordering=self.get_success(
                    self.store.get_position_for_event(
                        user1_membership_room_id_with_info_response["event_id"]
                    )
                ).stream,
                has_known_state=True,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
                tombstone_successor_room_id="another_room",
            ),
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((space_room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=space_room_id,
                user_id=user1_id,
                # Because user2 kicked/banned user1 from the room
                sender=user2_id,
                membership_event_id=user1_membership_space_room_id_response["event_id"],
                membership=test_membership,
                event_stream_ordering=self.get_success(
                    self.store.get_position_for_event(
                        user1_membership_space_room_id_response["event_id"]
                    )
                ).stream,
                has_known_state=True,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )

    def test_membership_snapshots_background_update_forgotten_missing(self) -> None:
        """
        Test that a new row is inserted into `sliding_sync_membership_snapshots` when it
        doesn't exist in the table yet.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 joins the room
        self.helper.join(room_id, user1_id, tok=user1_tok)
        # User1 leaves the room (we have to leave in order to forget the room)
        self.helper.leave(room_id, user1_id, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )

        # Forget the room
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Clean-up the `sliding_sync_membership_snapshots` table as if the inserts did not
        # happen during event creation.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_membership_snapshots",
                column="room_id",
                iterable=(room_id,),
                keyvalues={},
                desc="sliding_sync_membership_snapshots.test_membership_snapshots_background_update_forgotten_missing",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Make sure the table is populated
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
                membership=Membership.LEAVE,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
                # Room is forgotten
                forgotten=True,
            ),
        )
        # Holds the info according to the current state when the user joined
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user2_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id,
                user_id=user2_id,
                sender=user2_id,
                membership_event_id=state_map[(EventTypes.Member, user2_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user2_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
            ),
        )


class SlidingSyncTablesCatchUpBackgroundUpdatesTestCase(SlidingSyncTablesTestCaseBase):
    """
    Test the background updates for catch-up after Synapse downgrade to populate the
    `sliding_sync_joined_rooms` and `sliding_sync_membership_snapshots` tables.

    This to test the "catch-up" version of the background update vs the "normal"
    background update to populate the tables with all of the historical data. Both
    versions share the same background update but just serve different purposes. We
    check if the "catch-up" version needs to run on start-up based on whether there have
    been any changes to rooms that aren't reflected in the sliding sync tables.

    FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
    foreground update for
    `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
    https://github.com/element-hq/synapse/issues/17623)
    """

    def test_joined_background_update_catch_up_new_room(self) -> None:
        """
        Test that new rooms while Synapse is downgraded (making
        `sliding_sync_joined_rooms` stale) will be caught when Synapse is upgraded and
        the catch-up routine is run.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Instead of testing with various levels of room state that should appear in the
        # table, we're only using one room to keep this test simple. Because the
        # underlying background update to populate these tables is the same as this
        # catch-up routine, we are going to rely on
        # `SlidingSyncTablesBackgroundUpdatesTestCase` to cover that logic.
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Make sure all of the background updates have finished before we start the
        # catch-up. Even though it should work fine if the other background update is
        # still running, we want to see the catch-up routine restore the progress
        # correctly.
        #
        # We also don't want the normal background update messing with our results so we
        # run this before we do our manual database clean-up to simulate new events
        # being sent while Synapse was downgraded.
        self.wait_for_background_updates()

        # Clean-up the `sliding_sync_joined_rooms` table as if the the room never made
        # it into the table. This is to simulate the a new room while Synapse was
        # downgraded.
        self.get_success(
            self.store.db_pool.simple_delete(
                table="sliding_sync_joined_rooms",
                keyvalues={"room_id": room_id},
                desc="simulate new room while Synapse was downgraded",
            )
        )

        # The function under test. It should clear out stale data and start the
        # background update to catch-up on the missing data.
        self.get_success(
            self.store.db_pool.runInteraction(
                "_resolve_stale_data_in_sliding_sync_joined_rooms_table",
                _resolve_stale_data_in_sliding_sync_joined_rooms_table,
            )
        )

        # We shouldn't see any new data yet
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        # Wait for the catch-up background update to finish
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Ensure that the table is populated correctly after the catch-up background
        # update finishes
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id},
            exact=True,
        )

    def test_joined_background_update_catch_up_room_state_change(self) -> None:
        """
        Test that new events while Synapse is downgraded (making
        `sliding_sync_joined_rooms` stale) will be caught when Synapse is upgraded and
        the catch-up routine is run.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Instead of testing with various levels of room state that should appear in the
        # table, we're only using one room to keep this test simple. Because the
        # underlying background update to populate these tables is the same as this
        # catch-up routine, we are going to rely on
        # `SlidingSyncTablesBackgroundUpdatesTestCase` to cover that logic.
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Get a snapshot of the `sliding_sync_joined_rooms` table before we add some state
        sliding_sync_joined_rooms_results_before_state = (
            self._get_sliding_sync_joined_rooms()
        )
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results_before_state.keys()),
            {room_id},
            exact=True,
        )

        # Add a room name
        self.helper.send_state(
            room_id,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user1_tok,
        )

        # Make sure all of the background updates have finished before we start the
        # catch-up. Even though it should work fine if the other background update is
        # still running, we want to see the catch-up routine restore the progress
        # correctly.
        #
        # We also don't want the normal background update messing with our results so we
        # run this before we do our manual database clean-up to simulate new events
        # being sent while Synapse was downgraded.
        self.wait_for_background_updates()

        # Clean-up the `sliding_sync_joined_rooms` table as if the the room name
        # never made it into the table. This is to simulate the room name event
        # being sent while Synapse was downgraded.
        self.get_success(
            self.store.db_pool.simple_update(
                table="sliding_sync_joined_rooms",
                keyvalues={"room_id": room_id},
                updatevalues={
                    # Clear the room name
                    "room_name": None,
                    # Reset the `event_stream_ordering` back to the value before the room name
                    "event_stream_ordering": sliding_sync_joined_rooms_results_before_state[
                        room_id
                    ].event_stream_ordering,
                },
                desc="simulate new events while Synapse was downgraded",
            )
        )

        # The function under test. It should clear out stale data and start the
        # background update to catch-up on the missing data.
        self.get_success(
            self.store.db_pool.runInteraction(
                "_resolve_stale_data_in_sliding_sync_joined_rooms_table",
                _resolve_stale_data_in_sliding_sync_joined_rooms_table,
            )
        )

        # Ensure that the stale data is deleted from the table
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        # Wait for the catch-up background update to finish
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Ensure that the table is populated correctly after the catch-up background
        # update finishes
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id},
            exact=True,
        )

    def test_joined_background_update_catch_up_no_rooms(self) -> None:
        """
        Test that if you start your homeserver with no rooms on a Synapse version that
        supports the sliding sync tables and the historical background update completes
        (because no rooms to process), then Synapse is downgraded and new rooms are
        created/joined; when Synapse is upgraded, the rooms will be processed catch-up
        routine is run.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Instead of testing with various levels of room state that should appear in the
        # table, we're only using one room to keep this test simple. Because the
        # underlying background update to populate these tables is the same as this
        # catch-up routine, we are going to rely on
        # `SlidingSyncTablesBackgroundUpdatesTestCase` to cover that logic.
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Make sure all of the background updates have finished before we start the
        # catch-up. Even though it should work fine if the other background update is
        # still running, we want to see the catch-up routine restore the progress
        # correctly.
        #
        # We also don't want the normal background update messing with our results so we
        # run this before we do our manual database clean-up to simulate room being
        # created while Synapse was downgraded.
        self.wait_for_background_updates()

        # Clean-up the `sliding_sync_joined_rooms` table as if the the room never made
        # it into the table. This is to simulate the room being created while Synapse
        # was downgraded.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_joined_rooms",
                column="room_id",
                iterable=(room_id,),
                keyvalues={},
                desc="simulate room being created while Synapse was downgraded",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        # The function under test. It should clear out stale data and start the
        # background update to catch-up on the missing data.
        self.get_success(
            self.store.db_pool.runInteraction(
                "_resolve_stale_data_in_sliding_sync_joined_rooms_table",
                _resolve_stale_data_in_sliding_sync_joined_rooms_table,
            )
        )

        # We still shouldn't find any data yet
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        # Wait for the catch-up background update to finish
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Ensure that the table is populated correctly after the catch-up background
        # update finishes
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id},
            exact=True,
        )

    def test_membership_snapshots_background_update_catch_up_new_membership(
        self,
    ) -> None:
        """
        Test that completely new membership while Synapse is downgraded (making
        `sliding_sync_membership_snapshots` stale) will be caught when Synapse is
        upgraded and the catch-up routine is run.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Instead of testing with various levels of room state that should appear in the
        # table, we're only using one room to keep this test simple. Because the
        # underlying background update to populate these tables is the same as this
        # catch-up routine, we are going to rely on
        # `SlidingSyncTablesBackgroundUpdatesTestCase` to cover that logic.
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # User2 joins the room
        self.helper.join(room_id, user2_id, tok=user2_tok)

        # Both users are joined to the room
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )

        # Make sure all of the background updates have finished before we start the
        # catch-up. Even though it should work fine if the other background update is
        # still running, we want to see the catch-up routine restore the progress
        # correctly.
        #
        # We also don't want the normal background update messing with our results so we
        # run this before we do our manual database clean-up to simulate new events
        # being sent while Synapse was downgraded.
        self.wait_for_background_updates()

        # Clean-up the `sliding_sync_membership_snapshots` table as if the user2
        # membership never made it into the table. This is to simulate a membership
        # change while Synapse was downgraded.
        self.get_success(
            self.store.db_pool.simple_delete(
                table="sliding_sync_membership_snapshots",
                keyvalues={"room_id": room_id, "user_id": user2_id},
                desc="simulate new membership while Synapse was downgraded",
            )
        )

        # We shouldn't find the user2 membership in the table because we just deleted it
        # in preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
            },
            exact=True,
        )

        # The function under test. It should clear out stale data and start the
        # background update to catch-up on the missing data.
        self.get_success(
            self.store.db_pool.runInteraction(
                "_resolve_stale_data_in_sliding_sync_membership_snapshots_table",
                _resolve_stale_data_in_sliding_sync_membership_snapshots_table,
            )
        )

        # We still shouldn't find any data yet
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
            },
            exact=True,
        )

        # Wait for the catch-up background update to finish
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Ensure that the table is populated correctly after the catch-up background
        # update finishes
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )

    def test_membership_snapshots_background_update_catch_up_membership_change(
        self,
    ) -> None:
        """
        Test that membership changes while Synapse is downgraded (making
        `sliding_sync_membership_snapshots` stale) will be caught when Synapse is upgraded and
        the catch-up routine is run.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Instead of testing with various levels of room state that should appear in the
        # table, we're only using one room to keep this test simple. Because the
        # underlying background update to populate these tables is the same as this
        # catch-up routine, we are going to rely on
        # `SlidingSyncTablesBackgroundUpdatesTestCase` to cover that logic.
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # User2 joins the room
        self.helper.join(room_id, user2_id, tok=user2_tok)

        # Both users are joined to the room
        sliding_sync_membership_snapshots_results_before_membership_changes = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(
                sliding_sync_membership_snapshots_results_before_membership_changes.keys()
            ),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )

        # User2 leaves the room
        self.helper.leave(room_id, user2_id, tok=user2_tok)

        # Make sure all of the background updates have finished before we start the
        # catch-up. Even though it should work fine if the other background update is
        # still running, we want to see the catch-up routine restore the progress
        # correctly.
        #
        # We also don't want the normal background update messing with our results so we
        # run this before we do our manual database clean-up to simulate new events
        # being sent while Synapse was downgraded.
        self.wait_for_background_updates()

        # Rollback the `sliding_sync_membership_snapshots` table as if the user2
        # membership never made it into the table. This is to simulate a membership
        # change while Synapse was downgraded.
        self.get_success(
            self.store.db_pool.simple_update(
                table="sliding_sync_membership_snapshots",
                keyvalues={"room_id": room_id, "user_id": user2_id},
                updatevalues={
                    # Reset everything back to the value before user2 left the room
                    "membership": sliding_sync_membership_snapshots_results_before_membership_changes[
                        (room_id, user2_id)
                    ].membership,
                    "membership_event_id": sliding_sync_membership_snapshots_results_before_membership_changes[
                        (room_id, user2_id)
                    ].membership_event_id,
                    "event_stream_ordering": sliding_sync_membership_snapshots_results_before_membership_changes[
                        (room_id, user2_id)
                    ].event_stream_ordering,
                },
                desc="simulate membership change while Synapse was downgraded",
            )
        )

        # We should see user2 still joined to the room because we made that change in
        # preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user1_id)),
            sliding_sync_membership_snapshots_results_before_membership_changes[
                (room_id, user1_id)
            ],
        )
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user2_id)),
            sliding_sync_membership_snapshots_results_before_membership_changes[
                (room_id, user2_id)
            ],
        )

        # The function under test. It should clear out stale data and start the
        # background update to catch-up on the missing data.
        self.get_success(
            self.store.db_pool.runInteraction(
                "_resolve_stale_data_in_sliding_sync_membership_snapshots_table",
                _resolve_stale_data_in_sliding_sync_membership_snapshots_table,
            )
        )

        # Ensure that the stale data is deleted from the table
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
            },
            exact=True,
        )

        # Wait for the catch-up background update to finish
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Ensure that the table is populated correctly after the catch-up background
        # update finishes
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )

    def test_membership_snapshots_background_update_catch_up_no_membership(
        self,
    ) -> None:
        """
        Test that if you start your homeserver with no rooms on a Synapse version that
        supports the sliding sync tables and the historical background update completes
        (because no rooms/membership to process), then Synapse is downgraded and new
        rooms are created/joined; when Synapse is upgraded, the rooms will be processed
        catch-up routine is run.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Instead of testing with various levels of room state that should appear in the
        # table, we're only using one room to keep this test simple. Because the
        # underlying background update to populate these tables is the same as this
        # catch-up routine, we are going to rely on
        # `SlidingSyncTablesBackgroundUpdatesTestCase` to cover that logic.
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        # User2 joins the room
        self.helper.join(room_id, user2_id, tok=user2_tok)

        # Make sure all of the background updates have finished before we start the
        # catch-up. Even though it should work fine if the other background update is
        # still running, we want to see the catch-up routine restore the progress
        # correctly.
        #
        # We also don't want the normal background update messing with our results so we
        # run this before we do our manual database clean-up to simulate new events
        # being sent while Synapse was downgraded.
        self.wait_for_background_updates()

        # Rollback the `sliding_sync_membership_snapshots` table as if the user2
        # membership never made it into the table. This is to simulate a membership
        # change while Synapse was downgraded.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="sliding_sync_membership_snapshots",
                column="room_id",
                iterable=(room_id,),
                keyvalues={},
                desc="simulate room being created while Synapse was downgraded",
            )
        )

        # We shouldn't find anything in the table because we just deleted them in
        # preparation for the test.
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )

        # The function under test. It should clear out stale data and start the
        # background update to catch-up on the missing data.
        self.get_success(
            self.store.db_pool.runInteraction(
                "_resolve_stale_data_in_sliding_sync_membership_snapshots_table",
                _resolve_stale_data_in_sliding_sync_membership_snapshots_table,
            )
        )

        # We still shouldn't find any data yet
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            set(),
            exact=True,
        )

        # Wait for the catch-up background update to finish
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Ensure that the table is populated correctly after the catch-up background
        # update finishes
        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )


class SlidingSyncMembershipSnapshotsTableFixForgottenColumnBackgroundUpdatesTestCase(
    SlidingSyncTablesTestCaseBase
):
    """
    Test the background updates that fixes `sliding_sync_membership_snapshots` ->
    `forgotten` column.
    """

    def test_membership_snapshots_fix_forgotten_column_background_update(self) -> None:
        """
        Test that the background update, updates the `sliding_sync_membership_snapshots`
        -> `forgotten` column to be in sync with the `room_memberships` table.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # User1 joins the room
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Leave and forget the room
        self.helper.leave(room_id, user1_id, tok=user1_tok)
        # User1 forgets the room
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Re-join the room
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Reset `sliding_sync_membership_snapshots` table as if the `forgotten` column
        # got out of sync from the `room_memberships` table from the previous flawed
        # code.
        self.get_success(
            self.store.db_pool.simple_update_one(
                table="sliding_sync_membership_snapshots",
                keyvalues={"room_id": room_id, "user_id": user1_id},
                updatevalues={"forgotten": 1},
                desc="sliding_sync_membership_snapshots.test_membership_snapshots_fix_forgotten_column_background_update",
            )
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_FIX_FORGOTTEN_COLUMN_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Make sure the table is populated

        sliding_sync_membership_snapshots_results = (
            self._get_sliding_sync_membership_snapshots()
        )
        self.assertIncludes(
            set(sliding_sync_membership_snapshots_results.keys()),
            {
                (room_id, user1_id),
                (room_id, user2_id),
            },
            exact=True,
        )
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )
        # Holds the info according to the current state when the user joined.
        #
        # We only care about checking on user1 as that's what we reset and expect to be
        # correct now
        self.assertEqual(
            sliding_sync_membership_snapshots_results.get((room_id, user1_id)),
            _SlidingSyncMembershipSnapshotResult(
                room_id=room_id,
                user_id=user1_id,
                sender=user1_id,
                membership_event_id=state_map[(EventTypes.Member, user1_id)].event_id,
                membership=Membership.JOIN,
                event_stream_ordering=state_map[
                    (EventTypes.Member, user1_id)
                ].internal_metadata.stream_ordering,
                has_known_state=True,
                room_type=None,
                room_name=None,
                is_encrypted=False,
                tombstone_successor_room_id=None,
                # We should see the room as no longer forgotten
                forgotten=False,
            ),
        )
