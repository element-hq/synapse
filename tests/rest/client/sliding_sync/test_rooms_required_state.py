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

from parameterized import parameterized

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes, Membership
from synapse.handlers.sliding_sync import StateValues
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.test_utils.event_injection import mark_event_as_partial_state

logger = logging.getLogger(__name__)


class SlidingSyncRoomsRequiredStateTestCase(SlidingSyncBase):
    """
    Test `rooms.required_state` in the Sliding Sync API.
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

        # Reset the in-memory cache
        self.hs.get_sliding_sync_handler().connection_store._connections.clear()

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

    def test_rooms_required_state_lazy_loading_room_members(self) -> None:
        """
        Test `rooms.required_state` returns people relevant to the timeline when
        lazy-loading room members, `["m.room.member","$LAZY"]`.
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
    def test_rooms_required_state_leave_ban(self, stop_membership: str) -> None:
        """
        Test `rooms.required_state` should not return state past a leave/ban event.
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

        if stop_membership == Membership.LEAVE:
            # User 1 leaves
            self.helper.leave(room_id1, user1_id, tok=user1_tok)
        elif stop_membership == Membership.BAN:
            # User 1 is banned
            self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

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
        self.helper.leave(room_id1, user3_id, tok=user3_tok)

        # Make the Sliding Sync request with lazy loading for the room members
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Only user2 and user3 sent events in the 3 events we see in the `timeline`
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.Member, user1_id)],
                state_map[(EventTypes.Member, user2_id)],
                state_map[(EventTypes.Member, user3_id)],
                state_map[("org.matrix.foo_state", "")],
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
