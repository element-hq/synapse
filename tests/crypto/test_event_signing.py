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

from signedjson.key import decode_signing_key_base64
from signedjson.types import SigningKey

from synapse.api.room_versions import RoomVersions
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.events import make_event_from_dict

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
            "origin": "domain",
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
            event.hashes["sha256"], "6tJjLpXtggfke8UxFhAKg82QVkJzvKOVOOSjUDK4ZSI"
        )

        self.assertTrue(hasattr(event, "signatures"))
        self.assertIn(HOSTNAME, event.signatures)
        self.assertIn(KEY_NAME, event.signatures["domain"])
        self.assertEqual(
            event.signatures[HOSTNAME][KEY_NAME],
            "2Wptgo4CwmLo/Y8B8qinxApKaCkBG2fjTWB7AbP5Uy+"
            "aIbygsSdLOFzvdDjww8zUVKCmI02eP9xtyJxc/cLiBA",
        )

    def test_sign_message(self) -> None:
        event_dict = {
            "content": {"body": "Here is the message content"},
            "event_id": "$0:domain",
            "origin": "domain",
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
            event.hashes["sha256"], "onLKD1bGljeBWQhWZ1kaP9SorVmRQNdN5aM2JYU2n/g"
        )

        self.assertTrue(hasattr(event, "signatures"))
        self.assertIn(HOSTNAME, event.signatures)
        self.assertIn(KEY_NAME, event.signatures["domain"])
        self.assertEqual(
            event.signatures[HOSTNAME][KEY_NAME],
            "Wm+VzmOUOz08Ds+0NTWb1d4CZrVsJSikkeRxh6aCcUw"
            "u6pNC78FunoD7KNWzqFn241eYHYMGCA5McEiVPdhzBA",
        )
