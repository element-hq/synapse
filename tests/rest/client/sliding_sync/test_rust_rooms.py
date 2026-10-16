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

"""The rooms of a sliding sync response computed and encoded in Rust
(`rust/src/sliding_sync/room_entry.rs`, `encode.rs`) against Python's.

Each request goes to two connections of the same user, one served by Python
and one by Rust, and the `rooms` of the two responses must be equal.
"""

from typing import Any
from unittest import SkipTest

from parameterized import parameterized_class

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes, JoinRules, RelationTypes
from synapse.events import EventBase, StrippedStateEvent
from synapse.handlers.sliding_sync import rust_sliding_sync_rooms_counter
from synapse.metrics import SERVER_NAME_LABEL
from synapse.rest.client import knock, login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import _BlockingRustCalls


def _without_age(value: Any) -> Any:
    """`value` without the `age` of each event's unsigned data, which moves on
    with the clock between the two requests."""
    if isinstance(value, dict):
        return {
            key: _without_age(item)
            for key, item in value.items()
            if not (key == "age" and isinstance(item, int))
        }
    if isinstance(value, list):
        return [_without_age(item) for item in value]
    return value


def _normalized(rooms: JsonDict) -> JsonDict:
    """`rooms` without the ages, and with each room's `required_state` in
    order: it is a set, which neither side orders."""
    rooms = _without_age(rooms)
    for entry in rooms.values():
        if "required_state" in entry:
            entry["required_state"].sort(key=lambda e: (e["type"], e["state_key"]))
    return rooms


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
class SlidingSyncRustRoomsTestCase(SlidingSyncBase):
    maxDiff = None

    servlets = [
        synapse.rest.admin.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        rust = hs.get_rust_handlers().sliding_sync
        if rust is None:
            raise SkipTest("needs the native Rust Postgres driver")
        self.rust = _BlockingRustCalls(rust)
        self.handler = hs.get_sliding_sync_handler()
        self.handler._rust_sliding_sync_strict = True
        self.pos: dict[str, str | None] = {"python": None, "rust": None}

        self.user1_id = self.register_user("user1", "pass")
        self.user1_tok = self.login(self.user1_id, "pass")
        self.user2_id = self.register_user("user2", "pass")
        self.user2_tok = self.login(self.user2_id, "pass")
        self.user3_id = self.register_user("user3", "pass")
        self.user3_tok = self.login(self.user3_id, "pass")

    def _rust_rooms_computed(self) -> float:
        return rust_sliding_sync_rooms_counter.labels(
            result="rust", **{SERVER_NAME_LABEL: self.hs.hostname}
        )._value.get()

    def _sync_both(self, sync_body: JsonDict) -> JsonDict:
        """Sync `sync_body` on the Python and the Rust connection, assert the
        two `rooms` are equal, and return Rust's response."""
        responses = {}
        for side in ("python", "rust"):
            self.handler._rust_sliding_sync = (
                self.rust if side == "rust" else None  # type: ignore[assignment]
            )
            before = self._rust_rooms_computed()
            response, self.pos[side] = self.do_sync(
                {**sync_body, "conn_id": side},
                since=self.pos[side],
                tok=self.user1_tok,
            )
            computed = self._rust_rooms_computed() - before
            if side == "rust":
                self.assertGreaterEqual(computed, len(response["rooms"]))
            else:
                self.assertEqual(computed, 0)
            responses[side] = response
        python_rooms = _normalized(responses["python"]["rooms"])
        rust_rooms = _normalized(responses["rust"]["rooms"])
        self.assertEqual(python_rooms.keys(), rust_rooms.keys())
        for room_id, python_room in python_rooms.items():
            self.assertEqual(python_room, rust_rooms[room_id], room_id)
        self.assertEqual(
            responses["python"].get("lists"), responses["rust"].get("lists")
        )
        return responses["rust"]

    def _named_room(self) -> str:
        """A room with a name, an avatar, a topic and a busy timeline: an edit,
        a thread, a redaction."""
        room_id = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.send_state(
            room_id, EventTypes.Name, {"name": "named"}, tok=self.user2_tok
        )
        self.helper.send_state(
            room_id,
            EventTypes.RoomAvatar,
            {"url": "mxc://test/avatar"},
            tok=self.user2_tok,
        )
        self.helper.send_state(
            room_id, EventTypes.Topic, {"topic": "a topic"}, tok=self.user2_tok
        )
        self.helper.join(room_id, self.user1_id, tok=self.user1_tok)
        root = self.helper.send(room_id, "root", tok=self.user2_tok)["event_id"]
        self.helper.send_event(
            room_id,
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": "* edited",
                "m.new_content": {"msgtype": "m.text", "body": "edited"},
                "m.relates_to": {"rel_type": RelationTypes.REPLACE, "event_id": root},
            },
            tok=self.user2_tok,
        )
        doomed = self.helper.send(room_id, "doomed", tok=self.user1_tok)["event_id"]
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id}/redact/{doomed}/txn1",
            content={"reason": "test"},
            access_token=self.user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.helper.send_event(
            room_id,
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": "in thread",
                "m.relates_to": {"rel_type": RelationTypes.THREAD, "event_id": root},
            },
            tok=self.user1_tok,
        )
        self.helper.send(room_id, "last", tok=self.user2_tok)
        return room_id

    def _rooms(self) -> dict[str, str]:
        """One room of each kind the room computation tells apart."""
        rooms = {"named": self._named_room()}

        # No name: heroes, with their display names and avatars.
        unnamed = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.join(unnamed, self.user3_id, tok=self.user3_tok)
        self.helper.send_state(
            unnamed,
            EventTypes.Member,
            {"membership": "join", "displayname": "Three", "avatar_url": "mxc://a/b"},
            state_key=self.user3_id,
            tok=self.user3_tok,
        )
        self.helper.join(unnamed, self.user1_id, tok=self.user1_tok)
        self.helper.send(unnamed, "hello", tok=self.user3_tok)
        rooms["unnamed"] = unnamed

        # An empty name counts as none.
        empty_name = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.send_state(
            empty_name, EventTypes.Name, {"name": ""}, tok=self.user2_tok
        )
        self.helper.join(empty_name, self.user1_id, tok=self.user1_tok)
        rooms["empty_name"] = empty_name

        invited = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.invite(invited, self.user2_id, self.user1_id, tok=self.user2_tok)
        rooms["invited"] = invited

        knocked = self.helper.create_room_as(
            self.user2_id, tok=self.user2_tok, room_version="7"
        )
        self.helper.send_state(
            knocked,
            EventTypes.JoinRules,
            {"join_rule": JoinRules.KNOCK},
            tok=self.user2_tok,
        )
        self.helper.knock(knocked, self.user1_id, tok=self.user1_tok)
        rooms["knocked"] = knocked

        left = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.join(left, self.user1_id, tok=self.user1_tok)
        self.helper.send(left, "before leaving", tok=self.user2_tok)
        self.helper.leave(left, self.user1_id, tok=self.user1_tok)
        self.helper.send(left, "after leaving", tok=self.user2_tok)
        rooms["left"] = left

        banned = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.join(banned, self.user1_id, tok=self.user1_tok)
        self.helper.ban(banned, self.user2_id, self.user1_id, tok=self.user2_tok)
        rooms["banned"] = banned

        rooms["dm"] = self._create_dm_room(
            inviter_user_id=self.user2_id,
            inviter_tok=self.user2_tok,
            invitee_user_id=self.user1_id,
            invitee_tok=self.user1_tok,
        )

        remote_invite, _ = self._create_remote_invite_room_for_user(
            self.user1_id,
            [
                StrippedStateEvent(
                    type=EventTypes.Name,
                    state_key="",
                    sender="@inviter:remote_server",
                    content={"name": "remote"},
                )
            ],
        )
        rooms["remote_invite"] = remote_invite
        return rooms

    def _list(self, timeline_limit: int, required_state: list[list[str]]) -> JsonDict:
        return {
            "lists": {
                "all": {
                    "ranges": [[0, 99]],
                    "required_state": required_state,
                    "timeline_limit": timeline_limit,
                }
            }
        }

    def test_initial(self) -> None:
        """Initial requests with several configs."""
        rooms = self._rooms()
        bodies = [
            self._list(
                2,
                [
                    [EventTypes.Name, ""],
                    [EventTypes.Member, "$LAZY"],
                    [EventTypes.Member, "$ME"],
                ],
            ),
            self._list(20, [["*", "*"]]),
            self._list(0, []),
            # Back to the thread root, so that it has bundled aggregations.
            self._list(6, [[EventTypes.Member, "*"], [EventTypes.Topic, ""]]),
        ]
        for body in bodies:
            self.pos = {"python": None, "rust": None}
            response = self._sync_both(body)
            entries = response["rooms"]
            for kind in ("named", "unnamed", "invited", "knocked", "dm"):
                self.assertIn(rooms[kind], entries)
            self.assertIn("heroes", entries[rooms["unnamed"]])
            self.assertIn("invite_state", entries[rooms["invited"]])
            self.assertIn("invite_state", entries[rooms["remote_invite"]])

        # The last request covered the bundles and the redaction.
        timeline = entries[rooms["named"]]["timeline"]
        self.assertTrue(entries[rooms["named"]]["limited"])
        self.assertTrue(any("m.relations" in e["unsigned"] for e in timeline))
        self.assertTrue(any("redacted_because" in e["unsigned"] for e in timeline))

    def test_incremental(self) -> None:
        """Incremental requests: new events, state changes, memberships, and a
        changed config."""
        rooms = self._rooms()
        body = self._list(
            3,
            [
                [EventTypes.Name, ""],
                [EventTypes.Member, "$LAZY"],
            ],
        )
        self._sync_both(body)

        # Nothing happened.
        response = self._sync_both(body)
        self.assertEqual(response["rooms"], {})

        self.helper.send(rooms["named"], "new message", tok=self.user2_tok)
        self.helper.send_state(
            rooms["unnamed"], EventTypes.Name, {"name": "now named"}, tok=self.user2_tok
        )
        self.helper.leave(rooms["dm"], self.user2_id, tok=self.user2_tok)
        self.helper.join(rooms["named"], self.user3_id, tok=self.user3_tok)
        self.helper.send(rooms["named"], "from three", tok=self.user3_tok)
        self.helper.join(rooms["invited"], self.user1_id, tok=self.user1_tok)
        self.helper.leave(rooms["empty_name"], self.user1_id, tok=self.user1_tok)
        response = self._sync_both(body)
        self.assertIn(rooms["named"], response["rooms"])

        # The client asks for more: more state, and a longer timeline.
        body = self._list(
            10,
            [
                [EventTypes.Name, ""],
                [EventTypes.Topic, ""],
                [EventTypes.Member, "$LAZY"],
                [EventTypes.Member, self.user2_id],
            ],
        )
        self._sync_both(body)

        # And back to lazy members only, after more traffic.
        self.helper.send(rooms["named"], "more", tok=self.user1_tok)
        self.helper.send(rooms["unnamed"], "more", tok=self.user3_tok)
        self._sync_both(self._list(1, [[EventTypes.Member, "$LAZY"]]))

    def test_room_subscription(self) -> None:
        """A room subscription on top of a list, in a room the list leaves out."""
        rooms = self._rooms()
        body: JsonDict = {
            **self._list(1, [[EventTypes.Name, ""]]),
            "room_subscriptions": {
                rooms["named"]: {
                    "required_state": [["*", "*"]],
                    "timeline_limit": 4,
                }
            },
        }
        body["lists"]["all"]["ranges"] = [[0, 0]]
        self._sync_both(body)
        self.helper.send(rooms["named"], "new", tok=self.user2_tok)
        self._sync_both(body)

    def test_unsigned_callbacks(self) -> None:
        """Module callbacks add to the unsigned data of Rust rooms' events."""

        async def add_extra_fields(event: EventBase) -> JsonDict:
            return {"io.test.id": event.event_id}

        self.hs.get_event_client_serializer().register_add_extra_fields_to_unsigned_client_event_callback(
            add_extra_fields
        )
        self._named_room()
        response = self._sync_both(self._list(3, [["*", "*"]]))
        (room_entry,) = response["rooms"].values()
        for event in room_entry["timeline"] + room_entry["required_state"]:
            self.assertEqual(event["unsigned"]["io.test.id"], event["event_id"])
