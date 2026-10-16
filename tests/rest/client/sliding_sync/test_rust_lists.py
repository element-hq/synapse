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

"""The room lists, room subscriptions and connection state of a sliding sync
request in Rust (`rust/src/sliding_sync/room_lists/interested.rs`,
`state.rs`) against Python's.

Each request goes to two connections of the same user, one served by Python
and one by Rust. The two responses must be equal but for `pos`, and so must
the changes each side makes to its connection's state: the room configs, the
lazy-loaded members, and the status of each room in the rooms, receipts and
account data streams.
"""

from typing import Any, Mapping
from unittest import SkipTest

from immutabledict import immutabledict

from twisted.internet.defer import Deferred
from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import AccountDataTypes, EventTypes, RoomTypes
from synapse.handlers.sliding_sync import rust_sliding_sync_requests_counter
from synapse.metrics import SERVER_NAME_LABEL
from synapse.rest.client import account_data, knock, login, receipts, room, sync, tags
from synapse.server import HomeServer
from synapse.synapse_rust.sliding_sync import (
    ConnectionState as RustConnectionState,
    InterestedRooms as RustInterestedRooms,
    SlidingSyncHandler as RustSlidingSyncHandler,
    SlidingSyncRequest as RustSlidingSyncRequest,
    SlidingSyncStats as RustSlidingSyncStats,
    multi_writer_token_round_trip_for_test,
)
from synapse.types import JsonDict, MultiWriterStreamToken, SlidingSyncStreamToken
from synapse.types.handlers.sliding_sync import (
    MutablePerConnectionState,
    SlidingSyncConfig,
)
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_rust_rooms import _normalized
from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import _BlockingRustCalls
from tests.unittest import TestCase


def _changes(
    configs: Mapping[str, Any],
    lazy: Mapping[str, Any],
    streams: Mapping[str, Mapping[str, Any]],
) -> JsonDict:
    """A request's changes to its connection state, comparable across the two
    sides. A status is its flag and whether it has a token, as each connection
    has its own tokens. A lazy member's timestamp is only whether there is
    one, as the two connections send members a moment apart."""
    return {
        "room_configs": {
            room_id: (limit, {t: set(keys) for t, keys in state_map.items()})
            for room_id, (limit, state_map) in configs.items()
        },
        "lazy_membership": {
            room_id: (
                {user_id: ts is not None for user_id, ts in returned.items()},
                set(invalidated),
            )
            for room_id, (returned, invalidated) in lazy.items()
        },
        "streams": {
            stream: {
                room_id: (status.status, status.last_token is not None)
                for room_id, status in updates.items()
            }
            for stream, updates in streams.items()
        },
    }


def _python_changes(state: MutablePerConnectionState) -> JsonDict:
    return _changes(
        {
            room_id: (config.timeline_limit, config.required_state_map)
            for room_id, config in state.get_room_config_updates().items()
        },
        {
            room_id: (
                changes.returned_user_id_to_last_seen_ts_map,
                changes.invalidated_user_ids,
            )
            for room_id, changes in state.room_lazy_membership.items()
        },
        {
            "rooms": state.rooms.get_updates(),
            "receipts": state.receipts.get_updates(),
            "account_data": state.account_data.get_updates(),
        },
    )


def _rust_changes(state: RustConnectionState) -> JsonDict:
    configs, lazy = state.updates_for_test()
    new = state.new
    return _changes(
        configs,
        lazy,
        {
            "rooms": new.rooms.get_updates(),
            "receipts": new.receipts.get_updates(),
            "account_data": new.account_data.get_updates(),
        },
    )


class _RecordingRustCalls(_BlockingRustCalls):
    """Blocking Rust calls that record each request's changes to its
    connection state, by `conn_id`, once the request is finished."""

    def __init__(
        self, handler: RustSlidingSyncHandler, recorded: dict[str, JsonDict]
    ) -> None:
        super().__init__(handler)
        self._recorded = recorded

    def finish_request(
        self,
        request: RustSlidingSyncRequest,
        state: RustConnectionState,
        interested: RustInterestedRooms,
    ) -> "Deferred[tuple[int, RustSlidingSyncStats]]":
        result = super().__getattr__("finish_request")(request, state, interested)
        conn_id = request.body_for_test().get("conn_id")
        assert isinstance(conn_id, str)
        self._recorded[conn_id] = _rust_changes(state)
        return result


class SlidingSyncRustListsTestCase(SlidingSyncBase):
    maxDiff = None

    servlets = [
        synapse.rest.admin.register_servlets,
        account_data.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        receipts.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        tags.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {})["msc4155_enabled"] = True
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        rust = hs.get_rust_handlers().sliding_sync
        if rust is None:
            raise SkipTest("needs the native Rust Postgres driver")
        self.recorded: dict[str, JsonDict] = {}
        self.rust = _RecordingRustCalls(rust, self.recorded)
        self.handler = hs.get_sliding_sync_handler()
        self.handler._rust_sliding_sync_strict = True
        self.pos: dict[str, str | None] = {"python": None, "rust": None}

        connection_store = self.handler.connection_store
        record_new_state = connection_store.record_new_state

        async def recording_record_new_state(
            sync_config: SlidingSyncConfig,
            from_token: SlidingSyncStreamToken | None,
            new_connection_state: MutablePerConnectionState,
        ) -> int:
            assert sync_config.conn_id is not None
            self.recorded[sync_config.conn_id] = _python_changes(new_connection_state)
            return await record_new_state(sync_config, from_token, new_connection_state)

        connection_store.record_new_state = recording_record_new_state  # type: ignore[method-assign]

        self.user1_id = self.register_user("user1", "pass")
        self.user1_tok = self.login(self.user1_id, "pass")
        self.user2_id = self.register_user("user2", "pass")
        self.user2_tok = self.login(self.user2_id, "pass")

    def _rust_requests(self) -> float:
        return rust_sliding_sync_requests_counter.labels(
            result="rust", **{SERVER_NAME_LABEL: self.hs.hostname}
        )._value.get()

    def _sync_both(
        self, sync_body: JsonDict, unordered_lists: bool = False
    ) -> JsonDict:
        """Sync `sync_body` on the Python and the Rust connection, assert the
        two responses and the two sides' changes to the connection state are
        equal, and return Rust's response.

        With `unordered_lists`, each operation's rooms compare as a set: Python
        filters a list into a set, so a list it does not sort comes out in
        hash order."""
        responses = {}
        self.recorded.clear()
        for side in ("python", "rust"):
            self.handler._rust_sliding_sync = (
                self.rust if side == "rust" else None  # type: ignore[assignment]
            )
            before = self._rust_requests()
            response, self.pos[side] = self.do_sync(
                {**sync_body, "conn_id": side},
                since=self.pos[side],
                tok=self.user1_tok,
            )
            self.assertEqual(self._rust_requests() - before, int(side == "rust"))
            response.pop("pos")
            response["rooms"] = _normalized(response.get("rooms", {}))
            if unordered_lists:
                for list_result in response.get("lists", {}).values():
                    for op in list_result.get("ops", []):
                        op["room_ids"] = set(op["room_ids"])
            responses[side] = response
        self.assertEqual(responses["python"], responses["rust"])
        self.assertEqual(self.recorded.get("python"), self.recorded.get("rust"))
        return responses["rust"]

    def _send_message(self, room_id: str) -> str:
        return self.helper.send(room_id, "hi", tok=self.user2_tok)["event_id"]

    def _put_account_data(self, data_type: str, content: JsonDict) -> None:
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/user/{self.user1_id}/account_data/{data_type}",
            content=content,
            access_token=self.user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def _tag(self, room_id: str, tag: str) -> None:
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/user/{self.user1_id}/rooms/{room_id}/tags/{tag}",
            content={},
            access_token=self.user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def _joined_rooms(self, count: int) -> list[str]:
        rooms = []
        for _ in range(count):
            room_id = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
            self.helper.join(room_id, self.user1_id, tok=self.user1_tok)
            rooms.append(room_id)
        return rooms

    def test_sorted_ranges_and_unsent_rooms(self) -> None:
        """A list cut to a range sorts by the latest event, rooms out of range
        with new events are recorded as unsent, and widening the range sends
        them."""
        rooms = self._joined_rooms(6)
        for room_id in reversed(rooms):
            self._send_message(room_id)
        sync_body: JsonDict = {
            "lists": {
                "top": {"ranges": [[0, 1]], "required_state": [], "timeline_limit": 2},
                "all": {
                    "ranges": [[0, 99]],
                    "required_state": [["m.room.name", ""]],
                    "timeline_limit": 0,
                },
            }
        }
        self._sync_both(sync_body)

        # Activity in rooms out of the top range, and in one in it.
        self._send_message(rooms[4])
        self._send_message(rooms[5])
        self._sync_both({"lists": {"top": sync_body["lists"]["top"]}})

        # Rooms with updates the connection did not send come down now.
        self._send_message(rooms[2])
        self._sync_both(
            {
                "lists": {
                    "top": {**sync_body["lists"]["top"], "ranges": [[0, 4]]},
                }
            }
        )
        # Nothing new.
        self._sync_both(
            {"lists": {"top": {**sync_body["lists"]["top"], "ranges": [[0, 4]]}}}
        )

    def test_filters(self) -> None:
        """The `is_dm`, `is_invite`, `is_encrypted`, room type and tag filters,
        over rooms with known and unknown state."""
        joined = self._joined_rooms(2)
        dm = self._create_dm_room(
            inviter_user_id=self.user2_id,
            inviter_tok=self.user2_tok,
            invitee_user_id=self.user1_id,
            invitee_tok=self.user1_tok,
        )
        space = self.helper.create_room_as(
            self.user2_id,
            tok=self.user2_tok,
            extra_content={"creation_content": {"type": RoomTypes.SPACE}},
        )
        self.helper.join(space, self.user1_id, tok=self.user1_tok)
        encrypted = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.send_state(
            encrypted,
            EventTypes.RoomEncryption,
            {"algorithm": "m.megolm.v1.aes-sha2"},
            tok=self.user2_tok,
        )
        self.helper.join(encrypted, self.user1_id, tok=self.user1_tok)
        invite = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.invite(
            invite, src=self.user2_id, targ=self.user1_id, tok=self.user2_tok
        )
        self._create_remote_invite_room_for_user(self.user1_id, None)
        self._tag(joined[0], "m.favourite")
        self._tag(joined[1], "u.work")

        def list_with(filters: JsonDict) -> JsonDict:
            return {
                "ranges": [[0, 99]],
                "required_state": [],
                "timeline_limit": 1,
                "filters": filters,
            }

        sync_body = {
            "lists": {
                "dms": list_with({"is_dm": True}),
                "not_dms": list_with({"is_dm": False}),
                "invites": list_with({"is_invite": True}),
                "encrypted": list_with({"is_encrypted": True}),
                "unencrypted": list_with({"is_encrypted": False}),
                "spaces": list_with({"room_types": [RoomTypes.SPACE]}),
                "rooms": list_with({"room_types": [None]}),
                "not_spaces": list_with({"not_room_types": [RoomTypes.SPACE]}),
                "favourites": list_with({"tags": ["m.favourite"]}),
                "not_work": list_with({"not_tags": ["u.work"]}),
            }
        }
        response = self._sync_both(sync_body, unordered_lists=True)
        self.assertEqual(response["lists"]["dms"]["count"], 1)
        self.assertEqual(response["lists"]["spaces"]["count"], 1)

        self._send_message(dm)
        self._send_message(space)
        self._sync_both(sync_body, unordered_lists=True)

    def test_ignored_invites(self) -> None:
        """Invites from ignored users, and from servers the MSC4155 rules
        ignore, which only apply when the user ignores someone."""
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")
        from_user2 = self.helper.create_room_as(self.user2_id, tok=self.user2_tok)
        self.helper.invite(
            from_user2, src=self.user2_id, targ=self.user1_id, tok=self.user2_tok
        )
        from_user3 = self.helper.create_room_as(user3_id, tok=user3_tok)
        self.helper.invite(from_user3, src=user3_id, targ=self.user1_id, tok=user3_tok)
        self._put_account_data(
            AccountDataTypes.MSC4155_INVITE_PERMISSION_CONFIG,
            {"ignored_users": [user3_id]},
        )
        sync_body = {
            "lists": {
                "all": {"ranges": [[0, 99]], "required_state": [], "timeline_limit": 1}
            }
        }
        # The rules alone ignore nothing.
        response = self._sync_both(sync_body)
        self.assertEqual(response["lists"]["all"]["count"], 2)

        self._put_account_data(
            AccountDataTypes.IGNORED_USER_LIST, {"ignored_users": {"@nobody:test": {}}}
        )
        response = self._sync_both(sync_body)
        self.assertEqual(response["lists"]["all"]["count"], 1)

        self._put_account_data(
            AccountDataTypes.IGNORED_USER_LIST, {"ignored_users": {self.user2_id: {}}}
        )
        response = self._sync_both(sync_body)
        self.assertEqual(response["lists"]["all"]["count"], 0)

    def test_membership_changes(self) -> None:
        """Rooms left, kicked from and banned from in the token range, joined
        in it, and a room subscription to a room the user left."""
        rooms = self._joined_rooms(4)
        sync_body: JsonDict = {
            "lists": {
                "all": {"ranges": [[0, 99]], "required_state": [], "timeline_limit": 3}
            }
        }
        self._sync_both(sync_body)

        self.helper.leave(rooms[0], self.user1_id, tok=self.user1_tok)
        self.helper.change_membership(
            rooms[1], self.user2_id, self.user1_id, "leave", tok=self.user2_tok
        )
        self.helper.ban(rooms[2], self.user2_id, self.user1_id, tok=self.user2_tok)
        (new_room,) = self._joined_rooms(1)
        self._send_message(new_room)
        self._sync_both(sync_body)

        # The rooms left before the from token drop out, but for the kick
        # and the ban.
        self._send_message(rooms[3])
        self._sync_both(sync_body)

        # A subscription to the room left, which the lists leave out.
        self._sync_both(
            {
                **sync_body,
                "room_subscriptions": {
                    rooms[0]: {"required_state": [["*", "*"]], "timeline_limit": 1},
                    new_room: {"required_state": [], "timeline_limit": 5},
                },
            }
        )

    def test_extensions_state(self) -> None:
        """The receipts and account data extensions read and update the Rust
        connection state through its views: rooms sent before, rooms with
        updates out of range, and the range widening onto them."""
        rooms = self._joined_rooms(4)
        for room_id in rooms:
            event_id = self._send_message(room_id)
            self.helper.send_read_receipt(room_id, event_id, tok=self.user2_tok)
            self.get_success(
                self.hs.get_datastores().main.add_account_data_to_room(
                    self.user1_id, room_id, "org.example.data", {"room": room_id}
                )
            )
        extensions = {
            "receipts": {"enabled": True},
            "account_data": {"enabled": True},
        }
        sync_body: JsonDict = {
            "lists": {
                "top": {"ranges": [[0, 1]], "required_state": [], "timeline_limit": 1}
            },
            "extensions": extensions,
        }
        self._sync_both(sync_body)

        # Receipts and account data in every room, two of them out of range.
        for room_id in rooms:
            event_id = self._send_message(room_id)
            self.helper.send_read_receipt(room_id, event_id, tok=self.user2_tok)
        self._sync_both(
            {
                **sync_body,
                "lists": {"top": {**sync_body["lists"]["top"], "ranges": [[0, 0]]}},
            }
        )
        for room_id in rooms:
            self.get_success(
                self.hs.get_datastores().main.add_account_data_to_room(
                    self.user1_id, room_id, "org.example.data", {"again": room_id}
                )
            )
        self._sync_both(
            {
                **sync_body,
                "lists": {"top": {**sync_body["lists"]["top"], "ranges": [[0, 3]]}},
            }
        )
        self._sync_both(
            {
                **sync_body,
                "lists": {"top": {**sync_body["lists"]["top"], "ranges": [[0, 3]]}},
            }
        )


class MultiWriterTokenTestCase(TestCase):
    """A receipts token's database form, which Rust writes and parses without
    the store."""

    def test_round_trip(self) -> None:
        instances = [(1, "w1"), (2, "w2")]
        for token, string in [
            (MultiWriterStreamToken(stream=5), "5"),
            (
                MultiWriterStreamToken(
                    stream=5,
                    instance_map=immutabledict({"w1": 7, "w2": 9}),
                ),
                "m5~1.7~2.9",
            ),
        ]:
            rust_string, parsed = multi_writer_token_round_trip_for_test(
                token, instances
            )
            self.assertEqual(rust_string, string)
            self.assertEqual(parsed, token)
