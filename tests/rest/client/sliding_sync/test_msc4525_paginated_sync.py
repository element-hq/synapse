#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Tests for the Paginated Sync endpoint (MSC4525)."""

import logging
import urllib.parse
from unittest.mock import AsyncMock

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import (
    account_data,
    login,
    paginated_sync,
    receipts,
    room,
    sync,
)
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest

logger = logging.getLogger(__name__)


class MSC4525PaginatedSyncTestCase(unittest.HomeserverTestCase):
    """
    Tests for `POST /_matrix/client/unstable/org.matrix.msc4525/sync`:
    paging on initial sync, per-room gapping on incremental sync, backlog
    (`pending`) draining, and most-recent-first ordering.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        account_data.register_servlets,
        login.register_servlets,
        receipts.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        paginated_sync.register_servlets,
    ]

    sync_endpoint = "/_matrix/client/unstable/org.matrix.msc4525/sync"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4525_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Use the new sliding sync tables (c.f. SlidingSyncBase).
        hs.get_datastores().main.have_finished_sliding_sync_background_jobs = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )

        self.user = self.register_user("alice", "password")
        self.tok = self.login("alice", "password")

    def _sync(
        self,
        body: JsonDict,
        *,
        pos: str | None = None,
    ) -> JsonDict:
        path = self.sync_endpoint
        query: dict[str, str] = {"timeout": "0"}
        if pos is not None:
            query["pos"] = pos
        path += "?" + urllib.parse.urlencode(query)

        channel = self.make_request(
            method="POST", path=path, content=body, access_token=self.tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def _create_rooms(self, count: int) -> list[str]:
        room_ids = []
        for i in range(count):
            room_id = self.helper.create_room_as(self.user, tok=self.tok)
            self.helper.send(room_id, body=f"message in room {i}", tok=self.tok)
            room_ids.append(room_id)
        return room_ids

    def _drain(self, body: JsonDict, pos: str) -> tuple[dict[str, JsonDict], str]:
        """Keep syncing until `pending` is 0; returns all rooms seen and the
        final pos."""
        all_rooms: dict[str, JsonDict] = {}
        for _ in range(50):
            response = self._sync(body, pos=pos)
            all_rooms.update(response["rooms"])
            pos = response["pos"]
            if not response.get("pending"):
                break
        else:
            self.fail("Backlog never drained")
        return all_rooms, pos

    def test_initial_sync_pages_through_all_rooms(self) -> None:
        """An initial sync returns at most `page_size` rooms (most recently
        active first), reports the backlog in `pending`, and subsequent
        requests drain it without duplication or loss."""
        room_ids = self._create_rooms(25)

        body = {"page_size": 10, "limit": 5, "history": 1}
        response = self._sync(body)

        self.assertEqual(len(response["rooms"]), 10, response["rooms"].keys())
        self.assertEqual(response["pending"], 15)
        self.assertEqual(response["total_rooms"], 25)

        # Most recently active first: the last 10 rooms created.
        self.assertEqual(set(response["rooms"].keys()), set(room_ids[-10:]))

        for room_id, room_response in response["rooms"].items():
            self.assertTrue(room_response["initial"], room_id)
            # `history: 1`: one timeline event per room, with a prev_batch to
            # fetch more via /messages.
            self.assertEqual(len(room_response["timeline"]), 1, room_id)
            self.assertIn("prev_batch", room_response)

        # Drain the rest.
        seen_rooms, pos = self._drain(body, response["pos"])
        seen_rooms.update(response["rooms"])

        self.assertEqual(set(seen_rooms.keys()), set(room_ids))

        # Fully caught up: an immediate re-sync returns nothing.
        response = self._sync(body, pos=pos)
        self.assertEqual(response["rooms"], {})
        self.assertNotIn("pending", response)

    def test_incremental_sync_gaps_busy_rooms(self) -> None:
        """A room with more than `limit` new events comes down with the most
        recent `limit` of them, `limited: true` and a `prev_batch` - the gap is
        explicit and local to the room."""
        room_ids = self._create_rooms(3)

        body = {"page_size": 10, "limit": 5, "history": 1}
        response = self._sync(body)
        pos = response["pos"]

        # 8 new events in one room; only the last 5 should come down.
        event_ids = []
        for i in range(8):
            sent = self.helper.send(room_ids[0], body=f"burst {i}", tok=self.tok)
            event_ids.append(sent["event_id"])

        response = self._sync(body, pos=pos)

        self.assertEqual(set(response["rooms"].keys()), {room_ids[0]})
        room_response = response["rooms"][room_ids[0]]
        self.assertEqual(
            [event["event_id"] for event in room_response["timeline"]],
            event_ids[-5:],
        )
        self.assertTrue(room_response["limited"])
        self.assertIn("prev_batch", room_response)
        self.assertNotIn("initial", room_response)
        # `num_live` is derivable and not part of this API.
        self.assertNotIn("num_live", room_response)

    def test_incremental_backlog_is_paged_and_never_lost(self) -> None:
        """When more rooms have updates than fit in `page_size`, the rest are
        reported in `pending` and delivered (with their events) on subsequent
        requests."""
        room_ids = self._create_rooms(6)

        body = {"page_size": 10, "limit": 5, "history": 1}
        response = self._sync(body)
        pos = response["pos"]

        # One new event in every room.
        expected_event_ids = {}
        for i, room_id in enumerate(room_ids):
            sent = self.helper.send(room_id, body=f"update {i}", tok=self.tok)
            expected_event_ids[room_id] = sent["event_id"]

        # Page through them two at a time.
        small_page_body = {"page_size": 2, "limit": 5, "history": 1}
        response = self._sync(small_page_body, pos=pos)
        self.assertEqual(len(response["rooms"]), 2)
        self.assertEqual(response["pending"], 4)

        seen_rooms, _ = self._drain(small_page_body, response["pos"])
        seen_rooms.update(response["rooms"])

        self.assertEqual(set(seen_rooms.keys()), set(room_ids))
        for room_id, room_response in seen_rooms.items():
            self.assertEqual(
                room_response["timeline"][-1]["event_id"],
                expected_event_ids[room_id],
                room_id,
            )

    def test_aging_lane_prevents_starvation(self) -> None:
        """When more rooms have updates than fit in the page, part of the page
        is reserved for the longest-deferred rooms, so busier rooms can't
        starve a quiet room's update indefinitely."""
        room_ids = self._create_rooms(4)
        body = {"page_size": 10, "limit": 5, "history": 1}
        response = self._sync(body)
        pos = response["pos"]

        # Three rooms update; a page of 2 defers the least recently active.
        for room_id in room_ids[:3]:
            self.helper.send(room_id, body="update", tok=self.tok)
        small_body = {"page_size": 2, "limit": 5, "history": 1}
        response = self._sync(small_body, pos=pos)
        self.assertEqual(
            set(response["rooms"]), {room_ids[1], room_ids[2]}, response["rooms"]
        )
        self.assertEqual(response["pending"], 1)
        pos = response["pos"]

        # The busier rooms re-earn their place at the top; the deferred room
        # (room 0) stays the least recently active, but the aging lane
        # guarantees it a slot in the page anyway.
        for room_id in room_ids[1:]:
            self.helper.send(room_id, body="busy", tok=self.tok)
        response = self._sync(small_body, pos=pos)
        self.assertIn(room_ids[0], response["rooms"], response["rooms"].keys())
        self.assertEqual(len(response["rooms"]), 2)
        self.assertEqual(response["pending"], 2)

    def test_newly_joined_room_uses_history(self) -> None:
        """A room that appears mid-session (never sent on the connection) comes
        down `initial` with `history` events."""
        self._create_rooms(2)

        body = {"page_size": 10, "limit": 5, "history": 2}
        response = self._sync(body)
        pos = response["pos"]

        new_room_id = self.helper.create_room_as(self.user, tok=self.tok)
        for i in range(4):
            self.helper.send(new_room_id, body=f"new room message {i}", tok=self.tok)

        response = self._sync(body, pos=pos)

        self.assertIn(new_room_id, response["rooms"])
        room_response = response["rooms"][new_room_id]
        self.assertTrue(room_response.get("initial"), room_response)
        self.assertEqual(len(room_response["timeline"]), 2)
        self.assertIn("prev_batch", room_response)

    def test_required_state_is_returned(self) -> None:
        """The top-level `required_state` is applied to every room."""
        self._create_rooms(1)

        body = {
            "page_size": 10,
            "limit": 5,
            "history": 1,
            "required_state": [["m.room.create", ""], ["m.room.member", "$ME"]],
        }
        response = self._sync(body)

        (room_response,) = response["rooms"].values()
        state_types = {event["type"] for event in room_response["required_state"]}
        self.assertIn("m.room.create", state_types)
        self.assertIn("m.room.member", state_types)

    def test_lazy_members_on_incremental_sync(self) -> None:
        """`$LAZY` member state: an incremental response must include the
        membership of a timeline sender not previously sent on the connection,
        even though their membership isn't in the state deltas."""
        bob = self.register_user("bob", "password")
        bob_tok = self.login("bob", "password")
        charlie = self.register_user("charlie", "password")
        charlie_tok = self.login("charlie", "password")

        room_id = self.helper.create_room_as(self.user, tok=self.tok)
        self.helper.join(room_id, bob, tok=bob_tok)
        self.helper.join(room_id, charlie, tok=charlie_tok)
        # Charlie is the last speaker before the initial sync.
        self.helper.send(room_id, body="hi from charlie", tok=charlie_tok)

        body = {
            "page_size": 10,
            "limit": 5,
            "history": 1,
            "required_state": [["m.room.member", "$LAZY"]],
        }
        response = self._sync(body)
        pos = response["pos"]
        initial_members = {
            event["state_key"]
            for event in response["rooms"][room_id]["required_state"]
            if event["type"] == "m.room.member"
        }
        # `history: 1` means only charlie's message was in the timeline, so
        # bob's membership has not been sent on this connection.
        self.assertNotIn(bob, initial_members)

        # Bob speaks; his membership hasn't changed, so it isn't in the state
        # deltas and must be lazy-loaded into `required_state`.
        self.helper.send(room_id, body="hello from bob", tok=bob_tok)
        response = self._sync(body, pos=pos)
        incremental_members = {
            event["state_key"]
            for event in response["rooms"][room_id].get("required_state", [])
            if event["type"] == "m.room.member"
        }
        self.assertIn(bob, incremental_members)

    def test_unknown_pos_starts_afresh(self) -> None:
        """There is no M_UNKNOWN_POS: a pos the server doesn't recognise is
        treated as absent, and rooms come down as never-sent again."""
        room_ids = self._create_rooms(3)

        body = {"page_size": 10, "limit": 5, "history": 1}
        response = self._sync(body)
        self.assertEqual(len(response["rooms"]), 3)

        # Corrupt the connection position (keep the stream token valid).
        connection_position, stream_token = response["pos"].split("/", 1)
        bogus_pos = f"{int(connection_position) + 999}/{stream_token}"

        response = self._sync(body, pos=bogus_pos)

        # Not an error: a fresh connection, with every room initial again.
        self.assertEqual(set(response["rooms"].keys()), set(room_ids))
        for room_id, room_response in response["rooms"].items():
            self.assertTrue(room_response.get("initial"), room_id)

        # A syntactically invalid pos, by contrast, is a plain 400: only
        # well-formed-but-unrecognised positions restart the connection.
        path = (
            self.sync_endpoint
            + "?"
            + urllib.parse.urlencode({"timeout": "0", "pos": "not a token at all"})
        )
        channel = self.make_request(
            method="POST", path=path, content=body, access_token=self.tok
        )
        self.assertEqual(channel.code, 400, channel.json_body)

    def test_cold_start_backlog_not_starved_by_live_traffic(self) -> None:
        """Rooms never sent on the connection get a reserved slice of every
        page, so continuous traffic in already-sent rooms can't stall the
        initial drain indefinitely."""
        room_ids = self._create_rooms(6)

        body = {"page_size": 2, "limit": 5, "history": 1}
        response = self._sync(body)
        seen_rooms = dict(response["rooms"])
        pos = response["pos"]
        self.assertEqual(len(seen_rooms), 2)

        # Keep the already-delivered rooms busy while draining: without the
        # aging lane every page would fill with the busy (most recently
        # active) rooms and the never-sent backlog would starve.
        for _ in range(20):
            if not response.get("pending"):
                break
            for room_id in seen_rooms:
                self.helper.send(room_id, body="chatter", tok=self.tok)
            response = self._sync(body, pos=pos)
            seen_rooms.update(response["rooms"])
            pos = response["pos"]
        self.assertEqual(set(seen_rooms.keys()), set(room_ids))

    def test_extensions_apply_without_scoping(self) -> None:
        """Extensions have no lists/rooms scoping: enabling one is enough for
        it to apply to the rooms in the response."""
        room_id = self.helper.create_room_as(self.user, tok=self.tok)

        user2 = self.register_user("bob", "password")
        tok2 = self.login("bob", "password")
        self.helper.join(room_id, user2, tok=tok2)

        event_response = self.helper.send(room_id, body="hello", tok=self.tok)

        body = {
            "page_size": 10,
            "limit": 5,
            "history": 5,
            "extensions": {"receipts": {"enabled": True}},
        }
        response = self._sync(body)
        pos = response["pos"]

        # Bob sends a message and reads the room.
        self.helper.send(room_id, body="reply", tok=tok2)
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id}/receipt/m.read/{event_response['event_id']}",
            {},
            access_token=tok2,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        response = self._sync(body, pos=pos)

        self.assertIn(room_id, response["rooms"])
        receipts_response = response["extensions"]["receipts"]["rooms"]
        self.assertIn(room_id, receipts_response, receipts_response)

    def test_receipt_in_quiet_room_wakes_room(self) -> None:
        """A read receipt in a room with no new events must still be
        delivered: the room is woken into the page and the receipts extension
        carries the receipt (the room entry itself stays empty and is
        filtered out)."""
        room_id = self.helper.create_room_as(self.user, tok=self.tok)
        user2 = self.register_user("bob", "password")
        tok2 = self.login("bob", "password")
        self.helper.join(room_id, user2, tok=tok2)
        event_response = self.helper.send(room_id, body="hello", tok=self.tok)

        body = {
            "page_size": 10,
            "limit": 5,
            "history": 5,
            "extensions": {"receipts": {"enabled": True}},
        }
        response = self._sync(body)
        pos = response["pos"]

        # Bob reads the room; no events are sent anywhere.
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id}/receipt/m.read/{event_response['event_id']}",
            {},
            access_token=tok2,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        response = self._sync(body, pos=pos)
        receipts_response = response["extensions"]["receipts"]["rooms"]
        self.assertIn(room_id, receipts_response, response)
        # The room itself had nothing to say.
        self.assertNotIn(room_id, response["rooms"])

    def test_account_data_in_quiet_room_wakes_room(self) -> None:
        """Room account data (e.g. read-state set from another device) in a
        room with no new events must still be delivered via the account_data
        extension."""
        room_id = self.helper.create_room_as(self.user, tok=self.tok)
        self.helper.send(room_id, body="hello", tok=self.tok)

        body = {
            "page_size": 10,
            "limit": 5,
            "history": 5,
            "extensions": {"account_data": {"enabled": True}},
        }
        response = self._sync(body)
        pos = response["pos"]

        # "Another device" updates the room's account data; no events anywhere.
        channel = self.make_request(
            "PUT",
            f"/user/{self.user}/rooms/{room_id}/account_data/org.example.read_state",
            {"event_id": "$dummy"},
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        response = self._sync(body, pos=pos)
        account_data_response = response["extensions"]["account_data"]["rooms"]
        self.assertIn(room_id, account_data_response, response)


class MSC4525PaginatedSyncPerUserEnablementTestCase(unittest.HomeserverTestCase):
    """The endpoint is disabled by default and enablable per user via the
    admin experimental-features API (`ExperimentalFeature.MSC4525`)."""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        paginated_sync.register_servlets,
    ]

    sync_endpoint = "/_matrix/client/unstable/org.matrix.msc4525/sync?timeout=0"

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user = self.register_user("alice", "password")
        self.tok = self.login("alice", "password")
        self.admin_tok = self.login(
            self.register_user("admin", "password", admin=True), "password"
        )

    def test_disabled_by_default_and_enablable_per_user(self) -> None:
        body = {"page_size": 10, "limit": 5}

        channel = self.make_request(
            "POST", self.sync_endpoint, body, access_token=self.tok
        )
        self.assertEqual(channel.code, 404, channel.json_body)

        # Enable the feature for alice only.
        channel = self.make_request(
            "PUT",
            f"/_synapse/admin/v1/experimental_features/{self.user}",
            {"features": {"msc4525": True}},
            access_token=self.admin_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        channel = self.make_request(
            "POST", self.sync_endpoint, body, access_token=self.tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Other users remain gated.
        bob_tok = self.login(self.register_user("bob", "password"), "password")
        channel = self.make_request(
            "POST", self.sync_endpoint, body, access_token=bob_tok
        )
        self.assertEqual(channel.code, 404, channel.json_body)
