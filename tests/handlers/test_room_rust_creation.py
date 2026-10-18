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

"""`rust_room_creation`: a new room's events built in one Rust call."""

from typing import Any
from unittest.mock import Mock, patch

from twisted.internet.testing import MemoryReactor

import synapse
from synapse.api.constants import EventTypes
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.events import EventBase
from synapse.handlers.room import rust_room_creation_counter
from synapse.metrics import SERVER_NAME_LABEL
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict, create_requester
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import override_config
from tests.utils import RUST_ROOM_CREATION_FOR_TESTS

# Room configurations that between them create every kind of event the room
# handler sends before any invites.
ROOM_CONFIGS: list[JsonDict] = [
    {},
    {"preset": "public_chat", "name": "A room", "topic": "A topic"},
    {"preset": "trusted_private_chat", "invite": ["@bob:test"]},
    {
        "initial_state": [
            {"type": "m.room.guest_access", "content": {"guest_access": "can_join"}},
            {"type": "m.space.child", "state_key": "!a:test", "content": {"via": []}},
            {
                "type": "m.room.power_levels",
                "content": {"users_default": 100, "ban": 50},
            },
            {"type": "m.room.encryption", "content": {"algorithm": "m.megolm"}},
        ],
        "room_alias_name": "alias",
    },
    {
        "power_level_content_override": {"events_default": 10},
        "creation_content": {"m.federate": False},
        "is_direct": True,
    },
]

# Room versions the Rust builder covers, one per event format.
ROOM_VERSIONS = ["3", "10", "11", "12"]


def _comparable(event: EventBase, keys: dict[str, tuple[str, str]]) -> JsonDict:
    """The event's PDU, with the events it refers to given by their `(type,
    state_key)` in `keys`, and without its hashes, signatures and ID.

    Python orders the auth events by iterating a set of `(type, state_key)`,
    which changes with the process's hash seed, so the IDs, which cover that
    order, cannot be matched. The order means nothing to the protocol, so the
    auth events are compared as a set.
    """
    pdu = event.get_pdu_json()
    pdu["auth_events"] = sorted(keys[e] for e in pdu["auth_events"])
    pdu["prev_events"] = [keys[e] for e in pdu["prev_events"]]
    for key in ("hashes", "signatures", "event_id"):
        pdu.pop(key, None)
    return pdu


def _rust_rooms(hs: HomeServer, result: str) -> float:
    return rust_room_creation_counter.labels(
        result=result, **{SERVER_NAME_LABEL: hs.hostname}
    )._value.get()


class RustRoomCreationTestCase(unittest.HomeserverTestCase):
    if RUST_ROOM_CREATION_FOR_TESTS == "off":
        skip = "SYNAPSE_TEST_RUST_ROOM_CREATION turns the Rust room builder off"

    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {}).setdefault(
            "rust_room_creation", "on"
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user_id = self.register_user("alice", "pass")
        self.tok = self.login("alice", "pass", device_id="DEVICE")
        self.register_user("bob", "pass")
        self.handler = hs.get_room_creation_handler()
        # Strict, whatever SYNAPSE_TEST_RUST_ROOM_CREATION_STRICT says.
        self.handler._rust_room_creation_strict = True
        self.store = hs.get_datastores().main

    def _create_room(self, room_version: str, config: JsonDict) -> str:
        body = dict(config)
        body["room_version"] = room_version
        if "room_alias_name" in body:
            # Aliases are unique.
            self._aliases = getattr(self, "_aliases", 0) + 1
            body["room_alias_name"] += str(self._aliases)
        channel = self.make_request("POST", "/createRoom", body, access_token=self.tok)
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body["room_id"]

    def _room_events(self, room_id: str) -> list[EventBase]:
        """The room's events in the order they were sent."""
        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                "events", {"room_id": room_id}, ("event_id", "stream_ordering")
            )
        )
        event_ids = [event_id for event_id, _ in sorted(rows, key=lambda r: r[1])]
        return self.get_success(self.store.get_events_as_list(event_ids))

    def test_same_events_as_python(self) -> None:
        """For each configuration and room version, create the room in Python,
        then build its events after the create event in Rust from the same
        inputs: they come out identical, event IDs included."""
        requester = create_requester(self.user_id, device_id="DEVICE")
        for room_version in ROOM_VERSIONS:
            for config in ROOM_CONFIGS:
                with self.subTest(room_version=room_version, config=config):
                    self.handler._rust_room_creation = False
                    try:
                        room_id = self._create_room(room_version, config)
                    finally:
                        self.handler._rust_room_creation = True

                    events = self._room_events(room_id)
                    create, rest = events[0], events[1:]
                    # Stop at the first invite, which `create_event` builds.
                    rest = [
                        e
                        for e in rest
                        if not (
                            e.type == EventTypes.Member and e.membership == "invite"
                        )
                    ]
                    # The clock does not move while the batch is built.
                    (ts,) = {e.origin_server_ts for e in rest}

                    with patch.object(
                        self.handler.clock,
                        "time_msec",
                        return_value=ts,
                    ):
                        built = self.handler._build_room_events_in_rust(
                            requester,
                            KNOWN_ROOM_VERSIONS[room_version],
                            room_id,
                            [create.event_id],
                            create.depth + 1,
                            {(create.type, create.state_key): create.event_id},
                            [(e.type, e.state_key, e.content) for e in rest],
                        )

                    self.assertEqual(len(built), len(rest))
                    python_keys = {e.event_id: (e.type, e.state_key) for e in events}
                    rust_keys = {
                        e.event_id: (e.type, e.state_key) for e in [create] + built
                    }
                    for ours, theirs in zip(built, rest):
                        self.assertEqual(
                            _comparable(ours, rust_keys),
                            _comparable(theirs, python_keys),
                        )
                        self.assertEqual(ours.internal_metadata.device_id, "DEVICE")

    def test_rooms_built_in_rust(self) -> None:
        """Rooms of every configuration are created with their events built in
        Rust, and end up with the state Python would give them."""
        for room_version in ROOM_VERSIONS:
            for config in ROOM_CONFIGS:
                with self.subTest(room_version=room_version, config=config):
                    before = _rust_rooms(self.hs, "rust")
                    room_id = self._create_room(room_version, config)
                    self.assertEqual(_rust_rooms(self.hs, "rust"), before + 1)

                    state = self.get_success(
                        self.hs.get_storage_controllers().state.get_current_state(
                            room_id
                        )
                    )
                    self.assertEqual(
                        state[(EventTypes.Member, self.user_id)].membership, "join"
                    )
                    self.assertIn((EventTypes.PowerLevels, ""), state)

    def test_v1_rooms_fall_back(self) -> None:
        before = _rust_rooms(self.hs, "fallback")
        self._create_room("1", {})
        self.assertEqual(_rust_rooms(self.hs, "fallback"), before + 1)

    def test_aliases_in_initial_state_fall_back(self) -> None:
        """`m.room.aliases` needs the join persisted on its own."""
        before = _rust_rooms(self.hs, "fallback")
        self._create_room(
            "10",
            {
                "initial_state": [
                    {"type": "m.room.aliases", "state_key": "test", "content": {}}
                ]
            },
        )
        self.assertEqual(_rust_rooms(self.hs, "fallback"), before + 1)

    def test_invalid_initial_state(self) -> None:
        """An event `validate_new` rejects fails the request as in Python."""
        channel = self.make_request(
            "POST",
            "/createRoom",
            {
                "initial_state": [
                    {"type": "m.room.power_levels", "content": {"ban": "high"}}
                ]
            },
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_BAD_JSON")

    def test_create_event_in_initial_state(self) -> None:
        """A second create event is refused by `create_event`, as in Python."""
        before = _rust_rooms(self.hs, "fallback")
        channel = self.make_request(
            "POST",
            "/createRoom",
            {
                "room_version": "10",
                "initial_state": [
                    {"type": "m.room.create", "content": {"room_version": "10"}}
                ],
            },
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_INVALID_PARAM")
        self.assertEqual(_rust_rooms(self.hs, "fallback"), before + 1)

    def test_bad_input_is_not_a_rust_error(self) -> None:
        """Input Rust cannot build and Python rejects gets Python's error, even
        in strict mode, and does not count as a Rust error."""
        before = _rust_rooms(self.hs, "error")
        channel = self.make_request(
            "POST",
            "/createRoom",
            {
                "initial_state": [
                    {
                        "type": "m.room.member",
                        "state_key": "@bob:test",
                        "content": {"displayname": "no membership"},
                    }
                ]
            },
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(_rust_rooms(self.hs, "error"), before)

    def test_rust_error_falls_back(self) -> None:
        """Outside strict mode, a Rust failure builds the room in Python."""
        self.handler._rust_room_creation_strict = False

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("boom")

        before = _rust_rooms(self.hs, "error")
        with patch.object(self.handler, "_event_signer") as signer:
            signer.build_room_events.side_effect = fail
            room_id = self._create_room("10", {"name": "A room"})
        self.assertEqual(_rust_rooms(self.hs, "error"), before + 1)
        events = self._room_events(room_id)
        self.assertEqual(
            [e.type for e in events][:3],
            [EventTypes.Create, EventTypes.Member, EventTypes.PowerLevels],
        )

    @override_config({"experimental_features": {"rust_room_creation": "off"}})
    def test_off(self) -> None:
        if RUST_ROOM_CREATION_FOR_TESTS:
            self.skipTest("SYNAPSE_TEST_RUST_ROOM_CREATION sets the mode")
        before = _rust_rooms(self.hs, "rust")
        self._create_room("10", {})
        self.assertEqual(_rust_rooms(self.hs, "rust"), before)

    def test_appservice_falls_back(self) -> None:
        """An appservice sender's events carry it in their context."""
        appservice = Mock()
        appservice.id = "an_appservice"
        requester = create_requester(self.user_id, app_service=appservice)
        self.assertTrue(
            self.handler._rust_room_creation_unsupported(
                requester, KNOWN_ROOM_VERSIONS["10"], False
            )
        )
        self.assertFalse(
            self.handler._rust_room_creation_unsupported(
                create_requester(self.user_id), KNOWN_ROOM_VERSIONS["10"], False
            )
        )
