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
"""`/messages` answered by Python and by the Rust handler, compared."""

import json
import urllib.parse
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import AccountDataTypes
from synapse.handlers.pagination import rust_messages_counter
from synapse.metrics import SERVER_NAME_LABEL
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.synapse_rust.messages import RustMessagesFallback
from synapse.types import JsonDict
from synapse.util.clock import Clock

import tests.utils
from tests import unittest
from tests.server import _BlockingRustCalls
from tests.test_utils.event_injection import inject_event


def _normalise(body: JsonDict) -> JsonDict:
    """`body` without what legitimately differs between two requests: the
    `unsigned.age` of the events, bundled ones included, and the order of
    the lazy-loaded members."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            stripped = {key: strip(item) for key, item in value.items()}
            unsigned = stripped.get("unsigned")
            if isinstance(unsigned, dict):
                unsigned.pop("age", None)
                # The client format repeats it at the top of the event.
                stripped.pop("age", None)
            return stripped
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    body = strip(body)
    if "state" in body:
        body["state"] = sorted(body["state"], key=lambda event: event["event_id"])
    return body


class RustMessagesTwinTestCase(unittest.HomeserverTestCase):
    """Each request is answered by Python, then by Rust, and the two JSON
    responses must be equal, tokens included."""

    if not tests.utils.USE_RUST_DRIVER_FOR_TESTS:
        skip = "the Rust /messages handler needs the Rust driver"
    elif tests.utils.RUST_MESSAGES_FOR_TESTS not in (None, "on"):
        skip = "SYNAPSE_TEST_RUST_MESSAGES overrides the config"

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"rust_messages": "on"}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.handler = hs.get_pagination_handler()
        rust = self.handler._rust_messages
        assert rust is not None
        if not isinstance(rust, _BlockingRustCalls):
            rust = cast(Any, _BlockingRustCalls(rust))
        self.rust: Any = rust
        self.handler._rust_messages = rust
        self.handler._rust_messages_strict = True

        self.alice = self.register_user("alice", "password", admin=True)
        self.alice_tok = self.login("alice", "password")
        self.bob = self.register_user("bob", "password")
        self.bob_tok = self.login("bob", "password")
        self.carol = self.register_user("carol", "password")
        self.carol_tok = self.login("carol", "password")

    def _count(self, result: str) -> float:
        return rust_messages_counter.labels(
            result=result, **{SERVER_NAME_LABEL: self.hs.hostname}
        )._value.get()

    def _get(self, room_id: str, tok: str, params: dict[str, Any]) -> JsonDict:
        query = {
            key: json.dumps(value) if key == "filter" else str(value)
            for key, value in params.items()
        }
        channel = self.make_request(
            "GET",
            f"/rooms/{room_id}/messages?{urllib.parse.urlencode(query)}",
            access_token=tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def _twin(
        self, room_id: str, tok: str, expect: str | None = "rust", **params: Any
    ) -> JsonDict:
        """One request through Python and then Rust. `expect` is the outcome
        of the Rust call: "rust" or "fallback", or None for either."""
        # Each implementation starts from cold event caches, which the two
        # share, so that Rust's own loads are compared too.
        self._clear_event_caches()
        self.handler._rust_messages = None
        python = self._get(room_id, tok, params)
        self.handler._rust_messages = self.rust

        self._clear_event_caches()
        counts = {r: self._count(r) for r in ("rust", "fallback", "error")}
        rust = self._get(room_id, tok, params)
        deltas = {r: self._count(r) - counts[r] for r in counts}
        self.assertEqual(deltas["error"], 0)
        if expect is not None:
            self.assertEqual(deltas[expect], 1, deltas)
        self.assertEqual(_normalise(rust), _normalise(python), params)
        return rust

    def _clear_event_caches(self) -> None:
        """Empty the event cache, which Python and Rust share, and the Rust
        store's other caches."""
        self.store._attempt_to_invalidate_cache("_get_event_cache", None)
        state_res = self.hs.get_rust_handlers().state_resolution
        assert state_res is not None
        state_res.clear_event_cache()

    def _walk(
        self, room_id: str, tok: str, expect: str | None = "rust", **params: Any
    ) -> list[JsonDict]:
        """Page through the room with `_twin` until there is no `end`."""
        pages = []
        for _ in range(50):
            page = self._twin(room_id, tok, expect, **params)
            pages.append(page)
            if "end" not in page:
                return pages
            params["from"] = page["end"]
        self.fail("the pagination did not end")

    def _events(self, pages: list[JsonDict]) -> list[str]:
        return [event["event_id"] for page in pages for event in page["chunk"]]

    def _room_with_messages(self, count: int, **kwargs: Any) -> str:
        room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok, **kwargs)
        self.helper.join(room_id, self.bob, tok=self.bob_tok)
        for i in range(count):
            sender_tok = self.alice_tok if i % 2 else self.bob_tok
            self.helper.send(room_id, f"message {i}", tok=sender_tok)
        return room_id

    def _set_account_data(self, user_id: str, data_type: str, data: JsonDict) -> None:
        self.get_success(
            self.hs.get_account_data_handler().add_account_data_for_user(
                user_id, data_type, data
            )
        )

    def _relate(
        self,
        room_id: str,
        tok: str,
        rel_type: str,
        parent: str,
        event_type: str = "m.room.message",
        **content: Any,
    ) -> str:
        content["m.relates_to"] = {"rel_type": rel_type, "event_id": parent}
        if "key" in content:
            content["m.relates_to"]["key"] = content.pop("key")
        if event_type == "m.room.message":
            content.setdefault("msgtype", "m.text")
            content.setdefault("body", "related")
        return self.helper.send_event(room_id, event_type, content, tok=tok)["event_id"]

    def test_walk_both_directions(self) -> None:
        room_id = self._room_with_messages(12)
        self.helper.send_state(
            room_id, "m.room.topic", {"topic": "hi"}, tok=self.alice_tok
        )
        backwards = self._walk(room_id, self.alice_tok, dir="b", limit=5)
        forwards = self._walk(room_id, self.alice_tok, dir="f", limit=5)
        self.assertGreater(len(backwards), 3)
        self.assertEqual(self._events(backwards), self._events(forwards)[::-1])

    def test_limit_one(self) -> None:
        room_id = self._room_with_messages(3)
        self._walk(room_id, self.alice_tok, dir="b", limit=1)
        self._walk(room_id, self.alice_tok, dir="f", limit=1)

    def test_state_events_with_prev_content(self) -> None:
        """State events in the page carry their `prev_content`, including a
        redacted one that has a predecessor."""
        room_id = self._room_with_messages(2)
        for topic in ("one", "two", "three"):
            self.helper.send_state(
                room_id, "m.room.topic", {"topic": topic}, tok=self.alice_tok
            )
        topic_event = self.helper.send_state(
            room_id, "m.room.topic", {"topic": "four"}, tok=self.alice_tok
        )["event_id"]
        self._redact(room_id, topic_event)
        pages = self._walk(room_id, self.alice_tok, dir="b", limit=3)
        with_prev = [
            e
            for page in pages
            for e in page["chunk"]
            if "prev_content" in e.get("unsigned", {})
        ]
        self.assertGreater(len(with_prev), 2)

    def test_rejected_event(self) -> None:
        """A rejected event is left out of the page by both."""
        room_id = self._room_with_messages(3)
        rejected = self.helper.send(room_id, "rejected", tok=self.alice_tok)["event_id"]
        self.helper.send(room_id, "after", tok=self.alice_tok)
        self.get_success(
            self.store.db_pool.simple_insert(
                "rejections",
                {"event_id": rejected, "reason": "test", "last_check": "0"},
                desc="reject",
            )
        )
        pages = self._walk(room_id, self.alice_tok, dir="b", limit=2)
        self.assertNotIn(rejected, self._events(pages))

    def test_to_token_range_over_limit(self) -> None:
        """A `to` range longer than the limit: pages are limited before the
        bound."""
        room_id = self._room_with_messages(12)
        first = self._twin(room_id, self.alice_tok, dir="b", limit=1)
        end = self._walk(room_id, self.alice_tok, dir="b", limit=50)[0]["end"]
        self._walk(
            room_id,
            self.alice_tok,
            dir="b",
            limit=3,
            **{"from": first["start"], "to": end},
        )
        self._walk(room_id, self.alice_tok, dir="f", limit=3, to=first["start"])

    def test_to_token(self) -> None:
        room_id = self._room_with_messages(10)
        first = self._twin(room_id, self.alice_tok, dir="b", limit=3)
        second = self._twin(
            room_id, self.alice_tok, dir="b", limit=3, **{"from": first["end"]}
        )
        # Forwards up to a token, and backwards down to one.
        self._twin(
            room_id,
            self.alice_tok,
            dir="f",
            limit=10,
            **{"from": second["end"], "to": first["start"]},
        )
        self._twin(
            room_id,
            self.alice_tok,
            dir="b",
            limit=10,
            **{"from": first["start"], "to": second["end"]},
        )
        # An empty range.
        self._twin(
            room_id,
            self.alice_tok,
            dir="b",
            limit=10,
            **{"from": second["end"], "to": first["start"]},
        )

    def test_stream_token(self) -> None:
        """A `from` token from `/sync`: a live token, without a topological
        part."""
        room_id = self._room_with_messages(6)
        token = self.get_success(
            self.hs.get_event_sources().get_current_token().to_string(self.store)
        )
        self.assertTrue(token.startswith("s"), token)
        self._twin(room_id, self.alice_tok, dir="b", limit=4, **{"from": token})
        self._twin(room_id, self.alice_tok, dir="f", limit=4, **{"from": token})

    def _redact(self, room_id: str, event_id: str) -> str:
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id}/redact/{event_id}",
            content={},
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body["event_id"]

    def test_redactions(self) -> None:
        room_id = self._room_with_messages(3)
        target = self.helper.send(room_id, "secret", tok=self.bob_tok)["event_id"]
        redaction = self._redact(room_id, target)
        # A redaction of the redaction.
        self._redact(room_id, redaction)
        self.helper.send(room_id, "after", tok=self.bob_tok)
        pages = self._walk(room_id, self.alice_tok, dir="b", limit=3)
        self.assertIn(target, self._events(pages))

    def test_erased_sender(self) -> None:
        room_id = self._room_with_messages(4)
        self.get_success(self.store.mark_user_erased(self.bob))
        self._walk(room_id, self.alice_tok, dir="b", limit=10)

    def test_soft_failed(self) -> None:
        room_id = self._room_with_messages(2)
        for spammy in (False, True):
            self.get_success(
                inject_event(
                    self.hs,
                    type="m.room.message",
                    sender=self.alice,
                    room_id=room_id,
                    content={"body": "soft failed", "msgtype": "m.text"},
                    internal_metadata={
                        "soft_failed": True,
                        "policy_server_spammy": spammy,
                    },
                )
            )
        self.helper.send(room_id, "after", tok=self.bob_tok)

        def count(tok: str) -> int:
            return len(self._events(self._walk(room_id, tok, dir="b", limit=20)))

        visible = count(self.alice_tok)
        self.assertEqual(count(self.bob_tok), visible)
        for config in (
            {"return_policy_server_spammy_events": True},
            {"return_soft_failed_events": True},
        ):
            for user_id in (self.alice, self.bob):
                self._set_account_data(
                    user_id, AccountDataTypes.SYNAPSE_ADMIN_CLIENT_CONFIG, config
                )
            # Only the server admin sees them.
            self.assertGreaterEqual(count(self.alice_tok), visible)
            self.assertEqual(count(self.bob_tok), visible)
        self.assertGreater(count(self.alice_tok), visible)

    def test_ignored_user(self) -> None:
        room_id = self._room_with_messages(6)
        self._set_account_data(
            self.alice,
            AccountDataTypes.IGNORED_USER_LIST,
            {"ignored_users": {self.bob: {}}},
        )
        pages = self._walk(room_id, self.alice_tok, dir="b", limit=4)
        message_senders = {
            e["sender"]
            for page in pages
            for e in page["chunk"]
            if e["type"] == "m.room.message"
        }
        self.assertEqual(message_senders, {self.alice})

    def test_relations(self) -> None:
        room_id = self._room_with_messages(2)
        root = self.helper.send(room_id, "root", tok=self.alice_tok)["event_id"]
        self._relate(room_id, self.bob_tok, "m.thread", root)
        self._relate(room_id, self.alice_tok, "m.thread", root)
        edited = self.helper.send(room_id, "typo", tok=self.alice_tok)["event_id"]
        self._relate(
            room_id,
            self.alice_tok,
            "m.replace",
            edited,
            "m.room.message",
            **{"m.new_content": {"msgtype": "m.text", "body": "fixed"}},
        )
        self._relate(
            room_id, self.bob_tok, "m.annotation", edited, "m.reaction", key="+1"
        )
        self._relate(room_id, self.bob_tok, "m.reference", edited)
        pages = self._walk(room_id, self.alice_tok, dir="b", limit=4)
        root_event = next(
            event
            for page in pages
            for event in page["chunk"]
            if event["event_id"] == root
        )
        self.assertIn("m.thread", root_event["unsigned"]["m.relations"])
        self._walk(room_id, self.bob_tok, dir="f", limit=3)

    def test_ignored_latest_thread_reply_falls_back(self) -> None:
        room_id = self._room_with_messages(1)
        root = self.helper.send(room_id, "root", tok=self.alice_tok)["event_id"]
        self._relate(room_id, self.alice_tok, "m.thread", root)
        self._relate(room_id, self.bob_tok, "m.thread", root)
        self._set_account_data(
            self.alice,
            AccountDataTypes.IGNORED_USER_LIST,
            {"ignored_users": {self.bob: {}}},
        )
        self._twin(room_id, self.alice_tok, "fallback", dir="b", limit=20)

    def test_left_user(self) -> None:
        """A user who left sees the room up to their leave: the `from` token
        is clamped."""
        room_id = self._room_with_messages(4)
        leave = self.helper.leave(room_id, self.bob, tok=self.bob_tok)
        for i in range(4):
            self.helper.send(room_id, f"after leave {i}", tok=self.alice_tok)
        pages = self._walk(room_id, self.bob_tok, dir="b", limit=3)
        leave_token = self.get_success(
            self.store.get_topological_token_for_event(leave["event_id"])
        )
        self.assertTrue(
            pages[0]["start"].startswith(f"t{leave_token.topological}-"),
            pages[0]["start"],
        )

        # From a `/sync` token, which has no topological part: the leave is
        # compared with the position the token converts to.
        sync_token = self.get_success(
            self.hs.get_event_sources().get_current_token().to_string(self.store)
        )
        pages = self._walk(
            room_id, self.bob_tok, dir="b", limit=3, **{"from": sync_token}
        )
        self.assertTrue(
            pages[0]["start"].startswith(f"t{leave_token.topological}-"),
            pages[0]["start"],
        )

    def _room_with_visibility(self, visibility: str) -> str:
        return self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            extra_content={
                "initial_state": [
                    {
                        "type": "m.room.history_visibility",
                        "state_key": "",
                        "content": {"history_visibility": visibility},
                    }
                ]
            },
        )

    def test_peeking_world_readable(self) -> None:
        room_id = self._room_with_visibility("world_readable")
        for i in range(3):
            self.helper.send(room_id, f"message {i}", tok=self.alice_tok)
        self._walk(room_id, self.carol_tok, dir="b", limit=2)

    def test_joined_history_visibility(self) -> None:
        """Events from before a user joined are filtered out: pages come back
        empty but with an `end` token."""
        room_id = self._room_with_visibility("joined")
        for i in range(6):
            self.helper.send(room_id, f"before {i}", tok=self.alice_tok)
        self.helper.join(room_id, self.carol, tok=self.carol_tok)
        self.helper.send(room_id, "after", tok=self.alice_tok)
        pages = self._walk(room_id, self.carol_tok, dir="b", limit=2)
        self.assertTrue(any(not page["chunk"] and "end" in page for page in pages))

    def test_lazy_load_members(self) -> None:
        room_id = self._room_with_messages(6)
        self.helper.join(room_id, self.carol, tok=self.carol_tok)
        self.helper.send(room_id, "carol here", tok=self.carol_tok)
        for filter_json in (
            {"lazy_load_members": True},
            {"lazy_load_members": True, "include_redundant_members": True},
            {"lazy_load_members": False},
        ):
            pages = self._walk(
                room_id, self.bob_tok, dir="b", limit=3, filter=filter_json
            )
            has_state = any("state" in page for page in pages)
            self.assertEqual(has_state, filter_json["lazy_load_members"])

    def _backfill_calls(
        self, room_id: str, did_backfill: bool, **params: Any
    ) -> tuple[int, int]:
        """The backfills one request runs in the foreground and launches in
        the background, which must be the same through Python and Rust,
        arguments included. Also checks that Rust reads the page again
        exactly when a foreground backfill brought events."""
        federation = self.hs.get_federation_handler()
        outcomes = []
        for rust in (None, self.rust):
            foreground = AsyncMock(return_value=did_backfill)
            background = Mock()
            reads = Mock(wraps=self.rust._handler)
            self.handler._rust_messages = rust
            with (
                patch.object(federation, "maybe_backfill", foreground),
                patch.object(self.hs, "run_as_background_process", background),
                patch.object(self.rust, "_handler", reads),
            ):
                self._get(room_id, self.alice_tok, params)
            launched = [
                c
                for c in background.call_args_list
                if c.args[0] == "maybe_backfill_in_the_background"
            ]
            outcomes.append((foreground.call_args_list, launched))
            if rust is not None:
                expected_reads = 2 if did_backfill and foreground.called else 1
                self.assertEqual(reads.get_messages_blocking.call_count, expected_reads)
        self.handler._rust_messages = self.rust
        self.assertEqual(outcomes[0][0], outcomes[1][0])
        self.assertEqual(
            [(c.args[2:], c.kwargs) for c in outcomes[0][1]],
            [(c.args[2:], c.kwargs) for c in outcomes[1][1]],
        )
        return len(outcomes[0][0]), len(outcomes[0][1])

    def test_backfill(self) -> None:
        """A full page going backwards backfills in the background, a short
        one in the foreground, and the page is read again if backfill
        brought anything. Forwards never backfills."""
        room_id = self._room_with_messages(6)
        for did_backfill in (False, True):
            self.assertEqual(
                self._backfill_calls(room_id, did_backfill, dir="b", limit=3),
                (0, 1),
            )
            self.assertEqual(
                self._backfill_calls(room_id, did_backfill, dir="b", limit=50),
                (1, 0),
            )
            self.assertEqual(
                self._backfill_calls(room_id, did_backfill, dir="f", limit=3),
                (0, 0),
            )

    def test_depth_gap(self) -> None:
        """A gap of more than one event in the depths of a full page
        backfills in the foreground. The room is version 2, whose event IDs
        are not hashes of the event, so that a depth can be changed."""
        room_id = self._room_with_messages(6, room_version="2")
        event_id = self.helper.send(room_id, "gap", tok=self.alice_tok)["event_id"]
        event = self.get_success(self.store.get_event(event_id))

        def bump_depth(txn: LoggingTransaction) -> None:
            txn.execute(
                "UPDATE event_json SET json = REPLACE(json, ?, ?) WHERE event_id = ?",
                (f'"depth":{event.depth}', f'"depth":{event.depth + 10}', event_id),
            )
            self.assertEqual(txn.rowcount, 1)

        self.get_success(self.store.db_pool.runInteraction("bump_depth", bump_depth))
        self._clear_event_caches()
        for did_backfill in (False, True):
            self.assertEqual(
                self._backfill_calls(room_id, did_backfill, dir="b", limit=3),
                (1, 0),
            )
        self._twin(room_id, self.alice_tok, dir="b", limit=3)

    def test_fallback_after_backfill_does_not_backfill_again(self) -> None:
        """If Rust leaves the page to Python after a foreground backfill,
        Python reads the page without backfilling a second time."""
        room_id = self._room_with_messages(2)
        handler = self.rust._handler
        first_read = handler.get_messages_blocking

        reads: list[Any] = []

        def read(request: Any) -> Any:
            reads.append(request)
            if len(reads) == 1:
                return first_read(request)
            raise RustMessagesFallback("after backfill")

        federation = self.hs.get_federation_handler()
        backfill = AsyncMock(return_value=True)
        rust_handler = Mock(wraps=handler)
        rust_handler.get_messages_blocking.side_effect = read
        with (
            patch.object(self.rust, "_handler", rust_handler),
            patch.object(federation, "maybe_backfill", backfill),
        ):
            body = self._get(room_id, self.alice_tok, {"dir": "b", "limit": 50})
        self.assertEqual(len(reads), 2)
        self.assertEqual(backfill.call_count, 1)
        self.assertTrue(body["chunk"])
