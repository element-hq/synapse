#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C
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
import asyncio
from asyncio import Future
from http import HTTPStatus
from typing import Any, Awaitable, Dict, List, Optional, Tuple, TypeVar, cast
from unittest.mock import Mock

import attr
from parameterized import parameterized

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.api.errors import SynapseError
from synapse.config.auto_accept_invites import AutoAcceptInvitesConfig
from synapse.events.auto_accept_invites import InviteAutoAccepter
from synapse.federation.federation_base import event_from_pdu_json
from synapse.handlers.sync import JoinedSyncResult, SyncRequestKey, SyncVersion
from synapse.module_api import ModuleApi
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import StreamToken, create_requester
from synapse.util import Clock

from tests.handlers.test_sync import generate_sync_config
from tests.unittest import (
    FederatingHomeserverTestCase,
    HomeserverTestCase,
    TestCase,
    override_config,
)


class AutoAcceptInvitesTestCase(FederatingHomeserverTestCase):
    """
    Integration test cases for auto-accepting invites.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        self.handler = hs.get_federation_handler()
        self.store = hs.get_datastores().main
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sync_handler = self.hs.get_sync_handler()
        self.module_api = hs.get_module_api()

    @parameterized.expand(
        [
            [False],
            [True],
        ]
    )
    @override_config(
        {
            "auto_accept_invites": {
                "enabled": True,
            },
        }
    )
    def test_auto_accept_invites(self, direct_room: bool) -> None:
        """Test that a user automatically joins a room when invited, if the
        module is enabled.
        """
        # A local user who sends an invite
        inviting_user_id = self.register_user("inviter", "pass")
        inviting_user_tok = self.login("inviter", "pass")

        # A local user who receives an invite
        invited_user_id = self.register_user("invitee", "pass")
        self.login("invitee", "pass")

        # Create a room and send an invite to the other user
        room_id = self.helper.create_room_as(
            inviting_user_id,
            is_public=False,
            tok=inviting_user_tok,
        )

        self.helper.invite(
            room_id,
            inviting_user_id,
            invited_user_id,
            tok=inviting_user_tok,
            extra_data={"is_direct": direct_room},
        )

        # Check that the invite receiving user has automatically joined the room when syncing
        join_updates, _ = sync_join(self, invited_user_id)
        self.assertEqual(len(join_updates), 1)

        join_update: JoinedSyncResult = join_updates[0]
        self.assertEqual(join_update.room_id, room_id)

    @override_config(
        {
            "auto_accept_invites": {
                "enabled": False,
            },
        }
    )
    def test_module_not_enabled(self) -> None:
        """Test that a user does not automatically join a room when invited,
        if the module is not enabled.
        """
        # A local user who sends an invite
        inviting_user_id = self.register_user("inviter", "pass")
        inviting_user_tok = self.login("inviter", "pass")

        # A local user who receives an invite
        invited_user_id = self.register_user("invitee", "pass")
        self.login("invitee", "pass")

        # Create a room and send an invite to the other user
        room_id = self.helper.create_room_as(
            inviting_user_id, is_public=False, tok=inviting_user_tok
        )

        self.helper.invite(
            room_id,
            inviting_user_id,
            invited_user_id,
            tok=inviting_user_tok,
        )

        # Check that the invite receiving user has not automatically joined the room when syncing
        join_updates, _ = sync_join(self, invited_user_id)
        self.assertEqual(len(join_updates), 0)

    @override_config(
        {
            "auto_accept_invites": {
                "enabled": True,
            },
        }
    )
    def test_invite_from_remote_user(self) -> None:
        """Test that an invite from a remote user results in the invited user
        automatically joining the room.
        """
        # A remote user who sends the invite
        remote_server = "otherserver"
        remote_user = "@otheruser:" + remote_server

        # A local user who creates the room
        creator_user_id = self.register_user("creator", "pass")
        creator_user_tok = self.login("creator", "pass")

        # A local user who receives an invite
        invited_user_id = self.register_user("invitee", "pass")
        self.login("invitee", "pass")

        room_id = self.helper.create_room_as(
            room_creator=creator_user_id, tok=creator_user_tok
        )
        room_version = self.get_success(self.store.get_room_version(room_id))

        invite_event = event_from_pdu_json(
            {
                "type": EventTypes.Member,
                "content": {"membership": "invite"},
                "room_id": room_id,
                "sender": remote_user,
                "state_key": invited_user_id,
                "depth": 32,
                "prev_events": [],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            room_version,
        )
        self.get_success(
            self.handler.on_invite_request(
                remote_server,
                invite_event,
                invite_event.room_version,
            )
        )

        # Check that the invite receiving user has automatically joined the room when syncing
        join_updates, _ = sync_join(self, invited_user_id)
        self.assertEqual(len(join_updates), 1)

        join_update: JoinedSyncResult = join_updates[0]
        self.assertEqual(join_update.room_id, room_id)

    @parameterized.expand(
        [
            [False, False],
            [True, True],
        ]
    )
    @override_config(
        {
            "auto_accept_invites": {
                "enabled": True,
                "only_for_direct_messages": True,
            },
        }
    )
    def test_accept_invite_direct_message(
        self,
        direct_room: bool,
        expect_auto_join: bool,
    ) -> None:
        """Tests that, if the module is configured to only accept DM invites, invites to DM rooms are still
        automatically accepted. Otherwise they are rejected.
        """
        # A local user who sends an invite
        inviting_user_id = self.register_user("inviter", "pass")
        inviting_user_tok = self.login("inviter", "pass")

        # A local user who receives an invite
        invited_user_id = self.register_user("invitee", "pass")
        self.login("invitee", "pass")

        # Create a room and send an invite to the other user
        room_id = self.helper.create_room_as(
            inviting_user_id,
            is_public=False,
            tok=inviting_user_tok,
        )

        self.helper.invite(
            room_id,
            inviting_user_id,
            invited_user_id,
            tok=inviting_user_tok,
            extra_data={"is_direct": direct_room},
        )

        if expect_auto_join:
            # Check that the invite receiving user has automatically joined the room when syncing
            join_updates, _ = sync_join(self, invited_user_id)
            self.assertEqual(len(join_updates), 1)

            join_update: JoinedSyncResult = join_updates[0]
            self.assertEqual(join_update.room_id, room_id)
        else:
            # Check that the invite receiving user has not automatically joined the room when syncing
            join_updates, _ = sync_join(self, invited_user_id)
            self.assertEqual(len(join_updates), 0)

    @parameterized.expand(
        [
            [False, True],
            [True, False],
        ]
    )
    @override_config(
        {
            "auto_accept_invites": {
                "enabled": True,
                "only_from_local_users": True,
            },
        }
    )
    def test_accept_invite_local_user(
        self, remote_inviter: bool, expect_auto_join: bool
    ) -> None:
        """Tests that, if the module is configured to only accept invites from local users, invites
        from local users are still automatically accepted. Otherwise they are rejected.
        """
        # A local user who sends an invite
        creator_user_id = self.register_user("inviter", "pass")
        creator_user_tok = self.login("inviter", "pass")

        # A local user who receives an invite
        invited_user_id = self.register_user("invitee", "pass")
        self.login("invitee", "pass")

        # Create a room and send an invite to the other user
        room_id = self.helper.create_room_as(
            creator_user_id, is_public=False, tok=creator_user_tok
        )

        if remote_inviter:
            room_version = self.get_success(self.store.get_room_version(room_id))

            # A remote user who sends the invite
            remote_server = "otherserver"
            remote_user = "@otheruser:" + remote_server

            invite_event = event_from_pdu_json(
                {
                    "type": EventTypes.Member,
                    "content": {"membership": "invite"},
                    "room_id": room_id,
                    "sender": remote_user,
                    "state_key": invited_user_id,
                    "depth": 32,
                    "prev_events": [],
                    "auth_events": [],
                    "origin_server_ts": self.clock.time_msec(),
                },
                room_version,
            )
            self.get_success(
                self.handler.on_invite_request(
                    remote_server,
                    invite_event,
                    invite_event.room_version,
                )
            )
        else:
            self.helper.invite(
                room_id,
                creator_user_id,
                invited_user_id,
                tok=creator_user_tok,
            )

        if expect_auto_join:
            # Check that the invite receiving user has automatically joined the room when syncing
            join_updates, _ = sync_join(self, invited_user_id)
            self.assertEqual(len(join_updates), 1)

            join_update: JoinedSyncResult = join_updates[0]
            self.assertEqual(join_update.room_id, room_id)
        else:
            # Check that the invite receiving user has not automatically joined the room when syncing
            join_updates, _ = sync_join(self, invited_user_id)
            self.assertEqual(len(join_updates), 0)


_request_key = 0


def generate_request_key() -> SyncRequestKey:
    global _request_key
    _request_key += 1
    return ("request_key", _request_key)


def sync_join(
    testcase: HomeserverTestCase,
    user_id: str,
    since_token: Optional[StreamToken] = None,
) -> Tuple[List[JoinedSyncResult], StreamToken]:
    """Perform a sync request for the given user and return the user join updates
    they've received, as well as the next_batch token.

    This method assumes testcase.sync_handler points to the homeserver's sync handler.

    Args:
        testcase: The testcase that is currently being run.
        user_id: The ID of the user to generate a sync response for.
        since_token: An optional token to indicate from at what point to sync from.

    Returns:
        A tuple containing a list of join updates, and the sync response's
        next_batch token.
    """
    requester = create_requester(user_id)
    sync_config = generate_sync_config(requester.user.to_string())
    sync_result = testcase.get_success(
        testcase.hs.get_sync_handler().wait_for_sync_for_user(
            requester,
            sync_config,
            SyncVersion.SYNC_V2,
            generate_request_key(),
            since_token,
        )
    )

    return sync_result.joined, sync_result.next_batch


class InviteAutoAccepterInternalTestCase(TestCase):
    """
    Test cases which exercise the internals of the InviteAutoAccepter.
    """

    def setUp(self) -> None:
        self.module = create_module()
        self.user_id = "@peter:test"
        self.invitee = "@lesley:test"
        self.remote_invitee = "@thomas:remote"

        # We know our module API is a mock, but mypy doesn't.
        self.mocked_update_membership: Mock = self.module._api.update_room_membership  # type: ignore[assignment]

    async def test_accept_invite_with_failures(self) -> None:
        """Tests that receiving an invite for a local user makes the module attempt to
        make the invitee join the room. This test verifies that it works if the call to
        update membership returns exceptions before successfully completing and returning an event.
        """
        invite = MockEvent(
            sender="@inviter:test",
            state_key="@invitee:test",
            type="m.room.member",
            content={"membership": "invite"},
        )

        join_event = MockEvent(
            sender="someone",
            state_key="someone",
            type="m.room.member",
            content={"membership": "join"},
        )
        # the first two calls raise an exception while the third call is successful
        self.mocked_update_membership.side_effect = [
            SynapseError(HTTPStatus.FORBIDDEN, "Forbidden"),
            SynapseError(HTTPStatus.FORBIDDEN, "Forbidden"),
            make_awaitable(join_event),
        ]

        # Stop mypy from complaining that we give on_new_event a MockEvent rather than an
        # EventBase.
        await self.module.on_new_event(event=invite)  # type: ignore[arg-type]

        await self.retry_assertions(
            self.mocked_update_membership,
            3,
            sender=invite.state_key,
            target=invite.state_key,
            room_id=invite.room_id,
            new_membership="join",
        )

    async def test_accept_invite_failures(self) -> None:
        """Tests that receiving an invite for a local user makes the module attempt to
        make the invitee join the room. This test verifies that if the update_membership call
        fails consistently, _retry_make_join will break the loop after the set number of retries and
        execution will continue.
        """
        invite = MockEvent(
            sender=self.user_id,
            state_key=self.invitee,
            type="m.room.member",
            content={"membership": "invite"},
        )
        self.mocked_update_membership.side_effect = SynapseError(
            HTTPStatus.FORBIDDEN, "Forbidden"
        )

        # Stop mypy from complaining that we give on_new_event a MockEvent rather than an
        # EventBase.
        await self.module.on_new_event(event=invite)  # type: ignore[arg-type]

        await self.retry_assertions(
            self.mocked_update_membership,
            5,
            sender=invite.state_key,
            target=invite.state_key,
            room_id=invite.room_id,
            new_membership="join",
        )

    async def test_not_state(self) -> None:
        """Tests that receiving an invite that's not a state event does nothing."""
        invite = MockEvent(
            sender=self.user_id, type="m.room.member", content={"membership": "invite"}
        )

        # Stop mypy from complaining that we give on_new_event a MockEvent rather than an
        # EventBase.
        await self.module.on_new_event(event=invite)  # type: ignore[arg-type]

        self.mocked_update_membership.assert_not_called()

    async def test_not_invite(self) -> None:
        """Tests that receiving a membership update that's not an invite does nothing."""
        invite = MockEvent(
            sender=self.user_id,
            state_key=self.user_id,
            type="m.room.member",
            content={"membership": "join"},
        )

        # Stop mypy from complaining that we give on_new_event a MockEvent rather than an
        # EventBase.
        await self.module.on_new_event(event=invite)  # type: ignore[arg-type]

        self.mocked_update_membership.assert_not_called()

    async def test_not_membership(self) -> None:
        """Tests that receiving a state event that's not a membership update does
        nothing.
        """
        invite = MockEvent(
            sender=self.user_id,
            state_key=self.user_id,
            type="org.matrix.test",
            content={"foo": "bar"},
        )

        # Stop mypy from complaining that we give on_new_event a MockEvent rather than an
        # EventBase.
        await self.module.on_new_event(event=invite)  # type: ignore[arg-type]

        self.mocked_update_membership.assert_not_called()

    def test_config_parse(self) -> None:
        """Tests that a correct configuration parses."""
        config = {
            "auto_accept_invites": {
                "enabled": True,
                "only_for_direct_messages": True,
                "only_from_local_users": True,
            }
        }
        parsed_config = AutoAcceptInvitesConfig()
        parsed_config.read_config(config)

        self.assertTrue(parsed_config.enabled)
        self.assertTrue(parsed_config.accept_invites_only_for_direct_messages)
        self.assertTrue(parsed_config.accept_invites_only_from_local_users)

    def test_runs_on_only_one_worker(self) -> None:
        """
        Tests that the module only runs on the specified worker.
        """
        # By default, we run on the main process...
        main_module = create_module(
            config_override={"auto_accept_invites": {"enabled": True}}, worker_name=None
        )
        cast(
            Mock, main_module._api.register_third_party_rules_callbacks
        ).assert_called_once()

        # ...and not on other workers (like synchrotrons)...
        sync_module = create_module(worker_name="synchrotron42")
        cast(
            Mock, sync_module._api.register_third_party_rules_callbacks
        ).assert_not_called()

        # ...unless we configured them to be the designated worker.
        specified_module = create_module(
            config_override={
                "auto_accept_invites": {
                    "enabled": True,
                    "worker_to_run_on": "account_data1",
                }
            },
            worker_name="account_data1",
        )
        cast(
            Mock, specified_module._api.register_third_party_rules_callbacks
        ).assert_called_once()

    async def retry_assertions(
        self, mock: Mock, call_count: int, **kwargs: Any
    ) -> None:
        """
        This is a hacky way to ensure that the assertions are not called before the other coroutine
        has a chance to call `update_room_membership`. It catches the exception caused by a failure,
        and sleeps the thread before retrying, up until 5 tries.

        Args:
            call_count: the number of times the mock should have been called
            mock: the mocked function we want to assert on
            kwargs: keyword arguments to assert that the mock was called with
        """

        i = 0
        while i < 5:
            try:
                # Check that the mocked method is called the expected amount of times and with the right
                # arguments to attempt to make the user join the room.
                mock.assert_called_with(**kwargs)
                self.assertEqual(call_count, mock.call_count)
                break
            except AssertionError as e:
                i += 1
                if i == 5:
                    # we've used up the tries, force the test to fail as we've already caught the exception
                    self.fail(e)
                await asyncio.sleep(1)


@attr.s(auto_attribs=True)
class MockEvent:
    """Mocks an event. Only exposes properties the module uses."""

    sender: str
    type: str
    content: Dict[str, Any]
    room_id: str = "!someroom"
    state_key: Optional[str] = None

    def is_state(self) -> bool:
        """Checks if the event is a state event by checking if it has a state key."""
        return self.state_key is not None

    @property
    def membership(self) -> str:
        """Extracts the membership from the event. Should only be called on an event
        that's a membership event, and will raise a KeyError otherwise.
        """
        membership: str = self.content["membership"]
        return membership


T = TypeVar("T")
TV = TypeVar("TV")


async def make_awaitable(value: T) -> T:
    return value


def make_multiple_awaitable(result: TV) -> Awaitable[TV]:
    """
    Makes an awaitable, suitable for mocking an `async` function.
    This uses Futures as they can be awaited multiple times so can be returned
    to multiple callers.
    """
    future: Future[TV] = Future()
    future.set_result(result)
    return future


def create_module(
    config_override: Optional[Dict[str, Any]] = None, worker_name: Optional[str] = None
) -> InviteAutoAccepter:
    # Create a mock based on the ModuleApi spec, but override some mocked functions
    # because some capabilities are needed for running the tests.
    module_api = Mock(spec=ModuleApi)
    module_api.is_mine.side_effect = lambda a: a.split(":")[1] == "test"
    module_api.worker_name = worker_name
    module_api.sleep.return_value = make_multiple_awaitable(None)

    if config_override is None:
        config_override = {}

    config = AutoAcceptInvitesConfig()
    config.read_config(config_override)

    return InviteAutoAccepter(config, module_api)
