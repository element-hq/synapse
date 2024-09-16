#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock

from twisted.internet import defer
from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EduTypes, EventTypes
from synapse.api.errors import NotFoundError
from synapse.events import EventBase
from synapse.federation.units import Transaction
from synapse.handlers.device import DeviceHandler
from synapse.handlers.presence import UserPresenceState
from synapse.handlers.push_rules import InvalidRuleException
from synapse.module_api import ModuleApi
from synapse.rest import admin
from synapse.rest.client import login, notifications, presence, profile, room
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID, create_requester
from synapse.util import Clock

from tests.events.test_presence_router import send_presence_update, sync_presence
from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.test_utils.event_injection import inject_member_event
from tests.unittest import HomeserverTestCase, override_config


class BaseModuleApiTestCase(HomeserverTestCase):
    """Common properties of the two test case classes."""

    module_api: ModuleApi

    # These are all written by _test_sending_local_online_presence_to_local_user.
    presence_receiver_id: str
    presence_receiver_tok: str
    presence_sender_id: str
    presence_sender_tok: str


class ModuleApiTestCase(BaseModuleApiTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        presence.register_servlets,
        profile.register_servlets,
        notifications.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.module_api = hs.get_module_api()
        self.event_creation_handler = hs.get_event_creation_handler()
        self.sync_handler = hs.get_sync_handler()
        self.auth_handler = hs.get_auth_handler()

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # Mock out the calls over federation.
        self.fed_transport_client = Mock(spec=["send_transaction"])
        self.fed_transport_client.send_transaction = AsyncMock(return_value={})

        return self.setup_test_homeserver(
            federation_transport_client=self.fed_transport_client,
        )

    def test_can_register_user(self) -> None:
        """Tests that an external module can register a user"""
        # Register a new user
        user_id, access_token = self.get_success(
            self.module_api.register(
                "bob", displayname="Bobberino", emails=["bob@bobinator.bob"]
            )
        )

        # Check that the new user exists with all provided attributes
        self.assertEqual(user_id, "@bob:test")
        self.assertTrue(access_token)
        self.assertTrue(self.get_success(self.store.get_user_by_id(user_id)))

        # Check that the email was assigned
        emails = self.get_success(self.store.user_get_threepids(user_id))
        self.assertEqual(len(emails), 1)

        email = emails[0]
        self.assertEqual(email.medium, "email")
        self.assertEqual(email.address, "bob@bobinator.bob")

        # Should these be 0?
        self.assertEqual(email.validated_at, 0)
        self.assertEqual(email.added_at, 0)

        # Check that the displayname was assigned
        displayname = self.get_success(
            self.store.get_profile_displayname(UserID.from_string("@bob:test"))
        )
        self.assertEqual(displayname, "Bobberino")

    def test_can_register_admin_user(self) -> None:
        user_id = self.register_user(
            "bob_module_admin", "1234", displayname="Bobberino Admin", admin=True
        )

        found_user = self.get_success(self.module_api.get_userinfo_by_id(user_id))
        assert found_user is not None
        self.assertEqual(found_user.user_id.to_string(), user_id)
        self.assertIdentical(found_user.is_admin, True)

    def test_can_set_admin(self) -> None:
        user_id = self.register_user(
            "alice_wants_admin",
            "1234",
            displayname="Alice Powerhungry",
            admin=False,
        )

        self.get_success(self.module_api.set_user_admin(user_id, True))
        found_user = self.get_success(self.module_api.get_userinfo_by_id(user_id))
        assert found_user is not None
        self.assertEqual(found_user.user_id.to_string(), user_id)
        self.assertIdentical(found_user.is_admin, True)

    def test_can_set_displayname(self) -> None:
        localpart = "alice_wants_a_new_displayname"
        user_id = self.register_user(
            localpart, "1234", displayname="Alice", admin=False
        )
        found_userinfo = self.get_success(self.module_api.get_userinfo_by_id(user_id))
        assert found_userinfo is not None
        self.get_success(
            self.module_api.set_displayname(
                found_userinfo.user_id, "Bob", deactivation=False
            )
        )
        found_profile = self.get_success(
            self.module_api.get_profile_for_user(localpart)
        )

        self.assertEqual(found_profile.display_name, "Bob")

    def test_get_userinfo_by_id(self) -> None:
        user_id = self.register_user("alice", "1234")
        found_user = self.get_success(self.module_api.get_userinfo_by_id(user_id))
        assert found_user is not None
        self.assertEqual(found_user.user_id.to_string(), user_id)
        self.assertIdentical(found_user.is_admin, False)

    def test_get_userinfo_by_id__no_user_found(self) -> None:
        found_user = self.get_success(self.module_api.get_userinfo_by_id("@alice:test"))
        self.assertIsNone(found_user)

    def test_get_user_ip_and_agents(self) -> None:
        user_id = self.register_user("test_get_user_ip_and_agents_user", "1234")

        # Initially, we should have no ip/agent for our user.
        info = self.get_success(self.module_api.get_user_ip_and_agents(user_id))
        self.assertEqual(info, [])

        # Insert a first ip, agent. We should be able to retrieve it.
        self.get_success(
            self.store.insert_client_ip(
                user_id, "access_token", "ip_1", "user_agent_1", "device_1", None
            )
        )
        info = self.get_success(self.module_api.get_user_ip_and_agents(user_id))

        self.assertEqual(len(info), 1)
        last_seen_1 = info[0].last_seen

        # Insert a second ip, agent at a later date. We should be able to retrieve it.
        last_seen_2 = last_seen_1 + 10000
        self.get_success(
            self.store.insert_client_ip(
                user_id, "access_token", "ip_2", "user_agent_2", "device_2", last_seen_2
            )
        )
        info = self.get_success(self.module_api.get_user_ip_and_agents(user_id))

        self.assertEqual(len(info), 2)
        ip_1_seen = False
        ip_2_seen = False

        for i in info:
            if i.ip == "ip_1":
                ip_1_seen = True
                self.assertEqual(i.user_agent, "user_agent_1")
                self.assertEqual(i.last_seen, last_seen_1)
            elif i.ip == "ip_2":
                ip_2_seen = True
                self.assertEqual(i.user_agent, "user_agent_2")
                self.assertEqual(i.last_seen, last_seen_2)
        self.assertTrue(ip_1_seen)
        self.assertTrue(ip_2_seen)

        # If we fetch from a midpoint between last_seen_1 and last_seen_2,
        # we should only find the second ip, agent.
        info = self.get_success(
            self.module_api.get_user_ip_and_agents(
                user_id, (last_seen_1 + last_seen_2) // 2
            )
        )
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0].ip, "ip_2")
        self.assertEqual(info[0].user_agent, "user_agent_2")
        self.assertEqual(info[0].last_seen, last_seen_2)

        # If we fetch from a point later than last_seen_2, we shouldn't
        # find anything.
        info = self.get_success(
            self.module_api.get_user_ip_and_agents(user_id, last_seen_2 + 10000)
        )
        self.assertEqual(info, [])

    def test_get_user_ip_and_agents__no_user_found(self) -> None:
        info = self.get_success(
            self.module_api.get_user_ip_and_agents(
                "@test_get_user_ip_and_agents_user_nonexistent:example.com"
            )
        )
        self.assertEqual(info, [])

    def test_sending_events_into_room(self) -> None:
        """Tests that a module can send events into a room"""
        # Mock out create_and_send_nonmember_event to check whether events are being sent
        self.event_creation_handler.create_and_send_nonmember_event = Mock(  # type: ignore[method-assign]
            spec=[],
            side_effect=self.event_creation_handler.create_and_send_nonmember_event,
        )

        # Create a user and room to play with
        user_id = self.register_user("summer", "monkey")
        tok = self.login("summer", "monkey")
        room_id = self.helper.create_room_as(user_id, tok=tok)

        # Create and send a non-state event
        content: JsonDict = {"body": "I am a puppet", "msgtype": "m.text"}
        event_dict = {
            "room_id": room_id,
            "type": "m.room.message",
            "content": content,
            "sender": user_id,
        }
        event: EventBase = self.get_success(
            self.module_api.create_and_send_event_into_room(event_dict)
        )
        self.assertEqual(event.sender, user_id)
        self.assertEqual(event.type, "m.room.message")
        self.assertEqual(event.room_id, room_id)
        self.assertFalse(hasattr(event, "state_key"))
        self.assertDictEqual(event.content, content)

        expected_requester = create_requester(
            user_id, authenticated_entity=self.hs.hostname
        )

        # Check that the event was sent
        self.event_creation_handler.create_and_send_nonmember_event.assert_called_with(
            expected_requester,
            event_dict,
            ratelimit=False,
            ignore_shadow_ban=True,
        )

        # Create and send a state event
        content = {
            "events_default": 0,
            "users": {user_id: 100},
            "state_default": 50,
            "users_default": 0,
            "events": {"test.event.type": 25},
        }
        event_dict = {
            "room_id": room_id,
            "type": "m.room.power_levels",
            "content": content,
            "sender": user_id,
            "state_key": "",
        }
        event = self.get_success(
            self.module_api.create_and_send_event_into_room(event_dict)
        )
        self.assertEqual(event.sender, user_id)
        self.assertEqual(event.type, "m.room.power_levels")
        self.assertEqual(event.room_id, room_id)
        self.assertEqual(event.state_key, "")
        self.assertDictEqual(event.content, content)

        # Check that the event was sent
        self.event_creation_handler.create_and_send_nonmember_event.assert_called_with(
            expected_requester,
            {
                "type": "m.room.power_levels",
                "content": content,
                "room_id": room_id,
                "sender": user_id,
                "state_key": "",
            },
            ratelimit=False,
            ignore_shadow_ban=True,
        )

        # Check that we can't send membership events
        content = {
            "membership": "leave",
        }
        event_dict = {
            "room_id": room_id,
            "type": "m.room.member",
            "content": content,
            "sender": user_id,
            "state_key": user_id,
        }
        self.get_failure(
            self.module_api.create_and_send_event_into_room(event_dict), Exception
        )

    def test_public_rooms(self) -> None:
        """Tests that a room can be added and removed from the public rooms list,
        as well as have its public rooms directory state queried.
        """
        # Create a user and room to play with
        user_id = self.register_user("kermit", "monkey")
        tok = self.login("kermit", "monkey")
        room_id = self.helper.create_room_as(user_id, tok=tok, is_public=False)

        # The room should not currently be in the public rooms directory
        is_in_public_rooms = self.get_success(
            self.module_api.public_room_list_manager.room_is_in_public_room_list(
                room_id
            )
        )
        self.assertFalse(is_in_public_rooms)

        # Let's try adding it to the public rooms directory
        self.get_success(
            self.module_api.public_room_list_manager.add_room_to_public_room_list(
                room_id
            )
        )

        # And checking whether it's in there...
        is_in_public_rooms = self.get_success(
            self.module_api.public_room_list_manager.room_is_in_public_room_list(
                room_id
            )
        )
        self.assertTrue(is_in_public_rooms)

        # Let's remove it again
        self.get_success(
            self.module_api.public_room_list_manager.remove_room_from_public_room_list(
                room_id
            )
        )

        # Should be gone
        is_in_public_rooms = self.get_success(
            self.module_api.public_room_list_manager.room_is_in_public_room_list(
                room_id
            )
        )
        self.assertFalse(is_in_public_rooms)

    def test_send_local_online_presence_to(self) -> None:
        # Test sending local online presence to users from the main process
        _test_sending_local_online_presence_to_local_user(self, test_with_workers=False)

    # Enable federation sending on the main process.
    @override_config({"federation_sender_instances": None})
    def test_send_local_online_presence_to_federation(self) -> None:
        """Tests that send_local_presence_to_users sends local online presence to remote users."""
        # Create a user who will send presence updates
        self.presence_sender_id = self.register_user("presence_sender1", "monkey")
        self.presence_sender_tok = self.login("presence_sender1", "monkey")

        # And a room they're a part of
        room_id = self.helper.create_room_as(
            self.presence_sender_id,
            tok=self.presence_sender_tok,
        )

        # Mark them as online
        send_presence_update(
            self,
            self.presence_sender_id,
            self.presence_sender_tok,
            "online",
            "I'm online!",
        )

        # Make up a remote user to send presence to
        remote_user_id = "@far_away_person:island"

        # Create a join membership event for the remote user into the room.
        # This allows presence information to flow from one user to the other.
        self.get_success(
            inject_member_event(
                self.hs,
                room_id,
                sender=remote_user_id,
                target=remote_user_id,
                membership="join",
            )
        )

        # The remote user would have received the existing room members' presence
        # when they joined the room.
        #
        # Thus we reset the mock, and try sending online local user
        # presence again
        self.fed_transport_client.send_transaction.reset_mock()

        # Broadcast local user online presence
        self.get_success(
            self.module_api.send_local_online_presence_to([remote_user_id])
        )

        # We don't always send out federation immediately, so we advance the clock.
        self.reactor.advance(1000)

        # Check that a presence update was sent as part of a federation transaction
        found_update = False
        calls = self.fed_transport_client.send_transaction.call_args_list
        for call in calls:
            call_args = call[0]
            federation_transaction: Transaction = call_args[0]

            # Get the sent EDUs in this transaction
            edus = federation_transaction.get_dict()["edus"]

            for edu in edus:
                # Make sure we're only checking presence-type EDUs
                if edu["edu_type"] != EduTypes.PRESENCE:
                    continue

                # EDUs can contain multiple presence updates
                for presence_update in edu["content"]["push"]:
                    if presence_update["user_id"] == self.presence_sender_id:
                        found_update = True

        self.assertTrue(found_update)

    def test_update_membership(self) -> None:
        """Tests that the module API can update the membership of a user in a room."""
        peter = self.register_user("peter", "hackme")
        lesley = self.register_user("lesley", "hackme")
        tok = self.login("peter", "hackme")
        lesley_tok = self.login("lesley", "hackme")

        # Make peter create a public room.
        room_id = self.helper.create_room_as(
            room_creator=peter, is_public=True, tok=tok
        )

        # Set a profile for lesley.
        channel = self.make_request(
            method="PUT",
            path="/_matrix/client/r0/profile/%s/displayname" % lesley,
            content={"displayname": "Lesley May"},
            access_token=lesley_tok,
        )

        self.assertEqual(channel.code, 200, channel.result)

        channel = self.make_request(
            method="PUT",
            path="/_matrix/client/r0/profile/%s/avatar_url" % lesley,
            content={"avatar_url": "some_url"},
            access_token=lesley_tok,
        )

        self.assertEqual(channel.code, 200, channel.result)

        # Make Peter invite Lesley to the room.
        self.get_success(
            defer.ensureDeferred(
                self.module_api.update_room_membership(peter, lesley, room_id, "invite")
            )
        )

        res = self.helper.get_state(
            room_id=room_id,
            event_type="m.room.member",
            state_key=lesley,
            tok=tok,
        )

        # Check the membership is correct.
        self.assertEqual(res["membership"], "invite")

        # Also check that the profile was correctly filled out, and that it's not
        # Peter's.
        self.assertEqual(res["displayname"], "Lesley May")
        self.assertEqual(res["avatar_url"], "some_url")

        # Make lesley join it.
        self.get_success(
            defer.ensureDeferred(
                self.module_api.update_room_membership(lesley, lesley, room_id, "join")
            )
        )

        # Check that the membership of lesley in the room is "join".
        res = self.helper.get_state(
            room_id=room_id,
            event_type="m.room.member",
            state_key=lesley,
            tok=tok,
        )

        self.assertEqual(res["membership"], "join")

        # Also check that the profile was correctly filled out.
        self.assertEqual(res["displayname"], "Lesley May")
        self.assertEqual(res["avatar_url"], "some_url")

        # Make peter kick lesley from the room.
        self.get_success(
            defer.ensureDeferred(
                self.module_api.update_room_membership(peter, lesley, room_id, "leave")
            )
        )

        # Check that the membership of lesley in the room is "leave".
        res = self.helper.get_state(
            room_id=room_id,
            event_type="m.room.member",
            state_key=lesley,
            tok=tok,
        )

        self.assertEqual(res["membership"], "leave")

        # Try to send a membership update from a non-local user and check that it fails.
        d = defer.ensureDeferred(
            self.module_api.update_room_membership(
                "@nicolas:otherserver.com",
                lesley,
                room_id,
                "invite",
            )
        )

        self.get_failure(d, RuntimeError)

        # Check that inviting a user that doesn't have a profile falls back to using a
        # default (localpart + no avatar) profile.
        simone = "@simone:" + self.hs.config.server.server_name
        self.get_success(
            defer.ensureDeferred(
                self.module_api.update_room_membership(peter, simone, room_id, "invite")
            )
        )

        res = self.helper.get_state(
            room_id=room_id,
            event_type="m.room.member",
            state_key=simone,
            tok=tok,
        )

        self.assertEqual(res["membership"], "invite")
        self.assertEqual(res["displayname"], "simone")
        self.assertIsNone(res["avatar_url"])

    def test_update_room_membership_remote_join(self) -> None:
        """Test that the module API can join a remote room."""
        # Necessary to fake a remote join.
        fake_stream_id = 1
        mocked_remote_join = AsyncMock(return_value=("fake-event-id", fake_stream_id))
        self.hs.get_room_member_handler()._remote_join = mocked_remote_join  # type: ignore[method-assign]
        fake_remote_host = f"{self.module_api.server_name}-remote"

        # Given that the join is to be faked, we expect the relevant join event not to
        # be persisted and the module API method to raise that.
        self.get_failure(
            defer.ensureDeferred(
                self.module_api.update_room_membership(
                    sender=f"@user:{self.module_api.server_name}",
                    target=f"@user:{self.module_api.server_name}",
                    room_id=f"!nonexistent:{fake_remote_host}",
                    new_membership="join",
                    remote_room_hosts=[fake_remote_host],
                )
            ),
            NotFoundError,
        )

        # Check that a remote join was attempted.
        self.assertEqual(mocked_remote_join.call_count, 1)

    def test_get_room_state(self) -> None:
        """Tests that a module can retrieve the state of a room through the module API."""
        user_id = self.register_user("peter", "hackme")
        tok = self.login("peter", "hackme")

        # Create a room and send some custom state in it.
        room_id = self.helper.create_room_as(tok=tok)
        self.helper.send_state(room_id, "org.matrix.test", {}, tok=tok)

        # Check that the module API can successfully fetch state for the room.
        state = self.get_success(
            defer.ensureDeferred(self.module_api.get_room_state(room_id))
        )

        # Check that a few standard events are in the returned state.
        self.assertIn((EventTypes.Create, ""), state)
        self.assertIn((EventTypes.Member, user_id), state)

        # Check that our custom state event is in the returned state.
        self.assertEqual(state[("org.matrix.test", "")].sender, user_id)
        self.assertEqual(state[("org.matrix.test", "")].state_key, "")
        self.assertEqual(state[("org.matrix.test", "")].content, {})

    def test_set_push_rules_action(self) -> None:
        """Test that a module can change the actions of an existing push rule for a user."""

        # Create a room with 2 users in it. Push rules must not match if the user is the
        # event's sender, so we need one user to send messages and one user to receive
        # notifications.
        user_id = self.register_user("user", "password")
        tok = self.login("user", "password")

        room_id = self.helper.create_room_as(user_id, is_public=True, tok=tok)

        user_id2 = self.register_user("user2", "password")
        tok2 = self.login("user2", "password")
        self.helper.join(room_id, user_id2, tok=tok2)

        # Register a 3rd user and join them to the room, so that we don't accidentally
        # trigger 1:1 push rules.
        user_id3 = self.register_user("user3", "password")
        tok3 = self.login("user3", "password")
        self.helper.join(room_id, user_id3, tok=tok3)

        # Send a message as the second user and check that it notifies.
        res = self.helper.send(room_id=room_id, body="here's a message", tok=tok2)
        event_id = res["event_id"]

        channel = self.make_request(
            "GET",
            "/notifications",
            access_token=tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        self.assertEqual(len(channel.json_body["notifications"]), 1, channel.json_body)
        self.assertEqual(
            channel.json_body["notifications"][0]["event"]["event_id"],
            event_id,
            channel.json_body,
        )

        # Change the .m.rule.message actions to not notify on new messages.
        self.get_success(
            defer.ensureDeferred(
                self.module_api.set_push_rule_action(
                    user_id=user_id,
                    scope="global",
                    kind="underride",
                    rule_id=".m.rule.message",
                    actions=["dont_notify"],
                )
            )
        )

        # Send another message as the second user and check that the number of
        # notifications didn't change.
        self.helper.send(room_id=room_id, body="here's another message", tok=tok2)

        channel = self.make_request(
            "GET",
            "/notifications",
            access_token=tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(len(channel.json_body["notifications"]), 1, channel.json_body)

    def test_check_push_rules_actions(self) -> None:
        """Test that modules can check whether a list of push rules actions are spec
        compliant.
        """
        with self.assertRaises(InvalidRuleException):
            self.module_api.check_push_rule_actions(["foo"])

        with self.assertRaises(InvalidRuleException):
            self.module_api.check_push_rule_actions([{"foo": "bar"}])

        self.module_api.check_push_rule_actions(["notify"])

        self.module_api.check_push_rule_actions(
            [{"set_tweak": "sound", "value": "default"}]
        )

    def test_lookup_room_alias(self) -> None:
        """Test that modules can resolve a room alias to a room ID."""
        password = "password"
        user_id = self.register_user("user", password)
        access_token = self.login(user_id, password)
        room_alias = "my-alias"
        reference_room_id = self.helper.create_room_as(
            tok=access_token, extra_content={"room_alias_name": room_alias}
        )
        self.assertIsNotNone(reference_room_id)

        (room_id, _) = self.get_success(
            self.module_api.lookup_room_alias(
                f"#{room_alias}:{self.module_api.server_name}"
            )
        )

        self.assertEqual(room_id, reference_room_id)

    def test_create_room(self) -> None:
        """Test that modules can create a room."""
        # First test user validation (i.e. user is local).
        self.get_failure(
            self.module_api.create_room(
                user_id=f"@user:{self.module_api.server_name}abc",
                config={},
                ratelimit=False,
            ),
            RuntimeError,
        )

        # Now do the happy path.
        user_id = self.register_user("user", "password")
        access_token = self.login(user_id, "password")

        room_id, room_alias = self.get_success(
            self.module_api.create_room(
                user_id=user_id, config={"room_alias_name": "foo-bar"}, ratelimit=False
            )
        )

        # Check room creator.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/state/m.room.create",
            access_token=access_token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_body["creator"], user_id)

        # Check room alias.
        self.assertEqual(room_alias, f"#foo-bar:{self.module_api.server_name}")

        # Let's try a room with no alias.
        room_id, room_alias = self.get_success(
            self.module_api.create_room(user_id=user_id, config={}, ratelimit=False)
        )

        # Check room creator.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/state/m.room.create",
            access_token=access_token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_body["creator"], user_id)

        # Check room alias.
        self.assertIsNone(room_alias)

    def test_on_logged_out(self) -> None:
        """Test that on_logged_out module hook is properly called when logging out
        a device, and that related pushers are still available at this time.
        """
        device_id = "AAAAAAA"
        user_id = self.register_user("test_on_logged_out", "secret")
        self.login("test_on_logged_out", "secret", device_id)

        self.get_success(
            self.hs.get_pusherpool().add_or_update_pusher(
                user_id=user_id,
                device_id=device_id,
                kind="http",
                app_id="m.http",
                app_display_name="HTTP Push Notifications",
                device_display_name="pushy push",
                pushkey="a@example.com",
                lang=None,
                data={"url": "http://example.com/_matrix/push/v1/notify"},
            )
        )

        # Setup a callback counting the number of pushers.
        number_of_pushers_in_callback: Optional[int] = None

        async def _on_logged_out_mock(
            user_id: str, device_id: Optional[str], access_token: str
        ) -> None:
            nonlocal number_of_pushers_in_callback
            number_of_pushers_in_callback = len(
                self.hs.get_pusherpool().pushers[user_id].values()
            )

        self.module_api.register_password_auth_provider_callbacks(
            on_logged_out=_on_logged_out_mock
        )

        # Delete the device.
        device_handler = self.hs.get_device_handler()
        assert isinstance(device_handler, DeviceHandler)
        self.get_success(device_handler.delete_devices(user_id, [device_id]))

        # Check that the callback was called and the pushers still existed.
        self.assertEqual(number_of_pushers_in_callback, 1)

        # Ensure the pushers were deleted after the callback.
        self.assertEqual(len(self.hs.get_pusherpool().pushers[user_id].values()), 0)


class ModuleApiWorkerTestCase(BaseModuleApiTestCase, BaseMultiWorkerStreamTestCase):
    """For testing ModuleApi functionality in a multi-worker setup"""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        presence.register_servlets,
    ]

    def default_config(self) -> Dict[str, Any]:
        conf = super().default_config()
        conf["stream_writers"] = {"presence": ["presence_writer"]}
        conf["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "presence_writer": {"host": "testserv", "port": 1001},
        }
        return conf

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.module_api = hs.get_module_api()
        self.sync_handler = hs.get_sync_handler()

    def test_send_local_online_presence_to_workers(self) -> None:
        # Test sending local online presence to users from a worker process
        _test_sending_local_online_presence_to_local_user(self, test_with_workers=True)


def _test_sending_local_online_presence_to_local_user(
    test_case: BaseModuleApiTestCase, test_with_workers: bool = False
) -> None:
    """Tests that send_local_presence_to_users sends local online presence to local users.

    This simultaneously tests two different usecases:
        * Testing that this method works when either called from a worker or the main process.
            - We test this by calling this method from both a TestCase that runs in monolith mode, and one that
              runs with a main and generic_worker.
        * Testing that multiple devices syncing simultaneously will all receive a snapshot of local,
            online presence - but only once per device.

    Args:
        test_with_workers: If True, this method will call ModuleApi.send_local_online_presence_to on a
            worker process. The test users will still sync with the main process. The purpose of testing
            with a worker is to check whether a Synapse module running on a worker can inform other workers/
            the main process that they should include additional presence when a user next syncs.
            If this argument is True, `test_case` MUST be an instance of BaseMultiWorkerStreamTestCase.
    """
    if test_with_workers:
        assert isinstance(test_case, BaseMultiWorkerStreamTestCase)

        # Create a worker process to make module_api calls against
        worker_hs = test_case.make_worker_hs(
            "synapse.app.generic_worker", {"worker_name": "presence_writer"}
        )

    # Create a user who will send presence updates
    test_case.presence_receiver_id = test_case.register_user(
        "presence_receiver1", "monkey"
    )
    test_case.presence_receiver_tok = test_case.login("presence_receiver1", "monkey")

    # And another user that will send presence updates out
    test_case.presence_sender_id = test_case.register_user("presence_sender2", "monkey")
    test_case.presence_sender_tok = test_case.login("presence_sender2", "monkey")

    # Put them in a room together so they will receive each other's presence updates
    room_id = test_case.helper.create_room_as(
        test_case.presence_receiver_id,
        tok=test_case.presence_receiver_tok,
    )
    test_case.helper.join(
        room_id, test_case.presence_sender_id, tok=test_case.presence_sender_tok
    )

    # Presence sender comes online
    send_presence_update(
        test_case,
        test_case.presence_sender_id,
        test_case.presence_sender_tok,
        "online",
        "I'm online!",
    )

    # Presence receiver should have received it
    presence_updates, sync_token = sync_presence(
        test_case, test_case.presence_receiver_id
    )
    test_case.assertEqual(len(presence_updates), 1)

    presence_update: UserPresenceState = presence_updates[0]
    test_case.assertEqual(presence_update.user_id, test_case.presence_sender_id)
    test_case.assertEqual(presence_update.state, "online")

    if test_with_workers:
        # Replicate the current sync presence token from the main process to the worker process.
        # We need to do this so that the worker process knows the current presence stream ID to
        # insert into the database when we call ModuleApi.send_local_online_presence_to.
        assert isinstance(test_case, BaseMultiWorkerStreamTestCase)
        test_case.replicate()

    # Syncing again should result in no presence updates
    presence_updates, sync_token = sync_presence(
        test_case, test_case.presence_receiver_id, sync_token
    )
    test_case.assertEqual(len(presence_updates), 0)

    # We do an (initial) sync with a second "device" now, getting a new sync token.
    # We'll use this in a moment.
    _, sync_token_second_device = sync_presence(
        test_case, test_case.presence_receiver_id
    )

    # Determine on which process (main or worker) to call ModuleApi.send_local_online_presence_to on
    if test_with_workers:
        assert isinstance(test_case, BaseMultiWorkerStreamTestCase)
        module_api_to_use = worker_hs.get_module_api()
    else:
        module_api_to_use = test_case.module_api

    # Trigger sending local online presence. We expect this information
    # to be saved to the database where all processes can access it.
    # Note that we're syncing via the master.
    d = defer.ensureDeferred(
        module_api_to_use.send_local_online_presence_to(
            [test_case.presence_receiver_id],
        )
    )

    if test_with_workers:
        # In order for the required presence_set_state replication request to occur between the
        # worker and main process, we need to pump the reactor. Otherwise, the coordinator that
        # reads the request on the main process won't do so, and the request will time out.
        while not d.called:
            test_case.reactor.advance(0.1)

    test_case.get_success(d)

    # The presence receiver should have received online presence again.
    presence_updates, sync_token = sync_presence(
        test_case, test_case.presence_receiver_id, sync_token
    )
    test_case.assertEqual(len(presence_updates), 1)

    presence_update = presence_updates[0]
    test_case.assertEqual(presence_update.user_id, test_case.presence_sender_id)
    test_case.assertEqual(presence_update.state, "online")

    # We attempt to sync with the second sync token we received above - just to check that
    # multiple syncing devices will each receive the necessary online presence.
    presence_updates, sync_token_second_device = sync_presence(
        test_case, test_case.presence_receiver_id, sync_token_second_device
    )
    test_case.assertEqual(len(presence_updates), 1)

    presence_update = presence_updates[0]
    test_case.assertEqual(presence_update.user_id, test_case.presence_sender_id)
    test_case.assertEqual(presence_update.state, "online")

    # However, if we now sync with either "device", we won't receive another burst of online presence
    # until the API is called again sometime in the future
    presence_updates, sync_token = sync_presence(
        test_case, test_case.presence_receiver_id, sync_token
    )

    # Now we check that we don't receive *offline* updates using ModuleApi.send_local_online_presence_to.

    # Presence sender goes offline
    send_presence_update(
        test_case,
        test_case.presence_sender_id,
        test_case.presence_sender_tok,
        "offline",
        "I slink back into the darkness.",
    )

    # Presence receiver should have received the updated, offline state
    presence_updates, sync_token = sync_presence(
        test_case, test_case.presence_receiver_id, sync_token
    )
    test_case.assertEqual(len(presence_updates), 1)

    # Now trigger sending local online presence.
    d = defer.ensureDeferred(
        module_api_to_use.send_local_online_presence_to(
            [
                test_case.presence_receiver_id,
            ]
        )
    )

    if test_with_workers:
        # In order for the required presence_set_state replication request to occur between the
        # worker and main process, we need to pump the reactor. Otherwise, the coordinator that
        # reads the request on the main process won't do so, and the request will time out.
        while not d.called:
            test_case.reactor.advance(0.1)

    test_case.get_success(d)

    # Presence receiver should *not* have received offline state
    presence_updates, sync_token = sync_presence(
        test_case, test_case.presence_receiver_id, sync_token
    )
    test_case.assertEqual(len(presence_updates), 0)
