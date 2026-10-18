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

import copy

from canonicaljson import encode_canonical_json
from signedjson.key import decode_signing_key_base64, get_verify_key
from signedjson.sign import verify_signed_json

from synapse.api.errors import SynapseError
from synapse.api.room_versions import (
    KNOWN_ROOM_VERSIONS,
    EventFormatVersions,
    RoomVersion,
)
from synapse.crypto.event_signing import (
    add_hashes_and_signatures,
    check_event_content_hash,
)
from synapse.events import EventBase, make_event_from_dict
from synapse.events.builder import create_local_event_from_event_dict
from synapse.synapse_rust.events import LocalEventSigner, redact_event_dict
from synapse.types import JsonDict

from tests import unittest
from tests.server import get_clock

SIGNING_KEY = decode_signing_key_base64(
    "ed25519", "1", "YJDBA9Xnr2sVqXD9Vj7XVUnmFZcZrlw8Md7kMW+3XA1"
)
HOSTNAME = "domain"

# A v12 (MSC4291) room ID: the create event's reference hash.
HASH_ROOM_ID = "!" + "a" * 43


def _event_dict(room_version: RoomVersion, **fields: object) -> JsonDict:
    """A member event dict with the fields `create_local_event_from_event_dict`
    is given for `room_version`, updated with `fields`."""
    event: JsonDict = {
        "type": "m.room.member",
        "state_key": "@alice:domain",
        "sender": "@alice:domain",
        "room_id": "!room:domain",
        "content": {"membership": "join", "displayname": "Alice"},
        "depth": 3,
        "origin_server_ts": 1000,
        "unsigned": {"age_ts": 1000},
        "signatures": {},
    }
    if room_version.event_format == EventFormatVersions.ROOM_V1_V2:
        event["event_id"] = "$event:domain"
        event["auth_events"] = [["$auth:domain", {"sha256": "aaa"}]]
        event["prev_events"] = [["$prev:domain", {"sha256": "bbb"}]]
    else:
        event["auth_events"] = ["$auth"]
        event["prev_events"] = ["$prev"]
    if room_version.msc4291_room_ids_as_hashes:
        event["room_id"] = HASH_ROOM_ID
    if room_version.msc4242_state_dags:
        del event["auth_events"]
        event["prev_state_events"] = ["$state"]
    event.update(fields)
    return event


def _python_build(room_version: RoomVersion, event_dict: JsonDict) -> EventBase:
    """The Python path the Rust signer replaces."""
    add_hashes_and_signatures(room_version, event_dict, HOSTNAME, SIGNING_KEY)
    return make_event_from_dict(event_dict, room_version)


class LocalEventSignerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.signer = LocalEventSigner(HOSTNAME, SIGNING_KEY)

    def _assert_same_as_python(
        self, room_version: RoomVersion, event_dict: JsonDict
    ) -> EventBase:
        expected = _python_build(room_version, copy.deepcopy(event_dict))
        event = self.signer.build_event(
            encode_canonical_json(event_dict), room_version, {}
        )
        self.assertEqual(event.event_id, expected.event_id)
        self.assertEqual(event.room_id, expected.room_id)
        self.assertEqual(event.get_pdu_json(), expected.get_pdu_json())
        self.assertEqual(event.get_dict(), expected.get_dict())
        return event

    def test_all_room_versions(self) -> None:
        for room_version in KNOWN_ROOM_VERSIONS.values():
            with self.subTest(room_version=room_version.identifier):
                event = self._assert_same_as_python(
                    room_version, _event_dict(room_version)
                )

                # The hash and signature check out the way remote servers
                # check them.
                self.assertTrue(check_event_content_hash(event))
                verify_signed_json(
                    redact_event_dict(room_version, event.get_pdu_json()),
                    HOSTNAME,
                    get_verify_key(SIGNING_KEY),
                )

    def test_create_event_without_room_id(self) -> None:
        for room_version in KNOWN_ROOM_VERSIONS.values():
            if not room_version.msc4291_room_ids_as_hashes:
                continue
            with self.subTest(room_version=room_version.identifier):
                event_dict = _event_dict(
                    room_version,
                    type="m.room.create",
                    state_key="",
                    content={"room_version": room_version.identifier},
                    prev_events=[],
                    depth=1,
                )
                del event_dict["room_id"]
                event_dict.pop("auth_events", None)
                if room_version.msc4242_state_dags:
                    event_dict["prev_state_events"] = []
                else:
                    event_dict["auth_events"] = []
                self._assert_same_as_python(room_version, event_dict)

    def test_awkward_content(self) -> None:
        """Strings that canonical JSON escapes, and numbers outside the
        canonical range that old room versions accept."""
        for room_version in KNOWN_ROOM_VERSIONS.values():
            with self.subTest(room_version=room_version.identifier):
                self._assert_same_as_python(
                    room_version,
                    _event_dict(
                        room_version,
                        content={
                            "membership": "join",
                            "displayname": 'é \x01"\\ 🐈',
                            "nested": {"z": [1, {"b": None, "a": True}], "a": -5},
                        },
                    ),
                )

        for room_version in KNOWN_ROOM_VERSIONS.values():
            if room_version.strict_canonicaljson:
                continue
            with self.subTest(room_version=room_version.identifier, numbers=True):
                self._assert_same_as_python(
                    room_version,
                    _event_dict(
                        room_version,
                        content={"membership": "join", "big": 2**70, "float": 1.5},
                    ),
                )

    def test_keeps_existing_hashes_and_signatures(self) -> None:
        """A make_join template can carry the remote server's hashes and
        signatures, and fields that are not part of the federated event."""
        for room_version in KNOWN_ROOM_VERSIONS.values():
            with self.subTest(room_version=room_version.identifier):
                self._assert_same_as_python(
                    room_version,
                    _event_dict(
                        room_version,
                        hashes={"sha256": "old", "other": "kept"},
                        signatures={
                            "remote": {"ed25519:r": "sig"},
                            HOSTNAME: {"ed25519:0": "old"},
                        },
                        origin="domain",
                        outlier=True,
                        destinations={"remote": 1},
                        unsigned={"age_ts": 1000, "foo": "bar"},
                    ),
                )

    def test_invalid_event(self) -> None:
        """An event the room version cannot parse is a 400, as before."""
        room_version = KNOWN_ROOM_VERSIONS["10"]
        event_dict = _event_dict(room_version, depth="three")
        with self.assertRaises(SynapseError):
            _python_build(room_version, copy.deepcopy(event_dict))
        with self.assertRaises(ValueError):
            self.signer.build_event(encode_canonical_json(event_dict), room_version, {})

        _, clock = get_clock()
        with self.assertRaises(SynapseError) as cm:
            create_local_event_from_event_dict(
                clock, HOSTNAME, self.signer, room_version, event_dict
            )
        self.assertEqual(cm.exception.code, 400)

    def test_internal_metadata(self) -> None:
        room_version = KNOWN_ROOM_VERSIONS["10"]
        event = self.signer.build_event(
            encode_canonical_json(_event_dict(room_version)),
            room_version,
            {"txn_id": "t1", "outlier": False},
        )
        self.assertEqual(event.internal_metadata.txn_id, "t1")
