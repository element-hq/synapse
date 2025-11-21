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
from typing import Any, Iterable, Literal
from unittest.mock import AsyncMock

from parameterized import parameterized, parameterized_class
from typing_extensions import assert_never

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import (
    AccountDataTypes,
    EventContentFields,
    EventTypes,
    JoinRules,
    Membership,
    RoomTypes,
)
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase, StrippedStateEvent, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.handlers.sliding_sync import StateValues
from synapse.rest.client import account_data, devices, login, receipts, room, sync
from synapse.server import HomeServer
from synapse.types import (
    JsonDict,
    RoomStreamToken,
    SlidingSyncStreamToken,
    StreamKeyType,
    StreamToken,
)
from synapse.util.clock import Clock
from synapse.util.stringutils import random_string

from tests import unittest
from tests.server import TimedOutException
from tests.test_utils.event_injection import create_event

logger = logging.getLogger(__name__)


class SlidingSyncBase(unittest.HomeserverTestCase):
    """Base class for sliding sync test cases"""

    # Flag as to whether to use the new sliding sync tables or not
    #
    # FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
    # foreground update for
    # `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
    # https://github.com/element-hq/synapse/issues/17623)
    use_new_tables: bool = True

    sync_endpoint = "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
        # foreground update for
        # `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
        # https://github.com/element-hq/synapse/issues/17623)
        hs.get_datastores().main.have_finished_sliding_sync_background_jobs = AsyncMock(  # type: ignore[method-assign]
            return_value=self.use_new_tables
        )

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def do_sync(
        self, sync_body: JsonDict, *, since: str | None = None, tok: str
    ) -> tuple[JsonDict, str]:
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
        store = self.hs.get_datastores().main

        # Get the current DM map
        existing_dm_map = self.get_success(
            store.get_global_account_data_by_type_for_user(
                source_user_id, AccountDataTypes.DIRECT
            )
        )
        # Scrutinize the account data since it has no concrete type. We're just copying
        # everything into a known type. It should be a mapping from user ID to a list of
        # room IDs. Ignore anything else.
        new_dm_map: dict[str, list[str]] = {}
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
            store.add_account_data_for_user(
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

    _remote_invite_count: int = 0

    def _create_remote_invite_room_for_user(
        self,
        invitee_user_id: str,
        unsigned_invite_room_state: list[StrippedStateEvent] | None,
        invite_room_id: str | None = None,
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
            invite_room_id: Optional remote room ID to be invited to. When unset, we
                will generate one.

        Returns:
            The room ID of the remote invite room
        """
        store = self.hs.get_datastores().main

        if invite_room_id is None:
            invite_room_id = f"!test_room{self._remote_invite_count}:remote_server"

        invite_event_dict = {
            "room_id": invite_room_id,
            "sender": "@inviter:remote_server",
            "state_key": invitee_user_id,
            # Just keep advancing the depth
            "depth": self._remote_invite_count,
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
            store.maybe_store_room_on_outlier_membership(
                room_id=invite_room_id, room_version=invite_event.room_version
            )
        )
        context = EventContext.for_outlier(self.hs.get_storage_controllers())
        persist_controller = self.hs.get_storage_controllers().persistence
        assert persist_controller is not None
        self.get_success(persist_controller.persist_event(invite_event, context))

        self._remote_invite_count += 1

        return invite_room_id

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
        account_data.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.storage_controllers = hs.get_storage_controllers()
        self.account_data_handler = hs.get_account_data_handler()
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence

        super().prepare(reactor, clock, hs)

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

    def test_forgotten_up_to_date(self) -> None:
        """
        Make sure we get up-to-date `forgotten` status for rooms
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 is banned from the room (was never in the room)
        self.helper.ban(room_id, src=user2_id, targ=user1_id, tok=user2_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                    "filters": {},
                },
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

        # User1 forgets the room
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # We should no longer see the forgotten room
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            set(),
            exact=True,
        )

    def test_rejoin_forgotten_room(self) -> None:
        """
        Make sure we can see a forgotten room again if we rejoin (or any new membership
        like an invite) (no longer forgotten)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # User1 joins the room
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        # We should see the room (like normal)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

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

        # We should see the room again after re-joining
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

    def test_invited_to_forgotten_remote_room(self) -> None:
        """
        Make sure we can see a forgotten room again if we are invited again
        (remote/federated out-of-band memberships)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a remote room invite (out-of-band membership)
        room_id = self._create_remote_invite_room_for_user(user1_id, None)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        # We should see the room (like normal)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

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

        # Get invited to the room again
        # self.helper.join(room_id, user1_id, tok=user1_tok)
        self._create_remote_invite_room_for_user(user1_id, None, invite_room_id=room_id)

        # We should see the room again after re-joining
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

    def test_reject_remote_invite(self) -> None:
        """Test that rejecting a remote invite comes down incremental sync"""

        user_id = self.register_user("user1", "pass")
        user_tok = self.login(user_id, "pass")

        # Create a remote room invite (out-of-band membership)
        room_id = "!room:remote.server"
        self._create_remote_invite_room_for_user(user_id, None, room_id)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [(EventTypes.Member, StateValues.ME)],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user_tok)
        # We should see the room (like normal)
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id},
            exact=True,
        )

        # Reject the remote room invite
        self.helper.leave(room_id, user_id, tok=user_tok)

        # Sync again after rejecting the invite
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user_tok)

        # The fix to add the leave event to incremental sync when rejecting a remote
        # invite relies on the new tables to work.
        if self.use_new_tables:
            # We should see the newly_left room
            self.assertIncludes(
                set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
                {room_id},
                exact=True,
            )
            # We should see the leave state for the room so clients don't end up with stuck
            # invites
            self.assertIncludes(
                {
                    (
                        state["type"],
                        state["state_key"],
                        state["content"].get("membership"),
                    )
                    for state in response_body["rooms"][room_id]["required_state"]
                },
                {(EventTypes.Member, user_id, Membership.LEAVE)},
                exact=True,
            )

    def test_ignored_user_invites_initial_sync(self) -> None:
        """
        Make sure we ignore invites if they are from one of the `m.ignored_user_list` on
        initial sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room that user1 is already in
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create a room that user2 is already in
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 is invited to room_id2
        self.helper.invite(room_id2, src=user2_id, targ=user1_id, tok=user2_tok)

        # Sync once before we ignore to make sure the rooms can show up
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # room_id2 shows up because we haven't ignored the user yet
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id1, room_id2},
            exact=True,
        )

        # User1 ignores user2
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/user/{user1_id}/account_data/{AccountDataTypes.IGNORED_USER_LIST}",
            content={"ignored_users": {user2_id: {}}},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Sync again (initial sync)
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # The invite for room_id2 should no longer show up because user2 is ignored
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id1},
            exact=True,
        )

    def test_ignored_user_invites_incremental_sync(self) -> None:
        """
        Make sure we ignore invites if they are from one of the `m.ignored_user_list` on
        incremental sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room that user1 is already in
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create a room that user2 is already in
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 ignores user2
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/user/{user1_id}/account_data/{AccountDataTypes.IGNORED_USER_LIST}",
            content={"ignored_users": {user2_id: {}}},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Initial sync
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                },
            }
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        # User1 only has membership in room_id1 at this point
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id1},
            exact=True,
        )

        # User1 is invited to room_id2 after the initial sync
        self.helper.invite(room_id2, src=user2_id, targ=user1_id, tok=user2_tok)

        # Sync again (incremental sync)
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        # The invite for room_id2 doesn't show up because user2 is ignored
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id1},
            exact=True,
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

        # Make the Sliding Sync request where the range includes *some* of the rooms
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
        self.assertIncludes(
            response_body["lists"].keys(),
            {"foo-list"},
        )
        # Make sure the list is sorted in the way we expect (we only sort when the range
        # doesn't include all of the room)
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

        # Make the Sliding Sync request where the range includes *all* of the rooms
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
        self.assertIncludes(
            response_body["lists"].keys(),
            {"foo-list"},
        )
        # Since the range includes all of the rooms, we don't sort the list
        self.assertEqual(
            len(response_body["lists"]["foo-list"]["ops"]),
            1,
            response_body["lists"]["foo-list"],
        )
        op = response_body["lists"]["foo-list"]["ops"][0]
        self.assertEqual(op["op"], "SYNC")
        self.assertEqual(op["range"], [0, 99])
        # Note that we don't sort the rooms when the range includes all of the rooms, so
        # we just assert that the rooms are included
        self.assertIncludes(
            set(op["room_ids"]), {room_id1, room_id2, room_id3}, exact=True
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

    def test_state_reset_room_comes_down_incremental_sync(self) -> None:
        """Test that a room that we were state reset out of comes down
        incremental sync"""

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            is_public=True,
            tok=user2_tok,
            extra_content={
                "name": "my super room",
            },
        )

        # Create an event for us to point back to for the state reset
        event_response = self.helper.send(room_id1, "test", tok=user2_tok)
        event_id = event_response["event_id"]

        self.helper.join(room_id1, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        # Request all state just to see what we get back when we are
                        # state reset out of the room
                        [StateValues.WILDCARD, StateValues.WILDCARD]
                    ],
                    "timeline_limit": 1,
                }
            }
        }

        # Make the Sliding Sync request
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        # Make sure we see room1
        self.assertIncludes(set(response_body["rooms"].keys()), {room_id1}, exact=True)
        self.assertEqual(response_body["rooms"][room_id1]["initial"], True)

        # Trigger a state reset
        join_rule_event, join_rule_context = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[event_id],
                type=EventTypes.JoinRules,
                state_key="",
                content={"join_rule": JoinRules.INVITE},
                sender=user2_id,
                room_id=room_id1,
                room_version=self.get_success(self.store.get_room_version_id(room_id1)),
            )
        )
        _, join_rule_event_pos, _ = self.get_success(
            self.persistence.persist_event(join_rule_event, join_rule_context)
        )

        # Ensure that the state reset worked and only user2 is in the room now
        users_in_room = self.get_success(self.store.get_users_in_room(room_id1))
        self.assertIncludes(set(users_in_room), {user2_id}, exact=True)

        state_map_at_reset = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Update the state after user1 was state reset out of the room
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {EventContentFields.ROOM_NAME: "my super duper room"},
            tok=user2_tok,
        )

        # Make another Sliding Sync request (incremental)
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Expect to see room1 because it is `newly_left` thanks to being state reset out
        # of it since the last time we synced. We need to let the client know that
        # something happened and that they are no longer in the room.
        self.assertIncludes(set(response_body["rooms"].keys()), {room_id1}, exact=True)
        # We set `initial=True` to indicate that the client should reset the state they
        # have about the room
        self.assertEqual(response_body["rooms"][room_id1]["initial"], True)
        # They shouldn't see anything past the state reset
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            # We should see all the state events in the room
            state_map_at_reset.values(),
            exact=True,
        )
        # The position where the state reset happened
        self.assertEqual(
            response_body["rooms"][room_id1]["bump_stamp"],
            join_rule_event_pos.stream,
            response_body["rooms"][room_id1],
        )

        # Other non-important things. We just want to check what these are so we know
        # what happens in a state reset scenario.
        #
        # Room name was set at the time of the state reset so we should still be able to
        # see it.
        self.assertEqual(response_body["rooms"][room_id1]["name"], "my super room")
        # Could be set but there is no avatar for this room
        self.assertIsNone(
            response_body["rooms"][room_id1].get("avatar"),
            response_body["rooms"][room_id1],
        )
        # Could be set but this room isn't marked as a DM
        self.assertIsNone(
            response_body["rooms"][room_id1].get("is_dm"),
            response_body["rooms"][room_id1],
        )
        # Empty timeline because we are not in the room at all (they are all being
        # filtered out)
        self.assertIsNone(
            response_body["rooms"][room_id1].get("timeline"),
            response_body["rooms"][room_id1],
        )
        # `limited` since we're not providing any timeline events but there are some in
        # the room.
        self.assertEqual(response_body["rooms"][room_id1]["limited"], True)
        # User is no longer in the room so they can't see this info
        self.assertIsNone(
            response_body["rooms"][room_id1].get("joined_count"),
            response_body["rooms"][room_id1],
        )
        self.assertIsNone(
            response_body["rooms"][room_id1].get("invited_count"),
            response_body["rooms"][room_id1],
        )

    def test_state_reset_previously_room_comes_down_incremental_sync_with_filters(
        self,
    ) -> None:
        """
        Test that a room that we were state reset out of should always be sent down
        regardless of the filters if it has been sent down the connection before.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a space room
        space_room_id = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE},
                "name": "my super space",
            },
        )

        # Create an event for us to point back to for the state reset
        event_response = self.helper.send(space_room_id, "test", tok=user2_tok)
        event_id = event_response["event_id"]

        self.helper.join(space_room_id, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        # Request all state just to see what we get back when we are
                        # state reset out of the room
                        [StateValues.WILDCARD, StateValues.WILDCARD]
                    ],
                    "timeline_limit": 1,
                    "filters": {
                        "room_types": [RoomTypes.SPACE],
                    },
                }
            }
        }

        # Make the Sliding Sync request
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        # Make sure we see room1
        self.assertIncludes(
            set(response_body["rooms"].keys()), {space_room_id}, exact=True
        )
        self.assertEqual(response_body["rooms"][space_room_id]["initial"], True)

        # Trigger a state reset
        join_rule_event, join_rule_context = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[event_id],
                type=EventTypes.JoinRules,
                state_key="",
                content={"join_rule": JoinRules.INVITE},
                sender=user2_id,
                room_id=space_room_id,
                room_version=self.get_success(
                    self.store.get_room_version_id(space_room_id)
                ),
            )
        )
        _, join_rule_event_pos, _ = self.get_success(
            self.persistence.persist_event(join_rule_event, join_rule_context)
        )

        # Ensure that the state reset worked and only user2 is in the room now
        users_in_room = self.get_success(self.store.get_users_in_room(space_room_id))
        self.assertIncludes(set(users_in_room), {user2_id}, exact=True)

        state_map_at_reset = self.get_success(
            self.storage_controllers.state.get_current_state(space_room_id)
        )

        # Update the state after user1 was state reset out of the room
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {EventContentFields.ROOM_NAME: "my super duper space"},
            tok=user2_tok,
        )

        # User2 also leaves the room so the server is no longer participating in the room
        # and we don't have access to current state
        self.helper.leave(space_room_id, user2_id, tok=user2_tok)

        # Make another Sliding Sync request (incremental)
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Expect to see room1 because it is `newly_left` thanks to being state reset out
        # of it since the last time we synced. We need to let the client know that
        # something happened and that they are no longer in the room.
        self.assertIncludes(
            set(response_body["rooms"].keys()), {space_room_id}, exact=True
        )
        # We set `initial=True` to indicate that the client should reset the state they
        # have about the room
        self.assertEqual(response_body["rooms"][space_room_id]["initial"], True)
        # They shouldn't see anything past the state reset
        self._assertRequiredStateIncludes(
            response_body["rooms"][space_room_id]["required_state"],
            # We should see all the state events in the room
            state_map_at_reset.values(),
            exact=True,
        )
        # The position where the state reset happened
        self.assertEqual(
            response_body["rooms"][space_room_id]["bump_stamp"],
            join_rule_event_pos.stream,
            response_body["rooms"][space_room_id],
        )

        # Other non-important things. We just want to check what these are so we know
        # what happens in a state reset scenario.
        #
        # Room name was set at the time of the state reset so we should still be able to
        # see it.
        self.assertEqual(
            response_body["rooms"][space_room_id]["name"], "my super space"
        )
        # Could be set but there is no avatar for this room
        self.assertIsNone(
            response_body["rooms"][space_room_id].get("avatar"),
            response_body["rooms"][space_room_id],
        )
        # Could be set but this room isn't marked as a DM
        self.assertIsNone(
            response_body["rooms"][space_room_id].get("is_dm"),
            response_body["rooms"][space_room_id],
        )
        # Empty timeline because we are not in the room at all (they are all being
        # filtered out)
        self.assertIsNone(
            response_body["rooms"][space_room_id].get("timeline"),
            response_body["rooms"][space_room_id],
        )
        # `limited` since we're not providing any timeline events but there are some in
        # the room.
        self.assertEqual(response_body["rooms"][space_room_id]["limited"], True)
        # User is no longer in the room so they can't see this info
        self.assertIsNone(
            response_body["rooms"][space_room_id].get("joined_count"),
            response_body["rooms"][space_room_id],
        )
        self.assertIsNone(
            response_body["rooms"][space_room_id].get("invited_count"),
            response_body["rooms"][space_room_id],
        )

    @parameterized.expand(
        [
            ("server_leaves_room", True),
            ("server_participating_in_room", False),
        ]
    )
    def test_state_reset_never_room_incremental_sync_with_filters(
        self, test_description: str, server_leaves_room: bool
    ) -> None:
        """
        Test that a room that we were state reset out of should be sent down if we can
        figure out the state or if it was sent down the connection before.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a space room
        space_room_id = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE},
                "name": "my super space",
            },
        )

        # Create another space room
        space_room_id2 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE},
            },
        )

        # Create an event for us to point back to for the state reset
        event_response = self.helper.send(space_room_id, "test", tok=user2_tok)
        event_id = event_response["event_id"]

        # User1 joins the rooms
        #
        self.helper.join(space_room_id, user1_id, tok=user1_tok)
        # Join space_room_id2 so that it is at the top of the list
        self.helper.join(space_room_id2, user1_id, tok=user1_tok)

        # Make a SS request for only the top room.
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [
                        # Request all state just to see what we get back when we are
                        # state reset out of the room
                        [StateValues.WILDCARD, StateValues.WILDCARD]
                    ],
                    "timeline_limit": 1,
                    "filters": {
                        "room_types": [RoomTypes.SPACE],
                    },
                }
            }
        }

        # Make the Sliding Sync request
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        # Make sure we only see space_room_id2
        self.assertIncludes(
            set(response_body["rooms"].keys()), {space_room_id2}, exact=True
        )
        self.assertEqual(response_body["rooms"][space_room_id2]["initial"], True)

        # Just create some activity in space_room_id2 so it appears when we incremental sync again
        self.helper.send(space_room_id2, "test", tok=user2_tok)

        # Trigger a state reset
        join_rule_event, join_rule_context = self.get_success(
            create_event(
                self.hs,
                prev_event_ids=[event_id],
                type=EventTypes.JoinRules,
                state_key="",
                content={"join_rule": JoinRules.INVITE},
                sender=user2_id,
                room_id=space_room_id,
                room_version=self.get_success(
                    self.store.get_room_version_id(space_room_id)
                ),
            )
        )
        _, join_rule_event_pos, _ = self.get_success(
            self.persistence.persist_event(join_rule_event, join_rule_context)
        )

        # Ensure that the state reset worked and only user2 is in the room now
        users_in_room = self.get_success(self.store.get_users_in_room(space_room_id))
        self.assertIncludes(set(users_in_room), {user2_id}, exact=True)

        # Update the state after user1 was state reset out of the room.
        # This will also bump it to the top of the list.
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {EventContentFields.ROOM_NAME: "my super duper space"},
            tok=user2_tok,
        )

        if server_leaves_room:
            # User2 also leaves the room so the server is no longer participating in the room
            # and we don't have access to current state
            self.helper.leave(space_room_id, user2_id, tok=user2_tok)

        # Make another Sliding Sync request (incremental)
        sync_body = {
            "lists": {
                "foo-list": {
                    # Expand the range to include all rooms
                    "ranges": [[0, 1]],
                    "required_state": [
                        # Request all state just to see what we get back when we are
                        # state reset out of the room
                        [StateValues.WILDCARD, StateValues.WILDCARD]
                    ],
                    "timeline_limit": 1,
                    "filters": {
                        "room_types": [RoomTypes.SPACE],
                    },
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        if self.use_new_tables:
            if server_leaves_room:
                # We still only expect to see space_room_id2 because even though we were state
                # reset out of space_room_id, it was never sent down the connection before so we
                # don't need to bother the client with it.
                self.assertIncludes(
                    set(response_body["rooms"].keys()), {space_room_id2}, exact=True
                )
            else:
                # Both rooms show up because we can figure out the state for the
                # `filters.room_types` if someone is still in the room (we look at the
                # current state because `room_type` never changes).
                self.assertIncludes(
                    set(response_body["rooms"].keys()),
                    {space_room_id, space_room_id2},
                    exact=True,
                )
        else:
            # Both rooms show up because we can actually take the time to figure out the
            # state for the `filters.room_types` in the fallback path (we look at
            # historical state for `LEAVE` membership).
            self.assertIncludes(
                set(response_body["rooms"].keys()),
                {space_room_id, space_room_id2},
                exact=True,
            )

    def test_exclude_rooms_from_sync(self) -> None:
        """Tests that sliding sync honours the `exclude_rooms_from_sync` config
        option.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id_to_exclude = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )
        room_id_to_include = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )

        # We cheekily modify the stored config here, as we can't add it to the
        # raw config since we don't know the room ID before we start up.
        self.hs.get_sliding_sync_handler().rooms_to_exclude_globally.append(
            room_id_to_exclude
        )
        self.hs.get_sliding_sync_handler().room_lists.rooms_to_exclude_globally.append(
            room_id_to_exclude
        )

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 0,
                },
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Make sure response only contains room_id_to_include
        self.assertIncludes(
            set(response_body["rooms"].keys()),
            {room_id_to_include},
            exact=True,
        )

        # Test that the excluded room is not in the list ops
        # Make sure the list is sorted in the way we expect
        self.assertIncludes(
            set(response_body["lists"]["foo-list"]["ops"][0]["room_ids"]),
            {room_id_to_include},
            exact=True,
        )
