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

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventContentFields, EventTypes, JoinRules, Membership
from synapse.handlers.sliding_sync import StateValues
from synapse.rest.client import knock, login, room, sync
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.test_utils.event_injection import mark_event_as_partial_state

logger = logging.getLogger(__name__)


# Inherit from `str` so that they show up in the test description when we
# `@parameterized.expand(...)` the first parameter
class MembershipAction(str, enum.Enum):
    INVITE = "invite"
    JOIN = "join"
    KNOCK = "knock"
    LEAVE = "leave"
    BAN = "ban"
    KICK = "kick"


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
class SlidingSyncRoomsRequiredStateTestCase(SlidingSyncBase):
    """
    Test `rooms.required_state` in the Sliding Sync API.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        knock.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()

        super().prepare(reactor, clock, hs)

    def test_rooms_no_required_state(self) -> None:
        """
        Empty `rooms.required_state` should not return any state events in the room
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    # Empty `required_state`
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # No `required_state` in response
        self.assertIsNone(
            response_body["rooms"][room_id1].get("required_state"),
            response_body["rooms"][room_id1],
        )

    def test_rooms_required_state_initial_sync(self) -> None:
        """
        Test `rooms.required_state` returns requested state events in the room during an
        initial sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.RoomHistoryVisibility, ""],
                        # This one doesn't exist in the room
                        [EventTypes.Tombstone, ""],
                    ],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.RoomHistoryVisibility, "")],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_incremental_sync(self) -> None:
        """
        Test `rooms.required_state` returns requested state events in the room during an
        incremental sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.RoomHistoryVisibility, ""],
                        # This one doesn't exist in the room
                        [EventTypes.Tombstone, ""],
                    ],
                    "timeline_limit": 1,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # Make the incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # We only return updates but only if we've sent the room down the
        # connection before.
        self.assertIsNone(response_body["rooms"][room_id1].get("required_state"))
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_incremental_sync_restart(self) -> None:
        """
        Test that after a restart (and so the in memory caches are reset) that
        we correctly return an `M_UNKNOWN_POS`
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.RoomHistoryVisibility, ""],
                        # This one doesn't exist in the room
                        [EventTypes.Tombstone, ""],
                    ],
                    "timeline_limit": 1,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Reset the positions
        self.get_success(
            self.store.db_pool.simple_delete(
                table="sliding_sync_connections",
                keyvalues={"user_id": user1_id},
                desc="clear_sliding_sync_connections_cache",
            )
        )

        # Make the Sliding Sync request
        channel = self.make_request(
            method="POST",
            path=self.sync_endpoint + f"?pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(
            channel.json_body["errcode"], "M_UNKNOWN_POS", channel.json_body
        )

    def test_rooms_required_state_wildcard(self) -> None:
        """
        Test `rooms.required_state` returns all state events when using wildcard `["*", "*"]`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="namespaced",
            body={"foo": "bar"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with wildcards for the `event_type` and `state_key`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [StateValues.WILDCARD, StateValues.WILDCARD],
                    ],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            # We should see all the state events in the room
            state_map.values(),
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_wildcard_event_type(self) -> None:
        """
        Test `rooms.required_state` returns relevant state events when using wildcard in
        the event_type `["*", "foobarbaz"]`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key=user2_id,
            body={"foo": "bar"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with wildcards for the `event_type`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [StateValues.WILDCARD, user2_id],
                    ],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # We expect at-least any state event with the `user2_id` as the `state_key`
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Member, user2_id)],
                state_map[("org.matrix.foo_state", user2_id)],
            },
            # Ideally, this would be exact but we're currently returning all state
            # events when the `event_type` is a wildcard.
            exact=False,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_wildcard_state_key(self) -> None:
        """
        Test `rooms.required_state` returns relevant state events when using wildcard in
        the state_key `["foobarbaz","*"]`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request with wildcards for the `state_key`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Member, StateValues.WILDCARD],
                    ],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Member, user1_id)],
                state_map[(EventTypes.Member, user2_id)],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_lazy_loading_room_members_initial_sync(self) -> None:
        """
        On initial sync, test `rooms.required_state` returns people relevant to the
        timeline when lazy-loading room members, `["m.room.member","$LAZY"]`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)

        self.helper.send(room_id1, "1", tok=user2_tok)
        self.helper.send(room_id1, "2", tok=user3_tok)
        self.helper.send(room_id1, "3", tok=user2_tok)

        # Make the Sliding Sync request with lazy loading for the room members
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.Member, StateValues.LAZY],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Only user2 and user3 sent events in the 3 events we see in the `timeline`
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.Member, user2_id)],
                state_map[(EventTypes.Member, user3_id)],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_lazy_loading_room_members_incremental_sync(
        self,
    ) -> None:
        """
        On incremental sync, test `rooms.required_state` returns people relevant to the
        timeline when lazy-loading room members, `["m.room.member","$LAZY"]`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        self.helper.join(room_id1, user4_id, tok=user4_tok)

        self.helper.send(room_id1, "1", tok=user2_tok)
        self.helper.send(room_id1, "2", tok=user2_tok)
        self.helper.send(room_id1, "3", tok=user2_tok)

        # Make the Sliding Sync request with lazy loading for the room members
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.Member, StateValues.LAZY],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Send more timeline events into the room
        self.helper.send(room_id1, "4", tok=user2_tok)
        self.helper.send(room_id1, "5", tok=user4_tok)
        self.helper.send(room_id1, "6", tok=user4_tok)

        # Make an incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Only user2 and user4 sent events in the last 3 events we see in the `timeline`
        # but since we've seen user2 in the last sync (and their membership hasn't
        # changed), we should only see user4 here.
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Member, user4_id)],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    @parameterized.expand(
        [
            (MembershipAction.LEAVE,),
            (MembershipAction.INVITE,),
            (MembershipAction.KNOCK,),
            (MembershipAction.JOIN,),
            (MembershipAction.BAN,),
            (MembershipAction.KICK,),
        ]
    )
    def test_rooms_required_state_changed_membership_in_timeline_lazy_loading_room_members_incremental_sync(
        self,
        room_membership_action: str,
    ) -> None:
        """
        On incremental sync, test `rooms.required_state` returns people relevant to the
        timeline when lazy-loading room members, `["m.room.member","$LAZY"]` **including
        changes to membership**.
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

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # If we're testing knocks, set the room to knock
        if room_membership_action == MembershipAction.KNOCK:
            self.helper.send_state(
                room_id1,
                EventTypes.JoinRules,
                {"join_rule": JoinRules.KNOCK},
                tok=user2_tok,
            )

        # Join the test users to the room
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user3_id, tok=user2_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user4_id, tok=user2_tok)
        self.helper.join(room_id1, user4_id, tok=user4_tok)
        if room_membership_action in (
            MembershipAction.LEAVE,
            MembershipAction.BAN,
            MembershipAction.JOIN,
        ):
            self.helper.invite(room_id1, src=user2_id, targ=user5_id, tok=user2_tok)
            self.helper.join(room_id1, user5_id, tok=user5_tok)

        # Send some messages to fill up the space
        self.helper.send(room_id1, "1", tok=user2_tok)
        self.helper.send(room_id1, "2", tok=user2_tok)
        self.helper.send(room_id1, "3", tok=user2_tok)

        # Make the Sliding Sync request with lazy loading for the room members
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.Member, StateValues.LAZY],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Send more timeline events into the room
        self.helper.send(room_id1, "4", tok=user2_tok)
        self.helper.send(room_id1, "5", tok=user4_tok)
        # The third event will be our membership event concerning user5
        if room_membership_action == MembershipAction.LEAVE:
            # User 5 leaves
            self.helper.leave(room_id1, user5_id, tok=user5_tok)
        elif room_membership_action == MembershipAction.INVITE:
            # User 5 is invited
            self.helper.invite(room_id1, src=user2_id, targ=user5_id, tok=user2_tok)
        elif room_membership_action == MembershipAction.KNOCK:
            # User 5 knocks
            self.helper.knock(room_id1, user5_id, tok=user5_tok)
            # The admin of the room accepts the knock
            self.helper.invite(room_id1, src=user2_id, targ=user5_id, tok=user2_tok)
        elif room_membership_action == MembershipAction.JOIN:
            # Update the display name of user5 (causing a membership change)
            self.helper.send_state(
                room_id1,
                event_type=EventTypes.Member,
                state_key=user5_id,
                body={
                    EventContentFields.MEMBERSHIP: Membership.JOIN,
                    EventContentFields.MEMBERSHIP_DISPLAYNAME: "quick changer",
                },
                tok=user5_tok,
            )
        elif room_membership_action == MembershipAction.BAN:
            self.helper.ban(room_id1, src=user2_id, targ=user5_id, tok=user2_tok)
        elif room_membership_action == MembershipAction.KICK:
            # Kick user5 from the room
            self.helper.change_membership(
                room=room_id1,
                src=user2_id,
                targ=user5_id,
                tok=user2_tok,
                membership=Membership.LEAVE,
                extra_data={
                    "reason": "Bad manners",
                },
            )
        else:
            raise AssertionError(
                f"Unknown room_membership_action: {room_membership_action}"
            )

        # Make an incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Only user2, user4, and user5 sent events in the last 3 events we see in the
        # `timeline`.
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                # This appears because *some* membership in the room changed and the
                # heroes are recalculated and is thrown in because we have it. But this
                # is technically optional and not needed because we've already seen user2
                # in the last sync (and their membership hasn't changed).
                state_map[(EventTypes.Member, user2_id)],
                # Appears because there is a message in the timeline from this user
                state_map[(EventTypes.Member, user4_id)],
                # Appears because there is a membership event in the timeline from this user
                state_map[(EventTypes.Member, user5_id)],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_expand_lazy_loading_room_members_incremental_sync(
        self,
    ) -> None:
        """
        Test that when we expand the `required_state` to include lazy-loading room
        members, it returns people relevant to the timeline.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        self.helper.join(room_id1, user4_id, tok=user4_tok)

        self.helper.send(room_id1, "1", tok=user2_tok)
        self.helper.send(room_id1, "2", tok=user2_tok)
        self.helper.send(room_id1, "3", tok=user2_tok)

        # Make the Sliding Sync request *without* lazy loading for the room members
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Send more timeline events into the room
        self.helper.send(room_id1, "4", tok=user2_tok)
        self.helper.send(room_id1, "5", tok=user4_tok)
        self.helper.send(room_id1, "6", tok=user4_tok)

        # Expand `required_state` and make an incremental Sliding Sync request *with*
        # lazy-loading room members
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
            [EventTypes.Member, StateValues.LAZY],
        ]
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Only user2 and user4 sent events in the last 3 events we see in the `timeline`
        # and we haven't seen any membership before this sync so we should see both
        # users.
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Member, user2_id)],
                state_map[(EventTypes.Member, user4_id)],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "7", tok=user2_tok)
        self.helper.send(room_id1, "8", tok=user4_tok)
        self.helper.send(room_id1, "9", tok=user4_tok)

        # Make another incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Only user2 and user4 sent events in the last 3 events we see in the `timeline`
        # but since we've seen both memberships in the last sync, they shouldn't appear
        # again.
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1].get("required_state", []),
            set(),
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_expand_retract_expand_lazy_loading_room_members_incremental_sync(
        self,
    ) -> None:
        """
        Test that when we expand the `required_state` to include lazy-loading room
        members, it returns people relevant to the timeline.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        self.helper.join(room_id1, user4_id, tok=user4_tok)

        self.helper.send(room_id1, "1", tok=user2_tok)
        self.helper.send(room_id1, "2", tok=user2_tok)
        self.helper.send(room_id1, "3", tok=user2_tok)

        # Make the Sliding Sync request *without* lazy loading for the room members
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Send more timeline events into the room
        self.helper.send(room_id1, "4", tok=user2_tok)
        self.helper.send(room_id1, "5", tok=user4_tok)
        self.helper.send(room_id1, "6", tok=user4_tok)

        # Expand `required_state` and make an incremental Sliding Sync request *with*
        # lazy-loading room members
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
            [EventTypes.Member, StateValues.LAZY],
        ]
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Only user2 and user4 sent events in the last 3 events we see in the `timeline`
        # and we haven't seen any membership before this sync so we should see both
        # users because we're lazy-loading the room members.
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Member, user2_id)],
                state_map[(EventTypes.Member, user4_id)],
            },
            exact=True,
        )

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "msg", tok=user4_tok)

        # Retract `required_state` and make an incremental Sliding Sync request
        # requesting a few memberships
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
            [EventTypes.Member, StateValues.ME],
            [EventTypes.Member, user2_id],
        ]
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # We've seen user2's membership in the last sync so we shouldn't see it here
        # even though it's requested. We should only see user1's membership.
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Member, user1_id)],
            },
            exact=True,
        )

    def test_rooms_required_state_me(self) -> None:
        """
        Test `rooms.required_state` correctly handles $ME.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send(room_id1, "1", tok=user2_tok)

        # Also send normal state events with state keys of the users, first
        # change the power levels to allow this.
        self.helper.send_state(
            room_id1,
            event_type=EventTypes.PowerLevels,
            body={"users": {user1_id: 50, user2_id: 100}},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo",
            state_key=user1_id,
            body={},
            tok=user1_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo",
            state_key=user2_id,
            body={},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with a request for '$ME'.
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.Member, StateValues.ME],
                        ["org.matrix.foo", StateValues.ME],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Only user2 and user3 sent events in the 3 events we see in the `timeline`
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.Member, user1_id)],
                state_map[("org.matrix.foo", user1_id)],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    @parameterized.expand([(Membership.LEAVE,), (Membership.BAN,)])
    def test_rooms_required_state_leave_ban_initial(self, stop_membership: str) -> None:
        """
        Test `rooms.required_state` should not return state past a leave/ban event when
        it's the first "initial" time the room is being sent down the connection.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.Member, "*"],
                        ["org.matrix.foo_state", ""],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)

        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.bar_state",
            state_key="",
            body={"bar": "bar"},
            tok=user2_tok,
        )

        if stop_membership == Membership.LEAVE:
            # User 1 leaves
            self.helper.leave(room_id1, user1_id, tok=user1_tok)
        elif stop_membership == Membership.BAN:
            # User 1 is banned
            self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        # Get the state_map before we change the state as this is the final state we
        # expect User1 to be able to see
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Change the state after user 1 leaves
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "qux"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.bar_state",
            state_key="",
            body={"bar": "qux"},
            tok=user2_tok,
        )
        self.helper.leave(room_id1, user3_id, tok=user3_tok)

        # Make an incremental Sliding Sync request
        #
        # Also expand the required state to include the `org.matrix.bar_state` event.
        # This is just an extra complication of the test.
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.Member, "*"],
                        ["org.matrix.foo_state", ""],
                        ["org.matrix.bar_state", ""],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # We should only see the state up to the leave/ban event
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.Member, user1_id)],
                state_map[(EventTypes.Member, user2_id)],
                state_map[(EventTypes.Member, user3_id)],
                state_map[("org.matrix.foo_state", "")],
                state_map[("org.matrix.bar_state", "")],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    @parameterized.expand([(Membership.LEAVE,), (Membership.BAN,)])
    def test_rooms_required_state_leave_ban_incremental(
        self, stop_membership: str
    ) -> None:
        """
        Test `rooms.required_state` should not return state past a leave/ban event on
        incremental sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)

        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.bar_state",
            state_key="",
            body={"bar": "bar"},
            tok=user2_tok,
        )

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.Member, "*"],
                        ["org.matrix.foo_state", ""],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        if stop_membership == Membership.LEAVE:
            # User 1 leaves
            self.helper.leave(room_id1, user1_id, tok=user1_tok)
        elif stop_membership == Membership.BAN:
            # User 1 is banned
            self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        # Get the state_map before we change the state as this is the final state we
        # expect User1 to be able to see
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Change the state after user 1 leaves
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "qux"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.bar_state",
            state_key="",
            body={"bar": "qux"},
            tok=user2_tok,
        )
        self.helper.leave(room_id1, user3_id, tok=user3_tok)

        # Make an incremental Sliding Sync request
        #
        # Also expand the required state to include the `org.matrix.bar_state` event.
        # This is just an extra complication of the test.
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.Member, "*"],
                        ["org.matrix.foo_state", ""],
                        ["org.matrix.bar_state", ""],
                    ],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # User1 should only see the state up to the leave/ban event
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                # User1 should see their leave/ban membership
                state_map[(EventTypes.Member, user1_id)],
                state_map[("org.matrix.bar_state", "")],
                # The commented out state events were already returned in the initial
                # sync so we shouldn't see them again on the incremental sync. And we
                # shouldn't see the state events that changed after the leave/ban event.
                #
                # state_map[(EventTypes.Create, "")],
                # state_map[(EventTypes.Member, user2_id)],
                # state_map[(EventTypes.Member, user3_id)],
                # state_map[("org.matrix.foo_state", "")],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_combine_superset(self) -> None:
        """
        Test `rooms.required_state` is combined across lists and room subscriptions.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.bar_state",
            state_key="",
            body={"bar": "qux"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with wildcards for the `state_key`
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.Member, user1_id],
                    ],
                    "timeline_limit": 0,
                },
                "bar-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Member, StateValues.WILDCARD],
                        ["org.matrix.foo_state", ""],
                    ],
                    "timeline_limit": 0,
                },
            },
            "room_subscriptions": {
                room_id1: {
                    "required_state": [["org.matrix.bar_state", ""]],
                    "timeline_limit": 0,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.Member, user1_id)],
                state_map[(EventTypes.Member, user2_id)],
                state_map[("org.matrix.foo_state", "")],
                state_map[("org.matrix.bar_state", "")],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_partial_state(self) -> None:
        """
        Test partially-stated room are excluded if they require full state.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        _join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        join_response2 = self.helper.join(room_id2, user1_id, tok=user1_tok)

        # Mark room2 as partial state
        self.get_success(
            mark_event_as_partial_state(self.hs, join_response2["event_id"], room_id2)
        )

        # Make the Sliding Sync request with examples where `must_await_full_state()` is
        # `False`
        sync_body = {
            "lists": {
                "no-state-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                },
                "other-state-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 0,
                },
                "lazy-load-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        # Lazy-load room members
                        [EventTypes.Member, StateValues.LAZY],
                        # Local member
                        [EventTypes.Member, user2_id],
                    ],
                    "timeline_limit": 0,
                },
                "local-members-only-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        # Own user ID
                        [EventTypes.Member, user1_id],
                        # Local member
                        [EventTypes.Member, user2_id],
                    ],
                    "timeline_limit": 0,
                },
                "me-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        # Own user ID
                        [EventTypes.Member, StateValues.ME],
                        # Local member
                        [EventTypes.Member, user2_id],
                    ],
                    "timeline_limit": 0,
                },
                "wildcard-type-local-state-key-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        ["*", user1_id],
                        # Not a user ID
                        ["*", "foobarbaz"],
                        # Not a user ID
                        ["*", "foo.bar.baz"],
                        # Not a user ID
                        ["*", "@foo"],
                    ],
                    "timeline_limit": 0,
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # The list should include both rooms now because we don't need full state
        for list_key in response_body["lists"].keys():
            self.assertIncludes(
                set(response_body["lists"][list_key]["ops"][0]["room_ids"]),
                {room_id2, room_id1},
                exact=True,
                message=f"Expected all rooms to show up for list_key={list_key}. Response "
                + str(response_body["lists"][list_key]),
            )

        # Take each of the list variants and apply them to room subscriptions to make
        # sure the same rules apply
        for list_key in sync_body["lists"].keys():
            sync_body_for_subscriptions = {
                "room_subscriptions": {
                    room_id1: {
                        "required_state": sync_body["lists"][list_key][
                            "required_state"
                        ],
                        "timeline_limit": 0,
                    },
                    room_id2: {
                        "required_state": sync_body["lists"][list_key][
                            "required_state"
                        ],
                        "timeline_limit": 0,
                    },
                }
            }
            response_body, _ = self.do_sync(sync_body_for_subscriptions, tok=user1_tok)

            self.assertIncludes(
                set(response_body["rooms"].keys()),
                {room_id2, room_id1},
                exact=True,
                message=f"Expected all rooms to show up for test_key={list_key}.",
            )

        # =====================================================================

        # Make the Sliding Sync request with examples where `must_await_full_state()` is
        # `True`
        sync_body = {
            "lists": {
                "wildcard-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        ["*", "*"],
                    ],
                    "timeline_limit": 0,
                },
                "wildcard-type-remote-state-key-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        ["*", "@some:remote"],
                        # Not a user ID
                        ["*", "foobarbaz"],
                        # Not a user ID
                        ["*", "foo.bar.baz"],
                        # Not a user ID
                        ["*", "@foo"],
                    ],
                    "timeline_limit": 0,
                },
                "remote-member-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        # Own user ID
                        [EventTypes.Member, user1_id],
                        # Remote member
                        [EventTypes.Member, "@some:remote"],
                        # Local member
                        [EventTypes.Member, user2_id],
                    ],
                    "timeline_limit": 0,
                },
                "lazy-but-remote-member-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        # Lazy-load room members
                        [EventTypes.Member, StateValues.LAZY],
                        # Remote member
                        [EventTypes.Member, "@some:remote"],
                    ],
                    "timeline_limit": 0,
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Make sure the list includes room1 but room2 is excluded because it's still
        # partially-stated
        for list_key in response_body["lists"].keys():
            self.assertIncludes(
                set(response_body["lists"][list_key]["ops"][0]["room_ids"]),
                {room_id1},
                exact=True,
                message=f"Expected only fully-stated rooms to show up for list_key={list_key}. Response "
                + str(response_body["lists"][list_key]),
            )

        # Take each of the list variants and apply them to room subscriptions to make
        # sure the same rules apply
        for list_key in sync_body["lists"].keys():
            sync_body_for_subscriptions = {
                "room_subscriptions": {
                    room_id1: {
                        "required_state": sync_body["lists"][list_key][
                            "required_state"
                        ],
                        "timeline_limit": 0,
                    },
                    room_id2: {
                        "required_state": sync_body["lists"][list_key][
                            "required_state"
                        ],
                        "timeline_limit": 0,
                    },
                }
            }
            response_body, _ = self.do_sync(sync_body_for_subscriptions, tok=user1_tok)

            self.assertIncludes(
                set(response_body["rooms"].keys()),
                {room_id1},
                exact=True,
                message=f"Expected only fully-stated rooms to show up for test_key={list_key}.",
            )

    def test_rooms_required_state_expand(self) -> None:
        """Test that when we expand the required state argument we get the
        expanded state, and not just the changes to the new expanded."""

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room with a room name.
        room_id1 = self.helper.create_room_as(
            user1_id, tok=user1_tok, extra_content={"name": "Foo"}
        )

        # Only request the state event to begin with
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 1,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
            },
            exact=True,
        )

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # Update the sliding sync requests to include the room name
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
            [EventTypes.Name, ""],
        ]
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        # We should see the room name, even though there haven't been any
        # changes.
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Name, "")],
            },
            exact=True,
        )

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # We should not see any state changes.
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("required_state"))

    def test_rooms_required_state_expand_retract_expand(self) -> None:
        """Test that when expanding, retracting and then expanding the required
        state, we get the changes that happened."""

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room with a room name.
        room_id1 = self.helper.create_room_as(
            user1_id, tok=user1_tok, extra_content={"name": "Foo"}
        )

        # Only request the state event to begin with
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 1,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
            },
            exact=True,
        )

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # Update the sliding sync requests to include the room name
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
            [EventTypes.Name, ""],
        ]
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        # We should see the room name, even though there haven't been any
        # changes.
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Name, "")],
            },
            exact=True,
        )

        # Update the room name
        self.helper.send_state(
            room_id1, EventTypes.Name, {"name": "Bar"}, state_key="", tok=user1_tok
        )

        # Update the sliding sync requests to exclude the room name again
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
        ]
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        # We should not see the updated room name in state (though it will be in
        # the timeline).
        self.assertIsNone(response_body["rooms"][room_id1].get("required_state"))

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # Update the sliding sync requests to include the room name again
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
            [EventTypes.Name, ""],
        ]
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        # We should see the *new* room name, even though there haven't been any
        # changes.
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Name, "")],
            },
            exact=True,
        )

    def test_rooms_required_state_expand_deduplicate(self) -> None:
        """Test that when expanding, retracting and then expanding the required
        state, we don't get the state down again if it hasn't changed"""

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room with a room name.
        room_id1 = self.helper.create_room_as(
            user1_id, tok=user1_tok, extra_content={"name": "Foo"}
        )

        # Only request the state event to begin with
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 1,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
            },
            exact=True,
        )

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # Update the sliding sync requests to include the room name
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
            [EventTypes.Name, ""],
        ]
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        # We should see the room name, even though there haven't been any
        # changes.
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Name, "")],
            },
            exact=True,
        )

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # Update the sliding sync requests to exclude the room name again
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
        ]
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        # We should not see any state updates
        self.assertIsNone(response_body["rooms"][room_id1].get("required_state"))

        # Send a message so the room comes down sync.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # Update the sliding sync requests to include the room name again
        sync_body["lists"]["foo-list"]["required_state"] = [
            [EventTypes.Create, ""],
            [EventTypes.Name, ""],
        ]
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        # We should not see the room name again, as we have already sent that
        # down.
        self.assertIsNone(response_body["rooms"][room_id1].get("required_state"))
