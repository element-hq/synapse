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
# <https://www.gnu.org/licenses/agpl_3.0.html>.
#
#


from unittest.mock import patch

from signedjson.key import encode_verify_key_base64, get_verify_key

from synapse.crypto import keyring
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.events import make_event_from_dict
from synapse.federation.federation_base import InvalidEventSignatureError

from tests import unittest


class FederationBaseTestCase(unittest.HomeserverTestCase):
    def test_events_signed_by_banned_key_are_refused(self) -> None:
        """Ensure that event JSON signed using a banned server_signing_key fails verification."""
        event_dict = {
            "content": {"body": "Here is the message content"},
            "event_id": "$0:domain",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!r:domain",
            "sender": f"@u:{self.hs.config.server.server_name}",
            "signatures": {},
            "unsigned": {"age_ts": 1000000},
        }

        add_hashes_and_signatures(
            self.hs.config.server.default_room_version,
            event_dict,
            self.hs.config.server.server_name,
            self.hs.signing_key,
        )
        event = make_event_from_dict(event_dict)
        fs = self.hs.get_federation_server()

        # Ensure the signatures check out normally
        self.get_success(
            fs._check_sigs_and_hash(self.hs.config.server.default_room_version, event)
        )

        # Patch the list of banned signing keys and ensure the signature check fails
        with patch.object(
            keyring,
            "BANNED_SERVER_SIGNING_KEYS",
            (encode_verify_key_base64(get_verify_key(self.hs.signing_key))),
        ):
            self.get_failure(
                fs._check_sigs_and_hash(
                    self.hs.config.server.default_room_version, event
                ),
                InvalidEventSignatureError,
            )
