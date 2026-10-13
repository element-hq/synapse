#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
"""Creating, joining, sending to and leaving an MSC4430 member key room through
the client API, with one homeserver and no federation.

Clients speak in user IDs; the room's events must name member keys and be signed
by them. These are the local halves of the Complement tests for creating a room
(the create event is sent by a member key and the creator's membership carries
co-signed linkage information) and for joining, leaving and changing one's own
membership by user ID (the user never appears in the room as a user ID). What a
client is served (`/sync`, `/event`, `/members`) is a later change, so the room is
inspected through `/state` and the store.
"""

import json
import urllib.parse
from http import HTTPStatus
from unittest.mock import Mock

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    MemberInfo,
    MemberKeys,
    Membership,
)
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.rest import admin
from synapse.rest.client import (
    account_data,
    login,
    room,
    room_upgrade_rest_servlet,
    sync,
)
from synapse.server import HomeServer
from synapse.storage.member_keys import MemberLink
from synapse.types import JsonDict, is_member_key
from synapse.util.clock import Clock

from tests import unittest
from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.test_utils.member_key_rooms import Member, MemberKeyRoom
from tests.unittest import override_config

ROOM_VERSION = RoomVersions.MSC4430v12


class MemberKeyRoomsTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        room_upgrade_rest_servlet.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enabled unless a test's `override_config` says otherwise. This also
        # registers the room version, process wide.
        config.setdefault("experimental_features", {}).setdefault(
            "msc4430_enabled", True
        )
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # Nothing here federates; a profile lookup for a remote user must fail
        # fast rather than wait on the network.
        return self.setup_test_homeserver(federation_client=Mock())

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")

    def _create_room(self, **extra: object) -> str:
        return self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            room_version=ROOM_VERSION.identifier,
            extra_content=dict(extra),
        )

    def _key(self, room_id: str, user_id: str) -> str:
        """The member key we hold for a user in a room."""
        local_key = self.get_success(self.store.get_local_member_key(room_id, user_id))
        assert local_key is not None, f"{user_id} has no member key in {room_id}"
        return local_key.member_key

    def _event(self, event_id: str) -> EventBase:
        return self.get_success(self.store.get_event(event_id))

    def _current_state(self, room_id: str) -> dict[tuple[str, str], EventBase]:
        state_ids = self.get_success(self.store.get_partial_current_state_ids(room_id))
        events = self.get_success(self.store.get_events(state_ids.values()))
        return {key: events[event_id] for key, event_id in state_ids.items()}

    def _member_event(self, room_id: str, member_key: str) -> EventBase:
        return self._current_state(room_id)[(EventTypes.Member, member_key)]

    def _all_events(self, room_id: str) -> list[EventBase]:
        event_ids = self.get_success(
            self.store.db_pool.simple_select_onecol(
                table="events",
                keyvalues={"room_id": room_id},
                retcol="event_id",
            )
        )
        return list(self.get_success(self.store.get_events(event_ids)).values())

    def _assert_signed_by_member_key_only(self, event: EventBase) -> None:
        """The event carries exactly the sender's member key signature."""
        self.assertEqual(
            {entity: set(sigs) for entity, sigs in event.signatures.as_dict().items()},
            {MemberKeys.SIGNATURE_ENTITY: {MemberKeys.KEY_ID}},
            event.signatures,
        )

    def _assert_linked_and_cosigned(self, event: EventBase, user_id: str) -> None:
        """A membership event about one of our users: it names them and we
        co-signed it, next to the member key signature."""
        localpart, domain = user_id[1:].split(":", 1)
        self.assertEqual(event.content[EventContentFields.MSC4430_LOCALPART], localpart)
        self.assertEqual(event.content[EventContentFields.MSC4430_DOMAIN], domain)
        self.assertEqual(
            {entity: set(sigs) for entity, sigs in event.signatures.as_dict().items()},
            {
                MemberKeys.SIGNATURE_ENTITY: {MemberKeys.KEY_ID},
                self.hs.hostname: {
                    f"{self.hs.signing_key.alg}:{self.hs.signing_key.version}"
                },
            },
            event.signatures,
        )

    def _assert_no_user_ids_in_room(self, room_id: str) -> None:
        """No event in the room names a user ID as sender or membership target."""
        for event in self._all_events(room_id):
            self.assertTrue(is_member_key(event.sender), (event.type, event.sender))
            if event.type == EventTypes.Member:
                self.assertTrue(is_member_key(event.state_key), event.state_key)

    def test_create_room(self) -> None:
        """The create event is sent by the creator's member key and signed only by
        it; the creator's join names them and is co-signed; nothing in the room
        is a user ID."""
        room_id = self._create_room()
        alice_key = self._key(room_id, self.alice)

        create = self.get_success(self.store.get_create_event_for_room(room_id))
        self.assertEqual(create.sender, alice_key)
        self._assert_signed_by_member_key_only(create)
        self.assertNotIn(EventContentFields.ADDITIONAL_CREATORS, create.content)

        join = self._member_event(room_id, alice_key)
        self.assertEqual(join.sender, alice_key)
        self.assertEqual(join.membership, Membership.JOIN)
        self._assert_linked_and_cosigned(join, self.alice)

        # The rest of the initial state is sent by the key, and signed by it only.
        state = self._current_state(room_id)
        self.assertIn((EventTypes.PowerLevels, ""), state)
        self.assertIn((EventTypes.JoinRules, ""), state)
        for key, event in state.items():
            self.assertEqual(event.sender, alice_key, key)
            if event.type != EventTypes.Member:
                self._assert_signed_by_member_key_only(event)
        # Room v12 creators are not in the power level users map.
        self.assertEqual(state[(EventTypes.PowerLevels, "")].content["users"], {})
        self._assert_no_user_ids_in_room(room_id)

        # The user-keyed tables know the user.
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.alice)),
            frozenset({room_id}),
        )

    def test_join_send_and_leave_by_user_id(self) -> None:
        """Joining, sending and leaving by user ID produce events under the user's
        member key, which stays the same across leaving and re-joining."""
        room_id = self._create_room(preset="public_chat")
        alice_key = self._key(room_id, self.alice)

        self.helper.join(room_id, self.bob, tok=self.bob_tok)
        bob_key = self._key(room_id, self.bob)
        self.assertNotEqual(bob_key, alice_key)
        join = self._member_event(room_id, bob_key)
        self.assertEqual(join.sender, bob_key)
        self._assert_linked_and_cosigned(join, self.bob)

        # `/state` is served to the joined user, and names keys.
        channel = self.make_request(
            "GET", f"/rooms/{room_id}/state", access_token=self.bob_tok
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.text_body)
        members = {
            e["state_key"] for e in channel.json_list if e["type"] == EventTypes.Member
        }
        self.assertEqual(members, {alice_key, bob_key})
        channel = self.make_request(
            "GET",
            f"/rooms/{room_id}/state/{EventTypes.Member}/{bob_key}",
            access_token=self.bob_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        self.assertEqual(channel.json_body["membership"], Membership.JOIN)
        self.assertEqual(channel.json_body[EventContentFields.MSC4430_LOCALPART], "bob")

        # A message is sent by the key, and signed by it only; sending it again
        # under the same transaction ID is deduplicated.
        body = self.helper.send(room_id, body="hello", tok=self.bob_tok, txn_id="t1")
        message = self._event(body["event_id"])
        self.assertEqual(message.sender, bob_key)
        self._assert_signed_by_member_key_only(message)
        again = self.helper.send(room_id, body="hello", tok=self.bob_tok, txn_id="t1")
        self.assertEqual(again["event_id"], body["event_id"])

        self.helper.leave(room_id, self.bob, tok=self.bob_tok)
        leave = self._member_event(room_id, bob_key)
        self.assertEqual(leave.membership, Membership.LEAVE)
        self.assertEqual(leave.sender, bob_key)
        self._assert_linked_and_cosigned(leave, self.bob)
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.bob)), frozenset()
        )

        # Re-joining uses the same key.
        self.helper.join(room_id, self.bob, tok=self.bob_tok)
        self.assertEqual(self._key(room_id, self.bob), bob_key)
        self.assertEqual(self._member_event(room_id, bob_key).membership, "join")

        self._assert_no_user_ids_in_room(room_id)

    def test_membership_change_by_user_id(self) -> None:
        """`PUT /state/m.room.member/{user ID}` by the user themselves lands under
        their member key, keeping the linkage information."""
        room_id = self._create_room()
        alice_key = self._key(room_id, self.alice)

        body = self.helper.send_state(
            room_id,
            EventTypes.Member,
            {"membership": Membership.JOIN, "displayname": "Alice!"},
            tok=self.alice_tok,
            state_key=self.alice,
        )
        event = self._event(body["event_id"])
        self.assertEqual(event.sender, alice_key)
        self.assertEqual(event.state_key, alice_key)
        self.assertEqual(event.content["displayname"], "Alice!")
        self._assert_linked_and_cosigned(event, self.alice)
        self.assertEqual(self._member_event(room_id, alice_key), event)

    def test_invite_local_user(self) -> None:
        """An invite by user ID names the invitee's member key, carries their
        linkage information and is co-signed; the invitee can then join."""
        room_id = self._create_room()
        alice_key = self._key(room_id, self.alice)
        self.helper.invite(room_id, src=self.alice, targ=self.bob, tok=self.alice_tok)
        bob_key = self._key(room_id, self.bob)

        invite = self._member_event(room_id, bob_key)
        self.assertEqual(invite.membership, Membership.INVITE)
        self.assertEqual(invite.sender, alice_key)
        self._assert_linked_and_cosigned(invite, self.bob)
        invited = self.get_success(
            self.store.get_invited_rooms_for_local_user(self.bob)
        )
        self.assertEqual([r.room_id for r in invited], [room_id])

        self.helper.join(room_id, self.bob, tok=self.bob_tok)
        self.assertEqual(self._member_event(room_id, bob_key).membership, "join")
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.bob)),
            frozenset({room_id}),
        )
        self._assert_no_user_ids_in_room(room_id)

    def test_invite_remote_user_is_refused(self) -> None:
        """Members of other servers cannot be named yet."""
        room_id = self._create_room()
        self.helper.invite(
            room_id,
            src=self.alice,
            targ="@carol:other.example",
            tok=self.alice_tok,
            expect_code=HTTPStatus.BAD_REQUEST,
        )
        self._assert_no_user_ids_in_room(room_id)

    def test_only_user_ids_are_accepted_as_targets(self) -> None:
        """Clients cannot (yet) name a member key as a membership target."""
        room_id = self._create_room()
        self.helper.invite(
            room_id,
            src=self.alice,
            targ=Member().key,
            tok=self.alice_tok,
            expect_code=HTTPStatus.BAD_REQUEST,
        )

    def test_clients_cannot_assert_linkage_information(self) -> None:
        """Whatever a client puts in `localpart` / `domain` is replaced by what
        the server can vouch for."""
        room_id = self._create_room()
        body = self.helper.send_state(
            room_id,
            EventTypes.Member,
            {
                "membership": Membership.JOIN,
                EventContentFields.MSC4430_LOCALPART: "mallory",
                EventContentFields.MSC4430_DOMAIN: "evil.example",
            },
            tok=self.alice_tok,
            state_key=self.alice,
        )
        self._assert_linked_and_cosigned(self._event(body["event_id"]), self.alice)

    def test_erased_user_leaves_without_localpart(self) -> None:
        """An erased user's membership events no longer publish their localpart."""
        room_id = self._create_room(preset="public_chat")
        self.helper.join(room_id, self.bob, tok=self.bob_tok)
        bob_key = self._key(room_id, self.bob)
        self.get_success(self.store.mark_user_erased(self.bob))

        self.helper.leave(room_id, self.bob, tok=self.bob_tok)
        leave = self._member_event(room_id, bob_key)
        self.assertEqual(leave.membership, Membership.LEAVE)
        self.assertNotIn(EventContentFields.MSC4430_LOCALPART, leave.content)
        self.assertEqual(
            leave.content[EventContentFields.MSC4430_DOMAIN], self.hs.hostname
        )
        # Still ours, so still co-signed.
        self.assertIn(self.hs.hostname, leave.signatures)

    def test_additional_creators(self) -> None:
        """Additional creators are named by key in the create event, which is the
        key they then join with; the same user named twice gets one key."""
        room_id = self._create_room(
            creation_content={
                EventContentFields.ADDITIONAL_CREATORS: [self.bob, self.bob]
            }
        )
        create = self.get_success(self.store.get_create_event_for_room(room_id))
        bob_key = self._key(room_id, self.bob)
        self.assertEqual(
            create.content[EventContentFields.ADDITIONAL_CREATORS], [bob_key, bob_key]
        )
        self.assertEqual(create.sender, self._key(room_id, self.alice))

        self.helper.join(room_id, self.bob, tok=self.bob_tok)
        self.assertEqual(self._member_event(room_id, bob_key).membership, "join")

    def test_remote_additional_creator_is_refused(self) -> None:
        self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            room_version=ROOM_VERSION.identifier,
            extra_content={
                "creation_content": {
                    EventContentFields.ADDITIONAL_CREATORS: ["@carol:other.example"]
                }
            },
            expect_code=HTTPStatus.BAD_REQUEST,
        )

    def test_trusted_private_chat(self) -> None:
        """The preset that makes invitees creators names their keys."""
        room_id = self._create_room(preset="trusted_private_chat", invite=[self.bob])
        bob_key = self._key(room_id, self.bob)
        create = self.get_success(self.store.get_create_event_for_room(room_id))
        self.assertEqual(
            create.content[EventContentFields.ADDITIONAL_CREATORS], [bob_key]
        )
        self.assertEqual(self._member_event(room_id, bob_key).membership, "invite")
        self._assert_no_user_ids_in_room(room_id)

    def test_create_room_with_remote_invitee_creates_nothing(self) -> None:
        """A remote invitee is refused before the room exists."""
        rooms_before = self.get_success(self.store.get_rooms_for_user(self.alice))
        self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            room_version=ROOM_VERSION.identifier,
            extra_content={"invite": ["@carol:other.example"]},
            expect_code=HTTPStatus.BAD_REQUEST,
        )
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.alice)), rooms_before
        )

    def test_additional_creators_are_named_by_user_id(self) -> None:
        """A client cannot seat a member key it made up as a creator."""
        self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            room_version=ROOM_VERSION.identifier,
            extra_content={
                "creation_content": {
                    EventContentFields.ADDITIONAL_CREATORS: [Member().key]
                }
            },
            expect_code=HTTPStatus.BAD_REQUEST,
        )

    def test_upgrade_into_member_key_room_is_refused(self) -> None:
        legacy_room = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{legacy_room}/upgrade",
            {"new_version": ROOM_VERSION.identifier},
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_UNSUPPORTED_ROOM_VERSION")

    @override_config({"experimental_features": {"msc4430_enabled": False}})
    def test_room_version_needs_the_flag(self) -> None:
        self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            room_version=ROOM_VERSION.identifier,
            expect_code=HTTPStatus.BAD_REQUEST,
        )


class MemberKeyClientViewTestCase(unittest.HomeserverTestCase):
    """What a client is served from a member key room: events whose senders are
    keys with `stable_id` set, a `member_info` map resolving the keys, the
    requester's own events recognised for the transaction ID echo, visibility
    decided by the requester's key, and `/members` and `/joined_members` keyed
    by key. These are the local halves of the Complement tests for the stable
    ID, `member_info` in `/sync`, the member list endpoints, the transaction ID
    echo and the incremental sync after a join."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        account_data.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {}).setdefault(
            "msc4430_enabled", True
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")
        self.room_id = self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            room_version=ROOM_VERSION.identifier,
            extra_content={"preset": "public_chat"},
        )
        self.alice_key = self._key(self.alice)

    def _key(self, user_id: str) -> str:
        local_key = self.get_success(
            self.store.get_local_member_key(self.room_id, user_id)
        )
        assert local_key is not None
        return local_key.member_key

    def _sync(self, tok: str, since: str | None = None, **params: str) -> JsonDict:
        query = {**params}
        if since is not None:
            query["since"] = since
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        channel = self.make_request(
            "GET", f"/sync?{qs}" if qs else "/sync", access_token=tok
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        return channel.json_body

    def _joined_room(self, body: JsonDict) -> JsonDict:
        return body["rooms"]["join"][self.room_id]

    def _stranger_join(self, stranger: Member) -> EventBase:
        """A join by a key this server does not hold, as federation would bring."""
        builder = self.get_success(
            MemberKeyRoom.for_existing_room(self.hs, self.room_id)
        )
        return self.get_success(builder.send_member(stranger, "join"))

    def test_initial_sync(self) -> None:
        """Every event names a key as sender and stable ID; the map resolves
        every member's key to their user ID."""
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        bob_key = self._key(self.bob)
        self.helper.send(self.room_id, body="hello", tok=self.bob_tok)

        room_body = self._joined_room(self._sync(self.alice_tok))
        events = room_body["timeline"]["events"] + room_body["state"]["events"]
        self.assertTrue(events)
        for event in events:
            self.assertTrue(is_member_key(event["sender"]), event)
            self.assertEqual(
                event["unsigned"]["org.matrix.msc4428.stable_id"], event["sender"]
            )
            if event["type"] == EventTypes.Member:
                self.assertTrue(is_member_key(event["state_key"]), event)
        self.assertEqual(
            room_body[MemberInfo.FIELD],
            {
                self.alice_key: {MemberInfo.USER_ID: self.alice},
                bob_key: {MemberInfo.USER_ID: self.bob},
            },
        )
        # The user's own membership is served as state under their key.
        memberships = {
            e["state_key"]: e["content"]["membership"]
            for e in events
            if e["type"] == EventTypes.Member
        }
        self.assertEqual(memberships, {self.alice_key: "join", bob_key: "join"})

    def test_incremental_sync_after_join_and_leave(self) -> None:
        """A room joined after the last sync comes down with its create event,
        and only the new member's entry; leaving puts it under `leave`."""
        since = self._sync(self.bob_tok)["next_batch"]
        self.assertNotIn(
            self.room_id, self._sync(self.bob_tok).get("rooms", {}).get("join", {})
        )

        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        bob_key = self._key(self.bob)
        body = self._sync(self.bob_tok, since=since)
        room_body = self._joined_room(body)
        types = {e["type"] for e in room_body["timeline"]["events"]} | {
            e["type"] for e in room_body["state"]["events"]
        }
        self.assertIn(EventTypes.Create, types)
        self.assertEqual(
            room_body[MemberInfo.FIELD][bob_key], {MemberInfo.USER_ID: self.bob}
        )

        # Alice, already synced, learns only of Bob.
        alice_since = self._sync(self.alice_tok)["next_batch"]
        self.helper.send(self.room_id, body="hi", tok=self.bob_tok)
        room_body = self._joined_room(self._sync(self.alice_tok, since=alice_since))
        self.assertEqual(
            room_body[MemberInfo.FIELD], {bob_key: {MemberInfo.USER_ID: self.bob}}
        )

        since = body["next_batch"]
        self.helper.leave(self.room_id, self.bob, tok=self.bob_tok)
        body = self._sync(self.bob_tok, since=since)
        self.assertIn(self.room_id, body["rooms"]["leave"])
        self.assertNotIn(self.room_id, body["rooms"].get("join", {}))

    def test_transaction_id_echoed_to_the_sending_device_only(self) -> None:
        other_device_tok = self.login("alice", "pass")
        event_id = self.helper.send(
            self.room_id, body="hello", tok=self.alice_tok, txn_id="txn1"
        )["event_id"]

        def find(body: JsonDict) -> JsonDict:
            for event in self._joined_room(body)["timeline"]["events"]:
                if event["event_id"] == event_id:
                    return event
            raise AssertionError("event not in timeline")

        self.assertEqual(
            find(self._sync(self.alice_tok))["unsigned"]["transaction_id"], "txn1"
        )
        self.assertNotIn(
            "transaction_id", find(self._sync(other_device_tok))["unsigned"]
        )
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        self.assertNotIn("transaction_id", find(self._sync(self.bob_tok))["unsigned"])

    def test_members_and_joined_members(self) -> None:
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        bob_key = self._key(self.bob)

        channel = self.make_request(
            "GET", f"/rooms/{self.room_id}/members", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        self.assertEqual(
            {e["state_key"] for e in channel.json_body["chunk"]},
            {self.alice_key, bob_key},
        )
        self.assertEqual(
            channel.json_body[MemberInfo.FIELD],
            {
                self.alice_key: {MemberInfo.USER_ID: self.alice},
                bob_key: {MemberInfo.USER_ID: self.bob},
            },
        )

        channel = self.make_request(
            "GET", f"/rooms/{self.room_id}/joined_members", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        joined = channel.json_body["joined"]
        self.assertEqual(set(joined), {self.alice_key, bob_key})
        self.assertEqual(joined[bob_key][MemberInfo.USER_ID], self.bob)
        self.assertEqual(joined[bob_key]["display_name"], "bob")

    def test_event_and_messages_are_visible_to_members_only(self) -> None:
        """Visibility is decided by the requester's key: with `joined` history,
        a later joiner sees nothing from before their join."""
        self.helper.send_state(
            self.room_id,
            EventTypes.RoomHistoryVisibility,
            {"history_visibility": "joined"},
            tok=self.alice_tok,
        )
        before = self.helper.send(self.room_id, body="before", tok=self.alice_tok)[
            "event_id"
        ]
        self.helper.get_event(self.room_id, before, tok=self.alice_tok)
        self.helper.get_event(
            self.room_id, before, tok=self.bob_tok, expect_code=HTTPStatus.NOT_FOUND
        )

        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        after = self.helper.send(self.room_id, body="after", tok=self.alice_tok)[
            "event_id"
        ]
        self.helper.get_event(
            self.room_id, before, tok=self.bob_tok, expect_code=HTTPStatus.NOT_FOUND
        )
        self.helper.get_event(self.room_id, after, tok=self.bob_tok)

        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/messages?dir=b",
            access_token=self.bob_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        bodies = [
            e["content"]["body"]
            for e in channel.json_body["chunk"]
            if e["type"] == EventTypes.Message
        ]
        self.assertEqual(bodies, ["after"])

    def test_member_info_for_erased_and_unknown_members(self) -> None:
        """An erased member maps to null, as does a key nobody vouches for; a
        verified link names the user it claims."""
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        bob_key = self._key(self.bob)
        self.get_success(self.store.mark_user_erased(self.bob))

        stranger, linked = Member(), Member()
        self._stranger_join(stranger)
        self._stranger_join(linked)
        self.get_success(
            self.store.upsert_member_link(
                MemberLink(
                    room_id=self.room_id,
                    member_key=linked.key,
                    domain="other.example",
                    localpart="carol",
                    verified=True,
                    event_id="$verified",
                    key_id="ed25519:x",
                )
            )
        )

        channel = self.make_request(
            "GET", f"/rooms/{self.room_id}/members", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        self.assertEqual(
            channel.json_body[MemberInfo.FIELD],
            {
                self.alice_key: {MemberInfo.USER_ID: self.alice},
                bob_key: {MemberInfo.USER_ID: None},
                stranger.key: {MemberInfo.USER_ID: None},
                linked.key: {MemberInfo.USER_ID: "@carol:other.example"},
            },
        )

    def _ignore(self, tok: str, user_id: str, *ignored: str) -> None:
        channel = self.make_request(
            "PUT",
            f"/user/{user_id}/account_data/m.ignored_user_list",
            {"ignored_users": {i: {} for i in ignored}},
            access_token=tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

    def test_profile_change_is_not_a_rejoin(self) -> None:
        """Changing one's own membership (a display name) is an ordinary
        timeline event on the next sync, not the room sent again as newly
        joined; and the room's own leave is a leave, not a kick."""
        since = self._sync(self.alice_tok)["next_batch"]
        self.helper.send_state(
            self.room_id,
            EventTypes.Member,
            {"membership": Membership.JOIN, "displayname": "Alice!"},
            tok=self.alice_tok,
            state_key=self.alice,
        )
        room_body = self._joined_room(self._sync(self.alice_tok, since=since))
        self.assertEqual(
            [e["type"] for e in room_body["timeline"]["events"]], [EventTypes.Member]
        )
        self.assertFalse(room_body["timeline"]["limited"])
        self.assertEqual(room_body["state"]["events"], [])

    def test_device_lists_and_presence_name_users(self) -> None:
        """Device list changes and presence are about users, not keys."""
        since = self._sync(self.alice_tok)["next_batch"]
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        body = self._sync(self.alice_tok, since=since)
        self.assertEqual(body["device_lists"]["changed"], [self.bob])
        self.assertEqual({e["sender"] for e in body["presence"]["events"]}, {self.bob})

        since = body["next_batch"]
        self.helper.leave(self.room_id, self.bob, tok=self.bob_tok)
        body = self._sync(self.alice_tok, since=since)
        self.assertEqual(body["device_lists"]["left"], [self.bob])

    def test_erasure_changes_the_keys_entry(self) -> None:
        """Erasure is delivered against the member's key, the stable ID in
        these rooms, not against their user ID."""
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        bob_key = self._key(self.bob)
        since = self._sync(self.alice_tok)["next_batch"]

        self.get_success(self.store.mark_user_erased(self.bob))
        self.get_success(self.hs.get_member_info_handler().on_user_erased(self.bob))
        room_body = self._joined_room(self._sync(self.alice_tok, since=since))
        self.assertEqual(
            room_body[MemberInfo.FIELD], {bob_key: {MemberInfo.USER_ID: None}}
        )

    def test_ignored_senders_by_user_id_and_by_key(self) -> None:
        """A member may be ignored as the user behind their key or as the key
        itself; either way their events and notifications stop."""
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        alice_key = self.alice_key

        for ignored in (self.alice, alice_key):
            self._ignore(self.bob_tok, self.bob, ignored)
            since = self._sync(self.bob_tok)["next_batch"]
            self.helper.send(self.room_id, body="ignored?", tok=self.alice_tok)
            body = self._sync(self.bob_tok, since=since)
            room_body = body.get("rooms", {}).get("join", {}).get(self.room_id, {})
            self.assertEqual(room_body.get("timeline", {}).get("events", []), [])
            self.assertEqual(
                room_body.get("unread_notifications", {}).get("notification_count", 0),
                0,
            )
            self._ignore(self.bob_tok, self.bob)

    def test_invite_from_ignored_user_is_hidden(self) -> None:
        self._ignore(self.bob_tok, self.bob, self.alice)
        self.helper.invite(
            self.room_id, src=self.alice, targ=self.bob, tok=self.alice_tok
        )
        body = self._sync(self.bob_tok)
        self.assertNotIn(self.room_id, body.get("rooms", {}).get("invite", {}))

    def test_own_events_are_not_notified(self) -> None:
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        since = self._sync(self.alice_tok)["next_batch"]
        self.helper.send(self.room_id, body="mine", tok=self.alice_tok)
        room_body = self._joined_room(self._sync(self.alice_tok, since=since))
        self.assertEqual(room_body["unread_notifications"]["notification_count"], 0)

    def test_heroes_exclude_the_requester(self) -> None:
        """A nameless room is summarised by the other members' keys (the
        summary is sent to clients that lazy-load members)."""
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        bob_key = self._key(self.bob)
        lazy = urllib.parse.quote(
            json.dumps({"room": {"state": {"lazy_load_members": True}}})
        )
        summary = self._joined_room(self._sync(self.alice_tok, filter=lazy))["summary"]
        self.assertEqual(summary["m.heroes"], [bob_key])
        summary = self._joined_room(self._sync(self.bob_tok, filter=lazy))["summary"]
        self.assertEqual(summary["m.heroes"], [self.alice_key])

    def test_notification_count(self) -> None:
        """Push evaluation decides who can see the event by their key."""
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        since = self._sync(self.bob_tok)["next_batch"]
        self.helper.send(self.room_id, body="ping", tok=self.alice_tok)
        room_body = self._joined_room(self._sync(self.bob_tok, since=since))
        self.assertEqual(room_body["unread_notifications"]["notification_count"], 1)


class MemberKeySlidingSyncTestCase(SlidingSyncBase):
    """Sliding sync in a member key room: `$ME` is the requester's key, the room
    list follows joins and leaves, and `member_info` resolves the keys."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        account_data.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"]["msc4430_enabled"] = True
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.store = hs.get_datastores().main
        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")
        self.room_id = self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            room_version=ROOM_VERSION.identifier,
            extra_content={"preset": "public_chat"},
        )
        self.sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [[EventTypes.Member, "$ME"]],
                    "timeline_limit": 2,
                }
            }
        }

    def _key(self, user_id: str) -> str:
        local_key = self.get_success(
            self.store.get_local_member_key(self.room_id, user_id)
        )
        assert local_key is not None
        return local_key.member_key

    def test_invite_from_ignored_user_is_hidden(self) -> None:
        channel = self.make_request(
            "PUT",
            f"/user/{self.bob}/account_data/m.ignored_user_list",
            {"ignored_users": {self.alice: {}}},
            access_token=self.bob_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        self.helper.invite(
            self.room_id, src=self.alice, targ=self.bob, tok=self.alice_tok
        )
        body, _ = self.do_sync(self.sync_body, tok=self.bob_tok)
        self.assertNotIn(self.room_id, body["rooms"])

    def test_me_join_and_leave(self) -> None:
        body, pos = self.do_sync(self.sync_body, tok=self.bob_tok)
        self.assertNotIn(self.room_id, body["rooms"])

        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        bob_key = self._key(self.bob)
        body, pos = self.do_sync(self.sync_body, since=pos, tok=self.bob_tok)
        room_body = body["rooms"][self.room_id]
        self.assertTrue(room_body["initial"])
        # `$ME` is Bob's key: his own membership, and nobody else's.
        self.assertEqual(
            [(e["type"], e["state_key"]) for e in room_body["required_state"]],
            [(EventTypes.Member, bob_key)],
        )
        self.assertEqual(
            room_body[MemberInfo.FIELD][bob_key], {MemberInfo.USER_ID: self.bob}
        )

        # Leaving on one's own removes the room from the list (it is not a kick).
        self.helper.leave(self.room_id, self.bob, tok=self.bob_tok)
        body, pos = self.do_sync(self.sync_body, since=pos, tok=self.bob_tok)
        self.assertIn(self.room_id, body["rooms"])  # newly left: delivered once
        body, pos = self.do_sync(self.sync_body, since=pos, tok=self.bob_tok)
        self.assertNotIn(self.room_id, body["rooms"])
