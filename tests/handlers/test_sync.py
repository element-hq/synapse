#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
from http import HTTPStatus
from typing import Collection, ContextManager, cast
from unittest.mock import AsyncMock, Mock, patch

from parameterized import parameterized, parameterized_class

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.api.constants import AccountDataTypes, EventTypes, JoinRules
from synapse.api.errors import Codes, ResourceLimitError
from synapse.api.filtering import FilterCollection, Filtering
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.handlers.sync import (
    LAZY_LOADED_PROFILE_FIELDS_CACHE_MAX_AGE,
    SyncConfig,
    SyncRequestKey,
    SyncResult,
    TimelineBatch,
)
from synapse.rest import admin
from synapse.rest.client import knock, login, room
from synapse.server import HomeServer
from synapse.types import (
    JsonDict,
    JsonValue,
    MultiWriterStreamToken,
    RoomStreamToken,
    StreamKeyType,
    UserID,
    create_requester,
)
from synapse.util.clock import Clock
from synapse.util.duration import Duration

import tests.unittest
import tests.utils
from tests.test_utils.event_builders import make_test_pdu_event
from tests.test_utils.event_injection import inject_member_event
from tests.unittest import override_config

_request_key = 0


def generate_request_key() -> SyncRequestKey:
    global _request_key
    _request_key += 1
    return ("request_key", _request_key)


@parameterized_class(
    ("use_state_after",),
    [
        (True,),
        (False,),
    ],
    class_name_func=lambda cls,
    num,
    params_dict: f"{cls.__name__}_{'state_after' if params_dict['use_state_after'] else 'state'}",
)
class SyncTestCase(tests.unittest.HomeserverTestCase):
    """Tests Sync Handler."""

    use_state_after: bool

    servlets = [
        admin.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sync_handler = self.hs.get_sync_handler()
        self.store = self.hs.get_datastores().main

        # AuthBlocking reads from the hs' config on initialization. We need to
        # modify its config instead of the hs'
        self.auth_blocking = self.hs.get_auth_blocking()

    def test_wait_for_sync_for_user_auth_blocking(self) -> None:
        user_id1 = "@user1:test"
        user_id2 = "@user2:test"
        sync_config = generate_sync_config(
            user_id1, use_state_after=self.use_state_after
        )
        requester = create_requester(user_id1)

        self.reactor.advance(100)  # So we get not 0 time
        self.auth_blocking._limit_usage_by_mau = True
        self.auth_blocking._max_mau_value = 1

        # Check that the happy case does not throw errors
        self.get_success(self.store.upsert_monthly_active_user(user_id1))
        self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config,
                request_key=generate_request_key(),
            )
        )

        # Test that global lock works
        self.auth_blocking._hs_disabled = True
        e = self.get_failure(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config,
                request_key=generate_request_key(),
            ),
            ResourceLimitError,
        )
        self.assertEqual(e.value.errcode, Codes.RESOURCE_LIMIT_EXCEEDED)

        self.auth_blocking._hs_disabled = False

        sync_config = generate_sync_config(
            user_id2, use_state_after=self.use_state_after
        )
        requester = create_requester(user_id2)

        e = self.get_failure(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config,
                request_key=generate_request_key(),
            ),
            ResourceLimitError,
        )
        self.assertEqual(e.value.errcode, Codes.RESOURCE_LIMIT_EXCEEDED)

    def test_unknown_room_version(self) -> None:
        """
        A room with an unknown room version should not break sync (and should be excluded).
        """
        inviter = self.register_user("creator", "pass", admin=True)
        inviter_tok = self.login("@creator:test", "pass")

        user = self.register_user("user", "pass")
        tok = self.login("user", "pass")

        # Do an initial sync on a different device.
        requester = create_requester(user)
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user, device_id="dev", use_state_after=self.use_state_after
                ),
                request_key=generate_request_key(),
            )
        )

        # Create a room as the user.
        joined_room = self.helper.create_room_as(user, tok=tok)

        # Invite the user to the room as someone else.
        invite_room = self.helper.create_room_as(inviter, tok=inviter_tok)
        self.helper.invite(invite_room, targ=user, tok=inviter_tok)

        knock_room = self.helper.create_room_as(
            inviter, room_version=RoomVersions.V7.identifier, tok=inviter_tok
        )
        self.helper.send_state(
            knock_room,
            EventTypes.JoinRules,
            {"join_rule": JoinRules.KNOCK},
            tok=inviter_tok,
        )
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/knock/%s" % (knock_room,),
            b"{}",
            tok,
        )
        self.assertEqual(200, channel.code, channel.result)

        # The rooms should appear in the sync response.
        result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user, use_state_after=self.use_state_after
                ),
                request_key=generate_request_key(),
            )
        )
        self.assertIn(joined_room, [r.room_id for r in result.joined])
        self.assertIn(invite_room, [r.room_id for r in result.invited])
        self.assertIn(knock_room, [r.room_id for r in result.knocked])

        # Test a incremental sync (by providing a since_token).
        result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user, device_id="dev", use_state_after=self.use_state_after
                ),
                request_key=generate_request_key(),
                since_token=initial_result.next_batch,
            )
        )
        self.assertIn(joined_room, [r.room_id for r in result.joined])
        self.assertIn(invite_room, [r.room_id for r in result.invited])
        self.assertIn(knock_room, [r.room_id for r in result.knocked])

        # Poke the database and update the room version to an unknown one.
        for room_id in (joined_room, invite_room, knock_room):
            self.get_success(
                self.hs.get_datastores().main.db_pool.simple_update(
                    "rooms",
                    keyvalues={"room_id": room_id},
                    updatevalues={"room_version": "unknown-room-version"},
                    desc="updated-room-version",
                )
            )

        # Blow away caches (supported room versions can only change due to a restart).
        self.store.get_rooms_for_user.invalidate_all()
        self.store._get_rooms_for_local_user_where_membership_is_inner.invalidate_all()
        self.store._get_event_cache.clear()
        self.store._event_ref.clear()

        # The rooms should be excluded from the sync response.
        # Get a new request key.
        result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user, use_state_after=self.use_state_after
                ),
                request_key=generate_request_key(),
            )
        )
        self.assertNotIn(joined_room, [r.room_id for r in result.joined])
        self.assertNotIn(invite_room, [r.room_id for r in result.invited])
        self.assertNotIn(knock_room, [r.room_id for r in result.knocked])

        # The rooms should also not be in an incremental sync.
        result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user, device_id="dev", use_state_after=self.use_state_after
                ),
                request_key=generate_request_key(),
                since_token=initial_result.next_batch,
            )
        )
        self.assertNotIn(joined_room, [r.room_id for r in result.joined])
        self.assertNotIn(invite_room, [r.room_id for r in result.invited])
        self.assertNotIn(knock_room, [r.room_id for r in result.knocked])

    def test_ban_wins_race_with_join(self) -> None:
        """Rooms shouldn't appear under "joined" if a join loses a race to a ban.

        A complicated edge case. Imagine the following scenario:

        * you attempt to join a room
        * racing with that is a ban which comes in over federation, which ends up with
          an earlier stream_ordering than the join.
        * you get a sync response with a sync token which is _after_ the ban, but before
          the join
        * now your join lands; it is a valid event because its `prev_event`s predate the
          ban, but will not make it into current_state_events (because bans win over
          joins in state res, essentially).
        * When we do a sync from the incremental sync, the only event in the timeline
          is your join ... and yet you aren't joined.

        The ban coming in over federation isn't crucial for this behaviour; the key
        requirements are:
        1. the homeserver generates a join event with prev_events that precede the ban
           (so that it passes the "are you banned" test)
        2. the join event has a stream_ordering after that of the ban.

        We use monkeypatching to artificially trigger condition (1).
        """
        # A local user Alice creates a room.
        owner = self.register_user("alice", "password")
        owner_tok = self.login(owner, "password")
        room_id = self.helper.create_room_as(owner, is_public=True, tok=owner_tok)

        # Do a sync as Alice to get the latest event in the room.
        alice_sync_result: SyncResult = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                create_requester(owner),
                generate_sync_config(owner, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
            )
        )
        self.assertEqual(len(alice_sync_result.joined), 1)
        self.assertEqual(alice_sync_result.joined[0].room_id, room_id)
        last_room_creation_event_id = (
            alice_sync_result.joined[0].timeline.events[-1].event.event_id
        )

        # Eve, a ne'er-do-well, registers.
        eve = self.register_user("eve", "password")
        eve_token = self.login(eve, "password")

        # Alice preemptively bans Eve.
        self.helper.ban(room_id, owner, eve, tok=owner_tok)

        # Eve syncs.
        eve_requester = create_requester(eve)
        eve_sync_config = generate_sync_config(
            eve, use_state_after=self.use_state_after
        )
        eve_sync_after_ban: SyncResult = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                eve_requester,
                eve_sync_config,
                request_key=generate_request_key(),
            )
        )

        # Sanity check this sync result. We shouldn't be joined to the room.
        self.assertEqual(eve_sync_after_ban.joined, [])

        # Eve tries to join the room. We monkey patch the internal logic which selects
        # the prev_events used when creating the join event, such that the ban does not
        # precede the join.
        with self._patch_get_latest_events([last_room_creation_event_id]):
            self.helper.join(
                room_id,
                eve,
                tok=eve_token,
                # Previously, this join would succeed but now we expect it to fail at
                # this point. The rest of the test is for the case when this used to
                # succeed.
                expect_code=HTTPStatus.FORBIDDEN,
            )

        # Eve makes a second, incremental sync.
        eve_incremental_sync_after_join: SyncResult = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                eve_requester,
                eve_sync_config,
                request_key=generate_request_key(),
                since_token=eve_sync_after_ban.next_batch,
            )
        )
        # Eve should not see herself as joined to the room.
        self.assertEqual(eve_incremental_sync_after_join.joined, [])

        # If we did a third initial sync, we should _still_ see eve is not joined to the room.
        eve_initial_sync_after_join: SyncResult = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                eve_requester,
                eve_sync_config,
                request_key=generate_request_key(),
                since_token=None,
            )
        )
        self.assertEqual(eve_initial_sync_after_join.joined, [])

    def test_state_includes_changes_on_forks(self) -> None:
        """State changes that happen on a fork of the DAG must be included in `state`

        Given the following DAG:

             E1
           ↗    ↖
          |      S2
          |      ↑
        --|------|----
          |      |
          E3     |
           ↖    /
             E4

        ... and a filter that means we only return 2 events, represented by the dashed
        horizontal line: `S2` must be included in the `state` section.
        """
        alice = self.register_user("alice", "password")
        alice_tok = self.login(alice, "password")
        alice_requester = create_requester(alice)
        room_id = self.helper.create_room_as(alice, is_public=True, tok=alice_tok)

        # Do an initial sync as Alice to get a known starting point.
        initial_sync_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(alice, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
            )
        )
        last_room_creation_event_id = (
            initial_sync_result.joined[0].timeline.events[-1].event.event_id
        )

        # Send a state event, and a regular event, both using the same prev ID
        with self._patch_get_latest_events([last_room_creation_event_id]):
            s2_event = self.helper.send_state(room_id, "s2", {}, tok=alice_tok)[
                "event_id"
            ]
            e3_event = self.helper.send(room_id, "e3", tok=alice_tok)["event_id"]

        # Send a final event, joining the two branches of the dag
        e4_event = self.helper.send(room_id, "e4", tok=alice_tok)["event_id"]

        # do an incremental sync, with a filter that will ensure we only get two of
        # the three new events.
        incremental_sync = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(
                    alice,
                    filter_collection=FilterCollection(
                        self.hs, {"room": {"timeline": {"limit": 2}}}
                    ),
                    use_state_after=self.use_state_after,
                ),
                request_key=generate_request_key(),
                since_token=initial_sync_result.next_batch,
            )
        )

        # The state event should appear in the 'state' section of the response.
        room_sync = incremental_sync.joined[0]
        self.assertEqual(room_sync.room_id, room_id)
        self.assertTrue(room_sync.timeline.limited)
        self.assertEqual(
            [e.event.event_id for e in room_sync.timeline.events],
            [e3_event, e4_event],
        )
        self.assertEqual(
            [e.event_id for e in room_sync.state.values()],
            [s2_event],
        )

    def test_state_includes_changes_on_forks_when_events_excluded(self) -> None:
        """A variation on the previous test, but where one event is filtered

        The DAG is the same as the previous test, but E4 is excluded by the filter.

             E1
           ↗    ↖
          |      S2
          |      ↑
        --|------|----
          |      |
          E3     |
           ↖    /
            (E4)

        """

        alice = self.register_user("alice", "password")
        alice_tok = self.login(alice, "password")
        alice_requester = create_requester(alice)
        room_id = self.helper.create_room_as(alice, is_public=True, tok=alice_tok)

        # Do an initial sync as Alice to get a known starting point.
        initial_sync_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(alice, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
            )
        )
        last_room_creation_event_id = (
            initial_sync_result.joined[0].timeline.events[-1].event.event_id
        )

        # Send a state event, and a regular event, both using the same prev ID
        with self._patch_get_latest_events([last_room_creation_event_id]):
            s2_event = self.helper.send_state(room_id, "s2", {}, tok=alice_tok)[
                "event_id"
            ]
            e3_event = self.helper.send(room_id, "e3", tok=alice_tok)["event_id"]

        # Send a final event, joining the two branches of the dag
        self.helper.send(room_id, "e4", type="not_a_normal_message", tok=alice_tok)[
            "event_id"
        ]

        # do an incremental sync, with a filter that will only return E3, excluding S2
        # and E4.
        incremental_sync = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(
                    alice,
                    filter_collection=FilterCollection(
                        self.hs,
                        {
                            "room": {
                                "timeline": {
                                    "limit": 1,
                                    "not_types": ["not_a_normal_message"],
                                }
                            }
                        },
                    ),
                    use_state_after=self.use_state_after,
                ),
                request_key=generate_request_key(),
                since_token=initial_sync_result.next_batch,
            )
        )

        # The state event should appear in the 'state' section of the response.
        room_sync = incremental_sync.joined[0]
        self.assertEqual(room_sync.room_id, room_id)
        self.assertTrue(room_sync.timeline.limited)
        self.assertEqual(
            [e.event.event_id for e in room_sync.timeline.events],
            [e3_event],
        )
        self.assertEqual(
            [e.event_id for e in room_sync.state.values()],
            [s2_event],
        )

    def test_state_includes_changes_on_long_lived_forks(self) -> None:
        """State changes that happen on a fork of the DAG must be included in `state`

        Given the following DAG:

             E1
           ↗    ↖
          |      S2
          |      ↑
        --|------|----
          E3     |
        --|------|----
          |      E4
          |      |

        ... and a filter that means we only return 1 event, represented by the dashed
        horizontal lines: `S2` must be included in the `state` section on the second sync.

        When `use_state_after` is enabled, then we expect to see `s2` in the first sync.
        """
        alice = self.register_user("alice", "password")
        alice_tok = self.login(alice, "password")
        alice_requester = create_requester(alice)
        room_id = self.helper.create_room_as(alice, is_public=True, tok=alice_tok)

        # Do an initial sync as Alice to get a known starting point.
        initial_sync_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(alice, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
            )
        )
        last_room_creation_event_id = (
            initial_sync_result.joined[0].timeline.events[-1].event.event_id
        )

        # Send a state event, and a regular event, both using the same prev ID
        with self._patch_get_latest_events([last_room_creation_event_id]):
            s2_event = self.helper.send_state(room_id, "s2", {}, tok=alice_tok)[
                "event_id"
            ]
            e3_event = self.helper.send(room_id, "e3", tok=alice_tok)["event_id"]

        # Do an incremental sync, this will return E3 but *not* S2 at this
        # point.
        incremental_sync = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(
                    alice,
                    filter_collection=FilterCollection(
                        self.hs, {"room": {"timeline": {"limit": 1}}}
                    ),
                    use_state_after=self.use_state_after,
                ),
                request_key=generate_request_key(),
                since_token=initial_sync_result.next_batch,
            )
        )
        room_sync = incremental_sync.joined[0]
        self.assertEqual(room_sync.room_id, room_id)
        self.assertTrue(room_sync.timeline.limited)
        self.assertEqual(
            [e.event.event_id for e in room_sync.timeline.events],
            [e3_event],
        )

        if self.use_state_after:
            # When using `state_after` we get told about s2 immediately
            self.assertEqual(
                [e.event_id for e in room_sync.state.values()],
                [s2_event],
            )
        else:
            self.assertEqual(
                [e.event_id for e in room_sync.state.values()],
                [],
            )

        # Now send another event that points to S2, but not E3.
        with self._patch_get_latest_events([s2_event]):
            e4_event = self.helper.send(room_id, "e4", tok=alice_tok)["event_id"]

        # Doing an incremental sync should return S2 in state.
        incremental_sync = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(
                    alice,
                    filter_collection=FilterCollection(
                        self.hs, {"room": {"timeline": {"limit": 1}}}
                    ),
                    use_state_after=self.use_state_after,
                ),
                request_key=generate_request_key(),
                since_token=incremental_sync.next_batch,
            )
        )
        room_sync = incremental_sync.joined[0]
        self.assertEqual(room_sync.room_id, room_id)
        self.assertFalse(room_sync.timeline.limited)
        self.assertEqual(
            [e.event.event_id for e in room_sync.timeline.events],
            [e4_event],
        )

        if self.use_state_after:
            # When using `state_after` we got told about s2 previously, so we
            # don't again.
            self.assertEqual(
                [e.event_id for e in room_sync.state.values()],
                [],
            )
        else:
            self.assertEqual(
                [e.event_id for e in room_sync.state.values()],
                [s2_event],
            )

    def test_state_includes_changes_on_ungappy_syncs(self) -> None:
        """Test `state` where the sync is not gappy.

        We start with a DAG like this:

             E1
           ↗    ↖
          |      S2
          |
        --|---
          |
          E3

        ... and initialsync with `limit=1`, represented by the horizontal dashed line.
        At this point, we do not expect S2 to appear in the response at all (since
        it is excluded from the timeline by the `limit`, and the state is based on the
        state after the most recent event before the sync token (E3), which doesn't
        include S2.

        Now more events arrive, and we do an incremental sync:

             E1
           ↗    ↖
          |      S2
          |      ↑
          E3     |
          ↑      |
        --|------|----
          |      |
          E4     |
           ↖    /
             E5

        This is the last chance for us to tell the client about S2, so it *must* be
        included in the response.

        When `use_state_after` is enabled, then we expect to see `s2` in the first sync.
        """
        alice = self.register_user("alice", "password")
        alice_tok = self.login(alice, "password")
        alice_requester = create_requester(alice)
        room_id = self.helper.create_room_as(alice, is_public=True, tok=alice_tok)

        # Do an initial sync to get a known starting point.
        initial_sync_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(alice, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
            )
        )
        last_room_creation_event_id = (
            initial_sync_result.joined[0].timeline.events[-1].event.event_id
        )

        # Send a state event, and a regular event, both using the same prev ID
        with self._patch_get_latest_events([last_room_creation_event_id]):
            s2_event = self.helper.send_state(room_id, "s2", {}, tok=alice_tok)[
                "event_id"
            ]
            e3_event = self.helper.send(room_id, "e3", tok=alice_tok)["event_id"]

        # Another initial sync, with limit=1
        initial_sync_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(
                    alice,
                    filter_collection=FilterCollection(
                        self.hs, {"room": {"timeline": {"limit": 1}}}
                    ),
                    use_state_after=self.use_state_after,
                ),
                request_key=generate_request_key(),
            )
        )
        room_sync = initial_sync_result.joined[0]
        self.assertEqual(room_sync.room_id, room_id)
        self.assertEqual(
            [e.event.event_id for e in room_sync.timeline.events],
            [e3_event],
        )
        if self.use_state_after:
            # When using `state_after` we get told about s2 immediately
            self.assertIn(s2_event, [e.event_id for e in room_sync.state.values()])
        else:
            self.assertNotIn(s2_event, [e.event_id for e in room_sync.state.values()])

        # More events, E4 and E5
        with self._patch_get_latest_events([e3_event]):
            e4_event = self.helper.send(room_id, "e4", tok=alice_tok)["event_id"]
        e5_event = self.helper.send(room_id, "e5", tok=alice_tok)["event_id"]

        # Now incremental sync
        incremental_sync = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                alice_requester,
                generate_sync_config(alice, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
                since_token=initial_sync_result.next_batch,
            )
        )

        # The state event should appear in the 'state' section of the response.
        room_sync = incremental_sync.joined[0]
        self.assertEqual(room_sync.room_id, room_id)
        self.assertFalse(room_sync.timeline.limited)
        self.assertEqual(
            [e.event.event_id for e in room_sync.timeline.events],
            [e4_event, e5_event],
        )

        if self.use_state_after:
            # When using `state_after` we got told about s2 previously, so we
            # don't again.
            self.assertEqual(
                [e.event_id for e in room_sync.state.values()],
                [],
            )
        else:
            self.assertEqual(
                [e.event_id for e in room_sync.state.values()],
                [s2_event],
            )

    @parameterized.expand(
        [
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ],
        name_func=lambda func, num, p: f"{func.__name__}_{p.args[0]}_{p.args[1]}",
    )
    def test_archived_rooms_do_not_include_state_after_leave(
        self, initial_sync: bool, empty_timeline: bool
    ) -> None:
        """If the user leaves the room, state changes that happen after they leave are not returned.

        We try with both a zero and a normal timeline limit,
        and we try both an initial sync and an incremental sync for both.
        """
        if empty_timeline and not initial_sync:
            # FIXME synapse doesn't return the room at all in this situation!
            self.skipTest("Synapse does not correctly handle this case")

        # Alice creates the room, and bob joins.
        alice = self.register_user("alice", "password")
        alice_tok = self.login(alice, "password")

        bob = self.register_user("bob", "password")
        bob_tok = self.login(bob, "password")
        bob_requester = create_requester(bob)

        room_id = self.helper.create_room_as(alice, is_public=True, tok=alice_tok)
        self.helper.join(room_id, bob, tok=bob_tok)

        initial_sync_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                bob_requester,
                generate_sync_config(bob, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
            )
        )

        # Alice sends a message and a state
        before_message_event = self.helper.send(room_id, "before", tok=alice_tok)[
            "event_id"
        ]
        before_state_event = self.helper.send_state(
            room_id, "test_state", {"body": "before"}, tok=alice_tok
        )["event_id"]

        # Bob leaves
        leave_event = self.helper.leave(room_id, bob, tok=bob_tok)["event_id"]

        # Alice sends some more stuff
        self.helper.send(room_id, "after", tok=alice_tok)["event_id"]
        self.helper.send_state(room_id, "test_state", {"body": "after"}, tok=alice_tok)[
            "event_id"
        ]

        # And now, Bob resyncs.
        filter_dict: JsonDict = {"room": {"include_leave": True}}
        if empty_timeline:
            filter_dict["room"]["timeline"] = {"limit": 0}
        sync_room_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                bob_requester,
                generate_sync_config(
                    bob,
                    filter_collection=FilterCollection(self.hs, filter_dict),
                    use_state_after=self.use_state_after,
                ),
                request_key=generate_request_key(),
                since_token=None if initial_sync else initial_sync_result.next_batch,
            )
        ).archived[0]

        if empty_timeline:
            # The timeline should be empty
            self.assertEqual(sync_room_result.timeline.events, [])
        else:
            # The last three events in the timeline should be those leading up to the
            # leave
            self.assertEqual(
                [e.event.event_id for e in sync_room_result.timeline.events[-3:]],
                [before_message_event, before_state_event, leave_event],
            )

        if empty_timeline or self.use_state_after:
            # And the state should include the leave event...
            self.assertEqual(
                sync_room_result.state[("m.room.member", bob)].event_id, leave_event
            )
            # ... and the state change before he left.
            self.assertEqual(
                sync_room_result.state[("test_state", "")].event_id, before_state_event
            )
        else:
            # ... And the state should be empty
            self.assertEqual(sync_room_result.state, {})

    def _patch_get_latest_events(self, latest_events: list[str]) -> ContextManager:
        """Monkey-patch `get_prev_events_for_room`

        Returns a context manager which will replace the implementation of
        `get_prev_events_for_room` with one which returns `latest_events`.
        """
        return patch.object(
            self.hs.get_datastores().main,
            "get_prev_events_for_room",
            new_callable=AsyncMock,
            return_value=latest_events,
        )

    def test_call_invite_in_public_room_not_returned(self) -> None:
        user = self.register_user("alice", "password")
        tok = self.login(user, "password")
        room_id = self.helper.create_room_as(user, is_public=True, tok=tok)
        self.handler = self.hs.get_federation_handler()
        federation_event_handler = self.hs.get_federation_event_handler()

        async def _check_event_auth(
            origin: str | None, event: EventBase, context: EventContext
        ) -> None:
            pass

        federation_event_handler._check_event_auth = _check_event_auth  # type: ignore[method-assign]
        self.client = self.hs.get_federation_client()

        async def _check_sigs_and_hash_for_pulled_events_and_fetch(
            dest: str, pdus: Collection[EventBase], room_version: RoomVersion
        ) -> list[EventBase]:
            return list(pdus)

        self.client._check_sigs_and_hash_for_pulled_events_and_fetch = (  # type: ignore[method-assign]
            _check_sigs_and_hash_for_pulled_events_and_fetch  # type: ignore[assignment]
        )

        prev_events = self.get_success(self.store.get_prev_events_for_room(room_id))

        # create a call invite event
        call_event = make_test_pdu_event(
            {
                "type": EventTypes.CallInvite,
                "content": {},
                "room_id": room_id,
                "sender": user,
                "depth": 32,
                "prev_events": prev_events,
                "auth_events": prev_events,
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V10,
        )

        self.assertEqual(
            self.get_success(
                federation_event_handler.on_receive_pdu("test.serv", call_event)
            ),
            None,
        )

        # check that it is in DB
        recent_event = self.get_success(self.store.get_prev_events_for_room(room_id))
        self.assertIn(call_event.event_id, recent_event)

        # but that it does not come down /sync in public room
        sync_result: SyncResult = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                create_requester(user),
                generate_sync_config(user, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
            )
        )
        event_ids = []
        for event in sync_result.joined[0].timeline.events:
            event_ids.append(event.event.event_id)
        self.assertNotIn(call_event.event_id, event_ids)

        # it will come down in a private room, though
        user2 = self.register_user("bob", "password")
        tok2 = self.login(user2, "password")
        private_room_id = self.helper.create_room_as(
            user2, is_public=False, tok=tok2, extra_content={"preset": "private_chat"}
        )

        priv_prev_events = self.get_success(
            self.store.get_prev_events_for_room(private_room_id)
        )
        private_call_event = make_test_pdu_event(
            {
                "type": EventTypes.CallInvite,
                "content": {},
                "room_id": private_room_id,
                "sender": user,
                "depth": 32,
                "prev_events": priv_prev_events,
                "auth_events": priv_prev_events,
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V10,
        )

        self.assertEqual(
            self.get_success(
                federation_event_handler.on_receive_pdu("test.serv", private_call_event)
            ),
            None,
        )

        recent_events = self.get_success(
            self.store.get_prev_events_for_room(private_room_id)
        )
        self.assertIn(private_call_event.event_id, recent_events)

        private_sync_result: SyncResult = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                create_requester(user2),
                generate_sync_config(user2, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
            )
        )
        priv_event_ids = []
        for event in private_sync_result.joined[0].timeline.events:
            priv_event_ids.append(event.event.event_id)

        self.assertIn(private_call_event.event_id, priv_event_ids)

    def test_push_rules_with_bad_account_data(self) -> None:
        """Some old accounts have managed to set a `m.push_rules` account data,
        which we should ignore in /sync response.
        """

        user = self.register_user("alice", "password")

        # Insert the bad account data.
        self.get_success(
            self.store.add_account_data_for_user(user, AccountDataTypes.PUSH_RULES, {})
        )

        sync_result: SyncResult = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                create_requester(user),
                generate_sync_config(user, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
            )
        )

        for account_dict in sync_result.account_data:
            if account_dict["type"] == AccountDataTypes.PUSH_RULES:
                # We should have lots of push rules here, rather than the bad
                # empty data.
                self.assertNotEqual(account_dict["content"], {})
                return

        self.fail("No push rules found")

    def test_wait_for_future_sync_token(self) -> None:
        """Test that if we receive a token that is ahead of our current token,
        we'll wait until the stream position advances.

        This can happen if replication streams start lagging, and the client's
        previous sync request was serviced by a worker ahead of ours.
        """
        user = self.register_user("alice", "password")

        # We simulate a lagging stream by getting a stream ID from the ID gen
        # and then waiting to mark it as "persisted".
        presence_id_gen = self.store.get_presence_stream_id_gen()
        ctx_mgr = presence_id_gen.get_next()
        stream_id = self.get_success(ctx_mgr.__aenter__())

        # Create the new token based on the stream ID above.
        current_token = self.hs.get_event_sources().get_current_token()
        since_token = current_token.copy_and_advance(StreamKeyType.PRESENCE, stream_id)

        sync_d = defer.ensureDeferred(
            self.sync_handler.wait_for_sync_for_user(
                create_requester(user),
                generate_sync_config(user, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
                since_token=since_token,
                timeout=0,
            )
        )

        # This should block waiting for the presence stream to update
        #
        # Advance time a little bit to make the
        # `wait_for_stream_token(...)` sleep loop iterate.
        self.reactor.advance(Duration(seconds=2).as_secs())
        # It should still not be done yet
        self.assertFalse(sync_d.called)

        # Marking the stream ID as persisted should unblock the request.
        self.get_success(ctx_mgr.__aexit__(None, None, None))

        # Advance time to make another iteration of `wait_for_stream_token(...)` sleep
        # loop so it sees that we're finally caught up now.
        self.reactor.advance(Duration(seconds=1).as_secs())

        # Done waiting
        self.get_success(sync_d)

    @parameterized.expand(
        [(key,) for key in StreamKeyType.__members__.values()],
        name_func=lambda func, _, param: f"{func.__name__}_{param.args[0].name}",
    )
    def test_wait_for_invalid_future_sync_token(
        self, stream_key: StreamKeyType
    ) -> None:
        """Like the previous test, except we give a token that has a stream
        position ahead of what is in the DB, i.e. its invalid and we shouldn't
        wait for the stream to advance (as it may never do so).

        This can happen due to older versions of Synapse giving out stream
        positions without persisting them in the DB, and so on restart the
        stream would get reset back to an older position.
        """
        user = self.register_user("alice", "password")

        # Create a token and advance one of the streams.
        current_token = self.hs.get_event_sources().get_current_token()
        token_value = current_token.get_field(stream_key)

        # How we advance the streams depends on the type.
        if isinstance(token_value, int):
            since_token = current_token.copy_and_advance(stream_key, token_value + 1)
        elif isinstance(token_value, MultiWriterStreamToken):
            since_token = current_token.copy_and_advance(
                stream_key, MultiWriterStreamToken(stream=token_value.stream + 1)
            )
        elif isinstance(token_value, RoomStreamToken):
            since_token = current_token.copy_and_advance(
                stream_key, RoomStreamToken(stream=token_value.stream + 1)
            )
        else:
            raise Exception("Unreachable")

        sync_d = defer.ensureDeferred(
            self.sync_handler.wait_for_sync_for_user(
                create_requester(user),
                generate_sync_config(user, use_state_after=self.use_state_after),
                request_key=generate_request_key(),
                since_token=since_token,
                timeout=0,
            )
        )

        # We should return without waiting for the presence stream to advance.
        self.get_success(sync_d)


def generate_sync_config(
    user_id: str,
    device_id: str | None = "device_id",
    filter_collection: FilterCollection | None = None,
    use_state_after: bool = False,
) -> SyncConfig:
    """Generate a sync config (with a unique request key).

    Args:
        user_id: user who is syncing.
        device_id: device that is syncing. Defaults to "device_id".
        filter_collection: filter to apply. Defaults to the default filter (ie,
            return everything, with a default limit)
        use_state_after: whether the `use_state_after` flag was set.
    """
    if filter_collection is None:
        filter_collection = Filtering(Mock()).DEFAULT_FILTER_COLLECTION

    return SyncConfig(
        user=UserID.from_string(user_id),
        filter_collection=filter_collection,
        is_guest=False,
        device_id=device_id,
        use_state_after=use_state_after,
    )


class SyncProfileUpdatesTestCase(tests.unittest.HomeserverTestCase):
    """Tests Sync Handler for profile updates."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.sync_handler = self.hs.get_sync_handler()
        self.profile_handler = self.hs.get_profile_handler()
        self.store = self.hs.get_datastores().main
        self.user = self.register_user("user", "password")
        self.tok = self.login("user", "password")
        self.other_user = self.register_user("other_user", "password")
        self.other_tok = self.login("other_user", "password")
        self.joined_room = self.helper.create_room_as(self.user, tok=self.tok)
        self.get_success(
            self.store.set_profile_field(
                user_id=UserID.from_string(self.user),
                field_name="m.status",
                new_value={"text": "Swimming in the Great Lakes!", "emoji": "🏊"},
            )
        )
        self.helper.join(
            room=self.joined_room, user=self.other_user, tok=self.other_tok
        )

    def test_initial_sync_no_profile_updates_if_not_enabled(self) -> None:
        """Test that without MSC4429 enabled the initial sync response does not
        contain any profile updates."""
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="m.status",
                new_value={"text": "On holiday", "emoji": "🏖"},
            )
        )

        requester = create_requester(self.user)
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    self.user,
                ),
                request_key=generate_request_key(),
            )
        )
        self.assertEqual(initial_result.profile_updates, {})

    @override_config({"include_profile_updates_in_sync": True})
    def test_initial_sync_no_profile_updates_if_not_filtered_for(self) -> None:
        """Test that with MSC4429 enabled the initial sync response does not
        contain any profile updates, if fields are not filtered for."""
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="m.status",
                new_value={"text": "On holiday", "emoji": "🏖"},
            )
        )

        requester = create_requester(self.user)
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                ),
                request_key=generate_request_key(),
            )
        )
        self.assertEqual(
            initial_result.profile_updates,
            {},
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_initial_sync_responds_with_tracked_profile_updates(self) -> None:
        """Test that with MSC4429 enabled the initial sync response does
        contain profile updates for users who share rooms, for the fields the
        client requests. This response should include our syncing user."""
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="m.status",
                new_value={"text": "On holiday", "emoji": "🏖"},
            )
        )
        # Also set a field the client doesn't want
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="displayname",
                new_value="New displayname",
            )
        )

        requester = create_requester(self.user)
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {"ids": ["m.status"]}
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        assert initial_result.profile_updates[self.user] is not None
        assert initial_result.profile_updates["@other_user:test"] is not None
        self.assertEqual(
            initial_result.profile_updates["@other_user:test"]["m.status"],
            {"text": "On holiday", "emoji": "🏖"},
        )
        self.assertFalse(
            "displayname" in initial_result.profile_updates["@other_user:test"].keys(),
        )
        self.assertCountEqual(
            initial_result.profile_updates.keys(),
            [
                self.user,
                "@other_user:test",
            ],
        )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_initial_sync_does_not_include_untracked_users_profile_updates(
        self, is_lazy: bool
    ) -> None:
        """Test that with MSC4429 enabled the initial sync response does not
        contain profile updates for users who do not share rooms."""
        third_user = self.register_user("third_user", "password")
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(third_user),
                requester=create_requester(third_user),
                field_name="m.status",
                new_value={"text": "On holiday", "emoji": "🏖"},
            )
        )

        requester = create_requester(self.user)
        filter_json: dict[str, dict] = {
            "org.matrix.msc4429.profile_fields": {
                "ids": ["m.status", "displayname", "avatar_url"]
            }
        }
        if is_lazy:
            filter_json["room"] = {
                "state": {
                    "lazy_load_members": True,
                },
            }
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json=filter_json,
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        self.assertIsNone(initial_result.profile_updates.get(third_user))

    @override_config({"include_profile_updates_in_sync": True})
    def test_initial_sync_lazy_loading_responds_with_only_profiles_with_events(
        self,
    ) -> None:
        """Test that with MSC4429 enabled the initial sync lazy loading response does
        contain profile updates for events in the timeline.

        This test ensures lazy loading sync only returns profiles that we also have
        events for in the sync response. The second room in this test has the most
        recent events from "third_user" and thus we don't get the profile of
        "other_user" down the line, who is in the the same rooms as the syncer,
        but not in the second room.
        """
        third_user = self.register_user("third_user", "password")
        third_tok = self.login("third_user", "password")
        self.helper.join(
            room=self.joined_room,
            user=third_user,
            tok=third_tok,
        )

        requester = create_requester(self.user)

        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="m.status",
                new_value={"text": "On holiday", "emoji": "🏖"},
            )
        )
        # Check that lazy-loading filters out profile updates as well on initial sync.
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(third_user),
                requester=create_requester(third_user),
                field_name="m.status",
                new_value={"text": "On fire", "emoji": "🔥"},
            )
        )
        self.helper.send_messages(
            room_id=self.joined_room, num_events=1, tok=self.other_tok
        )
        self.helper.send_messages(
            room_id=self.joined_room, num_events=10, tok=third_tok
        )
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname", "avatar_url"]
                            },
                            "room": {
                                "state": {
                                    "lazy_load_members": True,
                                },
                            },
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        # Only third_user is returned, as lazy loading filters out the events from
        # the other users
        self.assertCountEqual(
            initial_result.profile_updates.keys(),
            [
                "@third_user:test",
            ],
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_incremental_sync_sends_down_profile_update_diffs(
        self,
    ) -> None:
        """Test that with MSC4429 enabled the incremental sync response does
        contain profile update diffs."""
        requester = create_requester(self.user)
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname", "avatar_url"]
                            }
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="m.status",
                new_value={"text": "On holiday", "emoji": "🏖"},
            )
        )
        # Set a field the client didn't ask for
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="uninterestingfield",
                new_value="Content",
            )
        )
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=initial_result.next_batch,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname", "avatar_url"]
                            }
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        assert incremental_result.profile_updates["@other_user:test"] is not None
        self.assertEqual(
            incremental_result.profile_updates["@other_user:test"]["m.status"],
            {"text": "On holiday", "emoji": "🏖"},
        )
        # We only send diffs in incremental sync for profile field updates
        self.assertFalse(
            "displayname"
            in incremental_result.profile_updates["@other_user:test"].keys(),
        )
        # The client didn't ask for this field
        self.assertFalse(
            "uninterestingfield"
            in incremental_result.profile_updates["@other_user:test"].keys(),
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_incremental_sync_does_not_filter_profile_updates_when_lazy_loading(
        self,
    ) -> None:
        """Test that with MSC4429 enabled the incremental sync lazy loading response
        does contain profile updates even if the user would be filtered out by lazy
        loading.
        """
        third_user = self.register_user("third_user", "password")
        third_tok = self.login("third_user", "password")
        self.helper.join(
            room=self.joined_room,
            user=third_user,
            tok=third_tok,
        )

        requester = create_requester(self.user)
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname"]
                            }
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="m.status",
                new_value={"text": "On holiday", "emoji": "🏖"},
            )
        )
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(third_user),
                requester=create_requester(third_user),
                field_name="m.status",
                new_value={"text": "On fire", "emoji": "🔥"},
            )
        )
        self.helper.send_messages(
            room_id=self.joined_room, num_events=1, tok=self.other_tok
        )
        self.helper.send_messages(
            room_id=self.joined_room, num_events=10, tok=third_tok
        )
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(third_user),
                requester=create_requester(third_user),
                field_name="uninterestingfield",
                new_value="Content",
            )
        )
        # Join a federated user to the room
        self.get_success(
            inject_member_event(
                self.hs,
                self.joined_room,
                "@federateduser:federatedhs",
                "join",
            )
        )
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=initial_result.next_batch,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname"]
                            },
                            "room": {
                                "state": {
                                    "lazy_load_members": True,
                                },
                            },
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )

        # Ensure our federated user is filtered out, even though they have an
        # event in the joined room timeline
        self.assertFalse(
            "@federateduser:federatedhs" in incremental_result.profile_updates.keys()
        )

        # Lazy loading only filters initial sync profile updates. Incremental syncs
        # should include all tracked profile updates for the syncing user.
        self.assertCountEqual(
            incremental_result.profile_updates.keys(),
            [
                "@other_user:test",
                "@third_user:test",
            ],
        )
        assert incremental_result.profile_updates["@other_user:test"] is not None

        # This is a field update, so should be here
        self.assertEqual(
            incremental_result.profile_updates["@other_user:test"]["m.status"],
            {"text": "On holiday", "emoji": "🏖"},
        )

        # We don't have events for this user in this response, so their full profile
        # is not included
        self.assertFalse(
            "displayname"
            in incremental_result.profile_updates["@other_user:test"].keys(),
        )
        assert incremental_result.profile_updates["@third_user:test"] is not None

        # This user has events in the timeline, thus the fields the client asked for
        # are included
        self.assertEqual(
            incremental_result.profile_updates["@third_user:test"]["m.status"],
            {"text": "On fire", "emoji": "🔥"},
        )
        self.assertFalse(
            "uninterestingfield"
            in incremental_result.profile_updates["@third_user:test"].keys(),
        )
        self.assertEqual(
            incremental_result.profile_updates["@third_user:test"]["displayname"],
            "third_user",
        )

    @parameterized.expand(
        [
            [True, True],
            [False, False],
            [True, False],
            [False, True],
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_sync_filters_out_profile_updates_from_federated_users(
        self,
        is_initial: bool,
        is_lazy: bool,
    ) -> None:
        """Test that with MSC4429 enabled any sync response
        doesn't contain federated users even if there are timeline events from them.
        """
        # Join a federated user to the room, causing a membership event into
        # the joined rooms sync response
        self.get_success(
            inject_member_event(
                self.hs,
                self.joined_room,
                "@federateduser1:federatedhs",
                "join",
            )
        )
        requester = create_requester(self.user)
        filter_json: dict[str, dict] = {
            "org.matrix.msc4429.profile_fields": {
                "ids": ["m.status", "displayname", "avatar_url"]
            },
        }
        if is_lazy:
            filter_json["room"] = {
                "state": {
                    "lazy_load_members": True,
                },
            }
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json=filter_json,
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        # Ensure our federated user is filtered out, even though they have an
        # event in the joined room timeline
        self.assertFalse(
            "@federateduser1:federatedhs" in initial_result.profile_updates.keys()
        )
        if not is_initial:
            # Join another federated user to the room, causing a membership event into
            # the joined rooms sync response
            self.get_success(
                inject_member_event(
                    self.hs,
                    self.joined_room,
                    "@federateduser2:federatedhs",
                    "join",
                )
            )
            incremental_result = self.get_success(
                self.sync_handler.wait_for_sync_for_user(
                    requester,
                    since_token=initial_result.next_batch,
                    sync_config=generate_sync_config(
                        user_id=self.user,
                        filter_collection=FilterCollection(
                            hs=self.hs,
                            filter_json=filter_json,
                        ),
                    ),
                    request_key=generate_request_key(),
                )
            )

            # Ensure our federated user is filtered out, even though they have an
            # event in the joined room timeline
            self.assertFalse(
                "@federateduser2:federatedhs"
                in incremental_result.profile_updates.keys()
            )

    @parameterized.expand(
        [
            [True, True],
            [False, False],
            [True, False],
            [False, True],
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_sync_profile_updates_works_correctly_with_falsey_values(
        self,
        is_initial: bool,
        is_lazy: bool,
    ) -> None:
        """Test that with MSC4429 enabled a sync response correctly includes falsey
        profile field values.
        """
        requester = create_requester(self.user)
        filter_json: dict[str, dict] = {
            "org.matrix.msc4429.profile_fields": {"ids": ["falseyvaluefield"]},
        }
        if is_lazy:
            filter_json["room"] = {
                "state": {
                    "lazy_load_members": True,
                },
            }
        for value in [False, 0, "", [], {}, None]:
            if is_initial:
                self.get_success(
                    self.profile_handler.set_field(
                        target_user=UserID.from_string(self.other_user),
                        requester=create_requester(self.other_user),
                        field_name="falseyvaluefield",
                        new_value=cast(JsonValue | dict[str, JsonValue], value),
                    )
                )
            initial_result = self.get_success(
                self.sync_handler.wait_for_sync_for_user(
                    requester,
                    sync_config=generate_sync_config(
                        user_id=self.user,
                        filter_collection=FilterCollection(
                            hs=self.hs,
                            filter_json=filter_json,
                        ),
                    ),
                    request_key=generate_request_key(),
                )
            )
            if is_initial:
                assert initial_result.profile_updates["@other_user:test"] is not None
                self.assertEqual(
                    initial_result.profile_updates["@other_user:test"][
                        "falseyvaluefield"
                    ],
                    value,
                )
            else:
                self.get_success(
                    self.profile_handler.set_field(
                        target_user=UserID.from_string(self.other_user),
                        requester=create_requester(self.other_user),
                        field_name="falseyvaluefield",
                        new_value=cast(JsonValue | dict[str, JsonValue], value),
                    )
                )
                incremental_result = self.get_success(
                    self.sync_handler.wait_for_sync_for_user(
                        requester,
                        since_token=initial_result.next_batch,
                        sync_config=generate_sync_config(
                            user_id=self.user,
                            filter_collection=FilterCollection(
                                hs=self.hs,
                                filter_json=filter_json,
                            ),
                        ),
                        request_key=generate_request_key(),
                    )
                )
                assert (
                    incremental_result.profile_updates["@other_user:test"] is not None
                )
                self.assertEqual(
                    incremental_result.profile_updates["@other_user:test"][
                        "falseyvaluefield"
                    ],
                    value,
                )

    @override_config({"include_profile_updates_in_sync": True})
    def test_incremental_sync_lazy_loading_cache_filters_recently_sent_profiles_and_fields(
        self,
    ) -> None:
        """Test that with MSC4429 enabled the incremental sync lazy loading response
        filters out unchanged profiles or fields we have recently sent to the client.
        """
        requester = create_requester(self.user)
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="sooninterestingfield",
                new_value="Content",
            )
        )
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname", "avatar_url"]
                            },
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        self.helper.send_messages(
            room_id=self.joined_room,
            num_events=1,
            tok=self.other_tok,
        )
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=initial_result.next_batch,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname", "avatar_url"]
                            },
                            "room": {
                                "state": {
                                    "lazy_load_members": True,
                                },
                            },
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        # Lazy loading incremental sync should include profiles from events
        self.assertCountEqual(
            incremental_result.profile_updates.keys(),
            [
                "@other_user:test",
            ],
        )
        assert incremental_result.profile_updates["@other_user:test"] is not None
        self.assertEqual(
            set(incremental_result.profile_updates["@other_user:test"].keys()),
            {"avatar_url", "displayname"},
        )

        # If we have more events from the other_user, and do another lazy sync,
        # we don't expect the full profile to be sent again due to our cache.
        self.helper.send_messages(
            room_id=self.joined_room, num_events=1, tok=self.other_tok
        )
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=incremental_result.next_batch,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname", "avatar_url"]
                            },
                            "room": {
                                "state": {
                                    "lazy_load_members": True,
                                },
                            },
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        self.assertCountEqual(
            incremental_result.profile_updates.keys(),
            [],
        )
        # However, if we again add an event, we do expect any fields the client didn't
        # previously ask for to be there.
        self.helper.send_messages(
            room_id=self.joined_room, num_events=1, tok=self.other_tok
        )
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=incremental_result.next_batch,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": [
                                    "m.status",
                                    "displayname",
                                    "avatar_url",
                                    "sooninterestingfield",
                                ]
                            },
                            "room": {
                                "state": {
                                    "lazy_load_members": True,
                                },
                            },
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        self.assertCountEqual(
            incremental_result.profile_updates.keys(),
            [
                "@other_user:test",
            ],
        )
        assert incremental_result.profile_updates["@other_user:test"] is not None
        self.assertEqual(
            set(incremental_result.profile_updates["@other_user:test"].keys()),
            {"sooninterestingfield"},
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_incremental_sync_sends_down_null_profile_if_user_no_longer_sharing_rooms(
        self,
    ) -> None:
        """Test that with MSC4429 enabled the incremental sync response
        includes a 'null' for users who are no longer sharing rooms.
        """
        requester = create_requester(self.user)
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname", "avatar_url"]
                            }
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        self.helper.leave(
            room=self.joined_room, user=self.other_user, tok=self.other_tok
        )
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=initial_result.next_batch,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["m.status", "displayname", "avatar_url"]
                            }
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        self.assertIsNone(
            incremental_result.profile_updates["@other_user:test"],
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_incremental_sync_sends_down_all_requested_fields_for_users_who_have_joined(
        self,
    ) -> None:
        """Test that with MSC4429 enabled the incremental sync response
        includes all the requested fields of a user who has joined a room with the
        syncing user.
        """
        requester = create_requester(self.user)
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["displayname", "avatar_url"]
                            },
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=initial_result.next_batch,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["displayname", "avatar_url"]
                            },
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )

        third_user = self.register_user("third_user", "password")
        third_tok = self.login("third_user", "password")
        self.helper.join(
            room=self.joined_room,
            user=third_user,
            tok=third_tok,
        )
        # Set a status field we don't except to see in sync
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(third_user),
                requester=create_requester(third_user),
                field_name="m.status",
                new_value={"text": "On fire", "emoji": "🔥"},
            )
        )
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=incremental_result.next_batch,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json={
                            "org.matrix.msc4429.profile_fields": {
                                "ids": ["displayname", "avatar_url"]
                            },
                        },
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        assert incremental_result.profile_updates["@third_user:test"] is not None
        self.assertCountEqual(
            incremental_result.profile_updates.keys(),
            [third_user],
        )
        self.assertEqual(
            incremental_result.profile_updates["@third_user:test"]["displayname"],
            "third_user",
        )
        self.assertIsNone(
            incremental_result.profile_updates["@third_user:test"]["avatar_url"],
        )
        self.assertFalse(
            "m.status" in incremental_result.profile_updates["@third_user:test"].keys(),
        )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_incremental_sync_includes_own_profile_updates(self, is_lazy: bool) -> None:
        """Test that with MSC4429 enabled the incremental sync response includes
        ones own profile updates."""
        requester = create_requester(self.user)
        filter_json: dict[str, dict] = {
            "org.matrix.msc4429.profile_fields": {"ids": ["m.status", "avatar_url"]}
        }
        if is_lazy:
            filter_json["room"] = {
                "state": {
                    "lazy_load_members": True,
                },
            }
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json=filter_json,
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.user),
                requester=requester,
                field_name="m.status",
                new_value={"text": "On holiday", "emoji": "🏖"},
            )
        )
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=initial_result.next_batch,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json=filter_json,
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        assert incremental_result.profile_updates["@user:test"] is not None
        self.assertEqual(
            incremental_result.profile_updates["@user:test"]["m.status"],
            {"text": "On holiday", "emoji": "🏖"},
        )
        # We didn't ask for displayname
        self.assertFalse(
            "displayname" in incremental_result.profile_updates["@user:test"].keys(),
        )

    @parameterized.expand([[True, False], [True, True], [False, False], [False, True]])
    @override_config({"include_profile_updates_in_sync": True})
    def test_incremental_sync_join_leave_join_leave(
        self,
        eager_sync: bool,
        is_lazy: bool,
    ) -> None:
        """Test that with MSC4429 enabled the incremental sync response
        correctly handles multiple join / leave / join / leave in a row.

        In the first variant we sync and check after each iteration of join/leave.
        In the second variant we only sync at the end of all the join/leaves.
        We do both of these as lazy and not-lazy variants.
        """
        # Use third_user for this test as other_user is already joined
        third_user = self.register_user("third_user", "password")
        third_tok = self.login("third_user", "password")

        filter_json: dict[str, dict] = {
            "org.matrix.msc4429.profile_fields": {"ids": ["displayname", "avatar_url"]}
        }
        if is_lazy:
            filter_json["room"] = {
                "state": {
                    "lazy_load_members": True,
                },
            }

        requester = create_requester(self.user)
        initial_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json=filter_json,
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        # Join the room
        self.helper.join(
            room=self.joined_room,
            user=third_user,
            tok=third_tok,
        )
        next_token = initial_result.next_batch
        if eager_sync:
            incremental_result = self.get_success(
                self.sync_handler.wait_for_sync_for_user(
                    requester,
                    since_token=next_token,
                    sync_config=generate_sync_config(
                        user_id=self.user,
                        filter_collection=FilterCollection(
                            hs=self.hs,
                            filter_json=filter_json,
                        ),
                    ),
                    request_key=generate_request_key(),
                )
            )
            # We expect there to be the users profile
            self.assertIsNotNone(
                incremental_result.profile_updates["@third_user:test"],
            )
            next_token = incremental_result.next_batch
        # Leave the room
        self.helper.leave(
            room=self.joined_room,
            user=third_user,
            tok=third_tok,
        )
        if eager_sync:
            # Ensure we don't get caught by the cache
            self.reactor.advance((LAZY_LOADED_PROFILE_FIELDS_CACHE_MAX_AGE / 1000) + 1)
            incremental_result = self.get_success(
                self.sync_handler.wait_for_sync_for_user(
                    requester,
                    since_token=next_token,
                    sync_config=generate_sync_config(
                        user_id=self.user,
                        filter_collection=FilterCollection(
                            hs=self.hs,
                            filter_json=filter_json,
                        ),
                    ),
                    request_key=generate_request_key(),
                )
            )
            # We expect there to be a null profile
            self.assertIsNone(
                incremental_result.profile_updates["@third_user:test"],
            )
            next_token = incremental_result.next_batch
        # Join the room
        self.helper.join(
            room=self.joined_room,
            user=third_user,
            tok=third_tok,
        )
        if eager_sync:
            # Ensure we don't get caught by the cache
            self.reactor.advance((LAZY_LOADED_PROFILE_FIELDS_CACHE_MAX_AGE / 1000) + 1)
            incremental_result = self.get_success(
                self.sync_handler.wait_for_sync_for_user(
                    requester,
                    since_token=next_token,
                    sync_config=generate_sync_config(
                        user_id=self.user,
                        filter_collection=FilterCollection(
                            hs=self.hs,
                            filter_json=filter_json,
                        ),
                    ),
                    request_key=generate_request_key(),
                )
            )
            # We expect there to be the users profile
            self.assertIsNotNone(
                incremental_result.profile_updates["@third_user:test"],
            )
            next_token = incremental_result.next_batch
        # Leave the room
        self.helper.leave(
            room=self.joined_room,
            user=third_user,
            tok=third_tok,
        )
        # Ensure we don't get caught by the cache
        self.reactor.advance((LAZY_LOADED_PROFILE_FIELDS_CACHE_MAX_AGE / 1000) + 1)
        incremental_result = self.get_success(
            self.sync_handler.wait_for_sync_for_user(
                requester,
                since_token=next_token,
                sync_config=generate_sync_config(
                    user_id=self.user,
                    filter_collection=FilterCollection(
                        hs=self.hs,
                        filter_json=filter_json,
                    ),
                ),
                request_key=generate_request_key(),
            )
        )
        # The end result should be null profile
        self.assertIsNone(
            incremental_result.profile_updates["@third_user:test"],
        )


class SyncStateAfterTestCase(tests.unittest.HomeserverTestCase):
    """Tests Sync Handler state behavior when using `use_state_after."""

    servlets = [
        admin.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sync_handler = self.hs.get_sync_handler()
        self.store = self.hs.get_datastores().main

        # AuthBlocking reads from the hs' config on initialization. We need to
        # modify its config instead of the hs'
        self.auth_blocking = self.hs.get_auth_blocking()

    def test_initial_sync_multiple_deltas(self) -> None:
        """Test that if multiple state deltas have happened during processing of
        a full state sync we return the correct state"""

        user = self.register_user("user", "password")
        tok = self.login("user", "password")

        # Create a room as the user and set some custom state.
        joined_room = self.helper.create_room_as(user, tok=tok)

        first_state = self.helper.send_state(
            joined_room, event_type="m.test_event", body={"num": 1}, tok=tok
        )

        # Take a snapshot of the stream token, to simulate doing an initial sync
        # at this point.
        end_stream_token = self.hs.get_event_sources().get_current_token()

        # Send some state *after* the stream token
        self.helper.send_state(
            joined_room, event_type="m.test_event", body={"num": 2}, tok=tok
        )

        # Calculating the full state will return the first state, and not the
        # second.
        state = self.get_success(
            self.sync_handler._compute_state_delta_for_full_sync(
                room_id=joined_room,
                sync_config=generate_sync_config(user, use_state_after=True),
                batch=TimelineBatch(
                    prev_batch=end_stream_token, events=[], limited=True
                ),
                end_token=end_stream_token,
                members_to_fetch=None,
                timeline_state={},
                joined=True,
            )
        )
        self.assertEqual(state[("m.test_event", "")], first_state["event_id"])

    def test_incremental_sync_multiple_deltas(self) -> None:
        """Test that if multiple state deltas have happened since an incremental
        state sync we return the correct state"""

        user = self.register_user("user", "password")
        tok = self.login("user", "password")

        # Create a room as the user and set some custom state.
        joined_room = self.helper.create_room_as(user, tok=tok)

        # Take a snapshot of the stream token, to simulate doing an incremental sync
        # from this point.
        since_token = self.hs.get_event_sources().get_current_token()

        self.helper.send_state(
            joined_room, event_type="m.test_event", body={"num": 1}, tok=tok
        )

        # Send some state *after* the stream token
        second_state = self.helper.send_state(
            joined_room, event_type="m.test_event", body={"num": 2}, tok=tok
        )

        end_stream_token = self.hs.get_event_sources().get_current_token()

        # Calculating the incrementals state will return the second state, and not the
        # first.
        state = self.get_success(
            self.sync_handler._compute_state_delta_for_incremental_sync(
                room_id=joined_room,
                sync_config=generate_sync_config(user, use_state_after=True),
                batch=TimelineBatch(
                    prev_batch=end_stream_token, events=[], limited=True
                ),
                since_token=since_token,
                end_token=end_stream_token,
                members_to_fetch=None,
                timeline_state={},
            )
        )
        self.assertEqual(state[("m.test_event", "")], second_state["event_id"])

    def test_incremental_sync_lazy_loaded_no_timeline(self) -> None:
        """Test that lazy-loading with an empty timeline doesn't return the full
        state.

        There was a bug where an empty state filter would cause the DB to return
        the full state, rather than an empty set.
        """
        user = self.register_user("user", "password")
        tok = self.login("user", "password")

        # Create a room as the user and set some custom state.
        joined_room = self.helper.create_room_as(user, tok=tok)

        since_token = self.hs.get_event_sources().get_current_token()
        end_stream_token = self.hs.get_event_sources().get_current_token()

        state = self.get_success(
            self.sync_handler._compute_state_delta_for_incremental_sync(
                room_id=joined_room,
                sync_config=generate_sync_config(user, use_state_after=True),
                batch=TimelineBatch(
                    prev_batch=end_stream_token, events=[], limited=True
                ),
                since_token=since_token,
                end_token=end_stream_token,
                members_to_fetch=set(),
                timeline_state={},
            )
        )

        self.assertEqual(state, {})
