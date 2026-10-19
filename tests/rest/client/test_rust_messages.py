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
"""`/messages` through the Rust handler (`rust_messages: on`)."""

import json
import urllib.parse
from typing import Any
from unittest.mock import Mock, patch

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.handlers.pagination import rust_messages_counter
from synapse.metrics import SERVER_NAME_LABEL
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

import tests.utils
from tests import unittest


class RustMessagesDispatchTestCase(unittest.HomeserverTestCase):
    """Which `/messages` requests the Rust handler serves."""

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
        self.handler = hs.get_pagination_handler()
        self.handler._rust_messages_strict = True
        self.user = self.register_user("alice", "password")
        self.tok = self.login(self.user, "password")
        self.room_id = self.helper.create_room_as(self.user, tok=self.tok)
        self.event_id = self.helper.send(self.room_id, "hello", tok=self.tok)[
            "event_id"
        ]

    def _count(self, result: str) -> float:
        return rust_messages_counter.labels(
            result=result, **{SERVER_NAME_LABEL: self.hs.hostname}
        )._value.get()

    def _messages(self, **params: Any) -> JsonDict:
        query = {"dir": "b", "limit": "5"}
        for key, value in params.items():
            query[key] = json.dumps(value) if key == "filter" else str(value)
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/messages?{urllib.parse.urlencode(query)}",
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def test_plain_page_in_rust(self) -> None:
        before = self._count("rust")
        body = self._messages()
        self.assertEqual(self._count("rust"), before + 1)
        self.assertEqual(body["chunk"][0]["event_id"], self.event_id)
        self.assertNotIn("state", body)

    def test_lazy_load_members_in_rust(self) -> None:
        before = self._count("rust")
        body = self._messages(filter={"lazy_load_members": True})
        self.assertEqual(self._count("rust"), before + 1)
        self.assertEqual(
            [(e["type"], e["state_key"]) for e in body["state"]],
            [("m.room.member", self.user)],
        )

    def test_event_filter_in_rust(self) -> None:
        before = self._count("rust")
        body = self._messages(filter={"types": ["m.room.message"]})
        self.assertEqual(self._count("rust"), before + 1)
        self.assertEqual([e["event_id"] for e in body["chunk"]], [self.event_id])

    def test_empty_allow_list_in_rust(self) -> None:
        """An empty allow list keeps no event."""
        before = self._count("rust")
        body = self._messages(filter={"senders": []})
        self.assertEqual(self._count("rust"), before + 1)
        self.assertEqual(body["chunk"], [])

    def test_relation_filter_in_python(self) -> None:
        """Filters on relations are served by Python, without counting them."""
        counts = [self._count(r) for r in ("rust", "fallback", "error")]
        self._messages(filter={"related_by_rel_types": ["m.thread"]})
        self.assertEqual(
            [self._count(r) for r in ("rust", "fallback", "error")], counts
        )

    def test_out_of_range_integer_in_filter(self) -> None:
        """An integer out of range in a field Python ignores is no error,
        even in strict mode."""
        before = self._count("error")
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/messages?dir=b&filter="
            + urllib.parse.quote('{"limit": 18446744073709551616000}'),
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(self._count("error"), before)

    def test_malformed_filter_in_python(self) -> None:
        """A field Python reads in its own way, such as a string where a list
        goes, is served by Python."""
        before = self._count("rust")
        self._messages(filter={"not_types": "m.room.member"})
        self.assertEqual(self._count("rust"), before)

    def test_stream_token_converted_in_rust(self) -> None:
        """A `from` token without a topological part is converted by Rust,
        not by the Python query."""
        token = self.get_success(
            self.hs.get_event_sources()
            .get_current_token()
            .to_string(self.hs.get_datastores().main)
        )
        store = self.hs.get_datastores().main
        with patch.object(
            store,
            "get_current_topological_token",
            Mock(side_effect=AssertionError("called")),
        ):
            body = self._messages(**{"from": token})
        self.assertEqual(body["chunk"][0]["event_id"], self.event_id)

    def test_error_falls_back_to_python(self) -> None:
        self.handler._rust_messages_strict = False
        before = self._count("error")
        failing = Mock()
        failing.get_messages.side_effect = RuntimeError("boom")
        with patch.object(self.handler, "_rust_messages", failing):
            body = self._messages()
        failing.get_messages.assert_called_once()
        self.assertEqual(self._count("error"), before + 1)
        self.assertEqual(body["chunk"][0]["event_id"], self.event_id)

    def test_error_raises_when_strict(self) -> None:
        failing = Mock()
        failing.get_messages.side_effect = RuntimeError("boom")
        with patch.object(self.handler, "_rust_messages", failing):
            channel = self.make_request(
                "GET",
                f"/rooms/{self.room_id}/messages?dir=b",
                access_token=self.tok,
            )
        self.assertEqual(channel.code, 500, channel.json_body)
