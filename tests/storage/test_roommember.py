#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
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
from typing import List, Optional, Tuple, cast

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes, JoinRules, Membership
from synapse.api.room_versions import RoomVersions
from synapse.rest import admin
from synapse.rest.admin import register_servlets_for_client_rest_resource
from synapse.rest.client import knock, login, room
from synapse.server import HomeServer
from synapse.storage.databases.main.roommember import extract_heroes_from_room_summary
from synapse.storage.roommember import MemberSummary
from synapse.types import UserID, create_requester
from synapse.util import Clock

from tests import unittest
from tests.server import TestHomeServer
from tests.test_utils import event_injection
from tests.unittest import skip_unless

logger = logging.getLogger(__name__)


class RoomMemberStoreTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        register_servlets_for_client_rest_resource,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: TestHomeServer) -> None:  # type: ignore[override]
        # We can't test the RoomMemberStore on its own without the other event
        # storage logic
        self.store = hs.get_datastores().main

        self.u_alice = self.register_user("alice", "pass")
        self.t_alice = self.login("alice", "pass")
        self.u_bob = self.register_user("bob", "pass")

        # User elsewhere on another host
        self.u_charlie = UserID.from_string("@charlie:elsewhere")

    def test_one_member(self) -> None:
        # Alice creates the room, and is automatically joined
        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)

        rooms_for_user = self.get_success(
            self.store.get_rooms_for_local_user_where_membership_is(
                self.u_alice, [Membership.JOIN]
            )
        )

        self.assertEqual([self.room], [m.room_id for m in rooms_for_user])

    def test_count_known_servers(self) -> None:
        """
        _count_known_servers will calculate how many servers are in a room.
        """
        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)
        self.inject_room_member(self.room, self.u_bob, Membership.JOIN)
        self.inject_room_member(self.room, self.u_charlie.to_string(), Membership.JOIN)

        servers = self.get_success(self.store._count_known_servers())
        self.assertEqual(servers, 2)

    def test_count_known_servers_stat_counter_disabled(self) -> None:
        """
        If enabled, the metrics for how many servers are known will be counted.
        """
        self.assertTrue("_known_servers_count" not in self.store.__dict__.keys())

        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)
        self.inject_room_member(self.room, self.u_bob, Membership.JOIN)
        self.inject_room_member(self.room, self.u_charlie.to_string(), Membership.JOIN)

        self.pump()

        self.assertTrue("_known_servers_count" not in self.store.__dict__.keys())

    @unittest.override_config(
        {"enable_metrics": True, "metrics_flags": {"known_servers": True}}
    )
    def test_count_known_servers_stat_counter_enabled(self) -> None:
        """
        If enabled, the metrics for how many servers are known will be counted.
        """
        # Initialises to 1 -- itself
        self.assertEqual(self.store._known_servers_count, 1)

        self.pump()

        # No rooms have been joined, so technically the SQL returns 0, but it
        # will still say it knows about itself.
        self.assertEqual(self.store._known_servers_count, 1)

        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)
        self.inject_room_member(self.room, self.u_bob, Membership.JOIN)
        self.inject_room_member(self.room, self.u_charlie.to_string(), Membership.JOIN)

        self.pump(1)

        # It now knows about Charlie's server.
        self.assertEqual(self.store._known_servers_count, 2)

    def test__null_byte_in_display_name_properly_handled(self) -> None:
        room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)

        res = cast(
            List[Tuple[Optional[str], str]],
            self.get_success(
                self.store.db_pool.simple_select_list(
                    "room_memberships",
                    {"user_id": "@alice:test"},
                    ["display_name", "event_id"],
                )
            ),
        )
        # Check that we only got one result back
        self.assertEqual(len(res), 1)

        # Check that alice's display name is "alice"
        self.assertEqual(res[0][0], "alice")

        # Grab the event_id to use later
        event_id = res[0][1]

        # Create a profile with the offending null byte in the display name
        new_profile = {"displayname": "ali\u0000ce"}

        # Ensure that the change goes smoothly and does not fail due to the null byte
        self.helper.change_membership(
            room,
            self.u_alice,
            self.u_alice,
            "join",
            extra_data=new_profile,
            tok=self.t_alice,
        )

        res2 = cast(
            List[Tuple[Optional[str], str]],
            self.get_success(
                self.store.db_pool.simple_select_list(
                    "room_memberships",
                    {"user_id": "@alice:test"},
                    ["display_name", "event_id"],
                )
            ),
        )
        # Check that we only have two results
        self.assertEqual(len(res2), 2)

        # Filter out the previous event using the event_id we grabbed above
        row = [row for row in res2 if row[1] != event_id]

        # Check that alice's display name is now None
        self.assertIsNone(row[0][0])

    def test_room_is_locally_forgotten(self) -> None:
        """Test that when the last local user has forgotten a room it is known as forgotten."""
        # join two local and one remote user
        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)
        self.get_success(
            event_injection.inject_member_event(self.hs, self.room, self.u_bob, "join")
        )
        self.get_success(
            event_injection.inject_member_event(
                self.hs, self.room, self.u_charlie.to_string(), "join"
            )
        )
        self.assertFalse(
            self.get_success(self.store.is_locally_forgotten_room(self.room))
        )

        # local users leave the room and the room is not forgotten
        self.get_success(
            event_injection.inject_member_event(
                self.hs, self.room, self.u_alice, "leave"
            )
        )
        self.get_success(
            event_injection.inject_member_event(self.hs, self.room, self.u_bob, "leave")
        )
        self.assertFalse(
            self.get_success(self.store.is_locally_forgotten_room(self.room))
        )

        # first user forgets the room, room is not forgotten
        self.get_success(self.store.forget(self.u_alice, self.room))
        self.assertFalse(
            self.get_success(self.store.is_locally_forgotten_room(self.room))
        )

        # second (last local) user forgets the room and the room is forgotten
        self.get_success(self.store.forget(self.u_bob, self.room))
        self.assertTrue(
            self.get_success(self.store.is_locally_forgotten_room(self.room))
        )

    def test_join_locally_forgotten_room(self) -> None:
        """Tests if a user joins a forgotten room the room is not forgotten anymore."""
        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)
        self.assertFalse(
            self.get_success(self.store.is_locally_forgotten_room(self.room))
        )

        # after leaving and forget the room, it is forgotten
        self.get_success(
            event_injection.inject_member_event(
                self.hs, self.room, self.u_alice, "leave"
            )
        )
        self.get_success(self.store.forget(self.u_alice, self.room))
        self.assertTrue(
            self.get_success(self.store.is_locally_forgotten_room(self.room))
        )

        # after rejoin the room is not forgotten anymore
        self.get_success(
            event_injection.inject_member_event(
                self.hs, self.room, self.u_alice, "join"
            )
        )
        self.assertFalse(
            self.get_success(self.store.is_locally_forgotten_room(self.room))
        )


class RoomSummaryTestCase(unittest.HomeserverTestCase):
    """
    Test `/sync` room summary related logic like `get_room_summary(...)` and
    `extract_heroes_from_room_summary(...)`
    """

    servlets = [
        admin.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sliding_sync_handler = self.hs.get_sliding_sync_handler()
        self.store = self.hs.get_datastores().main

    def _assert_member_summary(
        self,
        actual_member_summary: MemberSummary,
        expected_member_list: List[str],
        *,
        expected_member_count: Optional[int] = None,
    ) -> None:
        """
        Assert that the `MemberSummary` object has the expected members.
        """
        self.assertListEqual(
            [
                user_id
                for user_id, _membership_event_id in actual_member_summary.members
            ],
            expected_member_list,
        )
        self.assertEqual(
            actual_member_summary.count,
            (
                expected_member_count
                if expected_member_count is not None
                else len(expected_member_list)
            ),
        )

    def test_get_room_summary_membership(self) -> None:
        """
        Test that `get_room_summary(...)` gets every kind of membership when there
        aren't that many members in the room.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        _user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")
        user5_id = self.register_user("user5", "pass")
        user5_tok = self.login(user5_id, "pass")

        # Setup a room (user1 is the creator and is joined to the room)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # User2 is banned
        self.helper.join(room_id, user2_id, tok=user2_tok)
        self.helper.ban(room_id, src=user1_id, targ=user2_id, tok=user1_tok)

        # User3 is invited by user1
        self.helper.invite(room_id, targ=user3_id, tok=user1_tok)

        # User4 leaves
        self.helper.join(room_id, user4_id, tok=user4_tok)
        self.helper.leave(room_id, user4_id, tok=user4_tok)

        # User5 joins
        self.helper.join(room_id, user5_id, tok=user5_tok)

        room_membership_summary = self.get_success(self.store.get_room_summary(room_id))
        empty_ms = MemberSummary([], 0)

        self._assert_member_summary(
            room_membership_summary.get(Membership.JOIN, empty_ms),
            [user1_id, user5_id],
        )
        self._assert_member_summary(
            room_membership_summary.get(Membership.INVITE, empty_ms), [user3_id]
        )
        self._assert_member_summary(
            room_membership_summary.get(Membership.LEAVE, empty_ms), [user4_id]
        )
        self._assert_member_summary(
            room_membership_summary.get(Membership.BAN, empty_ms), [user2_id]
        )
        self._assert_member_summary(
            room_membership_summary.get(Membership.KNOCK, empty_ms),
            [
                # No one knocked
            ],
        )

    def test_get_room_summary_membership_order(self) -> None:
        """
        Test that `get_room_summary(...)` stacks our limit of 6 in this order: joins ->
        invites -> leave -> everything else (bans/knocks)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        _user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")
        user5_id = self.register_user("user5", "pass")
        user5_tok = self.login(user5_id, "pass")
        user6_id = self.register_user("user6", "pass")
        user6_tok = self.login(user6_id, "pass")
        user7_id = self.register_user("user7", "pass")
        user7_tok = self.login(user7_id, "pass")

        # Setup the room (user1 is the creator and is joined to the room)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # We expect the order to be joins -> invites -> leave -> bans so setup the users
        # *NOT* in that same order to make sure we're actually sorting them.

        # User2 is banned
        self.helper.join(room_id, user2_id, tok=user2_tok)
        self.helper.ban(room_id, src=user1_id, targ=user2_id, tok=user1_tok)

        # User3 is invited by user1
        self.helper.invite(room_id, targ=user3_id, tok=user1_tok)

        # User4 leaves
        self.helper.join(room_id, user4_id, tok=user4_tok)
        self.helper.leave(room_id, user4_id, tok=user4_tok)

        # User5, User6, User7 joins
        self.helper.join(room_id, user5_id, tok=user5_tok)
        self.helper.join(room_id, user6_id, tok=user6_tok)
        self.helper.join(room_id, user7_id, tok=user7_tok)

        room_membership_summary = self.get_success(self.store.get_room_summary(room_id))
        empty_ms = MemberSummary([], 0)

        self._assert_member_summary(
            room_membership_summary.get(Membership.JOIN, empty_ms),
            [user1_id, user5_id, user6_id, user7_id],
        )
        self._assert_member_summary(
            room_membership_summary.get(Membership.INVITE, empty_ms), [user3_id]
        )
        self._assert_member_summary(
            room_membership_summary.get(Membership.LEAVE, empty_ms), [user4_id]
        )
        self._assert_member_summary(
            room_membership_summary.get(Membership.BAN, empty_ms),
            [
                # The banned user is not in the summary because the summary can only fit
                # 6 members and prefers everything else before bans
                #
                # user2_id
            ],
            # But we still see the count of banned users
            expected_member_count=1,
        )
        self._assert_member_summary(
            room_membership_summary.get(Membership.KNOCK, empty_ms),
            [
                # No one knocked
            ],
        )

    def test_extract_heroes_from_room_summary_excludes_self(self) -> None:
        """
        Test that `extract_heroes_from_room_summary(...)` does not include the user
        itself.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Setup the room (user1 is the creator and is joined to the room)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # User2 joins
        self.helper.join(room_id, user2_id, tok=user2_tok)

        room_membership_summary = self.get_success(self.store.get_room_summary(room_id))

        # We first ask from the perspective of a random fake user
        hero_user_ids = extract_heroes_from_room_summary(
            room_membership_summary, me="@fakeuser"
        )

        # Make sure user1 is in the room (ensure our test setup is correct)
        self.assertListEqual(hero_user_ids, [user1_id, user2_id])

        # Now, we ask for the room summary from the perspective of user1
        hero_user_ids = extract_heroes_from_room_summary(
            room_membership_summary, me=user1_id
        )

        # User1 should not be included in the list of heroes because they are the one
        # asking
        self.assertListEqual(hero_user_ids, [user2_id])

    def test_extract_heroes_from_room_summary_first_five_joins(self) -> None:
        """
        Test that `extract_heroes_from_room_summary(...)` returns the first 5 joins.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")
        user5_id = self.register_user("user5", "pass")
        user5_tok = self.login(user5_id, "pass")
        user6_id = self.register_user("user6", "pass")
        user6_tok = self.login(user6_id, "pass")
        user7_id = self.register_user("user7", "pass")
        user7_tok = self.login(user7_id, "pass")

        # Setup the room (user1 is the creator and is joined to the room)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # User2 -> User7 joins
        self.helper.join(room_id, user2_id, tok=user2_tok)
        self.helper.join(room_id, user3_id, tok=user3_tok)
        self.helper.join(room_id, user4_id, tok=user4_tok)
        self.helper.join(room_id, user5_id, tok=user5_tok)
        self.helper.join(room_id, user6_id, tok=user6_tok)
        self.helper.join(room_id, user7_id, tok=user7_tok)

        room_membership_summary = self.get_success(self.store.get_room_summary(room_id))

        hero_user_ids = extract_heroes_from_room_summary(
            room_membership_summary, me="@fakuser"
        )

        # First 5 users to join the room
        self.assertListEqual(
            hero_user_ids, [user1_id, user2_id, user3_id, user4_id, user5_id]
        )

    def test_extract_heroes_from_room_summary_membership_order(self) -> None:
        """
        Test that `extract_heroes_from_room_summary(...)` prefers joins/invites over
        everything else.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        _user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")
        user5_id = self.register_user("user5", "pass")
        user5_tok = self.login(user5_id, "pass")

        # Setup the room (user1 is the creator and is joined to the room)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # We expect the order to be joins -> invites -> leave -> bans so setup the users
        # *NOT* in that same order to make sure we're actually sorting them.

        # User2 is banned
        self.helper.join(room_id, user2_id, tok=user2_tok)
        self.helper.ban(room_id, src=user1_id, targ=user2_id, tok=user1_tok)

        # User3 is invited by user1
        self.helper.invite(room_id, targ=user3_id, tok=user1_tok)

        # User4 leaves
        self.helper.join(room_id, user4_id, tok=user4_tok)
        self.helper.leave(room_id, user4_id, tok=user4_tok)

        # User5 joins
        self.helper.join(room_id, user5_id, tok=user5_tok)

        room_membership_summary = self.get_success(self.store.get_room_summary(room_id))

        hero_user_ids = extract_heroes_from_room_summary(
            room_membership_summary, me="@fakeuser"
        )

        # Prefer joins -> invites, over everything else
        self.assertListEqual(
            hero_user_ids,
            [
                # The joins
                user1_id,
                user5_id,
                # The invites
                user3_id,
            ],
        )

    @skip_unless(
        False,
        "Test is not possible because when everyone leaves the room, "
        + "the server is `no_longer_in_room` and we don't have any `current_state_events` to query",
    )
    def test_extract_heroes_from_room_summary_fallback_leave_ban(self) -> None:
        """
        Test that `extract_heroes_from_room_summary(...)` falls back to leave/ban if
        there aren't any joins/invites.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")

        # Setup the room (user1 is the creator and is joined to the room)
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # User2 is banned
        self.helper.join(room_id, user2_id, tok=user2_tok)
        self.helper.ban(room_id, src=user1_id, targ=user2_id, tok=user1_tok)

        # User3 leaves
        self.helper.join(room_id, user3_id, tok=user3_tok)
        self.helper.leave(room_id, user3_id, tok=user3_tok)

        # User1 leaves (we're doing this last because they're the room creator)
        self.helper.leave(room_id, user1_id, tok=user1_tok)

        room_membership_summary = self.get_success(self.store.get_room_summary(room_id))

        hero_user_ids = extract_heroes_from_room_summary(
            room_membership_summary, me="@fakeuser"
        )

        # Fallback to people who left -> banned
        self.assertListEqual(
            hero_user_ids,
            [user3_id, user1_id, user3_id],
        )

    def test_extract_heroes_from_room_summary_excludes_knocks(self) -> None:
        """
        People who knock on the room have (potentially) never been in the room before
        and are total outsiders. Plus the spec doesn't mention them at all for heroes.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Setup the knock room (user1 is the creator and is joined to the room)
        knock_room_id = self.helper.create_room_as(
            user1_id, tok=user1_tok, room_version=RoomVersions.V7.identifier
        )
        self.helper.send_state(
            knock_room_id,
            EventTypes.JoinRules,
            {"join_rule": JoinRules.KNOCK},
            tok=user1_tok,
        )

        # User2 knocks on the room
        knock_channel = self.make_request(
            "POST",
            "/_matrix/client/r0/knock/%s" % (knock_room_id,),
            b"{}",
            user2_tok,
        )
        self.assertEqual(knock_channel.code, 200, knock_channel.result)

        room_membership_summary = self.get_success(
            self.store.get_room_summary(knock_room_id)
        )

        hero_user_ids = extract_heroes_from_room_summary(
            room_membership_summary, me="@fakeuser"
        )

        # user1 is the creator and is joined to the room (should show up as a hero)
        # user2 is knocking on the room (should not show up as a hero)
        self.assertListEqual(
            hero_user_ids,
            [user1_id],
        )


class CurrentStateMembershipUpdateTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.room_creator = hs.get_room_creation_handler()

    def test_can_rerun_update(self) -> None:
        # First make sure we have completed all updates.
        self.wait_for_background_updates()

        # Now let's create a room, which will insert a membership
        user = UserID("alice", "test")
        requester = create_requester(user)
        self.get_success(self.room_creator.create_room(requester, {}))

        # Register the background update to run again.
        self.get_success(
            self.store.db_pool.simple_insert(
                table="background_updates",
                values={
                    "update_name": "current_state_events_membership",
                    "progress_json": "{}",
                    "depends_on": None,
                },
            )
        )

        # ... and tell the DataStore that it hasn't finished all updates yet
        self.store.db_pool.updates._all_done = False

        # Now let's actually drive the updates to completion
        self.wait_for_background_updates()
