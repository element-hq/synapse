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
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from typing_extensions import assert_never

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import (
    AccountDataTypes,
    EventContentFields,
    EventTypes,
    RoomTypes,
)
from synapse.events import EventBase
from synapse.rest.client import devices, login, receipts, room, sync
from synapse.server import HomeServer
from synapse.types import (
    JsonDict,
    RoomStreamToken,
    SlidingSyncStreamToken,
    StreamKeyType,
    StreamToken,
)
from synapse.util import Clock
from synapse.util.stringutils import random_string

from tests import unittest
from tests.server import TimedOutException

logger = logging.getLogger(__name__)


class SlidingSyncBase(unittest.HomeserverTestCase):
    """Base class for sliding sync test cases"""

    sync_endpoint = "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def do_sync(
        self, sync_body: JsonDict, *, since: Optional[str] = None, tok: str
    ) -> Tuple[JsonDict, str]:
        """Do a sliding sync request with given body.

        Asserts the request was successful.

        Attributes:
            sync_body: The full request body to use
            since: Optional since token
            tok: Access token to use

        Returns:
            A tuple of the response body and the `pos` field.
        """

        sync_path = self.sync_endpoint
        if since:
            sync_path += f"?pos={since}"

        channel = self.make_request(
            method="POST",
            path=sync_path,
            content=sync_body,
            access_token=tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        return channel.json_body, channel.json_body["pos"]

    def _assertRequiredStateIncludes(
        self,
        actual_required_state: Any,
        expected_state_events: Iterable[EventBase],
        exact: bool = False,
    ) -> None:
        """
        Wrapper around `assertIncludes` to give slightly better looking diff error
        messages that include some context "$event_id (type, state_key)".

        Args:
            actual_required_state: The "required_state" of a room from a Sliding Sync
                request response.
            expected_state_events: The expected state events to be included in the
                `actual_required_state`.
            exact: Whether the actual state should be exactly equal to the expected
                state (no extras).
        """

        assert isinstance(actual_required_state, list)
        for event in actual_required_state:
            assert isinstance(event, dict)

        self.assertIncludes(
            {
                f'{event["event_id"]} ("{event["type"]}", "{event["state_key"]}")'
                for event in actual_required_state
            },
            {
                f'{event.event_id} ("{event.type}", "{event.state_key}")'
                for event in expected_state_events
            },
            exact=exact,
            # Message to help understand the diff in context
            message=str(actual_required_state),
        )

    def _bump_notifier_wait_for_events(
        self,
        user_id: str,
        wake_stream_key: Literal[
            StreamKeyType.ACCOUNT_DATA,
            StreamKeyType.PRESENCE,
        ],
    ) -> None:
        """
        Wake-up a `notifier.wait_for_events(user_id)` call without affecting the Sliding
        Sync results.

        Args:
            user_id: The user ID to wake up the notifier for
            wake_stream_key: The stream key to wake up. This will create an actual new
                entity in that stream so it's best to choose one that won't affect the
                Sliding Sync results you're testing for. In other words, if your testing
                account data, choose `StreamKeyType.PRESENCE` instead. We support two
                possible stream keys because you're probably testing one or the other so
                one is always a "safe" option.
        """
        # We're expecting some new activity from this point onwards
        from_token = self.hs.get_event_sources().get_current_token()

        triggered_notifier_wait_for_events = False

        async def _on_new_acivity(
            before_token: StreamToken, after_token: StreamToken
        ) -> bool:
            nonlocal triggered_notifier_wait_for_events
            triggered_notifier_wait_for_events = True
            return True

        notifier = self.hs.get_notifier()

        # Listen for some new activity for the user. We're just trying to confirm that
        # our bump below actually does what we think it does (triggers new activity for
        # the user).
        result_awaitable = notifier.wait_for_events(
            user_id,
            1000,
            _on_new_acivity,
            from_token=from_token,
        )

        # Update the account data or presence so that `notifier.wait_for_events(...)`
        # wakes up. We chose these two options because they're least likely to show up
        # in the Sliding Sync response so it won't affect whether we have results.
        if wake_stream_key == StreamKeyType.ACCOUNT_DATA:
            self.get_success(
                self.hs.get_account_data_handler().add_account_data_for_user(
                    user_id,
                    "org.matrix.foobarbaz",
                    {"foo": "bar"},
                )
            )
        elif wake_stream_key == StreamKeyType.PRESENCE:
            sending_user_id = self.register_user(
                "user_bump_notifier_wait_for_events_" + random_string(10), "pass"
            )
            sending_user_tok = self.login(sending_user_id, "pass")
            test_msg = {"foo": "bar"}
            chan = self.make_request(
                "PUT",
                "/_matrix/client/r0/sendToDevice/m.test/1234",
                content={"messages": {user_id: {"d1": test_msg}}},
                access_token=sending_user_tok,
            )
            self.assertEqual(chan.code, 200, chan.result)
        else:
            assert_never(wake_stream_key)

        # Wait for our notifier result
        self.get_success(result_awaitable)

        if not triggered_notifier_wait_for_events:
            raise AssertionError(
                "Expected `notifier.wait_for_events(...)` to be triggered"
            )


class SlidingSyncTestCase(SlidingSyncBase):
    """
    Tests regarding MSC3575 Sliding Sync `/sync` endpoint.

    Please put tests in more specific test files if applicable. This test class is meant
    for generic behavior of the endpoint.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
        receipts.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.storage_controllers = hs.get_storage_controllers()
        self.account_data_handler = hs.get_account_data_handler()

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

    def test_sync_list(self) -> None:
        """
        Test that room IDs show up in the Sliding Sync `lists`
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 1,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(response_body["lists"].keys()),
            ["foo-list"],
            response_body["lists"].keys(),
        )

        # Make sure the list includes the room we are joined to
        self.assertListEqual(
            list(response_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [room_id],
                }
            ],
            response_body["lists"]["foo-list"],
        )

    def test_wait_for_sync_token(self) -> None:
        """
        Test that worker will wait until it catches up to the given token
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a future token that will cause us to wait. Since we never send a new
        # event to reach that future stream_ordering, the worker will wait until the
        # full timeout.
        stream_id_gen = self.store.get_events_stream_id_generator()
        stream_id = self.get_success(stream_id_gen.get_next().__aenter__())
        current_token = self.event_sources.get_current_token()
        future_position_token = current_token.copy_and_replace(
            StreamKeyType.ROOM,
            RoomStreamToken(stream=stream_id),
        )

        future_position_token_serialized = self.get_success(
            SlidingSyncStreamToken(future_position_token, 0).to_string(self.store)
        )

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 1,
                }
            }
        }
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?pos={future_position_token_serialized}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 10 seconds to make `notifier.wait_for_stream_token(from_token)`
        # timeout
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=9900)
        channel.await_result(timeout_ms=200)
        self.assertEqual(channel.code, 200, channel.json_body)

        # We expect the next `pos` in the result to be the same as what we requested
        # with because we weren't able to find anything new yet.
        self.assertEqual(channel.json_body["pos"], future_position_token_serialized)

    def test_wait_for_new_data(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive.

        (Only applies to incremental syncs with a `timeout` specified)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": 1,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?timeout=10000&pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Bump the room with new events to trigger new results
        event_response1 = self.helper.send(
            room_id, "new activity in room", tok=user1_tok
        )
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check to make sure the new event is returned
        self.assertEqual(
            [
                event["event_id"]
                for event in channel.json_body["rooms"][room_id]["timeline"]
            ],
            [
                event_response1["event_id"],
            ],
            channel.json_body["rooms"][room_id]["timeline"],
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        doesn't trigger a false-positive for new data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": 1,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?timeout=10000&pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Wake-up `notifier.wait_for_events(...)` that will cause us test
        # `SlidingSyncResult.__bool__` for new results.
        self._bump_notifier_wait_for_events(
            user1_id, wake_stream_key=StreamKeyType.ACCOUNT_DATA
        )
        # Block for a little bit more to ensure we don't see any new results.
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=4000)
        # Wait for the sync to complete (wait for the rest of the 10 second timeout,
        # 5000 + 4000 + 1200 > 10000)
        channel.await_result(timeout_ms=1200)
        self.assertEqual(channel.code, 200, channel.json_body)

        # There should be no room sent down.
        self.assertFalse(channel.json_body["rooms"])

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
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
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
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        from_token = channel.json_body["pos"]

        # Make sure the response has the lists we requested
        self.assertListEqual(
            list(channel.json_body["lists"].keys()),
            ["all-list", "foo-list"],
            channel.json_body["lists"].keys(),
        )

        # Make sure the lists have the correct rooms
        self.assertListEqual(
            list(channel.json_body["lists"]["all-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [space_room_id, room_id],
                }
            ],
        )
        self.assertListEqual(
            list(channel.json_body["lists"]["foo-list"]["ops"]),
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
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?pos={from_token}",
            {
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
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make sure the lists have the correct rooms even though we `newly_left`
        self.assertListEqual(
            list(channel.json_body["lists"]["all-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [space_room_id, room_id],
                }
            ],
        )
        self.assertListEqual(
            list(channel.json_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [space_room_id],
                }
            ],
        )

    def test_sort_list(self) -> None:
        """
        Test that the `lists` are sorted by `stream_ordering`
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        room_id3 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        # Activity that will order the rooms
        self.helper.send(room_id3, "activity in room3", tok=user1_tok)
        self.helper.send(room_id1, "activity in room1", tok=user1_tok)
        self.helper.send(room_id2, "activity in room2", tok=user1_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 1,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(response_body["lists"].keys()),
            ["foo-list"],
            response_body["lists"].keys(),
        )

        # Make sure the list is sorted in the way we expect
        self.assertListEqual(
            list(response_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [room_id2, room_id1, room_id3],
                }
            ],
            response_body["lists"]["foo-list"],
        )

    def test_sliced_windows(self) -> None:
        """
        Test that the `lists` `ranges` are sliced correctly. Both sides of each range
        are inclusive.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        _room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        room_id3 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        # Make the Sliding Sync request for a single room
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": 1,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(response_body["lists"].keys()),
            ["foo-list"],
            response_body["lists"].keys(),
        )
        # Make sure the list is sorted in the way we expect
        self.assertListEqual(
            list(response_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 0],
                    "room_ids": [room_id3],
                }
            ],
            response_body["lists"]["foo-list"],
        )

        # Make the Sliding Sync request for the first two rooms
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 1,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(response_body["lists"].keys()),
            ["foo-list"],
            response_body["lists"].keys(),
        )
        # Make sure the list is sorted in the way we expect
        self.assertListEqual(
            list(response_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 1],
                    "room_ids": [room_id3, room_id2],
                }
            ],
            response_body["lists"]["foo-list"],
        )

    def test_rooms_with_no_updates_do_not_come_down_incremental_sync(self) -> None:
        """
        Test that rooms with no updates are returned in subsequent incremental
        syncs.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }

        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make the incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Nothing has happened in the room, so the room should not come down
        # /sync.
        self.assertIsNone(response_body["rooms"].get(room_id1))

    def test_empty_initial_room_comes_down_sync(self) -> None:
        """
        Test that rooms come down /sync even with empty required state and
        timeline limit in initial sync.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }

        # Make the Sliding Sync request
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertEqual(response_body["rooms"][room_id1]["initial"], True)
