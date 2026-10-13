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

import copy
from typing import TypedDict

from signedjson.key import (
    decode_signing_key_base64,
    generate_signing_key,
    get_verify_key,
)
from signedjson.sign import SignatureVerifyException, verify_signed_json
from signedjson.types import SigningKey

from synapse.api.constants import EventContentFields, MemberKeys
from synapse.api.errors import MemberKeySigningRefusedError, SynapseError
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.crypto.event_signing import (
    EventSigner,
    add_hashes_and_signatures,
    add_hashes_and_signatures_from_signers,
    check_event_content_hash,
    compute_member_key_room_signature,
    event_needs_resigning,
    resign_event,
)
from synapse.events import EventBase, make_event_from_dict
from synapse.events.utils import prune_event
from synapse.types import JsonDict, MemberKey

from tests import unittest
from tests.test_utils.event_builders import make_test_event

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
            "content": {},
            "unsigned": {"age_ts": 1000000},
        }

        add_hashes_and_signatures(
            RoomVersions.V1, event_dict, HOSTNAME, self.signing_key
        )

        event = make_test_event(event_dict)

        self.assertTrue(hasattr(event, "hashes"))
        self.assertIn("sha256", event.hashes)
        self.assertEqual(
            event.hashes["sha256"], "mq4QfPPpC+QsBd6eqfVsmJIEz8uvMSVK0+AU67PLESk"
        )

        self.assertTrue(hasattr(event, "signatures"))
        self.assertIn(HOSTNAME, event.signatures)
        self.assertIn(KEY_NAME, event.signatures["domain"])
        self.assertEqual(
            event.signatures[HOSTNAME][KEY_NAME],
            "18rGIkd4JJXxw9m+1j3BtN+TmqmLip4VHvFbyXLngpB"
            "LXOqbxlQViQABRzep2cODQ2aa5FnFgz+Llt2P03WiAw",
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

        event = make_test_event(event_dict)

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
        event = make_test_event(event_dict)
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
        event = make_test_event(event_dict)
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
        event_that_needs_resigning = make_test_event(
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
                "event": make_test_event(
                    {**event_that_needs_resigning_dict, "sender": "@u:somewhereelse"},
                    RoomVersions.V1,
                    internal_metadata,
                ),
            },
            {
                "name": "already signed with this key",
                "event": make_test_event(
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


class MemberKeySigningTestCase(unittest.TestCase):
    """MSC4430: signing events as a member key, and co-signing linkage information."""

    room_version = RoomVersions.MSC4430v12
    server_name = "example.com"

    def setUp(self) -> None:
        self.member_signing_key = generate_signing_key(MemberKeys.KEY_VERSION)
        self.member_key = MemberKey.from_signing_key(self.member_signing_key)
        self.other_signing_key = generate_signing_key(MemberKeys.KEY_VERSION)
        self.other_member_key = MemberKey.from_signing_key(self.other_signing_key)
        self.server_signing_key = generate_signing_key("srv")

    def _member_event_dict(self) -> JsonDict:
        return {
            "type": "m.room.member",
            "state_key": self.member_key.to_string(),
            "sender": self.member_key.to_string(),
            "room_id": "!K3DOWWLmkHLl52yJ-vT8J5jX5wuYZati_KvC6PliIPE",
            "content": {
                "membership": "join",
                "localpart": "alice",
                "domain": self.server_name,
                "displayname": "Alice",
            },
            "depth": 1,
            "origin_server_ts": 1,
            "prev_events": [],
            "prev_state_events": [],
        }

    def _restricted_join_dict(self) -> JsonDict:
        event_dict = self._member_event_dict()
        event_dict["content"][EventContentFields.AUTHORISING_USER] = (
            self.other_member_key.to_string()
        )
        return event_dict

    def _sign(self, event_dict: JsonDict, *signers: EventSigner) -> EventBase:
        add_hashes_and_signatures_from_signers(
            self.room_version, event_dict, signers, server_name=self.server_name
        )
        return make_event_from_dict(event_dict, self.room_version)

    def _redacted_pdu(self, event: EventBase) -> JsonDict:
        """What a verifier checks signatures over: the redacted event."""
        pdu = prune_event(event).get_pdu_json()
        pdu.pop("unsigned", None)
        return pdu

    def _signatures(self, event: EventBase) -> dict[str, dict[str, str]]:
        return event.get_pdu_json()["signatures"]

    def _assert_refused(
        self, event_dict: JsonDict, signer: EventSigner, reason: str
    ) -> None:
        with self.assertRaises(MemberKeySigningRefusedError) as cm:
            self._sign(event_dict, signer)
        self.assertEqual(cm.exception.code, 500)
        self.assertEqual(cm.exception.entity, signer[0])
        self.assertIn(reason, cm.exception.reason)

    def test_sign_as_member_key(self) -> None:
        event = self._sign(
            self._member_event_dict(),
            (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
        )

        self.assertTrue(check_event_content_hash(event))
        # Exactly one signature: entity `sender`, key ID `ed25519:1`, and no
        # server-name entity.
        self.assertEqual(
            {k: list(v) for k, v in self._signatures(event).items()},
            {MemberKeys.SIGNATURE_ENTITY: [MemberKeys.KEY_ID]},
        )
        # It verifies with the key decoded from `sender`, over the redacted event.
        verify_signed_json(
            self._redacted_pdu(event),
            MemberKeys.SIGNATURE_ENTITY,
            MemberKey.from_string(event.sender).verify_key(),
        )
        # ... and not with any other member key.
        with self.assertRaises(SignatureVerifyException):
            verify_signed_json(
                self._redacted_pdu(event),
                MemberKeys.SIGNATURE_ENTITY,
                self.other_member_key.verify_key(),
            )

    def test_server_cosignature(self) -> None:
        event = self._sign(
            self._member_event_dict(),
            (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
            (self.server_name, self.server_signing_key),
        )

        # Both signatures are present: the second signer added to the first.
        self.assertEqual(
            set(self._signatures(event)),
            {MemberKeys.SIGNATURE_ENTITY, self.server_name},
        )
        redacted = self._redacted_pdu(event)
        verify_signed_json(
            redacted, MemberKeys.SIGNATURE_ENTITY, self.member_key.verify_key()
        )
        verify_signed_json(
            redacted, self.server_name, get_verify_key(self.server_signing_key)
        )

    def test_authorising_member_key_signature(self) -> None:
        """The authorising member of a restricted join signs under the member key
        string itself (MSC-QUESTIONS §2)."""
        event = self._sign(
            self._restricted_join_dict(),
            (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
            (self.other_member_key.to_string(), self.other_signing_key),
        )

        self.assertEqual(
            set(self._signatures(event)),
            {MemberKeys.SIGNATURE_ENTITY, self.other_member_key.to_string()},
        )
        self.assertEqual(
            list(self._signatures(event)[self.other_member_key.to_string()]),
            [MemberKeys.KEY_ID],
        )
        redacted = self._redacted_pdu(event)
        verify_signed_json(
            redacted, MemberKeys.SIGNATURE_ENTITY, self.member_key.verify_key()
        )
        verify_signed_json(
            redacted,
            self.other_member_key.to_string(),
            self.other_member_key.verify_key(),
        )

    def test_add_signature_to_received_event(self) -> None:
        """`compute_member_key_room_signature` adds to an already hashed and signed
        event (one from another server) without recomputing its hash: the
        resident server adding the authoriser's signature on `/send_join`, or the
        invitee's server co-signing an inbound `/invite`."""
        # A restricted join signed by its (remote) sender only.
        event_dict = self._restricted_join_dict()
        add_hashes_and_signatures_from_signers(
            self.room_version,
            event_dict,
            [(MemberKeys.SIGNATURE_ENTITY, self.member_signing_key)],
            server_name="remote.example",
        )
        hashes_before = copy.deepcopy(event_dict["hashes"])

        event_dict["signatures"] = compute_member_key_room_signature(
            self.room_version,
            event_dict,
            (self.other_member_key.to_string(), self.other_signing_key),
            server_name=self.server_name,
        )

        self.assertEqual(event_dict["hashes"], hashes_before)
        event = make_event_from_dict(event_dict, self.room_version)
        redacted = self._redacted_pdu(event)
        # The original signature is intact and the new one verifies.
        verify_signed_json(
            redacted, MemberKeys.SIGNATURE_ENTITY, self.member_key.verify_key()
        )
        verify_signed_json(
            redacted,
            self.other_member_key.to_string(),
            self.other_member_key.verify_key(),
        )

        # The same checks apply: this is not our key.
        with self.assertRaises(MemberKeySigningRefusedError):
            compute_member_key_room_signature(
                self.room_version,
                event_dict,
                (self.other_member_key.to_string(), self.member_signing_key),
                server_name=self.server_name,
            )

    def test_localpart_dropped_domain_kept(self) -> None:
        """Redaction drops the claimed `localpart` and keeps the claimed `domain`,
        so the signatures (made over the redacted form) and the reference hash
        cover `domain` but not `localpart`."""
        event = self._sign(
            self._member_event_dict(),
            (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
            (self.server_name, self.server_signing_key),
        )

        redacted = prune_event(event)
        self.assertNotIn("localpart", redacted.content)
        self.assertNotIn("displayname", redacted.content)
        self.assertEqual(redacted.content["domain"], self.server_name)
        self.assertEqual(redacted.content["membership"], "join")

        # Both signatures verify over the redacted form, i.e. without `localpart`...
        pdu = self._redacted_pdu(event)
        verify_signed_json(
            pdu, MemberKeys.SIGNATURE_ENTITY, self.member_key.verify_key()
        )
        verify_signed_json(
            pdu, self.server_name, get_verify_key(self.server_signing_key)
        )
        # ... and cover `domain`: changing it invalidates both.
        tampered = copy.deepcopy(pdu)
        tampered["content"]["domain"] = "evil.example"
        with self.assertRaises(SignatureVerifyException):
            verify_signed_json(
                tampered, MemberKeys.SIGNATURE_ENTITY, self.member_key.verify_key()
            )
        with self.assertRaises(SignatureVerifyException):
            verify_signed_json(
                tampered, self.server_name, get_verify_key(self.server_signing_key)
            )

        # The event ID (the reference hash) covers `domain` too...
        self.assertNotEqual(
            self._event_id_with_domain(event.get_pdu_json(), self.room_version),
            event.event_id,
        )
        # ... whereas in a room version without member keys `domain` is redacted
        # away, so it is covered by neither.
        legacy_dict = self._member_event_dict()
        add_hashes_and_signatures(
            RoomVersions.MSC4242v12,
            legacy_dict,
            self.server_name,
            self.server_signing_key,
        )
        legacy = make_event_from_dict(legacy_dict, RoomVersions.MSC4242v12)
        self.assertNotIn("domain", prune_event(legacy).content)
        self.assertEqual(
            self._event_id_with_domain(legacy_dict, RoomVersions.MSC4242v12),
            legacy.event_id,
        )

    def _event_id_with_domain(
        self, event_dict: JsonDict, room_version: RoomVersion
    ) -> str:
        """The event ID of a copy of the event with `content.domain` changed but
        `hashes` kept."""
        changed = copy.deepcopy(event_dict)
        changed["content"]["domain"] = "evil.example"
        return make_event_from_dict(changed, room_version).event_id

    def test_no_signers_only_hashes(self) -> None:
        """A template built for a remote member is hashed but not signed."""
        event_dict = self._member_event_dict()
        add_hashes_and_signatures_from_signers(
            self.room_version, event_dict, [], server_name=self.server_name
        )
        self.assertIn("sha256", event_dict["hashes"])
        self.assertNotIn("signatures", event_dict)

    def test_refuses_wrong_key_for_sender(self) -> None:
        """A `sender` signature must be made by the key `sender` names, so a bug in
        choosing the signer cannot make us sign as a key we do not hold."""
        self._assert_refused(
            self._member_event_dict(),
            (MemberKeys.SIGNATURE_ENTITY, self.other_signing_key),
            "not the event's sender",
        )

        # The key ID is fixed to `ed25519:1` by the MSC.
        wrong_version = generate_signing_key("2")
        event_dict = self._member_event_dict()
        event_dict["sender"] = event_dict["state_key"] = MemberKey.from_signing_key(
            wrong_version
        ).to_string()
        self._assert_refused(
            event_dict, (MemberKeys.SIGNATURE_ENTITY, wrong_version), "key ID"
        )

    def test_refuses_authorising_signature_not_for_a_restricted_join(self) -> None:
        not_a_join = "only the authorising member key of a restricted join"
        # Not named in `join_authorised_via_users_server` at all...
        self._assert_refused(
            self._member_event_dict(),
            (self.other_member_key.to_string(), self.other_signing_key),
            not_a_join,
        )
        # ... or named, but signed with a different key...
        self._assert_refused(
            self._restricted_join_dict(),
            (self.other_member_key.to_string(), self.member_signing_key),
            "not that member key",
        )
        # ... or named, but not on a join.
        for membership in ("leave", "ban", "invite"):
            event_dict = self._restricted_join_dict()
            event_dict["content"]["membership"] = membership
            self._assert_refused(
                event_dict,
                (self.other_member_key.to_string(), self.other_signing_key),
                not_a_join,
            )
        event_dict = self._restricted_join_dict()
        event_dict["type"] = "m.room.message"
        del event_dict["state_key"]
        self._assert_refused(
            event_dict,
            (self.other_member_key.to_string(), self.other_signing_key),
            not_a_join,
        )

    def test_refuses_server_cosignature_on_wrong_event(self) -> None:
        """A server co-signs only `m.room.member` events whose `content.domain` is
        itself: our co-signature is a statement about our own user."""
        # Another server's name, even if it matches `content.domain`.
        event_dict = self._member_event_dict()
        event_dict["content"]["domain"] = "other.example"
        self._assert_refused(
            event_dict, ("other.example", self.server_signing_key), "not this server"
        )
        # Our name, on another domain's linkage information.
        self._assert_refused(
            event_dict, (self.server_name, self.server_signing_key), "content.domain"
        )
        # No linkage information at all.
        event_dict = self._member_event_dict()
        del event_dict["content"]["domain"]
        self._assert_refused(
            event_dict, (self.server_name, self.server_signing_key), "content.domain"
        )
        # Not a membership event.
        event_dict = self._member_event_dict()
        event_dict["type"] = "m.room.message"
        del event_dict["state_key"]
        self._assert_refused(
            event_dict,
            (self.server_name, self.server_signing_key),
            "only co-sign m.room.member",
        )

    def test_refuses_non_member_key_room(self) -> None:
        for fn in (
            lambda: add_hashes_and_signatures_from_signers(
                RoomVersions.MSC4242v12,
                self._member_event_dict(),
                [],
                server_name=self.server_name,
            ),
            lambda: compute_member_key_room_signature(
                RoomVersions.MSC4242v12,
                self._member_event_dict(),
                (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
                server_name=self.server_name,
            ),
        ):
            with self.assertRaises(SynapseError) as cm:
                fn()
            self.assertEqual(cm.exception.code, 500)
