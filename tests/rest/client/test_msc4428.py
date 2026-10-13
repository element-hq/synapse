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
"""Tests for MSC4428 (stable identifiers for room members): the
`unsigned.org.matrix.msc4428.stable_id` field on events served to clients, and
the per-room `member_info` map.

These stand in for the Complement tests of COMPLEMENT.md §2 (C#2, C#3, C#4 and
C#14) until that suite exists.
"""

import json
from typing import Iterable

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import (
    EventTypes,
    EventUnsignedContentFields,
    MemberInfo,
    RelationTypes,
)
from synapse.rest import admin
from synapse.rest.client import login, relations, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, create_requester
from synapse.util.clock import Clock

from tests import unittest
from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

STABLE_ID = EventUnsignedContentFields.MSC4428_STABLE_ID


class StableIdTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        relations.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4428_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")

        # A legacy room: MSC4428 applies to every room version.
        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)

        # A message with an edit and a thread reply, so that bundled
        # aggregations embed events sent by someone else.
        self.parent_id = self.helper.send(self.room_id, body="Hi!", tok=self.alice_tok)[
            "event_id"
        ]
        self.edit_id = self.helper.send_event(
            self.room_id,
            EventTypes.Message,
            {
                "msgtype": "m.text",
                "body": "* Hello!",
                "m.new_content": {"msgtype": "m.text", "body": "Hello!"},
                "m.relates_to": {
                    "rel_type": RelationTypes.REPLACE,
                    "event_id": self.parent_id,
                },
            },
            tok=self.alice_tok,
        )["event_id"]
        self.reply_id = self.helper.send_event(
            self.room_id,
            EventTypes.Message,
            {
                "msgtype": "m.text",
                "body": "reply",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": self.parent_id,
                },
            },
            tok=self.bob_tok,
        )["event_id"]

        # A message redacted by someone other than its sender, so that the
        # embedded `redacted_because` has a different stable ID to its parent.
        self.redacted_id = self.helper.send(
            self.room_id, body="oops", tok=self.bob_tok
        )["event_id"]
        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id}/redact/{self.redacted_id}",
            {},
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.redaction_id = channel.json_body["event_id"]

    def assert_stable_ids(self, events: Iterable[JsonDict]) -> list[str]:
        """Assert every event, and every event embedded in its `unsigned`,
        carries `stable_id == sender`. Returns the event IDs checked."""
        checked = []
        for event in events:
            checked.extend(self._assert_stable_id(event))
        return checked

    def _assert_stable_id(self, event: JsonDict) -> list[str]:
        self.assertEqual(
            event.get("unsigned", {}).get(STABLE_ID),
            event["sender"],
            f"stable_id missing or wrong on {event.get('event_id')}: {event}",
        )
        checked = [event["event_id"]]

        unsigned = event.get("unsigned", {})
        if "redacted_because" in unsigned:
            checked.extend(self._assert_stable_id(unsigned["redacted_because"]))
        bundled = unsigned.get("m.relations", {})
        if RelationTypes.REPLACE in bundled:
            checked.extend(self._assert_stable_id(bundled[RelationTypes.REPLACE]))
        if RelationTypes.THREAD in bundled:
            checked.extend(
                self._assert_stable_id(bundled[RelationTypes.THREAD]["latest_event"])
            )
        return checked

    def test_sync(self) -> None:
        # Limit the timeline so that the state block is populated too.
        sync_filter = json.dumps({"room": {"timeline": {"limit": 3}}})
        channel = self.make_request(
            "GET", f"/sync?filter={sync_filter}", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        room = channel.json_body["rooms"]["join"][self.room_id]

        self.assertGreater(len(room["state"]["events"]), 0)
        self.assert_stable_ids(room["state"]["events"])
        checked = self.assert_stable_ids(room["timeline"]["events"])
        # The redacted message, with its embedded redaction, is in the timeline.
        self.assertIn(self.redacted_id, checked)
        self.assertIn(self.redaction_id, checked)

    def test_messages(self) -> None:
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/messages?dir=b",
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        checked = self.assert_stable_ids(channel.json_body["chunk"])
        # The whole room, with the embedded redaction and edit.
        for event_id in (
            self.parent_id,
            self.edit_id,
            self.reply_id,
            self.redacted_id,
            self.redaction_id,
        ):
            self.assertIn(event_id, checked)

    def test_context(self) -> None:
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/context/{self.parent_id}",
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        body = channel.json_body
        self.assert_stable_ids([body["event"]])
        self.assert_stable_ids(body["events_before"])
        self.assert_stable_ids(body["events_after"])
        self.assertGreater(len(body["state"]), 0)
        self.assert_stable_ids(body["state"])

    def test_event(self) -> None:
        for event_id in (self.parent_id, self.redacted_id):
            channel = self.make_request(
                "GET",
                f"/rooms/{self.room_id}/event/{event_id}",
                access_token=self.alice_tok,
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assert_stable_ids([channel.json_body])

        # The bundled edit and thread summary on the parent are events sent by
        # alice and bob respectively; both carry their own sender.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/event/{self.parent_id}",
            access_token=self.alice_tok,
        )
        bundled = channel.json_body["unsigned"]["m.relations"]
        self.assertEqual(
            bundled[RelationTypes.REPLACE]["unsigned"][STABLE_ID], self.alice
        )
        self.assertEqual(
            bundled[RelationTypes.THREAD]["latest_event"]["unsigned"][STABLE_ID],
            self.bob,
        )

    def test_state(self) -> None:
        channel = self.make_request(
            "GET", f"/rooms/{self.room_id}/state", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, 200, channel.text_body)
        self.assertGreater(len(channel.json_list), 0)
        self.assert_stable_ids(channel.json_list)

    def test_relations(self) -> None:
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room_id}/relations/{self.parent_id}",
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        checked = self.assert_stable_ids(channel.json_body["chunk"])
        self.assertIn(self.edit_id, checked)
        self.assertIn(self.reply_id, checked)

    def test_threads(self) -> None:
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room_id}/threads",
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        checked = self.assert_stable_ids(channel.json_body["chunk"])
        self.assertIn(self.parent_id, checked)
        # The thread summary embeds bob's reply.
        self.assertIn(self.reply_id, checked)


class StableIdDisabledTestCase(unittest.HomeserverTestCase):
    """Without the flag nothing changes on the wire."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def test_no_stable_id(self) -> None:
        alice = self.register_user("alice", "pass")
        alice_tok = self.login("alice", "pass")
        room_id = self.helper.create_room_as(alice, tok=alice_tok)
        event_id = self.helper.send(room_id, body="Hi!", tok=alice_tok)["event_id"]

        channel = self.make_request(
            "GET", f"/rooms/{room_id}/event/{event_id}", access_token=alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertNotIn(STABLE_ID, channel.json_body["unsigned"])

        channel = self.make_request("GET", "/sync", access_token=alice_tok)
        self.assertEqual(channel.code, 200, channel.json_body)
        for event in channel.json_body["rooms"]["join"][room_id]["timeline"]["events"]:
            self.assertNotIn(STABLE_ID, event.get("unsigned", {}))


class MemberInfoTestCase(unittest.HomeserverTestCase):
    """The `member_info` map: stands in for Complement tests C#3, C#4 and C#14
    (COMPLEMENT.md §2) until that suite exists. In today's rooms stable IDs equal
    user IDs, so the map only ever names erased users."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4428_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")

        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        self.helper.send(self.room_id, body="hi", tok=self.bob_tok)

    def _erase(self, user_id: str) -> None:
        """Mark a user erased without deactivating them, so they stay joined."""
        self.get_success(self.store.mark_user_erased(user_id))

    def _sync(self, since: str | None = None, **params: str) -> JsonDict:
        query = {**params}
        if since is not None:
            query["since"] = since
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        channel = self.make_request(
            "GET", f"/sync?{qs}" if qs else "/sync", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def test_sync_omits_map_when_nothing_to_say(self) -> None:
        room = self._sync()["rooms"]["join"][self.room_id]
        self.assertNotIn(MemberInfo.FIELD, room)

    def test_sync_full_map_names_erased_member(self) -> None:
        """C#14: erasure is visible on the next full sync."""
        self._erase(self.bob)

        room = self._sync()["rooms"]["join"][self.room_id]
        self.assertEqual(room[MemberInfo.FIELD], {self.bob: {MemberInfo.USER_ID: None}})

        # Likewise on a `full_state` incremental sync.
        since = self._sync()["next_batch"]
        room = self._sync(since, full_state="true")["rooms"]["join"][self.room_id]
        self.assertEqual(room[MemberInfo.FIELD], {self.bob: {MemberInfo.USER_ID: None}})

    def test_sync_incremental_only_for_mentioned_members(self) -> None:
        """An incremental sync carries entries only for the stable IDs the
        response mentions. A change with no event to anchor it (the erasure
        itself) waits for the next event from, or about, that member."""
        since = self._sync()["next_batch"]
        self._erase(self.bob)

        # Alice's own message mentions nobody but Alice.
        self.helper.send(self.room_id, body="alice", tok=self.alice_tok)
        body = self._sync(since)
        room = body["rooms"]["join"][self.room_id]
        self.assertNotIn(MemberInfo.FIELD, room)
        since = body["next_batch"]

        # Bob's next message is the anchor.
        self.helper.send(self.room_id, body="bob", tok=self.bob_tok)
        room = self._sync(since)["rooms"]["join"][self.room_id]
        self.assertEqual(room[MemberInfo.FIELD], {self.bob: {MemberInfo.USER_ID: None}})

    def test_sync_deactivation_leave_anchors_erasure(self) -> None:
        """Deactivating with `erase: true` parts the user from their rooms;
        the parting leave anchors the `null` entry for other members."""
        since = self._sync()["next_batch"]

        self.get_success(
            self.hs.get_deactivate_account_handler().deactivate_account(
                self.bob, erase_data=True, requester=create_requester(self.bob)
            )
        )
        # Parting rooms happens in the background.
        self.pump()
        self.reactor.advance(1)
        self.pump()

        room = self._sync(since)["rooms"]["join"][self.room_id]
        leave = [
            e
            for e in room["timeline"]["events"]
            if e["type"] == EventTypes.Member and e["state_key"] == self.bob
        ]
        self.assertEqual(len(leave), 1, room["timeline"])
        self.assertEqual(leave[0]["content"]["membership"], "leave")
        self.assertEqual(room[MemberInfo.FIELD], {self.bob: {MemberInfo.USER_ID: None}})

    def test_sync_left_room(self) -> None:
        """The map is also present on rooms under `rooms.leave`."""
        since = self._sync()["next_batch"]
        self._erase(self.bob)
        self.helper.send(self.room_id, body="bob", tok=self.bob_tok)
        self.helper.leave(self.room_id, self.alice, tok=self.alice_tok)

        room = self._sync(since)["rooms"]["leave"][self.room_id]
        self.assertEqual(room[MemberInfo.FIELD], {self.bob: {MemberInfo.USER_ID: None}})

    def test_members(self) -> None:
        """C#4: `/members` always carries the map."""
        channel = self.make_request(
            "GET", f"/rooms/{self.room_id}/members", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body[MemberInfo.FIELD], {})

        self._erase(self.bob)
        channel = self.make_request(
            "GET", f"/rooms/{self.room_id}/members", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            channel.json_body[MemberInfo.FIELD], {self.bob: {MemberInfo.USER_ID: None}}
        )

        # The map only covers the members in the (filtered) chunk.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/members?not_membership=join",
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["chunk"], [])
        self.assertEqual(channel.json_body[MemberInfo.FIELD], {})

    def test_joined_members(self) -> None:
        """C#4: `/joined_members` is keyed by stable ID and names the user ID."""
        channel = self.make_request(
            "GET", f"/rooms/{self.room_id}/joined_members", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        joined = channel.json_body["joined"]
        self.assertEqual(joined[self.alice][MemberInfo.USER_ID], self.alice)
        self.assertEqual(joined[self.bob][MemberInfo.USER_ID], self.bob)
        self.assertEqual(joined[self.bob]["display_name"], "bob")

        self._erase(self.bob)
        channel = self.make_request(
            "GET", f"/rooms/{self.room_id}/joined_members", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        joined = channel.json_body["joined"]
        self.assertEqual(joined[self.alice][MemberInfo.USER_ID], self.alice)
        self.assertIsNone(joined[self.bob][MemberInfo.USER_ID])


class MemberInfoSlidingSyncTestCase(SlidingSyncBase):
    """C#3 for sliding sync: `rooms.<id>.member_info`."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"]["msc4428_enabled"] = True
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.store = hs.get_datastores().main
        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")

        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)
        self.helper.send(self.room_id, body="hi", tok=self.bob_tok)

        self.sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [[EventTypes.Member, "*"]],
                    "timeline_limit": 2,
                }
            }
        }

    def test_member_info(self) -> None:
        body, pos = self.do_sync(self.sync_body, tok=self.alice_tok)
        self.assertTrue(body["rooms"][self.room_id]["initial"])
        self.assertNotIn(MemberInfo.FIELD, body["rooms"][self.room_id])

        self.get_success(self.store.mark_user_erased(self.bob))

        # Incrementally: only once Bob is mentioned again.
        self.helper.send(self.room_id, body="alice", tok=self.alice_tok)
        body, pos = self.do_sync(self.sync_body, since=pos, tok=self.alice_tok)
        self.assertNotIn(MemberInfo.FIELD, body["rooms"][self.room_id])

        self.helper.send(self.room_id, body="bob", tok=self.bob_tok)
        body, pos = self.do_sync(self.sync_body, since=pos, tok=self.alice_tok)
        self.assertEqual(
            body["rooms"][self.room_id][MemberInfo.FIELD],
            {self.bob: {MemberInfo.USER_ID: None}},
        )

        # A fresh connection: the initial response mentions every member.
        body, _ = self.do_sync(self.sync_body, tok=self.alice_tok)
        self.assertTrue(body["rooms"][self.room_id]["initial"])
        self.assertEqual(
            body["rooms"][self.room_id][MemberInfo.FIELD],
            {self.bob: {MemberInfo.USER_ID: None}},
        )


class MemberInfoDisabledTestCase(unittest.HomeserverTestCase):
    """Without the flag nothing changes on the wire, even with an erased member."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def test_no_member_info(self) -> None:
        alice = self.register_user("alice", "pass")
        alice_tok = self.login("alice", "pass")
        bob = self.register_user("bob", "pass")
        bob_tok = self.login("bob", "pass")
        room_id = self.helper.create_room_as(alice, tok=alice_tok)
        self.helper.join(room_id, bob, tok=bob_tok)
        self.get_success(self.hs.get_datastores().main.mark_user_erased(bob))

        channel = self.make_request("GET", "/sync", access_token=alice_tok)
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertNotIn(MemberInfo.FIELD, channel.json_body["rooms"]["join"][room_id])

        channel = self.make_request(
            "GET", f"/rooms/{room_id}/members", access_token=alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(set(channel.json_body.keys()), {"chunk"})

        channel = self.make_request(
            "GET", f"/rooms/{room_id}/joined_members", access_token=alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            set(channel.json_body["joined"][bob].keys()),
            {"avatar_url", "display_name"},
        )
