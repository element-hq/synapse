#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

from typing import TypedDict

from signedjson.key import (
    decode_signing_key_base64,
    generate_signing_key,
    get_verify_key,
)
from signedjson.types import SigningKey

from synapse.api.room_versions import RoomVersions
from synapse.crypto.event_signing import (
    add_hashes_and_signatures,
    event_needs_resigning,
    resign_event,
)
from synapse.events import EventBase, make_event_from_dict
from synapse.types import JsonDict

from tests import unittest

# Perform these tests using given secret key so we get entirely deterministic
# signatures output that we can test against.
SIGNING_KEY_SEED = "YJDBA9Xnr2sVqXD9Vj7XVUnmFZcZrlw8Md7kMW+3XA1"

KEY_ALG = "ed25519"
KEY_VER = "1"
KEY_NAME = "%s:%s" % (KEY_ALG, KEY_VER)

HOSTNAME = "domain"


class EventSigningTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.signing_key: SigningKey = decode_signing_key_base64(
            KEY_ALG, KEY_VER, SIGNING_KEY_SEED
        )

    def test_sign_minimal(self) -> None:
        event_dict = {
            "event_id": "$0:domain",
            "origin_server_ts": 1000000,
            "signatures": {},
            "type": "X",
            "unsigned": {"age_ts": 1000000},
        }

        add_hashes_and_signatures(
            RoomVersions.V1, event_dict, HOSTNAME, self.signing_key
        )

        event = make_event_from_dict(event_dict)

        self.assertTrue(hasattr(event, "hashes"))
        self.assertIn("sha256", event.hashes)
        self.assertEqual(
            event.hashes["sha256"], "A6Nco6sqoy18PPfPDVdYvoowfc0PVBk9g9OiyT3ncRM"
        )

        self.assertTrue(hasattr(event, "signatures"))
        self.assertIn(HOSTNAME, event.signatures)
        self.assertIn(KEY_NAME, event.signatures["domain"])
        self.assertEqual(
            event.signatures[HOSTNAME][KEY_NAME],
            "PBc48yDVszWB9TRaB/+CZC1B+pDAC10F8zll006j+NN"
            "fe4PEMWcVuLaG63LFTK9e4rwJE8iLZMPtCKhDTXhpAQ",
        )

    def test_sign_message(self) -> None:
        event_dict = {
            "content": {"body": "Here is the message content"},
            "event_id": "$0:domain",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!r:domain",
            "sender": "@u:domain",
            "signatures": {},
            "unsigned": {"age_ts": 1000000},
        }

        add_hashes_and_signatures(
            RoomVersions.V1, event_dict, HOSTNAME, self.signing_key
        )

        event = make_event_from_dict(event_dict)

        self.assertTrue(hasattr(event, "hashes"))
        self.assertIn("sha256", event.hashes)
        self.assertEqual(
            event.hashes["sha256"], "rDCeYBepPlI891h/RkI2/Lkf9bt7u0TxFku4tMs7WKk"
        )

        self.assertTrue(hasattr(event, "signatures"))
        self.assertIn(HOSTNAME, event.signatures)
        self.assertIn(KEY_NAME, event.signatures["domain"])
        self.assertEqual(
            event.signatures[HOSTNAME][KEY_NAME],
            "Ay4aj2b5oJ1k8INYZ9n3KnszCflM0emwcmQQ7vxpbdc"
            "Sv9bkJxIZdWX1IJllcZLq89+D3sSabE+vqPtZs9akDw",
        )


class EventResigningTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.signing_key: SigningKey = decode_signing_key_base64(
            KEY_ALG, KEY_VER, SIGNING_KEY_SEED
        )

    def test_resign(self) -> None:
        event_dict: JsonDict = {
            "content": {"body": "Here is the message content"},
            "event_id": "$fffff:" + HOSTNAME,
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!r:" + HOSTNAME,
            "sender": "@u:" + HOSTNAME,
            "signatures": {},
            "unsigned": {"age_ts": 1000000},
        }
        add_hashes_and_signatures(
            RoomVersions.V1, event_dict, HOSTNAME, self.signing_key
        )
        event = make_event_from_dict(event_dict)
        self.assertIn(HOSTNAME, event.signatures)
        self.assertIn(KEY_NAME, event.signatures[HOSTNAME])
        signature = event.signatures[HOSTNAME][KEY_NAME]

        # Re-sign with a different key
        signing_key_2: SigningKey = generate_signing_key("2")
        key_name_2 = "ed25519:2"

        resigned_event = resign_event(event, HOSTNAME, signing_key_2)
        self.assertIn(HOSTNAME, resigned_event["signatures"])
        self.assertIn(key_name_2, resigned_event["signatures"][HOSTNAME])
        self.assertEqual(
            len(resigned_event["signatures"][HOSTNAME]), 1
        )  # the previous signature was removed.
        self.assertNotEqual(
            signature, resigned_event["signatures"][HOSTNAME][key_name_2]
        )  # different signatures

        # Repeat but with an event without any signatures.
        event_dict = {
            "content": {"body": "Here is the message content"},
            "event_id": "$fffff:" + HOSTNAME,
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!r:" + HOSTNAME,
            "sender": "@u:" + HOSTNAME,
            "signatures": {},
            "unsigned": {"age_ts": 1000000},
        }
        event = make_event_from_dict(event_dict)
        resigned_event = resign_event(event, HOSTNAME, signing_key_2)
        self.assertIn(HOSTNAME, resigned_event["signatures"])
        self.assertIn(key_name_2, resigned_event["signatures"][HOSTNAME])
        self.assertEqual(len(resigned_event["signatures"][HOSTNAME]), 1)

    def test_event_needs_resigning(self) -> None:
        event_that_needs_resigning_dict: JsonDict = {
            "content": {"body": "Here is the message content"},
            "event_id": "$fffff:" + HOSTNAME,
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!r:" + HOSTNAME,
            "sender": "@u:" + HOSTNAME,
            "unsigned": {"age_ts": 1000000},
        }
        internal_metadata: JsonDict = {}
        event_that_needs_resigning = make_event_from_dict(
            event_that_needs_resigning_dict,
            RoomVersions.V1,
            internal_metadata,
        )
        self.assertEqual(
            event_needs_resigning(
                event_that_needs_resigning, HOSTNAME, get_verify_key(self.signing_key)
            ),
            True,
        )

        class TestCase(TypedDict):
            name: str
            event: EventBase

        events_that_dont_need_resigning: list[TestCase] = [
            {
                "name": "sender domain isn't ours",
                "event": make_event_from_dict(
                    {**event_that_needs_resigning_dict, "sender": "@u:somewhereelse"},
                    RoomVersions.V1,
                    internal_metadata,
                ),
            },
            {
                "name": "already signed with this key",
                "event": make_event_from_dict(
                    {
                        **event_that_needs_resigning_dict,
                        "signatures": {
                            HOSTNAME: {
                                KEY_NAME: "thisisntchecked",
                            },
                        },
                    },
                    RoomVersions.V1,
                    internal_metadata,
                ),
            },
        ]
        for test_case in events_that_dont_need_resigning:
            self.assertEqual(
                event_needs_resigning(
                    test_case["event"], HOSTNAME, get_verify_key(self.signing_key)
                ),
                False,
                test_case["name"],
            )
