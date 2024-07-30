#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
from http import HTTPStatus
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from parameterized import parameterized

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import (
    AccountDataTypes,
    EventContentFields,
    EventTypes,
    HistoryVisibility,
    Membership,
    ReceiptTypes,
    RoomTypes,
)
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.handlers.sliding_sync import StateValues
from synapse.rest.client import devices, login, receipts, room, sync
from synapse.server import HomeServer
from synapse.types import (
    JsonDict,
    RoomStreamToken,
    SlidingSyncStreamToken,
    StreamKeyType,
    StreamToken,
    UserID,
)
from synapse.types.handlers import SlidingSyncConfig
from synapse.util import Clock
from synapse.util.stringutils import random_string

from tests import unittest
from tests.server import TimedOutException
from tests.test_utils.event_injection import create_event, mark_event_as_partial_state

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
            raise AssertionError(
                "Unable to wake that stream in _bump_notifier_wait_for_events(...)"
            )

        # Wait for our notifier result
        self.get_success(result_awaitable)

        if not triggered_notifier_wait_for_events:
            raise AssertionError(
                "Expected `notifier.wait_for_events(...)` to be triggered"
            )


class SlidingSyncTestCase(SlidingSyncBase):
    """
    Tests regarding MSC3575 Sliding Sync `/sync` endpoint.
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
                    "required_state": [
                        ["m.room.join_rules", ""],
                        ["m.room.history_visibility", ""],
                        ["m.space.child", "*"],
                    ],
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
                    "required_state": [
                        ["m.room.join_rules", ""],
                        ["m.room.history_visibility", ""],
                        ["m.space.child", "*"],
                    ],
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
                    "required_state": [
                        ["m.room.join_rules", ""],
                        ["m.room.history_visibility", ""],
                        ["m.space.child", "*"],
                    ],
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

    def test_rooms_meta_when_joined(self) -> None:
        """
        Test that the `rooms` `name` and `avatar` are included in the response and
        reflect the current state of the room when the user is joined to the room.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "name": "my super room",
            },
        )
        # Set the room avatar URL
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Reflect the current state of the room
        self.assertEqual(
            response_body["rooms"][room_id1]["name"],
            "my super room",
            response_body["rooms"][room_id1],
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["avatar"],
            "mxc://DUMMY_MEDIA_ID",
            response_body["rooms"][room_id1],
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["joined_count"],
            2,
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["invited_count"],
            0,
        )
        self.assertIsNone(
            response_body["rooms"][room_id1].get("is_dm"),
        )

    def test_rooms_meta_when_invited(self) -> None:
        """
        Test that the `rooms` `name` and `avatar` are included in the response and
        reflect the current state of the room when the user is invited to the room.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "name": "my super room",
            },
        )
        # Set the room avatar URL
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        # User1 is invited to the room
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        # Update the room name after user1 has left
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )
        # Update the room avatar URL after user1 has left
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://UPDATED_DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # This should still reflect the current state of the room even when the user is
        # invited.
        self.assertEqual(
            response_body["rooms"][room_id1]["name"],
            "my super duper room",
            response_body["rooms"][room_id1],
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["avatar"],
            "mxc://UPDATED_DUMMY_MEDIA_ID",
            response_body["rooms"][room_id1],
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["joined_count"],
            1,
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["invited_count"],
            1,
        )
        self.assertIsNone(
            response_body["rooms"][room_id1].get("is_dm"),
        )

    def test_rooms_meta_when_banned(self) -> None:
        """
        Test that the `rooms` `name` and `avatar` reflect the state of the room when the
        user was banned (do not leak current state).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "name": "my super room",
            },
        )
        # Set the room avatar URL
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        # Update the room name after user1 has left
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )
        # Update the room avatar URL after user1 has left
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://UPDATED_DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Reflect the state of the room at the time of leaving
        self.assertEqual(
            response_body["rooms"][room_id1]["name"],
            "my super room",
            response_body["rooms"][room_id1],
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["avatar"],
            "mxc://DUMMY_MEDIA_ID",
            response_body["rooms"][room_id1],
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["joined_count"],
            # FIXME: The actual number should be "1" (user2) but we currently don't
            # support this for rooms where the user has left/been banned.
            0,
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["invited_count"],
            0,
        )
        self.assertIsNone(
            response_body["rooms"][room_id1].get("is_dm"),
        )

    def test_rooms_meta_heroes(self) -> None:
        """
        Test that the `rooms` `heroes` are included in the response when the room
        doesn't have a room name set.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        _user3_tok = self.login(user3_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "name": "my super room",
            },
        )
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        # User3 is invited
        self.helper.invite(room_id1, src=user2_id, targ=user3_id, tok=user2_tok)

        room_id2 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                # No room name set so that `heroes` is populated
                #
                # "name": "my super room2",
            },
        )
        self.helper.join(room_id2, user1_id, tok=user1_tok)
        # User3 is invited
        self.helper.invite(room_id2, src=user2_id, targ=user3_id, tok=user2_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Room1 has a name so we shouldn't see any `heroes` which the client would use
        # the calculate the room name themselves.
        self.assertEqual(
            response_body["rooms"][room_id1]["name"],
            "my super room",
            response_body["rooms"][room_id1],
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("heroes"))
        self.assertEqual(
            response_body["rooms"][room_id1]["joined_count"],
            2,
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["invited_count"],
            1,
        )

        # Room2 doesn't have a name so we should see `heroes` populated
        self.assertIsNone(response_body["rooms"][room_id2].get("name"))
        self.assertCountEqual(
            [
                hero["user_id"]
                for hero in response_body["rooms"][room_id2].get("heroes", [])
            ],
            # Heroes shouldn't include the user themselves (we shouldn't see user1)
            [user2_id, user3_id],
        )
        self.assertEqual(
            response_body["rooms"][room_id2]["joined_count"],
            2,
        )
        self.assertEqual(
            response_body["rooms"][room_id2]["invited_count"],
            1,
        )

        # We didn't request any state so we shouldn't see any `required_state`
        self.assertIsNone(response_body["rooms"][room_id1].get("required_state"))
        self.assertIsNone(response_body["rooms"][room_id2].get("required_state"))

    def test_rooms_meta_heroes_max(self) -> None:
        """
        Test that the `rooms` `heroes` only includes the first 5 users (not including
        yourself).
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

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                # No room name set so that `heroes` is populated
                #
                # "name": "my super room",
            },
        )
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        self.helper.join(room_id1, user4_id, tok=user4_tok)
        self.helper.join(room_id1, user5_id, tok=user5_tok)
        self.helper.join(room_id1, user6_id, tok=user6_tok)
        self.helper.join(room_id1, user7_id, tok=user7_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Room2 doesn't have a name so we should see `heroes` populated
        self.assertIsNone(response_body["rooms"][room_id1].get("name"))
        self.assertCountEqual(
            [
                hero["user_id"]
                for hero in response_body["rooms"][room_id1].get("heroes", [])
            ],
            # Heroes should be the first 5 users in the room (excluding the user
            # themselves, we shouldn't see `user1`)
            [user2_id, user3_id, user4_id, user5_id, user6_id],
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["joined_count"],
            7,
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["invited_count"],
            0,
        )

        # We didn't request any state so we shouldn't see any `required_state`
        self.assertIsNone(response_body["rooms"][room_id1].get("required_state"))

    def test_rooms_meta_heroes_when_banned(self) -> None:
        """
        Test that the `rooms` `heroes` are included in the response when the room
        doesn't have a room name set but doesn't leak information past their ban.
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
        _user5_tok = self.login(user5_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                # No room name set so that `heroes` is populated
                #
                # "name": "my super room",
            },
        )
        # User1 joins the room
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        # User3 is invited
        self.helper.invite(room_id1, src=user2_id, targ=user3_id, tok=user2_tok)

        # User1 is banned from the room
        self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        # User4 joins the room after user1 is banned
        self.helper.join(room_id1, user4_id, tok=user4_tok)
        # User5 is invited after user1 is banned
        self.helper.invite(room_id1, src=user2_id, targ=user5_id, tok=user2_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Room2 doesn't have a name so we should see `heroes` populated
        self.assertIsNone(response_body["rooms"][room_id1].get("name"))
        self.assertCountEqual(
            [
                hero["user_id"]
                for hero in response_body["rooms"][room_id1].get("heroes", [])
            ],
            # Heroes shouldn't include the user themselves (we shouldn't see user1). We
            # also shouldn't see user4 since they joined after user1 was banned.
            #
            # FIXME: The actual result should be `[user2_id, user3_id]` but we currently
            # don't support this for rooms where the user has left/been banned.
            [],
        )

        self.assertEqual(
            response_body["rooms"][room_id1]["joined_count"],
            # FIXME: The actual number should be "1" (user2) but we currently don't
            # support this for rooms where the user has left/been banned.
            0,
        )
        self.assertEqual(
            response_body["rooms"][room_id1]["invited_count"],
            # We shouldn't see user5 since they were invited after user1 was banned.
            #
            # FIXME: The actual number should be "1" (user3) but we currently don't
            # support this for rooms where the user has left/been banned.
            0,
        )

    def test_rooms_limited_initial_sync(self) -> None:
        """
        Test that we mark `rooms` as `limited=True` when we saturate the `timeline_limit`
        on initial sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity1", tok=user2_tok)
        self.helper.send(room_id1, "activity2", tok=user2_tok)
        event_response3 = self.helper.send(room_id1, "activity3", tok=user2_tok)
        event_pos3 = self.get_success(
            self.store.get_position_for_event(event_response3["event_id"])
        )
        event_response4 = self.helper.send(room_id1, "activity4", tok=user2_tok)
        event_pos4 = self.get_success(
            self.store.get_position_for_event(event_response4["event_id"])
        )
        event_response5 = self.helper.send(room_id1, "activity5", tok=user2_tok)
        user1_join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

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

        # We expect to saturate the `timeline_limit` (there are more than 3 messages in the room)
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            True,
            response_body["rooms"][room_id1],
        )
        # Check to make sure the latest events are returned
        self.assertEqual(
            [
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response4["event_id"],
                event_response5["event_id"],
                user1_join_response["event_id"],
            ],
            response_body["rooms"][room_id1]["timeline"],
        )

        # Check to make sure the `prev_batch` points at the right place
        prev_batch_token = self.get_success(
            StreamToken.from_string(
                self.store, response_body["rooms"][room_id1]["prev_batch"]
            )
        )
        prev_batch_room_stream_token_serialized = self.get_success(
            prev_batch_token.room_key.to_string(self.store)
        )
        # If we use the `prev_batch` token to look backwards, we should see `event3`
        # next so make sure the token encompasses it
        self.assertEqual(
            event_pos3.persisted_after(prev_batch_token.room_key),
            False,
            f"`prev_batch` token {prev_batch_room_stream_token_serialized} should be >= event_pos3={self.get_success(event_pos3.to_room_stream_token().to_string(self.store))}",
        )
        # If we use the `prev_batch` token to look backwards, we shouldn't see `event4`
        # anymore since it was just returned in this response.
        self.assertEqual(
            event_pos4.persisted_after(prev_batch_token.room_key),
            True,
            f"`prev_batch` token {prev_batch_room_stream_token_serialized} should be < event_pos4={self.get_success(event_pos4.to_room_stream_token().to_string(self.store))}",
        )

        # With no `from_token` (initial sync), it's all historical since there is no
        # "live" range
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            0,
            response_body["rooms"][room_id1],
        )

    def test_rooms_not_limited_initial_sync(self) -> None:
        """
        Test that we mark `rooms` as `limited=False` when there are no more events to
        paginate to.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity1", tok=user2_tok)
        self.helper.send(room_id1, "activity2", tok=user2_tok)
        self.helper.send(room_id1, "activity3", tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        timeline_limit = 100
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": timeline_limit,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # The timeline should be `limited=False` because we have all of the events (no
        # more to paginate to)
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            False,
            response_body["rooms"][room_id1],
        )
        expected_number_of_events = 9
        # We're just looking to make sure we got all of the events before hitting the `timeline_limit`
        self.assertEqual(
            len(response_body["rooms"][room_id1]["timeline"]),
            expected_number_of_events,
            response_body["rooms"][room_id1]["timeline"],
        )
        self.assertLessEqual(expected_number_of_events, timeline_limit)

        # With no `from_token` (initial sync), it's all historical since there is no
        # "live" token range.
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            0,
            response_body["rooms"][room_id1],
        )

    def test_rooms_incremental_sync(self) -> None:
        """
        Test `rooms` data during an incremental sync after an initial sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.send(room_id1, "activity before initial sync1", tok=user2_tok)

        # Make an initial Sliding Sync request to grab a token. This is also a sanity
        # check that we can go from initial to incremental sync.
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

        # Send some events but don't send enough to saturate the `timeline_limit`.
        # We want to later test that we only get the new events since the `next_pos`
        event_response2 = self.helper.send(room_id1, "activity after2", tok=user2_tok)
        event_response3 = self.helper.send(room_id1, "activity after3", tok=user2_tok)

        # Make an incremental Sliding Sync request (what we're trying to test)
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # We only expect to see the new events since the last sync which isn't enough to
        # fill up the `timeline_limit`.
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            False,
            f'Our `timeline_limit` was {sync_body["lists"]["foo-list"]["timeline_limit"]} '
            + f'and {len(response_body["rooms"][room_id1]["timeline"])} events were returned in the timeline. '
            + str(response_body["rooms"][room_id1]),
        )
        # Check to make sure the latest events are returned
        self.assertEqual(
            [
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response2["event_id"],
                event_response3["event_id"],
            ],
            response_body["rooms"][room_id1]["timeline"],
        )

        # All events are "live"
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            2,
            response_body["rooms"][room_id1],
        )

    def test_rooms_bump_stamp(self) -> None:
        """
        Test that `bump_stamp` is present and pointing to relevant events.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )
        event_response1 = message_response = self.helper.send(
            room_id1, "message in room1", tok=user1_tok
        )
        event_pos1 = self.get_success(
            self.store.get_position_for_event(event_response1["event_id"])
        )
        room_id2 = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )
        send_response2 = self.helper.send(room_id2, "message in room2", tok=user1_tok)
        event_pos2 = self.get_success(
            self.store.get_position_for_event(send_response2["event_id"])
        )

        # Send a reaction in room1 but it shouldn't affect the `bump_stamp`
        # because reactions are not part of the `DEFAULT_BUMP_EVENT_TYPES`
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

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 100,
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

        # Make sure the list includes the rooms in the right order
        self.assertListEqual(
            list(response_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 1],
                    # room1 sorts before room2 because it has the latest event (the
                    # reaction)
                    "room_ids": [room_id1, room_id2],
                }
            ],
            response_body["lists"]["foo-list"],
        )

        # The `bump_stamp` for room1 should point at the latest message (not the
        # reaction since it's not one of the `DEFAULT_BUMP_EVENT_TYPES`)
        self.assertEqual(
            response_body["rooms"][room_id1]["bump_stamp"],
            event_pos1.stream,
            response_body["rooms"][room_id1],
        )

        # The `bump_stamp` for room2 should point at the latest message
        self.assertEqual(
            response_body["rooms"][room_id2]["bump_stamp"],
            event_pos2.stream,
            response_body["rooms"][room_id2],
        )

    def test_rooms_bump_stamp_backfill(self) -> None:
        """
        Test that `bump_stamp` ignores backfilled events, i.e. events with a
        negative stream ordering.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a remote room
        creator = "@user:other"
        room_id = "!foo:other"
        shared_kwargs = {
            "room_id": room_id,
            "room_version": "10",
        }

        create_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[],
                type=EventTypes.Create,
                state_key="",
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
        # We add a message event as a valid "bump type"
        msg_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[creator_tuple[0].event_id],
                auth_event_ids=[create_tuple[0].event_id],
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
            msg_tuple,
            invite_tuple,
        ]

        # Ensure the local HS knows the room version
        self.get_success(
            self.store.store_room(room_id, creator, False, RoomVersions.V10)
        )

        # Persist these events as backfilled events.
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None

        for event, context in remote_events_and_contexts:
            self.get_success(persistence.persist_event(event, context, backfilled=True))

        # Now we join the local user to the room
        join_tuple = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[invite_tuple[0].event_id],
                auth_event_ids=[create_tuple[0].event_id, invite_tuple[0].event_id],
                type=EventTypes.Member,
                state_key=user1_id,
                content={"membership": Membership.JOIN},
                sender=user1_id,
                **shared_kwargs,
            )
        )
        self.get_success(persistence.persist_event(*join_tuple))

        # Doing an SS request should return a positive `bump_stamp`, even though
        # the only event that matches the bump types has as negative stream
        # ordering.
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 5,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        self.assertGreater(response_body["rooms"][room_id]["bump_stamp"], 0)

    def test_rooms_newly_joined_incremental_sync(self) -> None:
        """
        Test that when we make an incremental sync with a `newly_joined` `rooms`, we are
        able to see some historical events before the `from_token`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity before token1", tok=user2_tok)
        event_response2 = self.helper.send(
            room_id1, "activity before token2", tok=user2_tok
        )

        # The `timeline_limit` is set to 4 so we can at least see one historical event
        # before the `from_token`. We should see historical events because this is a
        # `newly_joined` room.
        timeline_limit = 4
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": timeline_limit,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Join the room after the `from_token` which will make us consider this room as
        # `newly_joined`.
        user1_join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Send some events but don't send enough to saturate the `timeline_limit`.
        # We want to later test that we only get the new events since the `next_pos`
        event_response3 = self.helper.send(
            room_id1, "activity after token3", tok=user2_tok
        )
        event_response4 = self.helper.send(
            room_id1, "activity after token4", tok=user2_tok
        )

        # Make an incremental Sliding Sync request (what we're trying to test)
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # We should see the new events and the rest should be filled with historical
        # events which will make us `limited=True` since there are more to paginate to.
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            True,
            f"Our `timeline_limit` was {timeline_limit} "
            + f'and {len(response_body["rooms"][room_id1]["timeline"])} events were returned in the timeline. '
            + str(response_body["rooms"][room_id1]),
        )
        # Check to make sure that the "live" and historical events are returned
        self.assertEqual(
            [
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response2["event_id"],
                user1_join_response["event_id"],
                event_response3["event_id"],
                event_response4["event_id"],
            ],
            response_body["rooms"][room_id1]["timeline"],
        )

        # Only events after the `from_token` are "live" (join, event3, event4)
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            3,
            response_body["rooms"][room_id1],
        )

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

    def test_rooms_ban_initial_sync(self) -> None:
        """
        Test that `rooms` we are banned from in an intial sync only allows us to see
        timeline events up to the ban event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.send(room_id1, "activity before2", tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        event_response3 = self.helper.send(room_id1, "activity after3", tok=user2_tok)
        event_response4 = self.helper.send(room_id1, "activity after4", tok=user2_tok)
        user1_ban_response = self.helper.ban(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )

        self.helper.send(room_id1, "activity after5", tok=user2_tok)
        self.helper.send(room_id1, "activity after6", tok=user2_tok)

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

        # We should see events before the ban but not after
        self.assertEqual(
            [
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response3["event_id"],
                event_response4["event_id"],
                user1_ban_response["event_id"],
            ],
            response_body["rooms"][room_id1]["timeline"],
        )
        # No "live" events in an initial sync (no `from_token` to define the "live"
        # range)
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            0,
            response_body["rooms"][room_id1],
        )
        # There are more events to paginate to
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            True,
            response_body["rooms"][room_id1],
        )

    def test_rooms_ban_incremental_sync1(self) -> None:
        """
        Test that `rooms` we are banned from during the next incremental sync only
        allows us to see timeline events up to the ban event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.send(room_id1, "activity before2", tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 4,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        event_response3 = self.helper.send(room_id1, "activity after3", tok=user2_tok)
        event_response4 = self.helper.send(room_id1, "activity after4", tok=user2_tok)
        # The ban is within the token range (between the `from_token` and the sliding
        # sync request)
        user1_ban_response = self.helper.ban(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )

        self.helper.send(room_id1, "activity after5", tok=user2_tok)
        self.helper.send(room_id1, "activity after6", tok=user2_tok)

        # Make the incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # We should see events before the ban but not after
        self.assertEqual(
            [
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response3["event_id"],
                event_response4["event_id"],
                user1_ban_response["event_id"],
            ],
            response_body["rooms"][room_id1]["timeline"],
        )
        # All live events in the incremental sync
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            3,
            response_body["rooms"][room_id1],
        )
        # There aren't anymore events to paginate to in this range
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            False,
            response_body["rooms"][room_id1],
        )

    def test_rooms_ban_incremental_sync2(self) -> None:
        """
        Test that `rooms` we are banned from before the incremental sync don't return
        any events in the timeline.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send(room_id1, "activity after2", tok=user2_tok)
        # The ban is before we get our `from_token`
        self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        self.helper.send(room_id1, "activity after3", tok=user2_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 4,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        self.helper.send(room_id1, "activity after4", tok=user2_tok)

        # Make the incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Nothing to see for this banned user in the room in the token range
        self.assertIsNone(response_body["rooms"].get(room_id1))

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

    def test_rooms_required_state_incremental_sync_restart(self) -> None:
        """
        Test `rooms.required_state` returns requested state events in the room during an
        incremental sync, after a restart (and so the in memory caches are reset).
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
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # If the cache has been cleared then we do expect the state to come down
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
        Test partially-stated room are excluded unless `rooms.required_state` is
        lazy-loading room members.
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

        # Make the Sliding Sync request (NOT lazy-loading room members)
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 0,
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Make sure the list includes room1 but room2 is excluded because it's still
        # partially-stated
        self.assertListEqual(
            list(response_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 1],
                    "room_ids": [room_id1],
                }
            ],
            response_body["lists"]["foo-list"],
        )

        # Make the Sliding Sync request (with lazy-loading room members)
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        # Lazy-load room members
                        [EventTypes.Member, StateValues.LAZY],
                    ],
                    "timeline_limit": 0,
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # The list should include both rooms now because we're lazy-loading room members
        self.assertListEqual(
            list(response_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 1],
                    "room_ids": [room_id2, room_id1],
                }
            ],
            response_body["lists"]["foo-list"],
        )

    def test_room_subscriptions_with_join_membership(self) -> None:
        """
        Test `room_subscriptions` with a joined room should give us timeline and current
        state events.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request with just the room subscription
        sync_body = {
            "room_subscriptions": {
                room_id1: {
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 1,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # We should see some state
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

        # We should see some events
        self.assertEqual(
            [
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            [
                join_response["event_id"],
            ],
            response_body["rooms"][room_id1]["timeline"],
        )
        # No "live" events in an initial sync (no `from_token` to define the "live"
        # range)
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            0,
            response_body["rooms"][room_id1],
        )
        # There are more events to paginate to
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            True,
            response_body["rooms"][room_id1],
        )

    def test_room_subscriptions_with_leave_membership(self) -> None:
        """
        Test `room_subscriptions` with a leave room should give us timeline and state
        events up to the leave event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )

        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Send some events after user1 leaves
        self.helper.send(room_id1, "activity after leave", tok=user2_tok)
        # Update state after user1 leaves
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "qux"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with just the room subscription
        sync_body = {
            "room_subscriptions": {
                room_id1: {
                    "required_state": [
                        ["org.matrix.foo_state", ""],
                    ],
                    "timeline_limit": 2,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # We should see the state at the time of the leave
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[("org.matrix.foo_state", "")],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

        # We should see some before we left (nothing after)
        self.assertEqual(
            [
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            [
                join_response["event_id"],
                leave_response["event_id"],
            ],
            response_body["rooms"][room_id1]["timeline"],
        )
        # No "live" events in an initial sync (no `from_token` to define the "live"
        # range)
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            0,
            response_body["rooms"][room_id1],
        )
        # There are more events to paginate to
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            True,
            response_body["rooms"][room_id1],
        )

    def test_room_subscriptions_no_leak_private_room(self) -> None:
        """
        Test `room_subscriptions` with a private room we have never been in should not
        leak any data to the user.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=False)

        # We should not be able to join the private room
        self.helper.join(
            room_id1, user1_id, tok=user1_tok, expect_code=HTTPStatus.FORBIDDEN
        )

        # Make the Sliding Sync request with just the room subscription
        sync_body = {
            "room_subscriptions": {
                room_id1: {
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 1,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # We should not see the room at all (we're not in it)
        self.assertIsNone(response_body["rooms"].get(room_id1), response_body["rooms"])

    def test_room_subscriptions_world_readable(self) -> None:
        """
        Test `room_subscriptions` with a room that has `world_readable` history visibility

        FIXME: We should be able to see the room timeline and state
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room with `world_readable` history visibility
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

        # Note: We never join the room

        # Make the Sliding Sync request with just the room subscription
        sync_body = {
            "room_subscriptions": {
                room_id1: {
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 1,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # FIXME: In the future, we should be able to see the room because it's
        # `world_readable` but currently we don't support this.
        self.assertIsNone(response_body["rooms"].get(room_id1), response_body["rooms"])

    # Any extensions that use `lists`/`rooms` should be tested here
    @parameterized.expand([("account_data",), ("receipts",)])
    def test_extensions_lists_rooms_relevant_rooms(self, extension_name: str) -> None:
        """
        With various extensions, test out requesting different variations of
        `lists`/`rooms`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create some rooms
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id3 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id4 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id5 = self.helper.create_room_as(user1_id, tok=user1_tok)

        room_id_to_human_name_map = {
            room_id1: "room1",
            room_id2: "room2",
            room_id3: "room3",
            room_id4: "room4",
            room_id5: "room5",
        }

        for room_id in room_id_to_human_name_map.keys():
            if extension_name == "account_data":
                # Add some account data to each room
                self.get_success(
                    self.account_data_handler.add_account_data_to_room(
                        user_id=user1_id,
                        room_id=room_id,
                        account_data_type="org.matrix.roorarraz",
                        content={"roo": "rar"},
                    )
                )
            elif extension_name == "receipts":
                event_response = self.helper.send(
                    room_id, body="new event", tok=user1_tok
                )
                # Read last event
                channel = self.make_request(
                    "POST",
                    f"/rooms/{room_id}/receipt/{ReceiptTypes.READ}/{event_response['event_id']}",
                    {},
                    access_token=user1_tok,
                )
                self.assertEqual(channel.code, 200, channel.json_body)
            else:
                raise AssertionError(f"Unknown extension name: {extension_name}")

        main_sync_body = {
            "lists": {
                # We expect this list range to include room5 and room4
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                },
                # We expect this list range to include room5, room4, room3
                "bar-list": {
                    "ranges": [[0, 2]],
                    "required_state": [],
                    "timeline_limit": 0,
                },
            },
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
        }

        # Mix lists and rooms
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    "lists": ["foo-list", "non-existent-list"],
                    "rooms": [room_id1, room_id2, "!non-existent-room"],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ✅ Requested via `rooms` and a room subscription exists
        # room2: ❌ Requested via `rooms` but not in the response (from lists or room subscriptions)
        # room3: ❌ Not requested
        # room4: ✅ Shows up because requested via `lists` and list exists in the response
        # room5: ✅ Shows up because requested via `lists` and list exists in the response
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            {"room1", "room4", "room5"},
            exact=True,
        )

        # Try wildcards (this is the default)
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    # "lists": ["*"],
                    # "rooms": ["*"],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ✅ Shows up because of default `rooms` wildcard and is in one of the room subscriptions
        # room2: ❌ Not requested
        # room3: ✅ Shows up because of default `lists` wildcard and is in a list
        # room4: ✅ Shows up because of default `lists` wildcard and is in a list
        # room5: ✅ Shows up because of default `lists` wildcard and is in a list
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            {"room1", "room3", "room4", "room5"},
            exact=True,
        )

        # Empty list will return nothing
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    "lists": [],
                    "rooms": [],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ❌ Not requested
        # room2: ❌ Not requested
        # room3: ❌ Not requested
        # room4: ❌ Not requested
        # room5: ❌ Not requested
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            set(),
            exact=True,
        )

        # Try wildcard and none
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    "lists": ["*"],
                    "rooms": [],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ❌ Not requested
        # room2: ❌ Not requested
        # room3: ✅ Shows up because of default `lists` wildcard and is in a list
        # room4: ✅ Shows up because of default `lists` wildcard and is in a list
        # room5: ✅ Shows up because of default `lists` wildcard and is in a list
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            {"room3", "room4", "room5"},
            exact=True,
        )

        # Try requesting a room that is only in a list
        sync_body = {
            **main_sync_body,
            "extensions": {
                extension_name: {
                    "enabled": True,
                    "lists": [],
                    "rooms": [room_id5],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # room1: ❌ Not requested
        # room2: ❌ Not requested
        # room3: ❌ Not requested
        # room4: ❌ Not requested
        # room5: ✅ Requested via `rooms` and is in a list
        self.assertIncludes(
            {
                room_id_to_human_name_map[room_id]
                for room_id in response_body["extensions"][extension_name]
                .get("rooms")
                .keys()
            },
            {"room5"},
            exact=True,
        )

    def test_rooms_required_state_incremental_sync_LIVE(self) -> None:
        """Test that we only get state updates in incremental sync for rooms
        we've already seen (LIVE).
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
                        [EventTypes.Name, ""],
                    ],
                    "timeline_limit": 0,
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
                state_map[(EventTypes.RoomHistoryVisibility, "")],
            },
            exact=True,
        )

        # Send a state event
        self.helper.send_state(
            room_id1, EventTypes.Name, body={"name": "foo"}, tok=user2_tok
        )

        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self.assertNotIn("initial", response_body["rooms"][room_id1])
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Name, "")],
            },
            exact=True,
        )

    @parameterized.expand([(False,), (True,)])
    def test_rooms_timeline_incremental_sync_PREVIOUSLY(self, limited: bool) -> None:
        """
        Test getting room data where we have previously sent down the room, but
        we missed sending down some timeline events previously and so its status
        is considered PREVIOUSLY.

        There are two versions of this test, one where there are more messages
        than the timeline limit, and one where there isn't.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)

        self.helper.send(room_id1, "msg", tok=user1_tok)

        timeline_limit = 5
        conn_id = "conn_id"
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": timeline_limit,
                }
            },
            "conn_id": "conn_id",
        }

        # The first room gets sent down the initial sync
        response_body, initial_from_token = self.do_sync(sync_body, tok=user1_tok)
        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )

        # We now send down some events in room1 (depending on the test param).
        expected_events = []  # The set of events in the timeline
        if limited:
            for _ in range(10):
                resp = self.helper.send(room_id1, "msg1", tok=user1_tok)
                expected_events.append(resp["event_id"])
        else:
            resp = self.helper.send(room_id1, "msg1", tok=user1_tok)
            expected_events.append(resp["event_id"])

        # A second messages happens in the other room, so room1 won't get sent down.
        self.helper.send(room_id2, "msg", tok=user1_tok)

        # Only the second room gets sent down sync.
        response_body, from_token = self.do_sync(
            sync_body, since=initial_from_token, tok=user1_tok
        )

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id2}, response_body["rooms"]
        )

        # FIXME: This is a hack to record that the first room wasn't sent down
        # sync, as we don't implement that currently.
        sliding_sync_handler = self.hs.get_sliding_sync_handler()
        requester = self.get_success(
            self.hs.get_auth().get_user_by_access_token(user1_tok)
        )
        sync_config = SlidingSyncConfig(
            user=requester.user,
            requester=requester,
            conn_id=conn_id,
        )

        parsed_initial_from_token = self.get_success(
            SlidingSyncStreamToken.from_string(self.store, initial_from_token)
        )
        connection_position = self.get_success(
            sliding_sync_handler.connection_store.record_rooms(
                sync_config,
                parsed_initial_from_token,
                sent_room_ids=[],
                unsent_room_ids=[room_id1],
            )
        )

        # FIXME: Now fix up `from_token` with new connect position above.
        parsed_from_token = self.get_success(
            SlidingSyncStreamToken.from_string(self.store, from_token)
        )
        parsed_from_token = SlidingSyncStreamToken(
            stream_token=parsed_from_token.stream_token,
            connection_position=connection_position,
        )
        from_token = self.get_success(parsed_from_token.to_string(self.store))

        # We now send another event to room1, so we should sync all the missing events.
        resp = self.helper.send(room_id1, "msg2", tok=user1_tok)
        expected_events.append(resp["event_id"])

        # This sync should contain the messages from room1 not yet sent down.
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )
        self.assertNotIn("initial", response_body["rooms"][room_id1])

        self.assertEqual(
            [ev["event_id"] for ev in response_body["rooms"][room_id1]["timeline"]],
            expected_events[-timeline_limit:],
        )
        self.assertEqual(response_body["rooms"][room_id1]["limited"], limited)
        self.assertEqual(response_body["rooms"][room_id1].get("required_state"), None)

    def test_rooms_required_state_incremental_sync_PREVIOUSLY(self) -> None:
        """
        Test getting room data where we have previously sent down the room, but
        we missed sending down some state previously and so its status is
        considered PREVIOUSLY.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)

        self.helper.send(room_id1, "msg", tok=user1_tok)

        conn_id = "conn_id"
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.RoomHistoryVisibility, ""],
                        # This one doesn't exist in the room
                        [EventTypes.Name, ""],
                    ],
                    "timeline_limit": 0,
                }
            },
            "conn_id": "conn_id",
        }

        # The first room gets sent down the initial sync
        response_body, initial_from_token = self.do_sync(sync_body, tok=user1_tok)
        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )

        # We now send down some state in room1
        resp = self.helper.send_state(
            room_id1, EventTypes.Name, {"name": "foo"}, tok=user1_tok
        )
        name_change_id = resp["event_id"]

        # A second messages happens in the other room, so room1 won't get sent down.
        self.helper.send(room_id2, "msg", tok=user1_tok)

        # Only the second room gets sent down sync.
        response_body, from_token = self.do_sync(
            sync_body, since=initial_from_token, tok=user1_tok
        )

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id2}, response_body["rooms"]
        )

        # FIXME: This is a hack to record that the first room wasn't sent down
        # sync, as we don't implement that currently.
        sliding_sync_handler = self.hs.get_sliding_sync_handler()
        requester = self.get_success(
            self.hs.get_auth().get_user_by_access_token(user1_tok)
        )
        sync_config = SlidingSyncConfig(
            user=requester.user,
            requester=requester,
            conn_id=conn_id,
        )

        parsed_initial_from_token = self.get_success(
            SlidingSyncStreamToken.from_string(self.store, initial_from_token)
        )
        connection_position = self.get_success(
            sliding_sync_handler.connection_store.record_rooms(
                sync_config,
                parsed_initial_from_token,
                sent_room_ids=[],
                unsent_room_ids=[room_id1],
            )
        )

        # FIXME: Now fix up `from_token` with new connect position above.
        parsed_from_token = self.get_success(
            SlidingSyncStreamToken.from_string(self.store, from_token)
        )
        parsed_from_token = SlidingSyncStreamToken(
            stream_token=parsed_from_token.stream_token,
            connection_position=connection_position,
        )
        from_token = self.get_success(parsed_from_token.to_string(self.store))

        # We now send another event to room1, so we should sync all the missing state.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # This sync should contain the state changes from room1.
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )
        self.assertNotIn("initial", response_body["rooms"][room_id1])

        # We should only see the name change.
        self.assertEqual(
            [
                ev["event_id"]
                for ev in response_body["rooms"][room_id1]["required_state"]
            ],
            [name_change_id],
        )

    def test_rooms_required_state_incremental_sync_NEVER(self) -> None:
        """
        Test getting `required_state` where we have NEVER sent down the room before
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)

        self.helper.send(room_id1, "msg", tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.RoomHistoryVisibility, ""],
                        # This one doesn't exist in the room
                        [EventTypes.Name, ""],
                    ],
                    "timeline_limit": 1,
                }
            },
        }

        # A message happens in the other room, so room1 won't get sent down.
        self.helper.send(room_id2, "msg", tok=user1_tok)

        # Only the second room gets sent down sync.
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id2}, response_body["rooms"]
        )

        # We now send another event to room1, so we should send down the full
        # room.
        self.helper.send(room_id1, "msg2", tok=user1_tok)

        # This sync should contain the messages from room1 not yet sent down.
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )

        self.assertEqual(response_body["rooms"][room_id1]["initial"], True)

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

    def test_rooms_timeline_incremental_sync_NEVER(self) -> None:
        """
        Test getting timeline room data where we have NEVER sent down the room
        before
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": 5,
                }
            },
        }

        expected_events = []
        for _ in range(4):
            resp = self.helper.send(room_id1, "msg", tok=user1_tok)
            expected_events.append(resp["event_id"])

        # A message happens in the other room, so room1 won't get sent down.
        self.helper.send(room_id2, "msg", tok=user1_tok)

        # Only the second room gets sent down sync.
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id2}, response_body["rooms"]
        )

        # We now send another event to room1 so it comes down sync
        resp = self.helper.send(room_id1, "msg2", tok=user1_tok)
        expected_events.append(resp["event_id"])

        # This sync should contain the messages from room1 not yet sent down.
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )

        self.assertEqual(
            [ev["event_id"] for ev in response_body["rooms"][room_id1]["timeline"]],
            expected_events,
        )
        self.assertEqual(response_body["rooms"][room_id1]["limited"], True)
        self.assertEqual(response_body["rooms"][room_id1]["initial"], True)

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
