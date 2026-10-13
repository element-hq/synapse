#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from unittest.mock import patch

import attr
from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import AuthError, Codes
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.events import EventBase
from synapse.rest import admin
from synapse.rest.client import delayed_events, login, room
from synapse.server import HomeServer
from synapse.types import JsonDict, StateMap, UserID, create_requester
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest


class ServerAnnouncementRulesTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        delayed_events.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["max_event_delay_duration"] = "24h"
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.handler = hs.get_event_creation_handler()
        self.config = hs.config.serverannouncements
        self.creator = self.config.room_management_mxid
        self.creator_requester = create_requester(
            self.creator, authenticated_entity=hs.hostname
        )
        self.alice = self.register_user("alice", "pass", admin=True)
        self.alice_token = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_token = self.login("bob", "pass")
        self.removed = self.register_user("removed", "pass")
        self.removed_token = self.login("removed", "pass")
        self.outsider = self.register_user("outsider", "pass")
        self.outsider_token = self.login("outsider", "pass")

        # Build a mapped room independently of reconciliation. Deliberately keep
        # stale public join rules, a joined removed user, and excessive client
        # power levels so Matrix auth alone would allow the prohibited actions.
        self.room_id, _, _ = self.get_success(
            hs.get_room_creation_handler().create_room(
                self.creator_requester,
                {
                    "room_version": "11",
                    "preset": "public_chat",
                    "creation_content": {"m.federate": False},
                    "power_level_content_override": {
                        "users": {
                            self.creator: 100,
                            self.alice: 100,
                            self.bob: 50,
                            self.removed: 50,
                        },
                        "state_default": 100,
                        "invite": 100,
                        "kick": 100,
                        "ban": 100,
                        "redact": 100,
                    },
                },
                ratelimit=False,
                creator_join_profile={},
            )
        )
        for user_id, token in (
            (self.alice, self.alice_token),
            (self.bob, self.bob_token),
            (self.removed, self.removed_token),
        ):
            self.helper.join(self.room_id, user_id, tok=token)

        self.own_topic = self.helper.send_state(
            self.room_id, EventTypes.Topic, {"topic": "Original"}, tok=self.alice_token
        )["event_id"]
        self.room_config = ServerAnnouncementRoom(
            config_id="announcements",
            room_name="Announcements",
            room_topic=None,
            room_avatar_url=None,
            room_avatar_path=None,
            allowed_user_ids=[self.alice, self.bob],
        )
        self.get_success(
            self.store.store_server_announcement_room(
                self.room_config.config_id, self.room_id
            )
        )
        self.config.rooms = [self.room_config]

    def _send_internal_state(self, event_type: str, content: JsonDict) -> EventBase:
        event, _ = self.get_success(
            self.handler.create_and_send_nonmember_event(
                self.creator_requester,
                {
                    "type": event_type,
                    "state_key": "",
                    "room_id": self.room_id,
                    "sender": self.creator,
                    "content": content,
                },
                ratelimit=False,
            )
        )
        return event

    def _redact(self, event_id: str, token: str, expected_code: int) -> None:
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/rooms/{self.room_id}/redact/{event_id}/{event_id}",
            {},
            access_token=token,
        )
        self.assertEqual(channel.code, expected_code, channel.json_body)
        if expected_code == 403:
            self.assertEqual(channel.json_body["errcode"], Codes.FORBIDDEN)

    @parameterized.expand(
        [
            (EventTypes.PowerLevels, {"users_default": 100}),
            (EventTypes.Topic, {"topic": "Changed"}),
            ("org.example.custom_state", {"value": "changed"}),
        ]
    )
    def test_client_state_is_protected(
        self, event_type: str, content: JsonDict
    ) -> None:
        response = self.helper.send_state(
            self.room_id, event_type, content, tok=self.alice_token, expect_code=403
        )
        self.assertEqual(response["errcode"], Codes.FORBIDDEN)
        self.assertIn("managed by the server", response["error"])

    def test_identical_state_is_rejected_before_deduplication(self) -> None:
        self.helper.send_state(
            self.room_id,
            EventTypes.Topic,
            {"topic": "Original"},
            tok=self.alice_token,
            expect_code=403,
        )

    def test_allowed_messages_and_self_redactions(self) -> None:
        event_id = self.helper.send(self.room_id, "Announcement", tok=self.bob_token)[
            "event_id"
        ]
        self._redact(event_id, self.bob_token, 200)

    @parameterized.expand([(EventTypes.Topic, ""), (EventTypes.Member, "@alice:test")])
    def test_cannot_redact_own_state(self, event_type: str, state_key: str) -> None:
        event = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state_event(
                self.room_id, event_type, state_key
            )
        )
        assert event is not None
        self.assertEqual(event.sender, self.alice)
        self._redact(event.event_id, self.alice_token, 403)
        self.assertFalse(
            self.get_success(
                self.store.get_event(event.event_id)
            ).internal_metadata.is_redacted()
        )

    def test_cannot_redact_managed_state(self) -> None:
        event = self._send_internal_state(EventTypes.Name, {"name": "Managed"})
        self._redact(event.event_id, self.alice_token, 403)

    def test_removed_sender_denied_despite_stale_membership_and_power(self) -> None:
        response = self.helper.send(
            self.room_id, "Not allowed", tok=self.removed_token, expect_code=403
        )
        self.assertIn("Sender is not allowed", response["error"])
        self.room_config.allowed_user_ids.remove(self.alice)
        self.helper.send(
            self.room_id, "Also not allowed", tok=self.alice_token, expect_code=403
        )
        self.helper.leave(self.room_id, self.alice, tok=self.alice_token)

    def test_client_cannot_invite_even_an_allowed_user(self) -> None:
        self.helper.leave(self.room_id, self.bob, tok=self.bob_token)
        self.helper.invite(
            self.room_id, self.alice, self.bob, tok=self.alice_token, expect_code=403
        )

    @parameterized.expand([(Membership.LEAVE,), (Membership.BAN,)])
    def test_client_cannot_remove_members(self, membership: str) -> None:
        self.helper.change_membership(
            self.room_id,
            self.alice,
            self.bob,
            membership,
            tok=self.alice_token,
            expect_code=403,
        )

    def test_allowed_user_can_update_own_membership(self) -> None:
        self.helper.change_membership(
            self.room_id,
            self.bob,
            self.bob,
            Membership.JOIN,
            extra_data={"displayname": "Announcer"},
            tok=self.bob_token,
        )
        self.helper.leave(self.room_id, self.bob, tok=self.bob_token)
        self.helper.join(self.room_id, self.bob, tok=self.bob_token)

    def test_outsider_cannot_join_public_room(self) -> None:
        self.helper.join(
            self.room_id, self.outsider, tok=self.outsider_token, expect_code=403
        )

    def test_admin_cannot_force_join_outsider(self) -> None:
        channel = self.make_request(
            "POST",
            f"/_synapse/admin/v1/join/{self.room_id}",
            {"user_id": self.outsider},
            access_token=self.alice_token,
        )
        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], Codes.FORBIDDEN)
        self.assertIn("not allowed", channel.json_body["error"])

    @parameterized.expand([(Membership.INVITE,), (Membership.JOIN,)])
    def test_server_cannot_add_outsider(self, membership: str) -> None:
        requester = (
            self.creator_requester
            if membership == Membership.INVITE
            else create_requester(self.outsider, authenticated_entity=self.hs.hostname)
        )
        failure = self.get_failure(
            self.hs.get_room_member_handler().update_membership(
                requester,
                UserID.from_string(self.outsider),
                self.room_id,
                membership,
                ratelimit=False,
            ),
            AuthError,
        )
        self.assertIn("not allowed", failure.value.msg)

    def test_internal_state_and_membership_management(self) -> None:
        self.assertIsNone(self.get_success(self.store.get_user_by_id(self.creator)))
        self._send_internal_state(EventTypes.Name, {"name": "Configured"})
        self._send_internal_state(EventTypes.JoinRules, {"join_rule": "invite"})
        self.helper.leave(self.room_id, self.bob, tok=self.bob_token)
        member_handler = self.hs.get_room_member_handler()
        self.get_success(
            member_handler.update_membership(
                self.creator_requester,
                UserID.from_string(self.bob),
                self.room_id,
                Membership.INVITE,
                ratelimit=False,
            )
        )
        self.get_success(
            member_handler.update_membership(
                create_requester(self.bob, authenticated_entity=self.hs.hostname),
                UserID.from_string(self.bob),
                self.room_id,
                Membership.JOIN,
                ratelimit=False,
            )
        )
        # The creator itself is the sole non-allowlisted join exception.
        self.get_success(
            member_handler.update_membership(
                self.creator_requester,
                UserID.from_string(self.creator),
                self.room_id,
                Membership.JOIN,
                content={"membership": Membership.JOIN, "displayname": "Announcements"},
                ratelimit=False,
            )
        )
        # A removed user can leave voluntarily or be removed by the creator.
        self.get_success(
            member_handler.update_membership(
                self.creator_requester,
                UserID.from_string(self.removed),
                self.room_id,
                "leave",
                ratelimit=False,
            )
        )
        self.helper.send(self.room_id, "Still allowed", tok=self.bob_token)

    @parameterized.expand([("client",), ("admin",), ("other_server_user",)])
    def test_creator_state_requires_server_authenticated_creator(
        self, identity: str
    ) -> None:
        requester = create_requester(self.creator)
        if identity == "admin":
            requester = create_requester(self.creator, authenticated_entity=self.alice)
        elif identity == "other_server_user":
            requester = create_requester(
                self.alice, authenticated_entity=self.hs.hostname
            )
        self.get_failure(
            self.handler.create_and_send_nonmember_event(
                requester,
                {
                    "room_id": self.room_id,
                    "sender": self.creator,
                    "type": EventTypes.Name,
                    "state_key": "",
                    "content": {"name": "Impersonated"},
                },
                ratelimit=False,
            ),
            AuthError,
        )

    def test_creator_cannot_send_messages(self) -> None:
        self.get_failure(
            self.handler.create_and_send_nonmember_event(
                self.creator_requester,
                {
                    "room_id": self.room_id,
                    "sender": self.creator,
                    "type": EventTypes.Message,
                    "content": {
                        "msgtype": "m.text",
                        "body": "Not an announcement user",
                    },
                },
                ratelimit=False,
            ),
            AuthError,
        )

    def test_normal_room_unaffected_and_new_mapping_seen(self) -> None:
        normal_room = self.helper.create_room_as(self.alice, tok=self.alice_token)
        self.helper.send_state(
            normal_room, EventTypes.Topic, {"topic": "Normal"}, tok=self.alice_token
        )
        self.helper.join(normal_room, self.outsider, tok=self.outsider_token)
        self.helper.send(normal_room, "Normal message", tok=self.outsider_token)
        # The prior unmapped lookup must not be cached.
        self.config.rooms.append(attr.evolve(self.room_config, config_id="new_mapping"))
        self.get_success(
            self.store.store_server_announcement_room("new_mapping", normal_room)
        )
        self.helper.send(
            normal_room, "Now forbidden", tok=self.outsider_token, expect_code=403
        )

    @parameterized.expand([("disabled",), ("removed",)])
    def test_no_current_configuration_leaves_room_unaffected(self, mode: str) -> None:
        self.config.rooms = (
            []
            if mode == "disabled"
            else [attr.evolve(self.room_config, config_id="different")]
        )
        self.helper.send_state(
            self.room_id, EventTypes.Topic, {"topic": "Unmanaged"}, tok=self.alice_token
        )
        self.helper.send(self.room_id, "Unmanaged", tok=self.removed_token)
        self.helper.join(self.room_id, self.outsider, tok=self.outsider_token)

    def test_writer_rechecks_configuration_for_already_created_events(self) -> None:
        requester = create_requester(self.alice)
        event, context = self.get_success(
            self.handler.create_event(
                requester,
                {
                    "room_id": self.room_id,
                    "sender": self.alice,
                    "type": EventTypes.Message,
                    "content": {"msgtype": "m.text", "body": "Created before removal"},
                },
            )
        )
        persisted_context = self.get_success(context.persist(event))
        self.room_config.allowed_user_ids.remove(self.alice)
        # Exercise the writer entry point used by event replication directly.
        self.get_failure(
            self.handler.persist_and_notify_client_events(
                requester, [(event, persisted_context)], ratelimit=False
            ),
            AuthError,
        )
        self.assertIsNone(
            self.get_success(self.store.get_event(event.event_id, allow_none=True))
        )

    def test_cannot_redact_membership_in_same_batch(self) -> None:
        requester = create_requester(self.alice)
        member, member_context = self.get_success(
            self.handler.create_event(
                requester,
                {
                    "room_id": self.room_id,
                    "sender": self.alice,
                    "type": EventTypes.Member,
                    "state_key": self.alice,
                    "content": {
                        "membership": Membership.JOIN,
                        "displayname": "New name",
                    },
                },
            )
        )
        redaction, redaction_context = self.get_success(
            self.handler.create_event(
                requester,
                {
                    "room_id": self.room_id,
                    "sender": self.alice,
                    "type": EventTypes.Redaction,
                    "content": {"redacts": member.event_id},
                },
            )
        )
        failure = self.get_failure(
            self.handler.handle_new_client_event(
                requester,
                [
                    (member, self.get_success(member_context.persist(member))),
                    (redaction, self.get_success(redaction_context.persist(redaction))),
                ],
                ratelimit=False,
            ),
            AuthError,
        )
        self.assertIn("state cannot be redacted", failure.value.msg)
        self.assertIsNone(
            self.get_success(self.store.get_event(member.event_id, allow_none=True))
        )
        self.assertIsNone(
            self.get_success(self.store.get_event(redaction.event_id, allow_none=True))
        )

    def test_cannot_preemptively_redact_unknown_event(self) -> None:
        self._redact("$unknown", self.alice_token, 404)

    def test_empty_allowlist_still_allows_cleanup(self) -> None:
        self.room_config.allowed_user_ids.clear()
        self.helper.send(
            self.room_id, "Not allowed", tok=self.alice_token, expect_code=403
        )
        self.helper.leave(self.room_id, self.alice, tok=self.alice_token)
        self._send_internal_state(EventTypes.Name, {"name": "Empty room"})

    def test_third_party_rewritten_sender_cannot_bypass_allowlist(self) -> None:
        async def rewrite(
            event: EventBase, state: StateMap[EventBase]
        ) -> tuple[bool, JsonDict | None]:
            result = event.get_dict()
            result["sender"] = self.removed
            return True, result

        self.hs.get_module_api_callbacks().third_party_event_rules.register_third_party_rules_callbacks(
            check_event_allowed=rewrite
        )
        self.helper.send(
            self.room_id, "Rewritten", tok=self.alice_token, expect_code=403
        )

    def test_third_party_rewritten_state_cannot_impersonate_creator(self) -> None:
        async def rewrite(
            event: EventBase, state: StateMap[EventBase]
        ) -> tuple[bool, JsonDict | None]:
            result = event.get_dict()
            result["sender"] = self.creator
            return True, result

        self.hs.get_module_api_callbacks().third_party_event_rules.register_third_party_rules_callbacks(
            check_event_allowed=rewrite
        )
        self.helper.send_state(
            self.room_id,
            EventTypes.Topic,
            {"topic": "Impersonated"},
            tok=self.alice_token,
            expect_code=403,
        )

    def test_third_party_rewritten_redaction_cannot_target_state(self) -> None:
        event_id = self.helper.send(self.room_id, "Redact me", tok=self.alice_token)[
            "event_id"
        ]

        async def rewrite(
            event: EventBase, state: StateMap[EventBase]
        ) -> tuple[bool, JsonDict | None]:
            result = event.get_dict()
            result["content"] = {"redacts": self.own_topic}
            return True, result

        self.hs.get_module_api_callbacks().third_party_event_rules.register_third_party_rules_callbacks(
            check_event_allowed=rewrite
        )
        self._redact(event_id, self.alice_token, 403)

    @parameterized.expand([("message",), ("state",)])
    def test_delayed_delivery_cannot_bypass_current_rules(self, kind: str) -> None:
        # Schedule while permitted; delivery must use the later configuration.
        state_key = "" if kind == "state" else None
        event_type = EventTypes.Topic if kind == "state" else EventTypes.Message
        content = (
            {"topic": "Delayed"}
            if kind == "state"
            else {"msgtype": "m.text", "body": "Delayed"}
        )
        with patch.object(self.config, "rooms", []):
            channel = self.make_request(
                "PUT",
                self.helper.build_delayed_event_request_path(
                    room_id=self.room_id, event_type=event_type
                ),
                self.helper.build_delayed_event_request_body(
                    delay=Duration(hours=1), content=content, state_key=state_key
                ),
                access_token=self.alice_token,
            )
        self.assertEqual(channel.code, 200, channel.json_body)
        delay_id = channel.json_body["delay_id"]
        if kind == "message":
            self.room_config.allowed_user_ids.remove(self.alice)
        before = self.get_success(self.store.get_prev_events_for_room(self.room_id))
        channel = self.make_request(
            "POST",
            f"/_matrix/client/unstable/org.matrix.msc4140/delayed_events/{delay_id}/send",
            {},
            access_token=self.alice_token,
        )
        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], Codes.FORBIDDEN)
        self.assertEqual(
            before, self.get_success(self.store.get_prev_events_for_room(self.room_id))
        )
