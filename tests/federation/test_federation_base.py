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


import string
from unittest.mock import patch

from signedjson.key import (
    encode_verify_key_base64,
    generate_signing_key,
    get_verify_key,
)
from signedjson.sign import sign_json

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventContentFields, MemberKeys
from synapse.api.room_versions import RoomVersions
from synapse.crypto import keyring
from synapse.crypto.event_signing import (
    EventSigner,
    add_hashes_and_signatures,
    add_hashes_and_signatures_from_signers,
    compute_event_signature,
)
from synapse.events import EventBase, make_event_from_dict
from synapse.events.utils import prune_event
from synapse.federation.federation_base import InvalidEventSignatureError
from synapse.server import HomeServer
from synapse.types import JsonDict, MemberKey
from synapse.util.clock import Clock

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
            "auth_events": [],
            "prev_events": [],
            "depth": 1,
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


def _non_canonical_twin(member_key: str) -> str:
    """The same public key with a spare bit of the last base64 character set:
    decodes to the same 32 bytes, but is not the canonical encoding."""
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
    return member_key[:-1] + alphabet[alphabet.index(member_key[-1]) + 1]


class MemberKeySignatureTestCase(unittest.HomeserverTestCase):
    """MSC4430: events in a member key room are verified against the member key in
    their `sender`, with no server signature and no key fetching."""

    room_version = RoomVersions.MSC4430v12
    room_id = "!K3DOWWLmkHLl52yJ-vT8J5jX5wuYZati_KvC6PliIPE"

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.member_signing_key = generate_signing_key(MemberKeys.KEY_VERSION)
        self.member_key = MemberKey.from_signing_key(
            self.member_signing_key
        ).to_string()
        self.other_signing_key = generate_signing_key(MemberKeys.KEY_VERSION)
        self.other_key = MemberKey.from_signing_key(self.other_signing_key).to_string()

    def _event_dict(self, sender: str, **overrides: object) -> JsonDict:
        event_dict: JsonDict = {
            "room_id": self.room_id,
            "type": "m.room.message",
            "sender": sender,
            "content": {"body": "hello"},
            "depth": 1,
            "origin_server_ts": 1,
            "prev_events": [],
            "prev_state_events": [],
        }
        event_dict.update(overrides)
        return event_dict

    def _sign(self, event_dict: JsonDict, *signers: EventSigner) -> JsonDict:
        add_hashes_and_signatures_from_signers(
            self.room_version, event_dict, signers, server_name=self.hs.hostname
        )
        return event_dict

    def _check(self, event_dict: JsonDict) -> EventBase:
        event = make_event_from_dict(event_dict, self.room_version)
        return self.get_success(
            self.hs.get_federation_server()._check_sigs_and_hash(
                self.room_version, event
            )
        )

    def _check_fails(self, event_dict: JsonDict, reason: str) -> None:
        event = make_event_from_dict(event_dict, self.room_version)
        failure = self.get_failure(
            self.hs.get_federation_server()._check_sigs_and_hash(
                self.room_version, event
            ),
            InvalidEventSignatureError,
        )
        self.assertIn(reason, str(failure.value))

    def test_signed_by_sender(self) -> None:
        event_dict = self._sign(
            self._event_dict(self.member_key),
            (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
        )
        checked = self._check(event_dict)
        self.assertEqual(checked.sender, self.member_key)
        # Neither redacted nor soft-failed.
        self.assertEqual(checked.content["body"], "hello")
        self.assertFalse(checked.internal_metadata.is_soft_failed())

        # The signature covers `hashes`: a changed content hash is a signature
        # failure, not merely a hash mismatch (which would return a redaction).
        hashes_changed = self._sign(
            self._event_dict(self.member_key),
            (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
        )
        hashes_changed["hashes"]["sha256"] = (
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        self._check_fails(hashes_changed, "unable to verify member key signature")

    def test_verified_json_is_the_redacted_event(self) -> None:
        """What is verified is the redacted event without `unsigned`, as for
        server signatures: a signature made that way by hand, without the signing
        helpers, is accepted."""
        event_dict = self._event_dict(self.member_key, unsigned={"age": 1})
        add_hashes_and_signatures_from_signers(
            self.room_version, event_dict, [], server_name=self.hs.hostname
        )
        to_sign = prune_event(
            make_event_from_dict(event_dict, self.room_version)
        ).get_pdu_json()
        to_sign.pop("unsigned", None)
        signed = sign_json(
            to_sign, MemberKeys.SIGNATURE_ENTITY, self.member_signing_key
        )
        event_dict["signatures"] = signed["signatures"]
        self.assertEqual(self._check(event_dict).content["body"], "hello")

    def test_missing_or_wrong_key_id(self) -> None:
        # No signatures at all.
        unsigned = self._sign(self._event_dict(self.member_key))
        self._check_fails(unsigned, "Not signed by sender")

        # Signed under `sender`, but with a key ID other than `ed25519:1`.
        other_version = generate_signing_key("2")
        event_dict = self._event_dict(
            MemberKey.from_signing_key(other_version).to_string()
        )
        add_hashes_and_signatures_from_signers(
            self.room_version, event_dict, [], server_name=self.hs.hostname
        )
        event_dict["signatures"] = compute_event_signature(
            self.room_version,
            event_dict,
            signature_name=MemberKeys.SIGNATURE_ENTITY,
            signing_key=other_version,
        )
        self.assertEqual(
            list(event_dict["signatures"][MemberKeys.SIGNATURE_ENTITY]),
            ["ed25519:2"],
        )
        self._check_fails(event_dict, "ed25519:1")

    def test_signed_by_another_member_key(self) -> None:
        """A valid signature by a different key than the one `sender` names."""
        event_dict = self._sign(
            self._event_dict(self.other_key),
            (MemberKeys.SIGNATURE_ENTITY, self.other_signing_key),
        )
        event_dict["sender"] = self.member_key
        self._check_fails(event_dict, "unable to verify member key signature")

    def test_server_signature_is_not_enough(self) -> None:
        """A signature by our server, as in every other room version, is not a
        signature by the sender."""
        event_dict = self._event_dict(self.member_key)
        add_hashes_and_signatures(
            self.room_version, event_dict, self.hs.hostname, self.hs.signing_key
        )
        self._check_fails(event_dict, "unable to verify member key signature")

    def test_sender_must_be_a_canonical_member_key(self) -> None:
        legacy_dict = self._event_dict(f"@alice:{self.hs.hostname}")
        add_hashes_and_signatures(
            self.room_version, legacy_dict, self.hs.hostname, self.hs.signing_key
        )
        self._check_fails(legacy_dict, "is not a valid member key")

        # The same 32 bytes with a spare bit of the last character set: not
        # canonical, so not the member's identity.
        event_dict = self._sign(
            self._event_dict(self.member_key),
            (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
        )
        event_dict["sender"] = _non_canonical_twin(self.member_key)
        self._check_fails(event_dict, "is not a valid member key")

    def test_restricted_join_authoriser(self) -> None:
        """The authorising member of a restricted join signs under the member key
        string itself, and that signature is verified with that key."""

        def join_dict() -> JsonDict:
            return self._event_dict(
                self.member_key,
                type="m.room.member",
                state_key=self.member_key,
                content={
                    "membership": "join",
                    EventContentFields.AUTHORISING_USER: self.other_key,
                },
            )

        both = self._sign(
            join_dict(),
            (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
            (self.other_key, self.other_signing_key),
        )
        self.assertEqual(self._check(both).sender, self.member_key)

        joiner_only = self._sign(
            join_dict(), (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key)
        )
        self._check_fails(joiner_only, f"under {self.other_key!r}")

        # A signature under the authoriser's name made by some other key (added
        # directly, as a forger would; our own helpers refuse to make one).
        forged = self._sign(
            join_dict(), (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key)
        )
        forged["signatures"] = compute_event_signature(
            self.room_version,
            forged,
            signature_name=self.other_key,
            signing_key=generate_signing_key(MemberKeys.KEY_VERSION),
        )
        self._check_fails(forged, f"under {self.other_key!r}")

        # The authoriser is not a member key at all. (Set before signing: the
        # field survives redaction, so changing it afterwards would break the
        # sender's signature first.)
        not_a_key = join_dict()
        not_a_key["content"][EventContentFields.AUTHORISING_USER] = (
            f"@mod:{self.hs.hostname}"
        )
        self._sign(not_a_key, (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key))
        self._check_fails(not_a_key, "authorising user")

        not_a_string = join_dict()
        not_a_string["content"][EventContentFields.AUTHORISING_USER] = {}
        self._sign(not_a_string, (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key))
        self._check_fails(not_a_string, "authorising user")

        # Only joins have an authoriser: a leave carrying the field needs no
        # second signature, and a membership event without `membership` is
        # rejected cleanly rather than crashing.
        leave = join_dict()
        leave["content"]["membership"] = "leave"
        self._sign(leave, (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key))
        self.assertEqual(self._check(leave).sender, self.member_key)
        no_membership = join_dict()
        del no_membership["content"]["membership"]
        self._sign(
            no_membership, (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key)
        )
        self.assertEqual(self._check(no_membership).sender, self.member_key)

    def test_tampered_content_is_redacted(self) -> None:
        """The signature is over the redacted event, so a content change passes
        the signature check and fails the hash check, as in every room version."""
        event_dict = self._sign(
            self._event_dict(self.member_key),
            (MemberKeys.SIGNATURE_ENTITY, self.member_signing_key),
        )
        event_dict["content"]["body"] = "tampered"
        checked = self._check(event_dict)
        self.assertNotIn("body", checked.content)
