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
from copy import deepcopy
from typing import Dict, List, Optional
from unittest.mock import patch

from parameterized import parameterized

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import (
    AccountDataTypes,
    EventContentFields,
    EventTypes,
    JoinRules,
    Membership,
    RoomTypes,
)
from synapse.api.room_versions import RoomVersions
from synapse.events import StrippedStateEvent, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.handlers.sliding_sync import (
    RoomSyncConfig,
    StateValues,
    _RoomMembershipForUser,
)
from synapse.rest import admin
from synapse.rest.client import knock, login, room
from synapse.server import HomeServer
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.types import JsonDict, StreamToken, UserID
from synapse.types.handlers import SlidingSyncConfig
from synapse.util import Clock

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.unittest import HomeserverTestCase, TestCase

logger = logging.getLogger(__name__)


class RoomSyncConfigTestCase(TestCase):
    def _assert_room_config_equal(
        self,
        actual: RoomSyncConfig,
        expected: RoomSyncConfig,
        message_prefix: Optional[str] = None,
    ) -> None:
        self.assertEqual(actual.timeline_limit, expected.timeline_limit, message_prefix)

        # `self.assertEqual(...)` works fine to catch differences but the output is
        # almost impossible to read because of the way it truncates the output and the
        # order doesn't actually matter.
        self.assertCountEqual(
            actual.required_state_map, expected.required_state_map, message_prefix
        )
        for event_type, expected_state_keys in expected.required_state_map.items():
            self.assertCountEqual(
                actual.required_state_map[event_type],
                expected_state_keys,
                f"{message_prefix}: Mismatch for {event_type}",
            )

    @parameterized.expand(
        [
            (
                "from_list_config",
                """
                Test that we can convert a `SlidingSyncConfig.SlidingSyncList` to a
                `RoomSyncConfig`.
                """,
                # Input
                SlidingSyncConfig.SlidingSyncList(
                    timeline_limit=10,
                    required_state=[
                        (EventTypes.Name, ""),
                        (EventTypes.Member, "@foo"),
                        (EventTypes.Member, "@bar"),
                        (EventTypes.Member, "@baz"),
                        (EventTypes.CanonicalAlias, ""),
                    ],
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Name: {""},
                        EventTypes.Member: {
                            "@foo",
                            "@bar",
                            "@baz",
                        },
                        EventTypes.CanonicalAlias: {""},
                    },
                ),
            ),
            (
                "from_room_subscription",
                """
                Test that we can convert a `SlidingSyncConfig.RoomSubscription` to a
                `RoomSyncConfig`.
                """,
                # Input
                SlidingSyncConfig.RoomSubscription(
                    timeline_limit=10,
                    required_state=[
                        (EventTypes.Name, ""),
                        (EventTypes.Member, "@foo"),
                        (EventTypes.Member, "@bar"),
                        (EventTypes.Member, "@baz"),
                        (EventTypes.CanonicalAlias, ""),
                    ],
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Name: {""},
                        EventTypes.Member: {
                            "@foo",
                            "@bar",
                            "@baz",
                        },
                        EventTypes.CanonicalAlias: {""},
                    },
                ),
            ),
            (
                "wildcard",
                """
                Test that a wildcard (*) for both the `event_type` and `state_key` will override
                all other values.

                Note: MSC3575 describes different behavior to how we're handling things here but
                since it's not wrong to return more state than requested (`required_state` is
                just the minimum requested), it doesn't matter if we include things that the
                client wanted excluded. This complexity is also under scrutiny, see
                https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1185109050

                > One unique exception is when you request all state events via ["*", "*"]. When used,
                > all state events are returned by default, and additional entries FILTER OUT the returned set
                > of state events. These additional entries cannot use '*' themselves.
                > For example, ["*", "*"], ["m.room.member", "@alice:example.com"] will _exclude_ every m.room.member
                > event _except_ for @alice:example.com, and include every other state event.
                > In addition, ["*", "*"], ["m.space.child", "*"] is an error, the m.space.child filter is not
                > required as it would have been returned anyway.
                >
                > -- MSC3575 (https://github.com/matrix-org/matrix-spec-proposals/pull/3575)
                """,
                # Input
                SlidingSyncConfig.SlidingSyncList(
                    timeline_limit=10,
                    required_state=[
                        (EventTypes.Name, ""),
                        (StateValues.WILDCARD, StateValues.WILDCARD),
                        (EventTypes.Member, "@foo"),
                        (EventTypes.CanonicalAlias, ""),
                    ],
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        StateValues.WILDCARD: {StateValues.WILDCARD},
                    },
                ),
            ),
            (
                "wildcard_type",
                """
                Test that a wildcard (*) as a `event_type` will override all other values for the
                same `state_key`.
                """,
                # Input
                SlidingSyncConfig.SlidingSyncList(
                    timeline_limit=10,
                    required_state=[
                        (EventTypes.Name, ""),
                        (StateValues.WILDCARD, ""),
                        (EventTypes.Member, "@foo"),
                        (EventTypes.CanonicalAlias, ""),
                    ],
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        StateValues.WILDCARD: {""},
                        EventTypes.Member: {"@foo"},
                    },
                ),
            ),
            (
                "multiple_wildcard_type",
                """
                Test that multiple wildcard (*) as a `event_type` will override all other values
                for the same `state_key`.
                """,
                # Input
                SlidingSyncConfig.SlidingSyncList(
                    timeline_limit=10,
                    required_state=[
                        (EventTypes.Name, ""),
                        (StateValues.WILDCARD, ""),
                        (EventTypes.Member, "@foo"),
                        (StateValues.WILDCARD, "@foo"),
                        ("org.matrix.personal_count", "@foo"),
                        (EventTypes.Member, "@bar"),
                        (EventTypes.CanonicalAlias, ""),
                    ],
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        StateValues.WILDCARD: {
                            "",
                            "@foo",
                        },
                        EventTypes.Member: {"@bar"},
                    },
                ),
            ),
            (
                "wildcard_state_key",
                """
                Test that a wildcard (*) as a `state_key` will override all other values for the
                same `event_type`.
                """,
                # Input
                SlidingSyncConfig.SlidingSyncList(
                    timeline_limit=10,
                    required_state=[
                        (EventTypes.Name, ""),
                        (EventTypes.Member, "@foo"),
                        (EventTypes.Member, StateValues.WILDCARD),
                        (EventTypes.Member, "@bar"),
                        (EventTypes.Member, StateValues.LAZY),
                        (EventTypes.Member, "@baz"),
                        (EventTypes.CanonicalAlias, ""),
                    ],
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Name: {""},
                        EventTypes.Member: {
                            StateValues.WILDCARD,
                        },
                        EventTypes.CanonicalAlias: {""},
                    },
                ),
            ),
            (
                "wildcard_merge",
                """
                Test that a wildcard (*) entries for the `event_type` and another one for
                `state_key` will play together.
                """,
                # Input
                SlidingSyncConfig.SlidingSyncList(
                    timeline_limit=10,
                    required_state=[
                        (EventTypes.Name, ""),
                        (StateValues.WILDCARD, ""),
                        (EventTypes.Member, "@foo"),
                        (EventTypes.Member, StateValues.WILDCARD),
                        (EventTypes.Member, "@bar"),
                        (EventTypes.CanonicalAlias, ""),
                    ],
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        StateValues.WILDCARD: {""},
                        EventTypes.Member: {StateValues.WILDCARD},
                    },
                ),
            ),
            (
                "wildcard_merge2",
                """
                Test that an all wildcard ("*", "*") entry will override any other
                values (including other wildcards).
                """,
                # Input
                SlidingSyncConfig.SlidingSyncList(
                    timeline_limit=10,
                    required_state=[
                        (EventTypes.Name, ""),
                        (StateValues.WILDCARD, ""),
                        (EventTypes.Member, StateValues.WILDCARD),
                        (EventTypes.Member, "@foo"),
                        # One of these should take precedence over everything else
                        (StateValues.WILDCARD, StateValues.WILDCARD),
                        (StateValues.WILDCARD, StateValues.WILDCARD),
                        (EventTypes.CanonicalAlias, ""),
                    ],
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        StateValues.WILDCARD: {StateValues.WILDCARD},
                    },
                ),
            ),
            (
                "lazy_members",
                """
                `$LAZY` room members should just be another additional key next to other
                explicit keys. We will unroll the special `$LAZY` meaning later.
                """,
                # Input
                SlidingSyncConfig.SlidingSyncList(
                    timeline_limit=10,
                    required_state=[
                        (EventTypes.Name, ""),
                        (EventTypes.Member, "@foo"),
                        (EventTypes.Member, "@bar"),
                        (EventTypes.Member, StateValues.LAZY),
                        (EventTypes.Member, "@baz"),
                        (EventTypes.CanonicalAlias, ""),
                    ],
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Name: {""},
                        EventTypes.Member: {
                            "@foo",
                            "@bar",
                            StateValues.LAZY,
                            "@baz",
                        },
                        EventTypes.CanonicalAlias: {""},
                    },
                ),
            ),
        ]
    )
    def test_from_room_config(
        self,
        _test_label: str,
        _test_description: str,
        room_params: SlidingSyncConfig.CommonRoomParameters,
        expected_room_sync_config: RoomSyncConfig,
    ) -> None:
        """
        Test `RoomSyncConfig.from_room_config(room_params)` will result in the `expected_room_sync_config`.
        """
        room_sync_config = RoomSyncConfig.from_room_config(room_params)

        self._assert_room_config_equal(
            room_sync_config,
            expected_room_sync_config,
        )

    @parameterized.expand(
        [
            (
                "no_direct_overlap",
                # A
                RoomSyncConfig(
                    timeline_limit=9,
                    required_state_map={
                        EventTypes.Name: {""},
                        EventTypes.Member: {
                            "@foo",
                            "@bar",
                        },
                    },
                ),
                # B
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Member: {
                            StateValues.LAZY,
                            "@baz",
                        },
                        EventTypes.CanonicalAlias: {""},
                    },
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Name: {""},
                        EventTypes.Member: {
                            "@foo",
                            "@bar",
                            StateValues.LAZY,
                            "@baz",
                        },
                        EventTypes.CanonicalAlias: {""},
                    },
                ),
            ),
            (
                "wildcard_overlap",
                # A
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        StateValues.WILDCARD: {StateValues.WILDCARD},
                    },
                ),
                # B
                RoomSyncConfig(
                    timeline_limit=9,
                    required_state_map={
                        EventTypes.Dummy: {StateValues.WILDCARD},
                        StateValues.WILDCARD: {"@bar"},
                        EventTypes.Member: {"@foo"},
                    },
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        StateValues.WILDCARD: {StateValues.WILDCARD},
                    },
                ),
            ),
            (
                "state_type_wildcard_overlap",
                # A
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Dummy: {"dummy"},
                        StateValues.WILDCARD: {
                            "",
                            "@foo",
                        },
                        EventTypes.Member: {"@bar"},
                    },
                ),
                # B
                RoomSyncConfig(
                    timeline_limit=9,
                    required_state_map={
                        EventTypes.Dummy: {"dummy2"},
                        StateValues.WILDCARD: {
                            "",
                            "@bar",
                        },
                        EventTypes.Member: {"@foo"},
                    },
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Dummy: {
                            "dummy",
                            "dummy2",
                        },
                        StateValues.WILDCARD: {
                            "",
                            "@foo",
                            "@bar",
                        },
                    },
                ),
            ),
            (
                "state_key_wildcard_overlap",
                # A
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Dummy: {"dummy"},
                        EventTypes.Member: {StateValues.WILDCARD},
                        "org.matrix.flowers": {StateValues.WILDCARD},
                    },
                ),
                # B
                RoomSyncConfig(
                    timeline_limit=9,
                    required_state_map={
                        EventTypes.Dummy: {StateValues.WILDCARD},
                        EventTypes.Member: {StateValues.WILDCARD},
                        "org.matrix.flowers": {"tulips"},
                    },
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Dummy: {StateValues.WILDCARD},
                        EventTypes.Member: {StateValues.WILDCARD},
                        "org.matrix.flowers": {StateValues.WILDCARD},
                    },
                ),
            ),
            (
                "state_type_and_state_key_wildcard_merge",
                # A
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Dummy: {"dummy"},
                        StateValues.WILDCARD: {
                            "",
                            "@foo",
                        },
                        EventTypes.Member: {"@bar"},
                    },
                ),
                # B
                RoomSyncConfig(
                    timeline_limit=9,
                    required_state_map={
                        EventTypes.Dummy: {"dummy2"},
                        StateValues.WILDCARD: {""},
                        EventTypes.Member: {StateValues.WILDCARD},
                    },
                ),
                # Expected
                RoomSyncConfig(
                    timeline_limit=10,
                    required_state_map={
                        EventTypes.Dummy: {
                            "dummy",
                            "dummy2",
                        },
                        StateValues.WILDCARD: {
                            "",
                            "@foo",
                        },
                        EventTypes.Member: {StateValues.WILDCARD},
                    },
                ),
            ),
        ]
    )
    def test_combine_room_sync_config(
        self,
        _test_label: str,
        a: RoomSyncConfig,
        b: RoomSyncConfig,
        expected: RoomSyncConfig,
    ) -> None:
        """
        Combine A into B and B into A to make sure we get the same result.
        """
        # Since we're mutating these in place, make a copy for each of our trials
        room_sync_config_a = deepcopy(a)
        room_sync_config_b = deepcopy(b)

        # Combine B into A
        room_sync_config_a.combine_room_sync_config(room_sync_config_b)

        self._assert_room_config_equal(room_sync_config_a, expected, "B into A")

        # Since we're mutating these in place, make a copy for each of our trials
        room_sync_config_a = deepcopy(a)
        room_sync_config_b = deepcopy(b)

        # Combine A into B
        room_sync_config_b.combine_room_sync_config(room_sync_config_a)

        self._assert_room_config_equal(room_sync_config_b, expected, "A into B")


class GetRoomMembershipForUserAtToTokenTestCase(HomeserverTestCase):
    """
    Tests Sliding Sync handler `get_room_membership_for_user_at_to_token()` to make sure it returns
    the correct list of rooms IDs.
    """

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
        self.storage_controllers = hs.get_storage_controllers()

    def test_no_rooms(self) -> None:
        """
        Test when the user has never joined any rooms before
        """
        user1_id = self.register_user("user1", "pass")
        # user1_tok = self.login(user1_id, "pass")

        now_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=now_token,
                to_token=now_token,
            )
        )

        self.assertEqual(room_id_results.keys(), set())

    def test_get_newly_joined_room(self) -> None:
        """
        Test that rooms that the user has newly_joined show up. newly_joined is when you
        join after the `from_token` and <= `to_token`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room_token = self.event_sources.get_current_token()

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response = self.helper.join(room_id, user1_id, tok=user1_tok)

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room_token,
                to_token=after_room_token,
            )
        )

        self.assertEqual(room_id_results.keys(), {room_id})
        # It should be pointing to the join event (latest membership event in the
        # from/to range)
        self.assertEqual(
            room_id_results[room_id].event_id,
            join_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id].membership, Membership.JOIN)
        # We should be considered `newly_joined` because we joined during the token
        # range
        self.assertEqual(room_id_results[room_id].newly_joined, True)
        self.assertEqual(room_id_results[room_id].newly_left, False)

    def test_get_already_joined_room(self) -> None:
        """
        Test that rooms that the user is already joined show up.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response = self.helper.join(room_id, user1_id, tok=user1_tok)

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room_token,
                to_token=after_room_token,
            )
        )

        self.assertEqual(room_id_results.keys(), {room_id})
        # It should be pointing to the join event (latest membership event in the
        # from/to range)
        self.assertEqual(
            room_id_results[room_id].event_id,
            join_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id].membership, Membership.JOIN)
        # We should *NOT* be `newly_joined` because we joined before the token range
        self.assertEqual(room_id_results[room_id].newly_joined, False)
        self.assertEqual(room_id_results[room_id].newly_left, False)

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
        invite_response = self.helper.invite(
            invited_room_id, targ=user1_id, tok=user2_tok
        )

        # Setup the ban room (user2 bans user1 from the room)
        ban_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(ban_room_id, user1_id, tok=user1_tok)
        ban_response = self.helper.ban(
            ban_room_id, src=user2_id, targ=user1_id, tok=user2_tok
        )

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
        knock_channel = self.make_request(
            "POST",
            "/_matrix/client/r0/knock/%s" % (knock_room_id,),
            b"{}",
            user1_tok,
        )
        self.assertEqual(knock_channel.code, 200, knock_channel.result)
        knock_room_membership_state_event = self.get_success(
            self.storage_controllers.state.get_current_state_event(
                knock_room_id, EventTypes.Member, user1_id
            )
        )
        assert knock_room_membership_state_event is not None

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room_token,
                to_token=after_room_token,
            )
        )

        # Ensure that the invited, ban, and knock rooms show up
        self.assertEqual(
            room_id_results.keys(),
            {
                invited_room_id,
                ban_room_id,
                knock_room_id,
            },
        )
        # It should be pointing to the the respective membership event (latest
        # membership event in the from/to range)
        self.assertEqual(
            room_id_results[invited_room_id].event_id,
            invite_response["event_id"],
        )
        self.assertEqual(room_id_results[invited_room_id].membership, Membership.INVITE)
        self.assertEqual(room_id_results[invited_room_id].newly_joined, False)
        self.assertEqual(room_id_results[invited_room_id].newly_left, False)

        self.assertEqual(
            room_id_results[ban_room_id].event_id,
            ban_response["event_id"],
        )
        self.assertEqual(room_id_results[ban_room_id].membership, Membership.BAN)
        self.assertEqual(room_id_results[ban_room_id].newly_joined, False)
        self.assertEqual(room_id_results[ban_room_id].newly_left, False)

        self.assertEqual(
            room_id_results[knock_room_id].event_id,
            knock_room_membership_state_event.event_id,
        )
        self.assertEqual(room_id_results[knock_room_id].membership, Membership.KNOCK)
        self.assertEqual(room_id_results[knock_room_id].newly_joined, False)
        self.assertEqual(room_id_results[knock_room_id].newly_left, False)

    def test_get_kicked_room(self) -> None:
        """
        Test that a room that the user was kicked from still shows up. When the user
        comes back to their client, they should see that they were kicked.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Setup the kick room (user2 kicks user1 from the room)
        kick_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(kick_room_id, user1_id, tok=user1_tok)
        # Kick user1 from the room
        kick_response = self.helper.change_membership(
            room=kick_room_id,
            src=user2_id,
            targ=user1_id,
            tok=user2_tok,
            membership=Membership.LEAVE,
            extra_data={
                "reason": "Bad manners",
            },
        )

        after_kick_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_kick_token,
                to_token=after_kick_token,
            )
        )

        # The kicked room should show up
        self.assertEqual(room_id_results.keys(), {kick_room_id})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[kick_room_id].event_id,
            kick_response["event_id"],
        )
        self.assertEqual(room_id_results[kick_room_id].membership, Membership.LEAVE)
        self.assertNotEqual(room_id_results[kick_room_id].sender, user1_id)
        # We should *NOT* be `newly_joined` because we were not joined at the the time
        # of the `to_token`.
        self.assertEqual(room_id_results[kick_room_id].newly_joined, False)
        self.assertEqual(room_id_results[kick_room_id].newly_left, False)

    def test_forgotten_rooms(self) -> None:
        """
        Forgotten rooms do not show up even if we forget after the from/to range.

        Ideally, we would be able to track when the `/forget` happens and apply it
        accordingly in the token range but the forgotten flag is only an extra bool in
        the `room_memberships` table.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Setup a normal room that we leave. This won't show up in the sync response
        # because we left it before our token but is good to check anyway.
        leave_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(leave_room_id, user1_id, tok=user1_tok)
        self.helper.leave(leave_room_id, user1_id, tok=user1_tok)

        # Setup the ban room (user2 bans user1 from the room)
        ban_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(ban_room_id, user1_id, tok=user1_tok)
        self.helper.ban(ban_room_id, src=user2_id, targ=user1_id, tok=user2_tok)

        # Setup the kick room (user2 kicks user1 from the room)
        kick_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(kick_room_id, user1_id, tok=user1_tok)
        # Kick user1 from the room
        self.helper.change_membership(
            room=kick_room_id,
            src=user2_id,
            targ=user1_id,
            tok=user2_tok,
            membership=Membership.LEAVE,
            extra_data={
                "reason": "Bad manners",
            },
        )

        before_room_forgets = self.event_sources.get_current_token()

        # Forget the room after we already have our tokens. This doesn't change
        # the membership event itself but will mark it internally in Synapse
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{leave_room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{ban_room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{kick_room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room_forgets,
                to_token=before_room_forgets,
            )
        )

        # We shouldn't see the room because it was forgotten
        self.assertEqual(room_id_results.keys(), set())

    def test_newly_left_rooms(self) -> None:
        """
        Test that newly_left are marked properly
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Leave before we calculate the `from_token`
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        leave_response1 = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave during the from_token/to_token range (newly_left)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        leave_response2 = self.helper.leave(room_id2, user1_id, tok=user1_tok)

        after_room2_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room2_token,
            )
        )

        self.assertEqual(room_id_results.keys(), {room_id1, room_id2})

        self.assertEqual(
            room_id_results[room_id1].event_id,
            leave_response1["event_id"],
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined` or `newly_left` because that happened before
        # the from/to range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

        self.assertEqual(
            room_id_results[room_id2].event_id,
            leave_response2["event_id"],
        )
        self.assertEqual(room_id_results[room_id2].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined` because we are instead `newly_left`
        self.assertEqual(room_id_results[room_id2].newly_joined, False)
        self.assertEqual(room_id_results[room_id2].newly_left, True)

    def test_no_joins_after_to_token(self) -> None:
        """
        Rooms we join after the `to_token` should *not* show up. See condition "1b)"
        comments in the `get_room_membership_for_user_at_to_token()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Room join after our `to_token` shouldn't show up
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id2, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            join_response1["event_id"],
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should be `newly_joined` because we joined during the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, True)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_join_during_range_and_left_room_after_to_token(self) -> None:
        """
        Room still shows up if we left the room but were joined during the
        from_token/to_token. See condition "1a)" comments in the
        `get_room_membership_for_user_at_to_token()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave the room after we already have our tokens
        leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # We should still see the room because we were joined during the
        # from_token/to_token time period.
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            join_response["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response": join_response["event_id"],
                    "leave_response": leave_response["event_id"],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should be `newly_joined` because we joined during the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, True)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_join_before_range_and_left_room_after_to_token(self) -> None:
        """
        Room still shows up if we left the room but were joined before the `from_token`
        so it should show up. See condition "1a)" comments in the
        `get_room_membership_for_user_at_to_token()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave the room after we already have our tokens
        leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # We should still see the room because we were joined before the `from_token`
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            join_response["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response": join_response["event_id"],
                    "leave_response": leave_response["event_id"],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should *NOT* be `newly_joined` because we joined before the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_kicked_before_range_and_left_after_to_token(self) -> None:
        """
        Room still shows up if we left the room but were kicked before the `from_token`
        so it should show up. See condition "1a)" comments in the
        `get_room_membership_for_user_at_to_token()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Setup the kick room (user2 kicks user1 from the room)
        kick_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        join_response1 = self.helper.join(kick_room_id, user1_id, tok=user1_tok)
        # Kick user1 from the room
        kick_response = self.helper.change_membership(
            room=kick_room_id,
            src=user2_id,
            targ=user1_id,
            tok=user2_tok,
            membership=Membership.LEAVE,
            extra_data={
                "reason": "Bad manners",
            },
        )

        after_kick_token = self.event_sources.get_current_token()

        # Leave the room after we already have our tokens
        #
        # We have to join before we can leave (leave -> leave isn't a valid transition
        # or at least it doesn't work in Synapse, 403 forbidden)
        join_response2 = self.helper.join(kick_room_id, user1_id, tok=user1_tok)
        leave_response = self.helper.leave(kick_room_id, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_kick_token,
                to_token=after_kick_token,
            )
        )

        # We shouldn't see the room because it was forgotten
        self.assertEqual(room_id_results.keys(), {kick_room_id})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[kick_room_id].event_id,
            kick_response["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response1": join_response1["event_id"],
                    "kick_response": kick_response["event_id"],
                    "join_response2": join_response2["event_id"],
                    "leave_response": leave_response["event_id"],
                }
            ),
        )
        self.assertEqual(room_id_results[kick_room_id].membership, Membership.LEAVE)
        self.assertNotEqual(room_id_results[kick_room_id].sender, user1_id)
        # We should *NOT* be `newly_joined` because we were kicked
        self.assertEqual(room_id_results[kick_room_id].newly_joined, False)
        self.assertEqual(room_id_results[kick_room_id].newly_left, False)

    def test_newly_left_during_range_and_join_leave_after_to_token(self) -> None:
        """
        Newly left room should show up. But we're also testing that joining and leaving
        after the `to_token` doesn't mess with the results. See condition "2)" and "1a)"
        comments in the `get_room_membership_for_user_at_to_token()` method.
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
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response1 = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        join_response2 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response2 = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should still show up because it's newly_left during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            leave_response1["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response1": join_response1["event_id"],
                    "leave_response1": leave_response1["event_id"],
                    "join_response2": join_response2["event_id"],
                    "leave_response2": leave_response2["event_id"],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined` because we are actually `newly_left` during
        # the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, True)

    def test_newly_left_during_range_and_join_after_to_token(self) -> None:
        """
        Newly left room should show up. But we're also testing that joining after the
        `to_token` doesn't mess with the results. See condition "2)" and "1b)" comments
        in the `get_room_membership_for_user_at_to_token()` method.
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
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response1 = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join the room after we already have our tokens
        join_response2 = self.helper.join(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should still show up because it's newly_left during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            leave_response1["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response1": join_response1["event_id"],
                    "leave_response1": leave_response1["event_id"],
                    "join_response2": join_response2["event_id"],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined` because we are actually `newly_left` during
        # the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, True)

    def test_no_from_token(self) -> None:
        """
        Test that if we don't provide a `from_token`, we get all the rooms that we had
        membership in up to the `to_token`.

        Providing `from_token` only really has the effect that it marks rooms as
        `newly_left` in the response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)

        # Join room1
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Join and leave the room2 before the `to_token`
        self.helper.join(room_id2, user1_id, tok=user1_tok)
        leave_response2 = self.helper.leave(room_id2, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join the room2 after we already have our tokens
        self.helper.join(room_id2, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=None,
                to_token=after_room1_token,
            )
        )

        # Only rooms we were joined to before the `to_token` should show up
        self.assertEqual(room_id_results.keys(), {room_id1, room_id2})

        # Room1
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            join_response1["event_id"],
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should *NOT* be `newly_joined`/`newly_left` because there is no
        # `from_token` to define a "live" range to compare against
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

        # Room2
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id2].event_id,
            leave_response2["event_id"],
        )
        self.assertEqual(room_id_results[room_id2].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined`/`newly_left` because there is no
        # `from_token` to define a "live" range to compare against
        self.assertEqual(room_id_results[room_id2].newly_joined, False)
        self.assertEqual(room_id_results[room_id2].newly_left, False)

    def test_from_token_ahead_of_to_token(self) -> None:
        """
        Test when the provided `from_token` comes after the `to_token`. We should
        basically expect the same result as having no `from_token`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        room_id3 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        room_id4 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)

        # Join room1 before `to_token`
        join_room1_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Join and leave the room2 before `to_token`
        _join_room2_response1 = self.helper.join(room_id2, user1_id, tok=user1_tok)
        leave_room2_response1 = self.helper.leave(room_id2, user1_id, tok=user1_tok)

        # Note: These are purposely swapped. The `from_token` should come after
        # the `to_token` in this test
        to_token = self.event_sources.get_current_token()

        # Join room2 after `to_token`
        _join_room2_response2 = self.helper.join(room_id2, user1_id, tok=user1_tok)

        # --------

        # Join room3 after `to_token`
        _join_room3_response1 = self.helper.join(room_id3, user1_id, tok=user1_tok)

        # Join and leave the room4 after `to_token`
        _join_room4_response1 = self.helper.join(room_id4, user1_id, tok=user1_tok)
        _leave_room4_response1 = self.helper.leave(room_id4, user1_id, tok=user1_tok)

        # Note: These are purposely swapped. The `from_token` should come after the
        # `to_token` in this test
        from_token = self.event_sources.get_current_token()

        # Join the room4 after we already have our tokens
        self.helper.join(room_id4, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=from_token,
                to_token=to_token,
            )
        )

        # In the "current" state snapshot, we're joined to all of the rooms but in the
        # from/to token range...
        self.assertIncludes(
            room_id_results.keys(),
            {
                # Included because we were joined before both tokens
                room_id1,
                # Included because we had membership before the to_token
                room_id2,
                # Excluded because we joined after the `to_token`
                # room_id3,
                # Excluded because we joined after the `to_token`
                # room_id4,
            },
            exact=True,
        )

        # Room1
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            join_room1_response1["event_id"],
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should *NOT* be `newly_joined`/`newly_left` because we joined `room1`
        # before either of the tokens
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

        # Room2
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id2].event_id,
            leave_room2_response1["event_id"],
        )
        self.assertEqual(room_id_results[room_id2].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined`/`newly_left` because we joined and left
        # `room1` before either of the tokens
        self.assertEqual(room_id_results[room_id2].newly_joined, False)
        self.assertEqual(room_id_results[room_id2].newly_left, False)

    def test_leave_before_range_and_join_leave_after_to_token(self) -> None:
        """
        Test old left rooms. But we're also testing that joining and leaving after the
        `to_token` doesn't mess with the results. See condition "1a)" comments in the
        `get_room_membership_for_user_at_to_token()` method.
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
        leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            leave_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined`/`newly_left` because we joined and left
        # `room1` before either of the tokens
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_leave_before_range_and_join_after_to_token(self) -> None:
        """
        Test old left room. But we're also testing that joining after the `to_token`
        doesn't mess with the results. See condition "1b)" comments in the
        `get_room_membership_for_user_at_to_token()` method.
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
        leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            leave_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined`/`newly_left` because we joined and left
        # `room1` before either of the tokens
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_join_leave_multiple_times_during_range_and_after_to_token(
        self,
    ) -> None:
        """
        Join and leave multiple times shouldn't affect rooms from showing up. It just
        matters that we had membership in the from/to range. But we're also testing that
        joining and leaving after the `to_token` doesn't mess with the results.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join, leave, join back to the room during the from/to range
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response1 = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        join_response2 = self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave and Join the room multiple times after we already have our tokens
        leave_response2 = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        join_response3 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response3 = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because it was newly_left and joined during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            join_response2["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response1": join_response1["event_id"],
                    "leave_response1": leave_response1["event_id"],
                    "join_response2": join_response2["event_id"],
                    "leave_response2": leave_response2["event_id"],
                    "join_response3": join_response3["event_id"],
                    "leave_response3": leave_response3["event_id"],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should be `newly_joined` because we joined during the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, True)
        # We should *NOT* be `newly_left` because we joined during the token range and
        # was still joined at the end of the range
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_join_leave_multiple_times_before_range_and_after_to_token(
        self,
    ) -> None:
        """
        Join and leave multiple times before the from/to range shouldn't affect rooms
        from showing up. It just matters that we had membership in the
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
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response1 = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        join_response2 = self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave and Join the room multiple times after we already have our tokens
        leave_response2 = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        join_response3 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response3 = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because we were joined before the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            join_response2["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response1": join_response1["event_id"],
                    "leave_response1": leave_response1["event_id"],
                    "join_response2": join_response2["event_id"],
                    "leave_response2": leave_response2["event_id"],
                    "join_response3": join_response3["event_id"],
                    "leave_response3": leave_response3["event_id"],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should *NOT* be `newly_joined` because we joined before the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_invite_before_range_and_join_leave_after_to_token(
        self,
    ) -> None:
        """
        Make it look like we joined after the token range but we were invited before the
        from/to range so the room should still show up. See condition "1a)" comments in
        the `get_room_membership_for_user_at_to_token()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)

        # Invited to the room before the token
        invite_response = self.helper.invite(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )

        after_room1_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        join_respsonse = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because we were invited before the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            invite_response["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "invite_response": invite_response["event_id"],
                    "join_respsonse": join_respsonse["event_id"],
                    "leave_response": leave_response["event_id"],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.INVITE)
        # We should *NOT* be `newly_joined` because we were only invited before the
        # token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_join_and_display_name_changes_in_token_range(
        self,
    ) -> None:
        """
        Test that we point to the correct membership event within the from/to range even
        if there are multiple `join` membership events in a row indicating
        `displayname`/`avatar_url` updates.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        # Update the displayname during the token range
        displayname_change_during_token_range_response = self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname during token range",
            },
            tok=user1_tok,
        )

        after_room1_token = self.event_sources.get_current_token()

        # Update the displayname after the token range
        displayname_change_after_token_range_response = self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname after token range",
            },
            tok=user1_tok,
        )

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because we were joined during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            displayname_change_during_token_range_response["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response": join_response["event_id"],
                    "displayname_change_during_token_range_response": displayname_change_during_token_range_response[
                        "event_id"
                    ],
                    "displayname_change_after_token_range_response": displayname_change_after_token_range_response[
                        "event_id"
                    ],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should be `newly_joined` because we joined during the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, True)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_display_name_changes_in_token_range(
        self,
    ) -> None:
        """
        Test that we point to the correct membership event within the from/to range even
        if there is `displayname`/`avatar_url` updates.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Update the displayname during the token range
        displayname_change_during_token_range_response = self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname during token range",
            },
            tok=user1_tok,
        )

        after_change1_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_change1_token,
            )
        )

        # Room should show up because we were joined during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            displayname_change_during_token_range_response["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response": join_response["event_id"],
                    "displayname_change_during_token_range_response": displayname_change_during_token_range_response[
                        "event_id"
                    ],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should *NOT* be `newly_joined` because we joined before the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_display_name_changes_before_and_after_token_range(
        self,
    ) -> None:
        """
        Test that we point to the correct membership event even though there are no
        membership events in the from/range but there are `displayname`/`avatar_url`
        changes before/after the token range.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        # Update the displayname before the token range
        displayname_change_before_token_range_response = self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname during token range",
            },
            tok=user1_tok,
        )

        after_room1_token = self.event_sources.get_current_token()

        # Update the displayname after the token range
        displayname_change_after_token_range_response = self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname after token range",
            },
            tok=user1_tok,
        )

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because we were joined before the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            displayname_change_before_token_range_response["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response": join_response["event_id"],
                    "displayname_change_before_token_range_response": displayname_change_before_token_range_response[
                        "event_id"
                    ],
                    "displayname_change_after_token_range_response": displayname_change_after_token_range_response[
                        "event_id"
                    ],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should *NOT* be `newly_joined` because we joined before the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_display_name_changes_leave_after_token_range(
        self,
    ) -> None:
        """
        Test that we point to the correct membership event within the from/to range even
        if there are multiple `join` membership events in a row indicating
        `displayname`/`avatar_url` updates and we leave after the `to_token`.

        See condition "1a)" comments in the `get_room_membership_for_user_at_to_token()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        # Update the displayname during the token range
        displayname_change_during_token_range_response = self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname during token range",
            },
            tok=user1_tok,
        )

        after_room1_token = self.event_sources.get_current_token()

        # Update the displayname after the token range
        displayname_change_after_token_range_response = self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname after token range",
            },
            tok=user1_tok,
        )

        # Leave after the token
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because we were joined during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            displayname_change_during_token_range_response["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response": join_response["event_id"],
                    "displayname_change_during_token_range_response": displayname_change_during_token_range_response[
                        "event_id"
                    ],
                    "displayname_change_after_token_range_response": displayname_change_after_token_range_response[
                        "event_id"
                    ],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should be `newly_joined` because we joined during the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, True)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_display_name_changes_join_after_token_range(
        self,
    ) -> None:
        """
        Test that multiple `join` membership events (after the `to_token`) in a row
        indicating `displayname`/`avatar_url` updates doesn't affect the results (we
        joined after the token range so it shouldn't show up)

        See condition "1b)" comments in the `get_room_membership_for_user_at_to_token()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)

        after_room1_token = self.event_sources.get_current_token()

        self.helper.join(room_id1, user1_id, tok=user1_tok)
        # Update the displayname after the token range
        self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname after token range",
            },
            tok=user1_tok,
        )

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room shouldn't show up because we joined after the from/to range
        self.assertEqual(room_id_results.keys(), set())

    def test_newly_joined_with_leave_join_in_token_range(
        self,
    ) -> None:
        """
        Test that even though we're joined before the token range, if we leave and join
        within the token range, it's still counted as `newly_joined`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave and join back during the token range
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        join_response2 = self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_more_changes_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_more_changes_token,
            )
        )

        # Room should show up because we were joined during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            join_response2["event_id"],
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should be considered `newly_joined` because there is some non-join event in
        # between our latest join event.
        self.assertEqual(room_id_results[room_id1].newly_joined, True)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

    def test_newly_joined_only_joins_during_token_range(
        self,
    ) -> None:
        """
        Test that a join and more joins caused by display name changes, all during the
        token range, still count as `newly_joined`.
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
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        # Update the displayname during the token range (looks like another join)
        displayname_change_during_token_range_response1 = self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname during token range",
            },
            tok=user1_tok,
        )
        # Update the displayname during the token range (looks like another join)
        displayname_change_during_token_range_response2 = self.helper.send_state(
            room_id1,
            event_type=EventTypes.Member,
            state_key=user1_id,
            body={
                "membership": Membership.JOIN,
                "displayname": "displayname during token range",
            },
            tok=user1_tok,
        )

        after_room1_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because it was newly_left and joined during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            displayname_change_during_token_range_response2["event_id"],
            "Corresponding map to disambiguate the opaque event IDs: "
            + str(
                {
                    "join_response1": join_response1["event_id"],
                    "displayname_change_during_token_range_response1": displayname_change_during_token_range_response1[
                        "event_id"
                    ],
                    "displayname_change_during_token_range_response2": displayname_change_during_token_range_response2[
                        "event_id"
                    ],
                }
            ),
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should be `newly_joined` because we first joined during the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, True)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

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
        leave_room1_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        # Invited to room2
        invite_room2_response = self.helper.invite(
            room_id2, src=user2_id, targ=user1_id, tok=user2_tok
        )

        before_room3_token = self.event_sources.get_current_token()

        # Invited and left room3 during the from/to range
        room_id3 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        self.helper.invite(room_id3, src=user2_id, targ=user1_id, tok=user2_tok)
        leave_room3_response = self.helper.leave(room_id3, user1_id, tok=user1_tok)

        after_room3_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        # Leave room2
        self.helper.leave(room_id2, user1_id, tok=user1_tok)
        # Leave room3
        self.helper.leave(room_id3, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_room3_token,
                to_token=after_room3_token,
            )
        )

        self.assertEqual(
            room_id_results.keys(),
            {
                # Left before the from/to range
                room_id1,
                # Invited before the from/to range
                room_id2,
                # `newly_left` during the from/to range
                room_id3,
            },
        )

        # Room1
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            leave_room1_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined`/`newly_left` because we were invited and left
        # before the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

        # Room2
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id2].event_id,
            invite_room2_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id2].membership, Membership.INVITE)
        # We should *NOT* be `newly_joined`/`newly_left` because we were invited before
        # the token range
        self.assertEqual(room_id_results[room_id2].newly_joined, False)
        self.assertEqual(room_id_results[room_id2].newly_left, False)

        # Room3
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id3].event_id,
            leave_room3_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id3].membership, Membership.LEAVE)
        # We should be `newly_left` because we were invited and left during
        # the token range
        self.assertEqual(room_id_results[room_id3].newly_joined, False)
        self.assertEqual(room_id_results[room_id3].newly_left, True)

    def test_state_reset(self) -> None:
        """
        Test a state reset scenario where the user gets removed from the room (when
        there is no corresponding leave event)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # The room where the state reset will happen
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Join another room so we don't hit the short-circuit and return early if they
        # have no room membership
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id2, user1_id, tok=user1_tok)

        before_reset_token = self.event_sources.get_current_token()

        # Send another state event to make a position for the state reset to happen at
        dummy_state_response = self.helper.send_state(
            room_id1,
            event_type="foobarbaz",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        dummy_state_pos = self.get_success(
            self.store.get_position_for_event(dummy_state_response["event_id"])
        )

        # Mock a state reset removing the membership for user1 in the current state
        self.get_success(
            self.store.db_pool.simple_delete(
                table="current_state_events",
                keyvalues={
                    "room_id": room_id1,
                    "type": EventTypes.Member,
                    "state_key": user1_id,
                },
                desc="state reset user in current_state_events",
            )
        )
        self.get_success(
            self.store.db_pool.simple_delete(
                table="local_current_membership",
                keyvalues={
                    "room_id": room_id1,
                    "user_id": user1_id,
                },
                desc="state reset user in local_current_membership",
            )
        )
        self.get_success(
            self.store.db_pool.simple_insert(
                table="current_state_delta_stream",
                values={
                    "stream_id": dummy_state_pos.stream,
                    "room_id": room_id1,
                    "type": EventTypes.Member,
                    "state_key": user1_id,
                    "event_id": None,
                    "prev_event_id": join_response1["event_id"],
                    "instance_name": dummy_state_pos.instance_name,
                },
                desc="state reset user in current_state_delta_stream",
            )
        )

        # Manually bust the cache since we we're just manually messing with the database
        # and not causing an actual state reset.
        self.store._membership_stream_cache.entity_has_changed(
            user1_id, dummy_state_pos.stream
        )

        after_reset_token = self.event_sources.get_current_token()

        # The function under test
        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_reset_token,
                to_token=after_reset_token,
            )
        )

        # Room1 should show up because it was `newly_left` via state reset during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1, room_id2})
        # It should be pointing to no event because we were removed from the room
        # without a corresponding leave event
        self.assertEqual(
            room_id_results[room_id1].event_id,
            None,
        )
        # State reset caused us to leave the room and there is no corresponding leave event
        self.assertEqual(room_id_results[room_id1].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined` because we joined before the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        # We should be `newly_left` because we were removed via state reset during the from/to range
        self.assertEqual(room_id_results[room_id1].newly_left, True)


class GetRoomMembershipForUserAtToTokenShardTestCase(BaseMultiWorkerStreamTestCase):
    """
    Tests Sliding Sync handler `get_room_membership_for_user_at_to_token()` to make sure it works with
    sharded event stream_writers enabled
    """

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}

        # Enable shared event stream_writers
        config["stream_writers"] = {"events": ["worker1", "worker2", "worker3"]}
        config["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "worker1": {"host": "testserv", "port": 1001},
            "worker2": {"host": "testserv", "port": 1002},
            "worker3": {"host": "testserv", "port": 1003},
        }
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sliding_sync_handler = self.hs.get_sliding_sync_handler()
        self.store = self.hs.get_datastores().main
        self.event_sources = hs.get_event_sources()

    def _create_room(self, room_id: str, user_id: str, tok: str) -> None:
        """
        Create a room with a specific room_id. We use this so that that we have a
        consistent room_id across test runs that hashes to the same value and will be
        sharded to a known worker in the tests.
        """

        # We control the room ID generation by patching out the
        # `_generate_room_id` method
        with patch(
            "synapse.handlers.room.RoomCreationHandler._generate_room_id"
        ) as mock:
            mock.side_effect = lambda: room_id
            self.helper.create_room_as(user_id, tok=tok)

    def test_sharded_event_persisters(self) -> None:
        """
        This test should catch bugs that would come from flawed stream position
        (`stream_ordering`) comparisons or making `RoomStreamToken`'s naively. To
        compare event positions properly, you need to consider both the `instance_name`
        and `stream_ordering` together.

        The test creates three event persister workers and a room that is sharded to
        each worker. On worker2, we make the event stream position stuck so that it lags
        behind the other workers and we start getting `RoomStreamToken` that have an
        `instance_map` component (i.e. q`m{min_pos}~{writer1}.{pos1}~{writer2}.{pos2}`).

        We then send some events to advance the stream positions of worker1 and worker3
        but worker2 is lagging behind because it's stuck. We are specifically testing
        that `get_room_membership_for_user_at_to_token(from_token=xxx, to_token=xxx)` should work
        correctly in these adverse conditions.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker1"},
        )

        worker_hs2 = self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker2"},
        )

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker3"},
        )

        # Specially crafted room IDs that get persisted on different workers.
        #
        # Sharded to worker1
        room_id1 = "!fooo:test"
        # Sharded to worker2
        room_id2 = "!bar:test"
        # Sharded to worker3
        room_id3 = "!quux:test"

        # Create rooms on the different workers.
        self._create_room(room_id1, user2_id, user2_tok)
        self._create_room(room_id2, user2_id, user2_tok)
        self._create_room(room_id3, user2_id, user2_tok)
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        join_response2 = self.helper.join(room_id2, user1_id, tok=user1_tok)
        # Leave room2
        leave_room2_response = self.helper.leave(room_id2, user1_id, tok=user1_tok)
        join_response3 = self.helper.join(room_id3, user1_id, tok=user1_tok)
        # Leave room3
        self.helper.leave(room_id3, user1_id, tok=user1_tok)

        # Ensure that the events were sharded to different workers.
        pos1 = self.get_success(
            self.store.get_position_for_event(join_response1["event_id"])
        )
        self.assertEqual(pos1.instance_name, "worker1")
        pos2 = self.get_success(
            self.store.get_position_for_event(join_response2["event_id"])
        )
        self.assertEqual(pos2.instance_name, "worker2")
        pos3 = self.get_success(
            self.store.get_position_for_event(join_response3["event_id"])
        )
        self.assertEqual(pos3.instance_name, "worker3")

        before_stuck_activity_token = self.event_sources.get_current_token()

        # We now gut wrench into the events stream `MultiWriterIdGenerator` on worker2 to
        # mimic it getting stuck persisting an event. This ensures that when we send an
        # event on worker1/worker3 we end up in a state where worker2 events stream
        # position lags that on worker1/worker3, resulting in a RoomStreamToken with a
        # non-empty `instance_map` component.
        #
        # Worker2's event stream position will not advance until we call `__aexit__`
        # again.
        worker_store2 = worker_hs2.get_datastores().main
        assert isinstance(worker_store2._stream_id_gen, MultiWriterIdGenerator)
        actx = worker_store2._stream_id_gen.get_next()
        self.get_success(actx.__aenter__())

        # For room_id1/worker1: leave and join the room to advance the stream position
        # and generate membership changes.
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        join_room1_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        # For room_id2/worker2: which is currently stuck, join the room.
        join_on_worker2_response = self.helper.join(room_id2, user1_id, tok=user1_tok)
        # For room_id3/worker3: leave and join the room to advance the stream position
        # and generate membership changes.
        self.helper.leave(room_id3, user1_id, tok=user1_tok)
        join_on_worker3_response = self.helper.join(room_id3, user1_id, tok=user1_tok)

        # Get a token while things are stuck after our activity
        stuck_activity_token = self.event_sources.get_current_token()
        # Let's make sure we're working with a token that has an `instance_map`
        self.assertNotEqual(len(stuck_activity_token.room_key.instance_map), 0)

        # Just double check that the join event on worker2 (that is stuck) happened
        # after the position recorded for worker2 in the token but before the max
        # position in the token. This is crucial for the behavior we're trying to test.
        join_on_worker2_pos = self.get_success(
            self.store.get_position_for_event(join_on_worker2_response["event_id"])
        )
        # Ensure the join technially came after our token
        self.assertGreater(
            join_on_worker2_pos.stream,
            stuck_activity_token.room_key.get_stream_pos_for_instance("worker2"),
        )
        # But less than the max stream position of some other worker
        self.assertLess(
            join_on_worker2_pos.stream,
            # max
            stuck_activity_token.room_key.get_max_stream_pos(),
        )

        # Just double check that the join event on worker3 happened after the min stream
        # value in the token but still within the position recorded for worker3. This is
        # crucial for the behavior we're trying to test.
        join_on_worker3_pos = self.get_success(
            self.store.get_position_for_event(join_on_worker3_response["event_id"])
        )
        # Ensure the join came after the min but still encapsulated by the token
        self.assertGreaterEqual(
            join_on_worker3_pos.stream,
            # min
            stuck_activity_token.room_key.stream,
        )
        self.assertLessEqual(
            join_on_worker3_pos.stream,
            stuck_activity_token.room_key.get_stream_pos_for_instance("worker3"),
        )

        # We finish the fake persisting an event we started above and advance worker2's
        # event stream position (unstuck worker2).
        self.get_success(actx.__aexit__(None, None, None))

        # The function under test
        room_id_results = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                UserID.from_string(user1_id),
                from_token=before_stuck_activity_token,
                to_token=stuck_activity_token,
            )
        )

        self.assertEqual(
            room_id_results.keys(),
            {
                room_id1,
                room_id2,
                room_id3,
            },
        )

        # Room1
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id1].event_id,
            join_room1_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id1].membership, Membership.JOIN)
        # We should be `newly_joined` because we joined during the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, True)
        self.assertEqual(room_id_results[room_id1].newly_left, False)

        # Room2
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id2].event_id,
            leave_room2_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id2].membership, Membership.LEAVE)
        # room_id2 should *NOT* be considered `newly_left` because we left before the
        # from/to range and the join event during the range happened while worker2 was
        # stuck. This means that from the perspective of the master, where the
        # `stuck_activity_token` is generated, the stream position for worker2 wasn't
        # advanced to the join yet. Looking at the `instance_map`, the join technically
        # comes after `stuck_activity_token`.
        self.assertEqual(room_id_results[room_id2].newly_joined, False)
        self.assertEqual(room_id_results[room_id2].newly_left, False)

        # Room3
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[room_id3].event_id,
            join_on_worker3_response["event_id"],
        )
        self.assertEqual(room_id_results[room_id3].membership, Membership.JOIN)
        # We should be `newly_joined` because we joined during the token range
        self.assertEqual(room_id_results[room_id3].newly_joined, True)
        self.assertEqual(room_id_results[room_id3].newly_left, False)


class FilterRoomsRelevantForSyncTestCase(HomeserverTestCase):
    """
    Tests Sliding Sync handler `filter_rooms_relevant_for_sync()` to make sure it returns
    the correct list of rooms IDs.
    """

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
        self.storage_controllers = hs.get_storage_controllers()

    def _get_sync_room_ids_for_user(
        self,
        user: UserID,
        to_token: StreamToken,
        from_token: Optional[StreamToken],
    ) -> Dict[str, _RoomMembershipForUser]:
        """
        Get the rooms the user should be syncing with
        """
        room_membership_for_user_map = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                user=user,
                from_token=from_token,
                to_token=to_token,
            )
        )
        filtered_sync_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms_relevant_for_sync(
                user=user,
                room_membership_for_user_map=room_membership_for_user_map,
            )
        )

        return filtered_sync_room_map

    def test_no_rooms(self) -> None:
        """
        Test when the user has never joined any rooms before
        """
        user1_id = self.register_user("user1", "pass")
        # user1_tok = self.login(user1_id, "pass")

        now_token = self.event_sources.get_current_token()

        room_id_results = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=now_token,
            to_token=now_token,
        )

        self.assertEqual(room_id_results.keys(), set())

    def test_basic_rooms(self) -> None:
        """
        Test that rooms that the user is joined to, invited to, banned from, and knocked
        on show up.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room_token = self.event_sources.get_current_token()

        join_room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response = self.helper.join(join_room_id, user1_id, tok=user1_tok)

        # Setup the invited room (user2 invites user1 to the room)
        invited_room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        invite_response = self.helper.invite(
            invited_room_id, targ=user1_id, tok=user2_tok
        )

        # Setup the ban room (user2 bans user1 from the room)
        ban_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(ban_room_id, user1_id, tok=user1_tok)
        ban_response = self.helper.ban(
            ban_room_id, src=user2_id, targ=user1_id, tok=user2_tok
        )

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
        knock_channel = self.make_request(
            "POST",
            "/_matrix/client/r0/knock/%s" % (knock_room_id,),
            b"{}",
            user1_tok,
        )
        self.assertEqual(knock_channel.code, 200, knock_channel.result)
        knock_room_membership_state_event = self.get_success(
            self.storage_controllers.state.get_current_state_event(
                knock_room_id, EventTypes.Member, user1_id
            )
        )
        assert knock_room_membership_state_event is not None

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=before_room_token,
            to_token=after_room_token,
        )

        # Ensure that the invited, ban, and knock rooms show up
        self.assertEqual(
            room_id_results.keys(),
            {
                join_room_id,
                invited_room_id,
                ban_room_id,
                knock_room_id,
            },
        )
        # It should be pointing to the the respective membership event (latest
        # membership event in the from/to range)
        self.assertEqual(
            room_id_results[join_room_id].event_id,
            join_response["event_id"],
        )
        self.assertEqual(room_id_results[join_room_id].membership, Membership.JOIN)
        self.assertEqual(room_id_results[join_room_id].newly_joined, True)
        self.assertEqual(room_id_results[join_room_id].newly_left, False)

        self.assertEqual(
            room_id_results[invited_room_id].event_id,
            invite_response["event_id"],
        )
        self.assertEqual(room_id_results[invited_room_id].membership, Membership.INVITE)
        self.assertEqual(room_id_results[invited_room_id].newly_joined, False)
        self.assertEqual(room_id_results[invited_room_id].newly_left, False)

        self.assertEqual(
            room_id_results[ban_room_id].event_id,
            ban_response["event_id"],
        )
        self.assertEqual(room_id_results[ban_room_id].membership, Membership.BAN)
        self.assertEqual(room_id_results[ban_room_id].newly_joined, False)
        self.assertEqual(room_id_results[ban_room_id].newly_left, False)

        self.assertEqual(
            room_id_results[knock_room_id].event_id,
            knock_room_membership_state_event.event_id,
        )
        self.assertEqual(room_id_results[knock_room_id].membership, Membership.KNOCK)
        self.assertEqual(room_id_results[knock_room_id].newly_joined, False)
        self.assertEqual(room_id_results[knock_room_id].newly_left, False)

    def test_only_newly_left_rooms_show_up(self) -> None:
        """
        Test that `newly_left` rooms still show up in the sync response but rooms that
        were left before the `from_token` don't show up. See condition "2)" comments in
        the `get_room_membership_for_user_at_to_token()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Leave before we calculate the `from_token`
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave during the from_token/to_token range (newly_left)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        _leave_response2 = self.helper.leave(room_id2, user1_id, tok=user1_tok)

        after_room2_token = self.event_sources.get_current_token()

        room_id_results = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=after_room1_token,
            to_token=after_room2_token,
        )

        # Only the `newly_left` room should show up
        self.assertEqual(room_id_results.keys(), {room_id2})
        self.assertEqual(
            room_id_results[room_id2].event_id,
            _leave_response2["event_id"],
        )
        # We should *NOT* be `newly_joined` because we are instead `newly_left`
        self.assertEqual(room_id_results[room_id2].newly_joined, False)
        self.assertEqual(room_id_results[room_id2].newly_left, True)

    def test_get_kicked_room(self) -> None:
        """
        Test that a room that the user was kicked from still shows up. When the user
        comes back to their client, they should see that they were kicked.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Setup the kick room (user2 kicks user1 from the room)
        kick_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(kick_room_id, user1_id, tok=user1_tok)
        # Kick user1 from the room
        kick_response = self.helper.change_membership(
            room=kick_room_id,
            src=user2_id,
            targ=user1_id,
            tok=user2_tok,
            membership=Membership.LEAVE,
            extra_data={
                "reason": "Bad manners",
            },
        )

        after_kick_token = self.event_sources.get_current_token()

        room_id_results = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=after_kick_token,
            to_token=after_kick_token,
        )

        # The kicked room should show up
        self.assertEqual(room_id_results.keys(), {kick_room_id})
        # It should be pointing to the latest membership event in the from/to range
        self.assertEqual(
            room_id_results[kick_room_id].event_id,
            kick_response["event_id"],
        )
        self.assertEqual(room_id_results[kick_room_id].membership, Membership.LEAVE)
        self.assertNotEqual(room_id_results[kick_room_id].sender, user1_id)
        # We should *NOT* be `newly_joined` because we were not joined at the the time
        # of the `to_token`.
        self.assertEqual(room_id_results[kick_room_id].newly_joined, False)
        self.assertEqual(room_id_results[kick_room_id].newly_left, False)

    def test_state_reset(self) -> None:
        """
        Test a state reset scenario where the user gets removed from the room (when
        there is no corresponding leave event)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # The room where the state reset will happen
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Join another room so we don't hit the short-circuit and return early if they
        # have no room membership
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id2, user1_id, tok=user1_tok)

        before_reset_token = self.event_sources.get_current_token()

        # Send another state event to make a position for the state reset to happen at
        dummy_state_response = self.helper.send_state(
            room_id1,
            event_type="foobarbaz",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        dummy_state_pos = self.get_success(
            self.store.get_position_for_event(dummy_state_response["event_id"])
        )

        # Mock a state reset removing the membership for user1 in the current state
        self.get_success(
            self.store.db_pool.simple_delete(
                table="current_state_events",
                keyvalues={
                    "room_id": room_id1,
                    "type": EventTypes.Member,
                    "state_key": user1_id,
                },
                desc="state reset user in current_state_events",
            )
        )
        self.get_success(
            self.store.db_pool.simple_delete(
                table="local_current_membership",
                keyvalues={
                    "room_id": room_id1,
                    "user_id": user1_id,
                },
                desc="state reset user in local_current_membership",
            )
        )
        self.get_success(
            self.store.db_pool.simple_insert(
                table="current_state_delta_stream",
                values={
                    "stream_id": dummy_state_pos.stream,
                    "room_id": room_id1,
                    "type": EventTypes.Member,
                    "state_key": user1_id,
                    "event_id": None,
                    "prev_event_id": join_response1["event_id"],
                    "instance_name": dummy_state_pos.instance_name,
                },
                desc="state reset user in current_state_delta_stream",
            )
        )

        # Manually bust the cache since we we're just manually messing with the database
        # and not causing an actual state reset.
        self.store._membership_stream_cache.entity_has_changed(
            user1_id, dummy_state_pos.stream
        )

        after_reset_token = self.event_sources.get_current_token()

        # The function under test
        room_id_results = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=before_reset_token,
            to_token=after_reset_token,
        )

        # Room1 should show up because it was `newly_left` via state reset during the from/to range
        self.assertEqual(room_id_results.keys(), {room_id1, room_id2})
        # It should be pointing to no event because we were removed from the room
        # without a corresponding leave event
        self.assertEqual(
            room_id_results[room_id1].event_id,
            None,
        )
        # State reset caused us to leave the room and there is no corresponding leave event
        self.assertEqual(room_id_results[room_id1].membership, Membership.LEAVE)
        # We should *NOT* be `newly_joined` because we joined before the token range
        self.assertEqual(room_id_results[room_id1].newly_joined, False)
        # We should be `newly_left` because we were removed via state reset during the from/to range
        self.assertEqual(room_id_results[room_id1].newly_left, True)


class FilterRoomsTestCase(HomeserverTestCase):
    """
    Tests Sliding Sync handler `filter_rooms()` to make sure it includes/excludes rooms
    correctly.
    """

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

    def _get_sync_room_ids_for_user(
        self,
        user: UserID,
        to_token: StreamToken,
        from_token: Optional[StreamToken],
    ) -> Dict[str, _RoomMembershipForUser]:
        """
        Get the rooms the user should be syncing with
        """
        room_membership_for_user_map = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                user=user,
                from_token=from_token,
                to_token=to_token,
            )
        )
        filtered_sync_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms_relevant_for_sync(
                user=user,
                room_membership_for_user_map=room_membership_for_user_map,
            )
        )

        return filtered_sync_room_map

    def _create_dm_room(
        self,
        inviter_user_id: str,
        inviter_tok: str,
        invitee_user_id: str,
        invitee_tok: str,
    ) -> str:
        """
        Helper to create a DM room as the "inviter" and invite the "invitee" user to the room. The
        "invitee" user also will join the room. The `m.direct` account data will be set
        for both users.
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
        # Person that was invited joins the room
        self.helper.join(room_id, invitee_user_id, tok=invitee_tok)

        # Mimic the client setting the room as a direct message in the global account
        # data
        self.get_success(
            self.store.add_account_data_for_user(
                invitee_user_id,
                AccountDataTypes.DIRECT,
                {inviter_user_id: [room_id]},
            )
        )
        self.get_success(
            self.store.add_account_data_for_user(
                inviter_user_id,
                AccountDataTypes.DIRECT,
                {invitee_user_id: [room_id]},
            )
        )

        return room_id

    _remote_invite_count: int = 0

    def _create_remote_invite_room_for_user(
        self,
        invitee_user_id: str,
        unsigned_invite_room_state: Optional[List[StrippedStateEvent]],
    ) -> str:
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
            The room ID of the remote invite room
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
        persist_controller = self.hs.get_storage_controllers().persistence
        assert persist_controller is not None
        self.get_success(persist_controller.persist_event(invite_event, context))

        self._remote_invite_count += 1

        return invite_room_id

    def test_filter_dm_rooms(self) -> None:
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try with `is_dm=True`
        truthy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_dm=True,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(truthy_filtered_room_map.keys(), {dm_room_id})

        # Try with `is_dm=False`
        falsy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_dm=False,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(falsy_filtered_room_map.keys(), {room_id})

    def test_filter_encrypted_rooms(self) -> None:
        """
        Test `filter.is_encrypted` for encrypted rooms
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try with `is_encrypted=True`
        truthy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=True,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(truthy_filtered_room_map.keys(), {encrypted_room_id})

        # Try with `is_encrypted=False`
        falsy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=False,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(falsy_filtered_room_map.keys(), {room_id})

    def test_filter_encrypted_server_left_room(self) -> None:
        """
        Test that we can apply a `filter.is_encrypted` against a room that everyone has left.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        before_rooms_token = self.event_sources.get_current_token()

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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            # We're using a `from_token` so that the room is considered `newly_left` and
            # appears in our list of relevant sync rooms
            from_token=before_rooms_token,
            to_token=after_rooms_token,
        )

        # Try with `is_encrypted=True`
        truthy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=True,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(truthy_filtered_room_map.keys(), {encrypted_room_id})

        # Try with `is_encrypted=False`
        falsy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=False,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(falsy_filtered_room_map.keys(), {room_id})

    def test_filter_encrypted_server_left_room2(self) -> None:
        """
        Test that we can apply a `filter.is_encrypted` against a room that everyone has
        left.

        There is still someone local who is invited to the rooms but that doesn't affect
        whether the server is participating in the room (users need to be joined).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        _user2_tok = self.login(user2_id, "pass")

        before_rooms_token = self.event_sources.get_current_token()

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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            # We're using a `from_token` so that the room is considered `newly_left` and
            # appears in our list of relevant sync rooms
            from_token=before_rooms_token,
            to_token=after_rooms_token,
        )

        # Try with `is_encrypted=True`
        truthy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=True,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(truthy_filtered_room_map.keys(), {encrypted_room_id})

        # Try with `is_encrypted=False`
        falsy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=False,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(falsy_filtered_room_map.keys(), {room_id})

    def test_filter_encrypted_after_we_left(self) -> None:
        """
        Test that we can apply a `filter.is_encrypted` against a room that was encrypted
        after we left the room (make sure we don't just use the current state)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_rooms_token = self.event_sources.get_current_token()

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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            # We're using a `from_token` so that the room is considered `newly_left` and
            # appears in our list of relevant sync rooms
            from_token=before_rooms_token,
            to_token=after_rooms_token,
        )

        # Try with `is_encrypted=True`
        truthy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=True,
                ),
                after_rooms_token,
            )
        )

        # Even though we left the room before it was encrypted, we still see it because
        # someone else on our server is still participating in the room and we "leak"
        # the current state to the left user. But we consider the room encryption status
        # to not be a secret given it's often set at the start of the room and it's one
        # of the stripped state events that is normally handed out.
        self.assertEqual(
            truthy_filtered_room_map.keys(), {encrypted_after_we_left_room_id}
        )

        # Try with `is_encrypted=False`
        falsy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=False,
                ),
                after_rooms_token,
            )
        )

        # Even though we left the room before it was encrypted... (see comment above)
        self.assertEqual(falsy_filtered_room_map.keys(), {room_id})

    def test_filter_encrypted_with_remote_invite_room_no_stripped_state(self) -> None:
        """
        Test that we can apply a `filter.is_encrypted` filter against a remote invite
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try with `is_encrypted=True`
        truthy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=True,
                ),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should not appear because we can't figure out whether
        # it is encrypted or not (no stripped state, `unsigned.invite_room_state`).
        self.assertEqual(truthy_filtered_room_map.keys(), {encrypted_room_id})

        # Try with `is_encrypted=False`
        falsy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=False,
                ),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should not appear because we can't figure out whether
        # it is encrypted or not (no stripped state, `unsigned.invite_room_state`).
        self.assertEqual(falsy_filtered_room_map.keys(), {room_id})

    def test_filter_encrypted_with_remote_invite_encrypted_room(self) -> None:
        """
        Test that we can apply a `filter.is_encrypted` filter against a remote invite
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try with `is_encrypted=True`
        truthy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=True,
                ),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should appear here because it is encrypted
        # according to the stripped state
        self.assertEqual(
            truthy_filtered_room_map.keys(), {encrypted_room_id, remote_invite_room_id}
        )

        # Try with `is_encrypted=False`
        falsy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=False,
                ),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should not appear here because it is encrypted
        # according to the stripped state
        self.assertEqual(falsy_filtered_room_map.keys(), {room_id})

    def test_filter_encrypted_with_remote_invite_unencrypted_room(self) -> None:
        """
        Test that we can apply a `filter.is_encrypted` filter against a remote invite
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try with `is_encrypted=True`
        truthy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=True,
                ),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should not appear here because it is unencrypted
        # according to the stripped state
        self.assertEqual(truthy_filtered_room_map.keys(), {encrypted_room_id})

        # Try with `is_encrypted=False`
        falsy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_encrypted=False,
                ),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should appear because it is unencrypted according to
        # the stripped state
        self.assertEqual(
            falsy_filtered_room_map.keys(), {room_id, remote_invite_room_id}
        )

    def test_filter_invite_rooms(self) -> None:
        """
        Test `filter.is_invite` for rooms that the user has been invited to
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try with `is_invite=True`
        truthy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_invite=True,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(truthy_filtered_room_map.keys(), {invite_room_id})

        # Try with `is_invite=False`
        falsy_filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    is_invite=False,
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(falsy_filtered_room_map.keys(), {room_id})

    def test_filter_room_types(self) -> None:
        """
        Test `filter.room_types` for different room types
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try finding only normal rooms
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[None]),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {room_id})

        # Try finding only spaces
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[RoomTypes.SPACE]),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {space_room_id})

        # Try finding normal rooms and spaces
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    room_types=[None, RoomTypes.SPACE]
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {room_id, space_room_id})

        # Try finding an arbitrary room type
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    room_types=["org.matrix.foobarbaz"]
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {foo_room_id})

    def test_filter_not_room_types(self) -> None:
        """
        Test `filter.not_room_types` for different room types
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try finding *NOT* normal rooms
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(not_room_types=[None]),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {space_room_id, foo_room_id})

        # Try finding *NOT* spaces
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    not_room_types=[RoomTypes.SPACE]
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {room_id, foo_room_id})

        # Try finding *NOT* normal rooms or spaces
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    not_room_types=[None, RoomTypes.SPACE]
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {foo_room_id})

        # Test how it behaves when we have both `room_types` and `not_room_types`.
        # `not_room_types` should win.
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    room_types=[None], not_room_types=[None]
                ),
                after_rooms_token,
            )
        )

        # Nothing matches because nothing is both a normal room and not a normal room
        self.assertEqual(filtered_room_map.keys(), set())

        # Test how it behaves when we have both `room_types` and `not_room_types`.
        # `not_room_types` should win.
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(
                    room_types=[None, RoomTypes.SPACE], not_room_types=[None]
                ),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {space_room_id})

    def test_filter_room_types_server_left_room(self) -> None:
        """
        Test that we can apply a `filter.room_types` against a room that everyone has left.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        before_rooms_token = self.event_sources.get_current_token()

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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            # We're using a `from_token` so that the room is considered `newly_left` and
            # appears in our list of relevant sync rooms
            from_token=before_rooms_token,
            to_token=after_rooms_token,
        )

        # Try finding only normal rooms
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[None]),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {room_id})

        # Try finding only spaces
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[RoomTypes.SPACE]),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {space_room_id})

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

        before_rooms_token = self.event_sources.get_current_token()

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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            # We're using a `from_token` so that the room is considered `newly_left` and
            # appears in our list of relevant sync rooms
            from_token=before_rooms_token,
            to_token=after_rooms_token,
        )

        # Try finding only normal rooms
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[None]),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {room_id})

        # Try finding only spaces
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[RoomTypes.SPACE]),
                after_rooms_token,
            )
        )

        self.assertEqual(filtered_room_map.keys(), {space_room_id})

    def test_filter_room_types_with_remote_invite_room_no_stripped_state(self) -> None:
        """
        Test that we can apply a `filter.room_types` filter against a remote invite
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try finding only normal rooms
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[None]),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should not appear because we can't figure out what
        # room type it is (no stripped state, `unsigned.invite_room_state`)
        self.assertEqual(filtered_room_map.keys(), {room_id})

        # Try finding only spaces
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[RoomTypes.SPACE]),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should not appear because we can't figure out what
        # room type it is (no stripped state, `unsigned.invite_room_state`)
        self.assertEqual(filtered_room_map.keys(), {space_room_id})

    def test_filter_room_types_with_remote_invite_space(self) -> None:
        """
        Test that we can apply a `filter.room_types` filter against a remote invite
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try finding only normal rooms
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[None]),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should not appear here because it is a space room
        # according to the stripped state
        self.assertEqual(filtered_room_map.keys(), {room_id})

        # Try finding only spaces
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[RoomTypes.SPACE]),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should appear here because it is a space room
        # according to the stripped state
        self.assertEqual(
            filtered_room_map.keys(), {space_room_id, remote_invite_room_id}
        )

    def test_filter_room_types_with_remote_invite_normal_room(self) -> None:
        """
        Test that we can apply a `filter.room_types` filter against a remote invite
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

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Try finding only normal rooms
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[None]),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should appear here because it is a normal room
        # according to the stripped state (no room type)
        self.assertEqual(filtered_room_map.keys(), {room_id, remote_invite_room_id})

        # Try finding only spaces
        filtered_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms(
                UserID.from_string(user1_id),
                sync_room_map,
                SlidingSyncConfig.SlidingSyncList.Filters(room_types=[RoomTypes.SPACE]),
                after_rooms_token,
            )
        )

        # `remote_invite_room_id` should not appear here because it is a normal room
        # according to the stripped state (no room type)
        self.assertEqual(filtered_room_map.keys(), {space_room_id})


class SortRoomsTestCase(HomeserverTestCase):
    """
    Tests Sliding Sync handler `sort_rooms()` to make sure it sorts/orders rooms
    correctly.
    """

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

    def _get_sync_room_ids_for_user(
        self,
        user: UserID,
        to_token: StreamToken,
        from_token: Optional[StreamToken],
    ) -> Dict[str, _RoomMembershipForUser]:
        """
        Get the rooms the user should be syncing with
        """
        room_membership_for_user_map = self.get_success(
            self.sliding_sync_handler.get_room_membership_for_user_at_to_token(
                user=user,
                from_token=from_token,
                to_token=to_token,
            )
        )
        filtered_sync_room_map = self.get_success(
            self.sliding_sync_handler.filter_rooms_relevant_for_sync(
                user=user,
                room_membership_for_user_map=room_membership_for_user_map,
            )
        )

        return filtered_sync_room_map

    def test_sort_activity_basic(self) -> None:
        """
        Rooms with newer activity are sorted first.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )
        room_id2 = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Sort the rooms (what we're testing)
        sorted_sync_rooms = self.get_success(
            self.sliding_sync_handler.sort_rooms(
                sync_room_map=sync_room_map,
                to_token=after_rooms_token,
            )
        )

        self.assertEqual(
            [room_membership.room_id for room_membership in sorted_sync_rooms],
            [room_id2, room_id1],
        )

    @parameterized.expand(
        [
            (Membership.LEAVE,),
            (Membership.INVITE,),
            (Membership.KNOCK,),
            (Membership.BAN,),
        ]
    )
    def test_activity_after_xxx(self, room1_membership: str) -> None:
        """
        When someone has left/been invited/knocked/been banned from a room, they
        shouldn't take anything into account after that membership event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_rooms_token = self.event_sources.get_current_token()

        # Create the rooms as user2 so we can have user1 with a clean slate to work from
        # and join in whatever order we need for the tests.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # If we're testing knocks, set the room to knock
        if room1_membership == Membership.KNOCK:
            self.helper.send_state(
                room_id1,
                EventTypes.JoinRules,
                {"join_rule": JoinRules.KNOCK},
                tok=user2_tok,
            )
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        room_id3 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)

        # Here is the activity with user1 that will determine the sort of the rooms
        # (room2, room1, room3)
        self.helper.join(room_id3, user1_id, tok=user1_tok)
        if room1_membership == Membership.LEAVE:
            self.helper.join(room_id1, user1_id, tok=user1_tok)
            self.helper.leave(room_id1, user1_id, tok=user1_tok)
        elif room1_membership == Membership.INVITE:
            self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        elif room1_membership == Membership.KNOCK:
            self.helper.knock(room_id1, user1_id, tok=user1_tok)
        elif room1_membership == Membership.BAN:
            self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.join(room_id2, user1_id, tok=user1_tok)

        # Activity before the token but the user is only been xxx to this room so it
        # shouldn't be taken into account
        self.helper.send(room_id1, "activity in room1", tok=user2_tok)

        after_rooms_token = self.event_sources.get_current_token()

        # Activity after the token. Just make it in a different order than what we
        # expect to make sure we're not taking the activity after the token into
        # account.
        self.helper.send(room_id1, "activity in room1", tok=user2_tok)
        self.helper.send(room_id2, "activity in room2", tok=user2_tok)
        self.helper.send(room_id3, "activity in room3", tok=user2_tok)

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=before_rooms_token,
            to_token=after_rooms_token,
        )

        # Sort the rooms (what we're testing)
        sorted_sync_rooms = self.get_success(
            self.sliding_sync_handler.sort_rooms(
                sync_room_map=sync_room_map,
                to_token=after_rooms_token,
            )
        )

        self.assertEqual(
            [room_membership.room_id for room_membership in sorted_sync_rooms],
            [room_id2, room_id1, room_id3],
            "Corresponding map to disambiguate the opaque room IDs: "
            + str(
                {
                    "room_id1": room_id1,
                    "room_id2": room_id2,
                    "room_id3": room_id3,
                }
            ),
        )

    def test_default_bump_event_types(self) -> None:
        """
        Test that we only consider the *latest* event in the room when sorting (not
        `bump_event_types`).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )
        message_response = self.helper.send(room_id1, "message in room1", tok=user1_tok)
        room_id2 = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )
        self.helper.send(room_id2, "message in room2", tok=user1_tok)

        # Send a reaction in room1 which isn't in `DEFAULT_BUMP_EVENT_TYPES` but we only
        # care about sorting by the *latest* event in the room.
        self.helper.send_event(
            room_id1,
            type=EventTypes.Reaction,
            content={
                "m.relates_to": {
                    "event_id": message_response["event_id"],
                    "key": "👍",
                    "rel_type": "m.annotation",
                }
            },
            tok=user1_tok,
        )

        after_rooms_token = self.event_sources.get_current_token()

        # Get the rooms the user should be syncing with
        sync_room_map = self._get_sync_room_ids_for_user(
            UserID.from_string(user1_id),
            from_token=None,
            to_token=after_rooms_token,
        )

        # Sort the rooms (what we're testing)
        sorted_sync_rooms = self.get_success(
            self.sliding_sync_handler.sort_rooms(
                sync_room_map=sync_room_map,
                to_token=after_rooms_token,
            )
        )

        self.assertEqual(
            [room_membership.room_id for room_membership in sorted_sync_rooms],
            # room1 sorts before room2 because it has the latest event (the reaction).
            # We only care about the *latest* event in the room.
            [room_id1, room_id2],
        )
