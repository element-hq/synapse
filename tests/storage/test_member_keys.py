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
"""Tests for the MSC4430 member key storage: the keys we hold and the links we
learn. Runs on SQLite and, when `SYNAPSE_POSTGRES` is set, Postgres."""

import attr
from signedjson.key import generate_signing_key
from signedjson.types import SigningKey

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import MemberKeys
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.member_keys import (
    LocalMemberKey,
    MemberLink,
    decode_member_signing_key,
    encode_member_signing_key,
)
from synapse.types import MemberKey
from synapse.util.clock import Clock

from tests import unittest

ROOM_A = "!K3DOWWLmkHLl52yJ-vT8J5jX5wuYZati_KvC6PliIPE"
ROOM_B = "!oF3b2QiDEjKZbBYbuSVYn_9ygzXlDzYFmzAZeDTAI7U"
ALICE = "@alice:test"
BOB = "@bob:test"


def _new_signing_key() -> SigningKey:
    return generate_signing_key(MemberKeys.KEY_VERSION)


def _member_key(signing_key: SigningKey) -> str:
    return MemberKey.from_signing_key(signing_key).to_string()


class MemberSigningKeyEncodingTestCase(unittest.TestCase):
    def test_round_trip(self) -> None:
        signing_key = _new_signing_key()
        decoded = decode_member_signing_key(encode_member_signing_key(signing_key))
        self.assertEqual(bytes(decoded), bytes(signing_key))
        self.assertEqual(decoded.alg, MemberKeys.KEY_ALGORITHM)
        self.assertEqual(decoded.version, MemberKeys.KEY_VERSION)
        self.assertEqual(_member_key(decoded), _member_key(signing_key))


class MemberLinkTypeTestCase(unittest.TestCase):
    def test_user_ids(self) -> None:
        link = MemberLink(
            room_id=ROOM_A,
            member_key="@k",
            domain="other.example",
            localpart="bob",
            verified=False,
            event_id=None,
            key_id=None,
        )
        self.assertEqual(link.claimed_user_id, "@bob:other.example")
        self.assertIsNone(link.verified_user_id)

        verified = MemberLink(
            room_id=ROOM_A,
            member_key="@k",
            domain="other.example",
            localpart="bob",
            verified=True,
            event_id="$join",
            key_id="ed25519:abc",
        )
        self.assertEqual(verified.verified_user_id, "@bob:other.example")

        # Once the localpart is gone there is no user ID, verified or not.
        redacted = MemberLink(
            room_id=ROOM_A,
            member_key="@k",
            domain="other.example",
            localpart=None,
            verified=True,
            event_id="$join",
            key_id="ed25519:abc",
        )
        self.assertIsNone(redacted.claimed_user_id)
        self.assertIsNone(redacted.verified_user_id)

    def test_verified_requires_proof(self) -> None:
        """A verified link always names the event and server signing key that
        verified it."""
        for event_id, key_id in ((None, "ed25519:abc"), ("$join", None), (None, None)):
            with self.assertRaises(ValueError):
                MemberLink(
                    room_id=ROOM_A,
                    member_key="@k",
                    domain="other.example",
                    localpart="bob",
                    verified=True,
                    event_id=event_id,
                    key_id=key_id,
                )


class LocalMemberKeysStoreTestCase(unittest.HomeserverTestCase):
    """`msc4430_member_keys`: the keys we hold for our own users."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def _create(self, room_id: str, user_id: str) -> LocalMemberKey:
        signing_key = _new_signing_key()
        local_key = self.get_success(
            self.store.create_local_member_key(room_id, user_id, signing_key)
        )
        self.assertEqual(local_key.member_key, _member_key(signing_key))
        return local_key

    def _assert_same_key(self, a: LocalMemberKey, b: LocalMemberKey) -> None:
        self.assertEqual(a.room_id, b.room_id)
        self.assertEqual(a.user_id, b.user_id)
        self.assertEqual(a.member_key, b.member_key)
        self.assertEqual(bytes(a.signing_key), bytes(b.signing_key))

    def test_create_and_lookup(self) -> None:
        # Nothing is stored yet; these negative answers get cached, so the
        # creation below must invalidate them.
        self.assertIsNone(
            self.get_success(self.store.get_local_member_key(ROOM_A, ALICE))
        )
        self.assertEqual(
            self.get_success(self.store.get_local_member_keys_for_user(ALICE)), {}
        )
        self.assertEqual(
            self.get_success(self.store.get_local_member_keys_in_room(ROOM_A)), {}
        )

        signing_key = _new_signing_key()
        self.assertIsNone(
            self.get_success(
                self.store.get_local_member_key_by_key(ROOM_A, _member_key(signing_key))
            )
        )

        created = self.get_success(
            self.store.create_local_member_key(ROOM_A, ALICE, signing_key)
        )
        self.assertEqual(created.member_key, _member_key(signing_key))
        self.assertIsNone(
            self.get_success(self.store.get_local_member_key_by_key(ROOM_A, "@nope"))
        )

        by_user = self.get_success(self.store.get_local_member_key(ROOM_A, ALICE))
        assert by_user is not None
        self._assert_same_key(by_user, created)
        # The stored private half signs as the stored public half.
        self.assertEqual(_member_key(by_user.signing_key), by_user.member_key)
        self.assertEqual(by_user.signing_key.version, MemberKeys.KEY_VERSION)

        by_key = self.get_success(
            self.store.get_local_member_key_by_key(ROOM_A, created.member_key)
        )
        assert by_key is not None
        self._assert_same_key(by_key, created)

        batched = self.get_success(
            self.store.get_local_member_keys_by_key(
                ROOM_A, [created.member_key, "@nope"]
            )
        )
        self.assertEqual(set(batched), {created.member_key, "@nope"})
        batched_key = batched[created.member_key]
        assert batched_key is not None
        self._assert_same_key(batched_key, created)
        self.assertIsNone(batched["@nope"])

        self.assertEqual(
            self.get_success(
                self.store.db_pool.runInteraction(
                    "test",
                    self.store.get_local_user_ids_for_member_keys_txn,
                    ROOM_A,
                    [created.member_key, "@nope"],
                )
            ),
            {created.member_key: ALICE},
        )
        self.assertEqual(
            self.get_success(
                self.store.db_pool.runInteraction(
                    "test",
                    self.store.get_local_user_ids_for_member_keys_txn,
                    ROOM_A,
                    [],
                )
            ),
            {},
        )
        self.assertEqual(
            self.get_success(self.store.get_local_member_keys_for_user(ALICE)),
            {ROOM_A: created.member_key},
        )
        self.assertEqual(
            self.get_success(self.store.get_local_member_keys_in_room(ROOM_A)),
            {created.member_key: ALICE},
        )

    def test_create_is_insert_if_absent(self) -> None:
        """A user's key in a room never changes: a second creation, however it
        arises, yields the first key and the second is discarded."""
        first = self._create(ROOM_A, ALICE)

        second_signing_key = _new_signing_key()
        stored = self.get_success(
            self.store.create_local_member_key(ROOM_A, ALICE, second_signing_key)
        )
        self._assert_same_key(stored, first)

        self.assertIsNone(
            self.get_success(
                self.store.get_local_member_key_by_key(
                    ROOM_A, _member_key(second_signing_key)
                )
            )
        )
        self.assertEqual(
            self.get_success(self.store.get_local_member_keys_in_room(ROOM_A)),
            {first.member_key: ALICE},
        )

    def test_keys_are_per_room(self) -> None:
        """The same user has a different key in every room, and a key is only
        ours in the room it was created for."""
        in_a = self._create(ROOM_A, ALICE)
        in_b = self._create(ROOM_B, ALICE)
        bob_in_a = self._create(ROOM_A, BOB)
        self.assertNotEqual(in_a.member_key, in_b.member_key)

        # Alice's key from room A means nothing in room B, by any lookup.
        self.assertIsNone(
            self.get_success(
                self.store.get_local_member_key_by_key(ROOM_B, in_a.member_key)
            )
        )
        self.assertEqual(
            self.get_success(
                self.store.get_local_member_keys_by_key(ROOM_B, [in_a.member_key])
            ),
            {in_a.member_key: None},
        )
        self.assertEqual(
            self.get_success(
                self.store.db_pool.runInteraction(
                    "test",
                    self.store.get_local_user_ids_for_member_keys_txn,
                    ROOM_B,
                    [in_a.member_key, in_b.member_key, bob_in_a.member_key],
                )
            ),
            {in_b.member_key: ALICE},
        )
        self.assertEqual(
            self.get_success(self.store.get_local_member_keys_in_room(ROOM_A)),
            {in_a.member_key: ALICE, bob_in_a.member_key: BOB},
        )
        self.assertEqual(
            self.get_success(self.store.get_local_member_keys_for_user(ALICE)),
            {ROOM_A: in_a.member_key, ROOM_B: in_b.member_key},
        )

    def test_rejects_wrong_key_id(self) -> None:
        self.get_failure(
            self.store.create_local_member_key(
                ROOM_A, ALICE, generate_signing_key("2")
            ),
            ValueError,
        )
        self.assertIsNone(
            self.get_success(self.store.get_local_member_key(ROOM_A, ALICE))
        )


class MemberLinksStoreTestCase(unittest.HomeserverTestCase):
    """`msc4430_member_links`: what we know about keys we do not hold."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.key = _member_key(_new_signing_key())

    def _link(self, **kwargs: object) -> MemberLink:
        fields: dict = {
            "room_id": ROOM_A,
            "member_key": self.key,
            "domain": "other.example",
            "localpart": "bob",
            "verified": False,
            "event_id": None,
            "key_id": None,
        }
        fields.update(kwargs)
        return MemberLink(**fields)

    def _get(self, room_id: str = ROOM_A, member_key: str | None = None) -> MemberLink:
        link = self.get_success(
            self.store.get_member_link(room_id, member_key or self.key)
        )
        assert link is not None
        return link

    def test_upsert_and_lookup(self) -> None:
        # Unknown key: no link (cached, so the upsert must invalidate).
        self.assertIsNone(
            self.get_success(self.store.get_member_link(ROOM_A, self.key))
        )

        # An unverified claim.
        claim = self._link(event_id="$join")
        self.assertIsNone(self.get_success(self.store.upsert_member_link(claim)))
        self.assertEqual(self._get(), claim)

        # Verifying it: the previous link comes back so the caller can see what
        # changed, and the stored link is the new one.
        verified = self._link(verified=True, event_id="$join", key_id="ed25519:abc")
        self.assertEqual(
            self.get_success(self.store.upsert_member_link(verified)), claim
        )
        self.assertEqual(self._get(), verified)
        self.assertEqual(self._get().verified_user_id, "@bob:other.example")

        # Re-pointing it to another server replaces everything.
        moved = self._link(
            domain="third.example",
            localpart="robert",
            verified=True,
            event_id="$join2",
            key_id="ed25519:def",
        )
        self.assertEqual(
            self.get_success(self.store.upsert_member_link(moved)), verified
        )
        self.assertEqual(self._get(), moved)

        # The transaction variants, for read-decide-write in one transaction.
        def decide_and_write(txn: LoggingTransaction) -> MemberLink | None:
            current = self.store.get_member_link_txn(txn, ROOM_A, self.key)
            self.assertEqual(current, moved)
            self.assertIsNone(self.store.get_member_link_txn(txn, ROOM_B, self.key))
            return self.store.upsert_member_link_txn(txn, claim)

        self.assertEqual(
            self.get_success(
                self.store.db_pool.runInteraction("test", decide_and_write)
            ),
            moved,
        )
        self.assertEqual(self._get(), claim)

    def test_batched_and_per_room_lookups(self) -> None:
        other_key = _member_key(_new_signing_key())
        in_a = self._link(verified=True, event_id="$a", key_id="ed25519:abc")
        other_in_a = self._link(
            member_key=other_key, domain="third.example", localpart="carol"
        )
        # The same key in another room is a separate link.
        in_b = self._link(room_id=ROOM_B, domain="elsewhere.example")
        for link in (in_a, other_in_a, in_b):
            self.get_success(self.store.upsert_member_link(link))

        links = self.get_success(
            self.store.get_member_links(ROOM_A, [self.key, other_key, "@nope"])
        )
        self.assertEqual(links, {self.key: in_a, other_key: other_in_a, "@nope": None})
        self.assertEqual(
            self.get_success(self.store.get_member_links_in_room(ROOM_A)),
            {self.key: in_a, other_key: other_in_a},
        )
        self.assertEqual(
            self.get_success(self.store.get_member_links_in_room(ROOM_B)),
            {self.key: in_b},
        )
        self.assertEqual(self._get(ROOM_B), in_b)

    def test_lookups_by_domain(self) -> None:
        verified_key = _member_key(_new_signing_key())
        unverified_key = _member_key(_new_signing_key())
        elsewhere_key = _member_key(_new_signing_key())
        for link in (
            self._link(
                member_key=verified_key,
                verified=True,
                event_id="$v",
                key_id="ed25519:abc",
            ),
            self._link(member_key=unverified_key, localpart="bobby"),
            self._link(member_key=elsewhere_key, domain="third.example"),
            # Same domain, other room.
            self._link(room_id=ROOM_B),
        ):
            self.get_success(self.store.upsert_member_link(link))

        self.assertEqual(
            self.get_success(
                self.store.get_member_keys_for_domain_in_room(ROOM_A, "other.example")
            ),
            frozenset({verified_key, unverified_key}),
        )
        self.assertEqual(
            self.get_success(
                self.store.get_member_keys_for_domain_in_room(
                    ROOM_A, "other.example", verified_only=True
                )
            ),
            frozenset({verified_key}),
        )
        self.assertEqual(
            self.get_success(
                self.store.get_member_keys_for_domain_in_room(ROOM_A, "nobody.example")
            ),
            frozenset(),
        )

        # Our own domain is answered from the keys we hold, never from links: a
        # link claiming our domain for a key we do not hold is somebody else's.
        self.get_success(
            self.store.upsert_member_link(
                self._link(member_key=_member_key(_new_signing_key()), domain="test")
            )
        )
        self.assertEqual(
            self.get_success(
                self.store.get_member_keys_for_domain_in_room(ROOM_A, "test")
            ),
            frozenset(),
        )
        ours = self.get_success(
            self.store.create_local_member_key(ROOM_A, ALICE, _new_signing_key())
        )
        for verified_only in (False, True):
            self.assertEqual(
                self.get_success(
                    self.store.get_member_keys_for_domain_in_room(
                        ROOM_A, "test", verified_only=verified_only
                    )
                ),
                frozenset({ours.member_key}),
            )

        # The reverse mapping, from a claimed user ID to the keys claiming it.
        self.assertEqual(
            [
                link.member_key
                for link in self.get_success(
                    self.store.get_member_links_for_claimed_user(
                        ROOM_A, "other.example", "bobby"
                    )
                )
            ],
            [unverified_key],
        )
        self.assertEqual(
            self.get_success(
                self.store.get_member_links_for_claimed_user(
                    ROOM_A, "other.example", "nobody"
                )
            ),
            [],
        )
        # Several keys claiming the same user are all returned, verified first
        # and then in key order, whatever order they were learned in.
        claimants = sorted(
            (verified_key, unverified_key, _member_key(_new_signing_key())),
            reverse=True,
        )
        for claimant in claimants:
            if claimant != verified_key:
                self.get_success(
                    self.store.upsert_member_link(self._link(member_key=claimant))
                )
        self.assertEqual(
            [
                (link.member_key, link.verified)
                for link in self.get_success(
                    self.store.get_member_links_for_claimed_user(
                        ROOM_A, "other.example", "bob"
                    )
                )
            ],
            [(verified_key, True)]
            + [(key, False) for key in sorted(claimants) if key != verified_key],
        )

    def test_reverification(self) -> None:
        """Links are tracked by the server signing key that vouched for them, so
        that they can all be unverified when that key is no longer current."""
        other_key = _member_key(_new_signing_key())
        by_abc = self._link(verified=True, event_id="$a", key_id="ed25519:abc")
        by_abc_in_b = self._link(
            room_id=ROOM_B, verified=True, event_id="$b", key_id="ed25519:abc"
        )
        by_def = self._link(
            member_key=other_key, verified=True, event_id="$c", key_id="ed25519:def"
        )
        unverified = self._link(
            room_id=ROOM_B, member_key=other_key, event_id="$d", key_id="ed25519:abc"
        )
        for link in (by_abc, by_abc_in_b, by_def, unverified):
            self.get_success(self.store.upsert_member_link(link))

        self.assertEqual(
            self.get_success(self.store.get_verified_link_signing_keys()),
            [("other.example", "ed25519:abc"), ("other.example", "ed25519:def")],
        )

        def by_room(link: MemberLink) -> tuple[str, str]:
            return (link.room_id, link.member_key)

        self.assertEqual(
            sorted(
                self.get_success(
                    self.store.get_links_for_signing_key(
                        "other.example", "ed25519:abc", verified=True
                    )
                ),
                key=by_room,
            ),
            sorted([by_abc, by_abc_in_b], key=by_room),
        )
        self.assertEqual(
            self.get_success(
                self.store.get_links_for_signing_key(
                    "other.example", "ed25519:abc", verified=False
                )
            ),
            [unverified],
        )

        changed = self.get_success(
            self.store.mark_links_unverified("other.example", "ed25519:abc")
        )
        self.assertEqual(
            sorted(changed), sorted([(ROOM_A, self.key), (ROOM_B, self.key)])
        )
        for room_id, event_id in ((ROOM_A, "$a"), (ROOM_B, "$b")):
            link = self._get(room_id)
            self.assertFalse(link.verified)
            self.assertIsNone(link.verified_user_id)
            # Routing information, the claim, and which event and key the
            # co-signature named all survive the downgrade.
            self.assertEqual(link.domain, "other.example")
            self.assertEqual(link.claimed_user_id, "@bob:other.example")
            self.assertEqual(link.event_id, event_id)
            self.assertEqual(link.key_id, "ed25519:abc")
        # ... so the downgraded links can be found by the key that used to vouch.
        self.assertEqual(
            sorted(
                self.get_success(
                    self.store.get_links_for_signing_key("other.example", "ed25519:abc")
                ),
                key=by_room,
            ),
            sorted(
                [
                    attr.evolve(by_abc, verified=False),
                    attr.evolve(by_abc_in_b, verified=False),
                    unverified,
                ],
                key=by_room,
            ),
        )
        # The other key's link is untouched, and there is nothing left to do for
        # the rotated key.
        self.assertEqual(self._get(ROOM_A, other_key), by_def)
        self.assertEqual(
            self.get_success(self.store.get_verified_link_signing_keys()),
            [("other.example", "ed25519:def")],
        )
        self.assertEqual(
            self.get_success(
                self.store.mark_links_unverified("other.example", "ed25519:abc")
            ),
            [],
        )

    def test_redaction_clears_localpart(self) -> None:
        self.get_success(
            self.store.upsert_member_link(
                self._link(verified=True, event_id="$join", key_id="ed25519:abc")
            )
        )

        def clear(event_id: str, room_id: str = ROOM_A) -> list[str]:
            return self.get_success(
                self.store.db_pool.runInteraction(
                    "test",
                    self.store.clear_link_localpart_for_redacted_event_txn,
                    room_id,
                    event_id,
                )
            )

        # Redacting an unrelated event, or the event in another room, does nothing.
        self.assertEqual(clear("$other"), [])
        self.assertEqual(clear("$join", ROOM_B), [])
        self.assertEqual(self._get().localpart, "bob")

        # Redacting the event the link was learned from forgets the localpart;
        # the domain and the verification are kept.
        self.assertEqual(clear("$join"), [self.key])
        link = self._get()
        self.assertIsNone(link.localpart)
        self.assertIsNone(link.claimed_user_id)
        self.assertIsNone(link.verified_user_id)
        self.assertEqual(link.domain, "other.example")
        self.assertTrue(link.verified)
        self.assertEqual(link.event_id, "$join")

    def test_verified_requires_proof_in_the_database(self) -> None:
        """The table enforces what `MemberLink` enforces, for rows written by
        any other means."""
        self.get_failure(
            self.store.db_pool.simple_insert(
                table="msc4430_member_links",
                values={
                    "room_id": ROOM_A,
                    "member_key": self.key,
                    "domain": "other.example",
                    "localpart": "bob",
                    "verified": True,
                    "event_id": None,
                    "key_id": None,
                },
                desc="test",
            ),
            self.store.database_engine.module.IntegrityError,
        )
