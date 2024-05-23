from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes, JoinRules
from synapse.api.room_versions import RoomVersions
from synapse.rest import admin
from synapse.rest.client import knock, login, room
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class GetSyncRoomIdsForUserTestCase(HomeserverTestCase):
    """Tests Sliding Sync handler `get_sync_room_ids_for_user`."""

    servlets = [
        admin.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sliding_sync_handler = self.hs.get_sliding_sync_handler()
        self.store = self.hs.get_datastores().main
        self.event_sources = hs.get_event_sources()

    def test_no_rooms(self) -> None:
        """
        Test when the user has never joined any rooms before
        """
        user1_id = self.register_user("user1", "pass")
        # user1_tok = self.login(user1_id, "pass")

        now_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=now_token,
                to_token=now_token,
            )
        )

        self.assertEqual(room_id_results, set())

    def test_get_newly_joined_room(self) -> None:
        """
        Test that rooms that the user has newly_joined show up. newly_joined is when you
        join after the `from_token` and <= `to_token`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        before_room_token = self.event_sources.get_current_token()

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room_token,
                to_token=after_room_token,
            )
        )

        self.assertEqual(room_id_results, {room_id})

    def test_get_already_joined_room(self) -> None:
        """
        Test that rooms that the user is already joined show up.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room_token,
                to_token=after_room_token,
            )
        )

        self.assertEqual(room_id_results, {room_id})

    def test_get_invited_banned_knocked_room(self) -> None:
        """
        Test that rooms that the user is invited to, banned from, and knocked on show
        up.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room_token = self.event_sources.get_current_token()

        # Setup the invited room (user2 invites user1 to the room)
        invited_room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.invite(invited_room_id, targ=user1_id, tok=user2_tok)

        # Setup the ban room (user2 bans user1 from the room)
        ban_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(ban_room_id, user1_id, tok=user1_tok)
        self.helper.ban(ban_room_id, src=user2_id, targ=user1_id, tok=user2_tok)

        # Setup the knock room (user1 knocks on the room)
        knock_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, room_version=RoomVersions.V7.identifier
        )
        self.helper.send_state(
            knock_room_id,
            EventTypes.JoinRules,
            {"join_rule": JoinRules.KNOCK},
            tok=user2_tok,
        )
        # User1 knocks on the room
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/knock/%s" % (knock_room_id,),
            b"{}",
            user1_tok,
        )
        self.assertEqual(200, channel.code, channel.result)

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room_token,
                to_token=after_room_token,
            )
        )

        # Ensure that the invited, ban, and knock rooms show up
        self.assertEqual(
            room_id_results,
            {
                invited_room_id,
                ban_room_id,
                knock_room_id,
            },
        )

    def test_only_newly_left_rooms_show_up(self) -> None:
        """
        Test that newly_left rooms still show up in the sync response but rooms that
        were left before the `from_token` don't show up. See condition "1)" comments in
        the `get_sync_room_ids_for_user` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Leave before we calculate the `from_token`
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave during the from_token/to_token range (newly_left)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room2_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room2_token,
            )
        )

        # Only the newly_left room should show up
        self.assertEqual(room_id_results, {room_id2})

    def test_no_joins_after_to_token(self) -> None:
        """
        Rooms we join after the `to_token` should not show up. See condition "2b)"
        comments in the `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Room join after after our `to_token` shouldn't show up
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        _ = room_id2

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        self.assertEqual(room_id_results, {room_id1})

    def test_join_during_range_and_left_room_after_to_token(self) -> None:
        """
        Room still shows up if we left the room but were joined during the
        from_token/to_token. See condition "2b)" comments in the
        `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave the room after we already have our tokens
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # We should still see the room because we were joined during the
        # from_token/to_token time period.
        self.assertEqual(room_id_results, {room_id1})

    def test_join_before_range_and_left_room_after_to_token(self) -> None:
        """
        Room still shows up if we left the room but were joined before the `from_token`
        so it should show up. See condition "2b)" comments in the
        `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave the room after we already have our tokens
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # We should still see the room because we were joined before the `from_token`
        self.assertEqual(room_id_results, {room_id1})

    def test_newly_left_during_range_and_join_leave_after_to_token(self) -> None:
        """
        Newly left room should show up. But we're also testing that joining and leaving
        after the `to_token` doesn't mess with the results. See condition "2a)" comments
        in the `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join and leave the room during the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should still show up because it's newly_left during the from/to range
        self.assertEqual(room_id_results, {room_id1})

    def test_leave_before_range_and_join_leave_after_to_token(self) -> None:
        """
        Old left room shouldn't show up. But we're also testing that joining and leaving
        after the `to_token` doesn't mess with the results. See condition "2a)" comments
        in the `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join and leave the room before the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room shouldn't show up because it was left before the `from_token`
        self.assertEqual(room_id_results, set())

    def test_join_leave_multiple_times_during_range_and_after_to_token(
        self,
    ) -> None:
        """
        Join and leave multiple times shouldn't affect rooms from showing up. It just
        matters that we were joined or newly_left in the from/to range. But we're also
        testing that joining and leaving after the `to_token` doesn't mess with the
        results.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join, leave, join back to the room before the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave and Join the room multiple times after we already have our tokens
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because it was newly_left and joined during the from/to range
        self.assertEqual(room_id_results, {room_id1})

    def test_join_leave_multiple_times_before_range_and_after_to_token(
        self,
    ) -> None:
        """
        Join and leave multiple times before the from/to range shouldn't affect rooms
        from showing up. It just matters that we were joined or newly_left in the
        from/to range. But we're also testing that joining and leaving after the
        `to_token` doesn't mess with the results.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join, leave, join back to the room before the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave and Join the room multiple times after we already have our tokens
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because we were joined before the from/to range
        self.assertEqual(room_id_results, {room_id1})

    def test_invite_before_range_and_join_leave_after_to_token(
        self,
    ) -> None:
        """
        Make it look like we joined after the token range but we were invited before the
        from/to range so the room should still show up. See condition "2a)" comments in
        the `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)

        # Invited to the room before the token
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because we were invited before the from/to range
        self.assertEqual(room_id_results, {room_id1})

    def test_multiple_rooms_are_not_confused(
        self,
    ) -> None:
        """
        Test that multiple rooms are not confused as we fixup the list. This test is
        spawning from a real world bug in the code where I was accidentally using
        `event.room_id` in one of the fix-up loops but the `event` being referenced was
        actually from a different loop.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)

        # Invited and left the room before the token
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        # Invited to room2
        self.helper.invite(room_id2, src=user2_id, targ=user1_id, tok=user2_tok)

        before_room3_token = self.event_sources.get_current_token()

        # Invited and left room3 during the from/to range
        room_id3 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        self.helper.invite(room_id3, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.leave(room_id3, user1_id, tok=user1_tok)

        after_room3_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        # Leave room2
        self.helper.leave(room_id2, user1_id, tok=user1_tok)
        # Leave room3
        self.helper.leave(room_id3, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room3_token,
                to_token=after_room3_token,
            )
        )

        self.assertEqual(
            room_id_results,
            {
                # `room_id1` shouldn't show up because we left before the from/to range
                #
                # Room should show up because we were invited before the from/to range
                room_id2,
                # Room should show up because it was newly_left during the from/to range
                room_id3,
            },
        )
