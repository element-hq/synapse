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

"""The Rust parse of the sliding sync request body
(`rust/src/sliding_sync/request.rs`) against pydantic's `SlidingSyncBody`."""

import json
import random
from typing import Any
from unittest import SkipTest
from unittest.mock import patch

from pydantic import ValidationError

from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.synapse_rust.sliding_sync import SlidingSyncRequest
from synapse.types import (
    Absent,
    JsonDict,
    RoomStreamToken,
    SlidingSyncStreamToken,
    StreamToken,
)
from synapse.types.handlers.sliding_sync import SlidingSyncConfig
from synapse.types.rest.client import SlidingSyncBody
from synapse.util.clock import Clock

import tests.utils
from tests import unittest
from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

# A JSON object as the generator builds it: its entries in order, so that a
# key can repeat.
Entries = list[tuple[str, Any]]


class _Object:
    def __init__(self, entries: Entries) -> None:
        self.entries = entries


def _encode(value: Any, rng: random.Random) -> str:
    """Encode `value` as JSON text, keeping the repeated keys of an
    `_Object` and varying the whitespace and string escapes."""
    space = rng.choice(["", " ", "\n  "])
    if isinstance(value, _Object):
        items = [
            f"{_encode(key, rng)}:{space}{_encode(item, rng)}"
            for key, item in value.entries
        ]
        return "{" + f",{space}".join(items) + "}"
    if isinstance(value, list):
        return "[" + f",{space}".join(_encode(item, rng) for item in value) + "]"
    return json.dumps(value, ensure_ascii=rng.random() < 0.5)


_STATE_PARTS = [
    "*",
    "$LAZY",
    "$ME",
    "",
    "m.room.member",
    "m.room.name",
    "m.space.child",
    "@alice:test",
    "org.example.é",
    "☃",
]
_NAMES = ["a", "b", "list", "", "é", "x" * 64, "☃", "a b"]
_ROOM_IDS = ["!a:test", "!b:test", "!c:test", "!é:test"]


class _BodyGenerator:
    """Random request bodies, most of which pydantic accepts."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def maybe(self, value: Any, p: float = 0.5) -> list[Any]:
        """`[value]` with probability `p`, else `[]`: an optional field."""
        return [value] if self.rng.random() < p else []

    def nullable(self, value: Any) -> Any:
        return None if self.rng.random() < 0.15 else value

    def unknown_fields(self) -> Entries:
        return self.maybe(
            ("org.example.unknown", self.rng.choice([1, "x", None, []])), 0.1
        )

    def coin(self) -> bool:
        return self.rng.random() < 0.5

    def strings(self, pool: list[str], allow_none: bool = False) -> list[Any]:
        values: list[Any] = list(pool) + ([None] if allow_none else [])
        return self.rng.sample(values, self.rng.randint(0, min(4, len(values))))

    def required_state(self) -> list[list[str]]:
        return [
            [self.rng.choice(_STATE_PARTS), self.rng.choice(_STATE_PARTS)]
            for _ in range(self.rng.randint(0, 5))
        ]

    def room_params(self) -> Entries:
        return [
            ("required_state", self.required_state()),
            ("timeline_limit", self.rng.choice([0, 1, 10, 1000, -3])),
            *self.unknown_fields(),
        ]

    def filters(self) -> _Object:
        rng = self.rng
        fields: Entries = [
            ("is_dm", self.nullable(self.coin())),
            ("spaces", self.nullable(self.strings(_ROOM_IDS))),
            ("is_encrypted", self.nullable(self.coin())),
            ("is_invite", self.nullable(self.coin())),
            (
                "room_types",
                self.nullable(self.strings(["m.space", "org.example"], True)),
            ),
            (
                "not_room_types",
                self.nullable(self.strings(["m.space", "org.example"], True)),
            ),
            ("room_name_like", self.nullable(rng.choice(["foo", "", "É"]))),
            ("tags", self.nullable(self.strings(["m.favourite", "u.work"]))),
            ("not_tags", self.nullable(self.strings(["m.lowpriority", "u.x"]))),
        ]
        entries = [field for field in fields if rng.random() < 0.4]
        return _Object(entries + self.unknown_fields())

    def sliding_list(self) -> _Object:
        rng = self.rng
        entries = self.room_params()
        if rng.random() < 0.7:
            ranges = [
                sorted([rng.randint(0, 200), rng.randint(0, 200)])
                for _ in range(rng.randint(0, 3))
            ]
            entries.append(("ranges", self.nullable(ranges)))
        entries += self.maybe(("slow_get_all_rooms", self.nullable(self.coin())), 0.2)
        entries += self.maybe(("filters", self.nullable(self.filters())), 0.4)
        rng.shuffle(entries)
        return _Object(entries)

    def keyed(self, keys: list[str], make: Any) -> _Object:
        """An object of up to four entries, sometimes with a key repeated."""
        entries = [
            (self.rng.choice(keys), make()) for _ in range(self.rng.randint(0, 4))
        ]
        return _Object(entries)

    def lists_and_rooms_extension(self) -> _Object:
        entries: Entries = [
            *self.maybe(("enabled", self.nullable(self.coin()))),
            *self.maybe(("lists", self.nullable(self.strings(["*", *_NAMES])))),
            *self.maybe(("rooms", self.nullable(self.strings(["*", *_ROOM_IDS])))),
        ]
        return _Object(entries + self.unknown_fields())

    def extensions(self) -> _Object:
        rng = self.rng
        candidates: Entries = [
            (
                "to_device",
                _Object(
                    [
                        *self.maybe(("enabled", self.nullable(self.coin()))),
                        *self.maybe(("limit", rng.choice([0, 7, 100, -1]))),
                        *self.maybe(("since", self.nullable(str(rng.randint(0, 99))))),
                    ]
                ),
            ),
            ("e2ee", _Object(self.maybe(("enabled", self.nullable(self.coin()))))),
            ("account_data", self.lists_and_rooms_extension()),
            ("receipts", self.lists_and_rooms_extension()),
            ("typing", self.lists_and_rooms_extension()),
            (
                "io.element.msc4308.thread_subscriptions",
                _Object(
                    [
                        *self.maybe(("enabled", self.nullable(self.coin()))),
                        *self.maybe(("limit", rng.choice([1, 100]))),
                    ]
                ),
            ),
            # Only the unstable name counts: this one is ignored.
            ("thread_subscriptions", _Object([("enabled", True)])),
            (
                "org.matrix.msc4354.sticky_events",
                _Object(
                    [
                        *self.maybe(("enabled", self.coin())),
                        *self.maybe(("limit", rng.choice([0, 5, 100]))),
                        *self.maybe(
                            ("since", rng.choice(["sticky_0", "sticky_m4~1.6"]))
                        ),
                    ]
                ),
            ),
            (
                "org.matrix.msc4262.profiles",
                _Object(
                    [
                        *self.maybe(("enabled", self.coin())),
                        *self.maybe(("fields", self.strings(["displayname", "m.tz"]))),
                    ]
                ),
            ),
            ("org.example.unknown_extension", _Object([("enabled", True)])),
        ]
        entries = [
            (name, self.nullable(value))
            if name not in _NOT_NULLABLE_EXTENSIONS
            else (name, value)
            for name, value in candidates
            if rng.random() < 0.4
        ]
        return _Object(entries)

    def body(self) -> _Object:
        entries: Entries = [
            *self.maybe(("conn_id", self.nullable(self.rng.choice(["", "room-list"])))),
            *self.maybe(
                ("lists", self.nullable(self.keyed(_NAMES, self.sliding_list))), 0.8
            ),
            *self.maybe(
                (
                    "room_subscriptions",
                    self.nullable(
                        self.keyed(_ROOM_IDS, lambda: _Object(self.room_params()))
                    ),
                ),
            ),
            *self.maybe(("extensions", self.nullable(self.extensions()))),
            *self.unknown_fields(),
        ]
        self.rng.shuffle(entries)
        return _Object(entries)


# Pydantic rejects an explicit `null` for these: they are `Absent` or a value.
_NOT_NULLABLE_EXTENSIONS = {
    "org.matrix.msc4354.sticky_events",
    "org.matrix.msc4262.profiles",
}


def _expected(body: SlidingSyncBody) -> JsonDict:
    """What `SlidingSyncRequest.body_for_test` should return for `body`."""

    def room_params(params: SlidingSyncBody.CommonRoomParameters) -> JsonDict:
        return {
            "required_state": [list(pair) for pair in params.required_state],
            "timeline_limit": params.timeline_limit,
        }

    def sliding_list(sliding_list: SlidingSyncBody.SlidingSyncList) -> JsonDict:
        return {
            **room_params(sliding_list),
            "ranges": (
                None
                if sliding_list.ranges is None
                else [list(r) for r in sliding_list.ranges]
            ),
            "slow_get_all_rooms": sliding_list.slow_get_all_rooms,
            "filters": (
                None
                if sliding_list.filters is None
                else sliding_list.filters.model_dump()
            ),
        }

    def dump(model: Any) -> Any:
        return None if model is None else model.model_dump()

    extensions = None
    if body.extensions is not None:
        ext = body.extensions
        sticky = None
        if ext.sticky_events is not Absent:
            sticky = {
                "enabled": ext.sticky_events.enabled,
                "limit": ext.sticky_events.limit,
                "since": (
                    ext.sticky_events.since.serialise()
                    if ext.sticky_events.since is not Absent
                    else None
                ),
            }
        profiles = None
        if ext.profiles is not Absent:
            profiles = {
                "enabled": ext.profiles.enabled,
                "fields": (
                    list(ext.profiles.fields)
                    if ext.profiles.fields is not Absent
                    else None
                ),
            }
        extensions = {
            "to_device": dump(ext.to_device),
            "e2ee": dump(ext.e2ee),
            "account_data": dump(ext.account_data),
            "receipts": dump(ext.receipts),
            "typing": dump(ext.typing),
            "io.element.msc4308.thread_subscriptions": dump(ext.thread_subscriptions),
            "org.matrix.msc4354.sticky_events": sticky,
            "org.matrix.msc4262.profiles": profiles,
        }

    return {
        "conn_id": body.conn_id,
        "lists": (
            None
            if body.lists is None
            else [[name, sliding_list(value)] for name, value in body.lists.items()]
        ),
        "room_subscriptions": (
            None
            if body.room_subscriptions is None
            else [
                [room_id, room_params(value)]
                for room_id, value in body.room_subscriptions.items()
            ]
        ),
        "extensions": extensions,
    }


def _as_json(value: Any) -> Any:
    """`value` with its tuples as lists, for comparing."""
    return json.loads(json.dumps(value))


def _rust_request(body: bytes) -> SlidingSyncRequest:
    return SlidingSyncRequest(
        body,
        user_id="@alice:test",
        device_id="DEVICE",
        from_room_key=None,
        connection_position=None,
        to_room_key=RoomStreamToken(stream=1),
        to_suffix="0_0",
        is_server_admin=False,
        return_soft_failed_events=False,
        return_policy_server_spammy_events=False,
        have_finished_bg_jobs=True,
        rooms_to_exclude=set(),
        msc4306_enabled=False,
        msc4354_enabled=False,
        profiles_enabled=False,
    )


class RustRequestParseTestCase(unittest.TestCase):
    """Rust parses every body pydantic accepts the same way."""

    def test_generated_bodies(self) -> None:
        accepted = 0
        for seed in range(400):
            rng = random.Random(seed)
            text = _encode(_BodyGenerator(rng).body(), rng)
            try:
                body = SlidingSyncBody.model_validate(json.loads(text))
            except ValidationError:
                continue
            accepted += 1
            expected = _as_json(_expected(body))
            self.assertEqual(
                _as_json(_rust_request(text.encode()).body_for_test()),
                expected,
                f"seed {seed}: {text}",
            )
            # What `_build_rust_request` sends for a config that was not built
            # from a request.
            reencoded = body.model_dump_json(by_alias=True, exclude_unset=True)
            self.assertEqual(
                _as_json(_rust_request(reencoded.encode()).body_for_test()),
                expected,
                f"seed {seed}, re-encoded: {reencoded}",
            )
        # The generator is no use if pydantic rejects what it makes.
        self.assertGreater(accepted, 300)

    def test_repeated_list_name(self) -> None:
        """A repeated key keeps its first position and its last value, as in
        the dict `json.loads` builds."""
        text = (
            '{"lists": {'
            '"b": {"required_state": [], "timeline_limit": 1},'
            '"a": {"required_state": [], "timeline_limit": 2},'
            '"b": {"required_state": [], "timeline_limit": 3}}}'
        )
        body = SlidingSyncBody.model_validate(json.loads(text))
        rust = _rust_request(text.encode()).body_for_test()
        self.assertEqual(_as_json(rust), _as_json(_expected(body)))
        self.assertEqual([name for name, _ in rust["lists"]], ["b", "a"])
        self.assertEqual(rust["lists"][0][1]["timeline_limit"], 3)

    def test_unparseable_body(self) -> None:
        """A body Rust does not cover raises `ValueError`, and Python computes
        the request."""
        text = (
            '{"lists": {"a": {"required_state": [], "timeline_limit": 1,'
            ' "ranges": [[0, 100000000000000000000000]]}}}'
        )
        SlidingSyncBody.model_validate(json.loads(text))
        with self.assertRaises(ValueError):
            _rust_request(text.encode())

    def test_tokens_go_together(self) -> None:
        with self.assertRaises(ValueError):
            SlidingSyncRequest(
                b"{}",
                user_id="@alice:test",
                device_id=None,
                from_room_key=RoomStreamToken(stream=1),
                connection_position=None,
                to_room_key=RoomStreamToken(stream=1),
                to_suffix="0",
                is_server_admin=False,
                return_soft_failed_events=False,
                return_policy_server_spammy_events=False,
                have_finished_bg_jobs=True,
                rooms_to_exclude=set(),
                msc4306_enabled=False,
                msc4354_enabled=False,
                profiles_enabled=False,
            )


class RustRequestBuildTestCase(SlidingSyncBase):
    """The servlet keeps the body's bytes, and the handler builds the Rust
    request from them."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"]["rust_sliding_sync"] = "on"
        config["exclude_rooms_from_sync"] = ["!excluded:test"]
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        if tests.utils.RUST_SLIDING_SYNC_FOR_TESTS not in (None, "on"):
            raise SkipTest("SYNAPSE_TEST_RUST_SLIDING_SYNC overrides the config")
        hs = self.setup_test_homeserver()
        if hs.get_sliding_sync_handler()._rust_sliding_sync is None:
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.handler = hs.get_sliding_sync_handler()
        self.user = self.register_user("alice", "pass", admin=True)
        self.tok = self.login("alice", "pass")

    def _capture_configs(self, text: str, since: str | None = None) -> list[Any]:
        """Send `text` as a sliding sync body, and return the arguments of each
        `current_sync_for_user` call it led to."""
        calls: list[Any] = []
        original = self.handler.current_sync_for_user

        async def capture(
            sync_config: SlidingSyncConfig,
            to_token: StreamToken,
            from_token: SlidingSyncStreamToken | None = None,
        ) -> Any:
            calls.append((sync_config, to_token, from_token))
            return await original(sync_config, to_token=to_token, from_token=from_token)

        path = self.sync_endpoint + (f"?pos={since}" if since else "")
        with patch.object(self.handler, "current_sync_for_user", side_effect=capture):
            channel = self.make_request(
                "POST", path, content=text.encode(), access_token=self.tok
            )
        self.assertEqual(channel.code, 200, channel.json_body)
        return calls

    def test_body_bytes_and_request(self) -> None:
        text = (
            '{"lists": {"a": {"ranges": [[0, 9]],'
            ' "required_state": [["m.room.name", ""]], "timeline_limit": 3}},'
            ' "conn_id": "c"}'
        )
        [(sync_config, to_token, from_token)] = self._capture_configs(text)
        self.assertEqual(sync_config.body_bytes, text.encode())
        self.assertIsNone(from_token)

        request = self.get_success(
            self.handler._build_rust_request(sync_config, from_token, to_token)
        )
        self.assertEqual(request.user_id, self.user)
        self.assertEqual(request.device_id, sync_config.requester.device_id)
        self.assertIsNone(request.from_room_key)
        self.assertIsNone(request.connection_position)
        self.assertEqual(request.to_room_key, to_token.room_key)
        self.assertEqual(
            f"{self.get_success(to_token.room_key.to_string(self.store))}_"
            f"{request.to_suffix}",
            self.get_success(to_token.to_string(self.store)),
        )
        self.assertTrue(request.is_server_admin)
        self.assertTrue(request.have_finished_bg_jobs)
        self.assertEqual(request.rooms_to_exclude, {"!excluded:test"})
        self.assertEqual(
            _as_json(request.body_for_test()),
            _as_json(_expected(SlidingSyncBody.model_validate(json.loads(text)))),
        )

    def test_incremental_request(self) -> None:
        text = '{"lists": {"a": {"required_state": [], "timeline_limit": 1}}}'
        [(sync_config, to_token, _)] = self._capture_configs(text)
        channel = self.make_request(
            "POST", self.sync_endpoint, content=text.encode(), access_token=self.tok
        )
        pos = channel.json_body["pos"]
        [(sync_config, to_token, from_token)] = self._capture_configs(text, pos)
        assert from_token is not None
        request = self.get_success(
            self.handler._build_rust_request(sync_config, from_token, to_token)
        )
        self.assertEqual(request.from_room_key, from_token.stream_token.room_key)
        self.assertEqual(request.connection_position, from_token.connection_position)

    def test_config_without_body_bytes(self) -> None:
        """A config not built from a request is re-encoded for Rust."""
        text = '{"lists": {"a": {"required_state": [["*", "*"]], "timeline_limit": 2}}}'
        [(sync_config, to_token, _)] = self._capture_configs(text)
        config = sync_config.model_copy(update={"body_bytes": None})
        request = self.get_success(
            self.handler._build_rust_request(config, None, to_token)
        )
        self.assertEqual(
            _as_json(request.body_for_test()),
            _as_json(_expected(SlidingSyncBody.model_validate(json.loads(text)))),
        )

    @property
    def store(self) -> Any:
        return self.hs.get_datastores().main
