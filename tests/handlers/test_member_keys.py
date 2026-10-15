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
"""Tests for the MSC4430 identity module (`synapse.handlers.member_keys`)."""

from prometheus_client import REGISTRY
from signedjson.key import generate_signing_key

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, MemberKeys
from synapse.api.errors import SynapseError
from synapse.api.room_versions import RoomVersions
from synapse.events import make_event_from_dict
from synapse.handlers import member_keys
from synapse.handlers.member_keys import record_unsupported_path
from synapse.metrics import SERVER_NAME_LABEL
from synapse.rest import admin
from synapse.server import HomeServer
from synapse.types import JsonDict, is_member_key
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.member_key_rooms import Member

# The example member key from MSC4430.
EXAMPLE_MEMBER_KEY = "@l8Hft5qXKn1vfHrg3p4-W8gELQVo8N13JkluMfmn2sQ"


class IsMineIdentityTestCase(unittest.HomeserverTestCase):
    def test_user_ids(self) -> None:
        self.assertTrue(
            self.get_success(self.hs.is_mine_identity("!room:test", "@alice:test"))
        )
        self.assertFalse(
            self.get_success(self.hs.is_mine_identity("!room:test", "@bob:other"))
        )

    def test_member_keys(self) -> None:
        """A member key is ours in the room we hold it for, and nowhere else."""
        store = self.hs.get_datastores().main
        self.assertFalse(
            self.get_success(self.hs.is_mine_identity("!room:test", EXAMPLE_MEMBER_KEY))
        )

        ours = self.get_success(
            store.create_local_member_key(
                "!room:test",
                "@alice:test",
                generate_signing_key(MemberKeys.KEY_VERSION),
            )
        )
        self.assertTrue(
            self.get_success(self.hs.is_mine_identity("!room:test", ours.member_key))
        )
        self.assertFalse(
            self.get_success(self.hs.is_mine_identity("!other:test", ours.member_key))
        )


class RecordUnsupportedPathTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # The warn-once set is process global.
        member_keys._warned_paths.discard("tests.example")

    def test_counter(self) -> None:
        labels = {SERVER_NAME_LABEL: "test", "path": "tests.example"}

        def value() -> float:
            return (
                REGISTRY.get_sample_value(
                    "synapse_msc4430_unsupported_path_total", labels
                )
                or 0.0
            )

        before = value()
        with self.assertLogs("synapse.handlers.member_keys", level="WARNING") as cm:
            record_unsupported_path("test", "tests.example")
            # Only the first occurrence of a path is logged.
            record_unsupported_path("test", "tests.example")
        self.assertEqual(len(cm.output), 1)
        self.assertIn("tests.example", cm.output[0])
        self.assertEqual(value(), before + 2)


ROOM_VERSION = RoomVersions.MSC4430v12
ROOM_ID = "!K3DOWWLmkHLl52yJ-vT8J5jX5wuYZati_KvC6PliIPE"


class EventTranslationTestCase(unittest.HomeserverTestCase):
    """`translate_event_dict` and `get_signers_for_event_dict`: how the user IDs a
    client speaks in become member keys, and which keys sign the result. The
    security property is that nothing a client sends can make the server sign
    as a key it does not hold or vouch for linkage information that is not ours."""

    servlets = [admin.register_servlets]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {}).setdefault(
            "msc4430_enabled", True
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.handler = hs.get_member_key_handler()
        self.alice = self.register_user("alice", "pass")
        self.bob = self.register_user("bob", "pass")

    def _translate(self, event_dict: JsonDict) -> JsonDict:
        self.get_success(self.handler.translate_event_dict(ROOM_VERSION, event_dict))
        return event_dict

    def _signers(self, event_dict: JsonDict) -> list[str]:
        """The entities that would sign the event."""
        signers = self.get_success(
            self.handler.get_signers_for_event_dict(ROOM_VERSION, event_dict)
        )
        return [entity for entity, _ in signers]

    def _key(self, user_id: str, room_id: str = ROOM_ID) -> str:
        local_key = self.get_success(self.store.get_local_member_key(room_id, user_id))
        assert local_key is not None
        return local_key.member_key

    def test_other_room_versions_are_untouched(self) -> None:
        event_dict: JsonDict = {
            "type": EventTypes.Member,
            "room_id": ROOM_ID,
            "sender": self.alice,
            "state_key": self.alice,
            "content": {"membership": "join", "localpart": "x"},
        }
        before = dict(event_dict)
        self.get_success(
            self.handler.translate_event_dict(RoomVersions.V12, event_dict)
        )
        self.assertEqual(event_dict, before)

    def test_sender_becomes_the_users_key(self) -> None:
        event_dict = self._translate(
            {
                "type": EventTypes.Message,
                "room_id": ROOM_ID,
                "sender": self.alice,
                "content": {"body": "hi"},
            }
        )
        self.assertEqual(event_dict["sender"], self._key(self.alice))
        # The same key again, and only the member key signs a message.
        self.assertEqual(
            self._translate({**event_dict, "sender": self.alice})["sender"],
            self._key(self.alice),
        )
        self.assertEqual(self._signers(event_dict), [MemberKeys.SIGNATURE_ENTITY])

    def test_remote_sender_is_refused(self) -> None:
        err = self.get_failure(
            self.handler.translate_event_dict(
                ROOM_VERSION,
                {
                    "type": EventTypes.Message,
                    "room_id": ROOM_ID,
                    "sender": "@carol:other.example",
                    "content": {},
                },
            ),
            SynapseError,
        )
        self.assertEqual(err.value.code, 403)

    def test_membership_of_our_user_is_linked_and_cosigned(self) -> None:
        """A client's linkage information is dropped and ours put in; the target
        being ours, we co-sign."""
        event_dict = self._translate(
            {
                "type": EventTypes.Member,
                "room_id": ROOM_ID,
                "sender": self.alice,
                "state_key": self.bob,
                "content": {
                    "membership": "invite",
                    EventContentFields.MSC4430_LOCALPART: "mallory",
                    EventContentFields.MSC4430_DOMAIN: "evil.example",
                },
            }
        )
        self.assertEqual(event_dict["sender"], self._key(self.alice))
        self.assertEqual(event_dict["state_key"], self._key(self.bob))
        self.assertEqual(
            event_dict["content"],
            {
                "membership": "invite",
                EventContentFields.MSC4430_LOCALPART: "bob",
                EventContentFields.MSC4430_DOMAIN: self.hs.hostname,
            },
        )
        self.assertEqual(
            self._signers(event_dict),
            [MemberKeys.SIGNATURE_ENTITY, MemberKeys.LINKAGE_SIGNATURE_ENTITY],
        )

    def test_erased_user_gets_no_localpart(self) -> None:
        self.get_success(self.store.mark_user_erased(self.bob))
        event_dict = self._translate(
            {
                "type": EventTypes.Member,
                "room_id": ROOM_ID,
                "sender": self.bob,
                "state_key": self.bob,
                "content": {"membership": "leave"},
            }
        )
        self.assertEqual(
            event_dict["content"],
            {
                "membership": "leave",
                EventContentFields.MSC4430_DOMAIN: self.hs.hostname,
            },
        )
        self.assertEqual(
            self._signers(event_dict),
            [MemberKeys.SIGNATURE_ENTITY, MemberKeys.LINKAGE_SIGNATURE_ENTITY],
        )

    def test_remote_target_is_refused(self) -> None:
        err = self.get_failure(
            self.handler.translate_event_dict(
                ROOM_VERSION,
                {
                    "type": EventTypes.Member,
                    "room_id": ROOM_ID,
                    "sender": self.alice,
                    "state_key": "@carol:other.example",
                    "content": {"membership": "invite"},
                },
            ),
            SynapseError,
        )
        self.assertEqual(err.value.code, 400)

    def test_we_never_sign_as_a_key_we_do_not_hold(self) -> None:
        """An event sent as somebody else's key is not signed at all, and a
        membership event about such a key gets no co-signature even when it
        claims our domain."""
        stranger = Member()
        self.assertEqual(
            self._signers(
                {
                    "type": EventTypes.Member,
                    "room_id": ROOM_ID,
                    "sender": stranger.key,
                    "state_key": stranger.key,
                    "content": {
                        "membership": "join",
                        EventContentFields.MSC4430_DOMAIN: self.hs.hostname,
                    },
                }
            ),
            [],
        )
        alice_key = self._key_for(self.alice)
        self.assertEqual(
            self._signers(
                {
                    "type": EventTypes.Member,
                    "room_id": ROOM_ID,
                    "sender": alice_key,
                    "state_key": stranger.key,
                    "content": {
                        "membership": "invite",
                        EventContentFields.MSC4430_LOCALPART: "alice",
                        EventContentFields.MSC4430_DOMAIN: self.hs.hostname,
                    },
                }
            ),
            [MemberKeys.SIGNATURE_ENTITY],
        )

    def test_restricted_join_authorised_by_our_member(self) -> None:
        alice_key = self._key_for(self.alice)
        event_dict = self._translate(
            {
                "type": EventTypes.Member,
                "room_id": ROOM_ID,
                "sender": self.bob,
                "state_key": self.bob,
                "content": {
                    "membership": "join",
                    EventContentFields.AUTHORISING_USER: alice_key,
                },
            }
        )
        self.assertEqual(
            self._signers(event_dict),
            [
                MemberKeys.SIGNATURE_ENTITY,
                MemberKeys.LINKAGE_SIGNATURE_ENTITY,
                alice_key,
            ],
        )

    def _key_for(self, user_id: str) -> str:
        return self.get_success(
            self.handler.get_or_create_local_member_key(ROOM_ID, user_id)
        ).member_key

    def test_create_event_uses_pending_keys(self) -> None:
        """The create event's sender and additional creators get keys before the
        room ID exists; building the event stores them under the room. One user
        named twice gets one key."""
        event_dict = self._translate(
            {
                "type": EventTypes.Create,
                "state_key": "",
                "sender": self.alice,
                "content": {
                    "room_version": ROOM_VERSION.identifier,
                    EventContentFields.ADDITIONAL_CREATORS: [self.bob, self.alice],
                },
            }
        )
        sender = event_dict["sender"]
        bob_key, alice_again = event_dict["content"][
            EventContentFields.ADDITIONAL_CREATORS
        ]
        self.assertTrue(is_member_key(sender))
        self.assertTrue(is_member_key(bob_key))
        self.assertEqual(alice_again, sender)
        self.assertNotEqual(bob_key, sender)
        # Nothing is stored yet, but the pending keys sign.
        self.assertIsNone(
            self.get_success(self.store.get_local_member_keys_for_user(self.alice)).get(
                "anything"
            )
        )
        self.assertEqual(self._signers(event_dict), [MemberKeys.SIGNATURE_ENTITY])

        signers = self.get_success(
            self.handler.get_signers_for_event_dict(ROOM_VERSION, event_dict)
        )
        from synapse.crypto.event_signing import add_hashes_and_signatures_from_signers

        event_dict.update({"depth": 1, "origin_server_ts": 1, "prev_events": []})
        event_dict["prev_state_events"] = []
        add_hashes_and_signatures_from_signers(
            ROOM_VERSION, event_dict, signers, server_name=self.hs.hostname
        )
        create = make_event_from_dict(event_dict, ROOM_VERSION)
        self.get_success(self.handler.persist_pending_member_keys(create))

        self.assertEqual(self._key(self.alice, create.room_id), sender)
        self.assertEqual(self._key(self.bob, create.room_id), bob_key)
        self.assertEqual(
            self.get_success(
                self.handler.room_identity_for_user(
                    create.room_id, self.alice, ROOM_VERSION
                )
            ),
            sender,
        )

    def test_a_create_event_with_a_room_is_refused(self) -> None:
        err = self.get_failure(
            self.handler.translate_event_dict(
                ROOM_VERSION,
                {
                    "type": EventTypes.Create,
                    "state_key": "",
                    "room_id": ROOM_ID,
                    "sender": self.alice,
                    "content": {EventContentFields.ADDITIONAL_CREATORS: [self.bob]},
                },
            ),
            SynapseError,
        )
        self.assertEqual(err.value.code, 400)
        # Nothing was minted for it.
        self.assertEqual(
            self.get_success(self.store.get_local_member_keys_for_user(self.bob)), {}
        )

    def test_member_keys_are_refused_as_additional_creators(self) -> None:
        err = self.get_failure(
            self.handler.translate_event_dict(
                ROOM_VERSION,
                {
                    "type": EventTypes.Create,
                    "state_key": "",
                    "sender": self.alice,
                    "content": {EventContentFields.ADDITIONAL_CREATORS: [Member().key]},
                },
            ),
            SynapseError,
        )
        self.assertEqual(err.value.code, 400)

    def test_a_key_we_do_not_hold_is_refused_as_target(self) -> None:
        err = self.get_failure(
            self.handler.translate_event_dict(
                ROOM_VERSION,
                {
                    "type": EventTypes.Member,
                    "room_id": ROOM_ID,
                    "sender": self.alice,
                    "state_key": Member().key,
                    "content": {"membership": "invite"},
                },
            ),
            SynapseError,
        )
        self.assertEqual(err.value.code, 400)

    def test_pending_keys_are_not_ours_in_any_room(self) -> None:
        pending = self.handler.generate_pending_member_key(self.alice)
        self.assertEqual(
            self.get_success(self.handler.local_user_id_for_identity(None, pending)),
            self.alice,
        )
        self.assertIsNone(
            self.get_success(self.handler.local_user_id_for_identity(ROOM_ID, pending))
        )
        self.assertEqual(
            self._signers(
                {
                    "type": EventTypes.Message,
                    "room_id": ROOM_ID,
                    "sender": pending,
                    "content": {},
                }
            ),
            [],
        )

    def test_only_our_own_linkage_information_is_cosigned(self) -> None:
        """Whatever a dict says, the server co-signs a membership event only
        when its linkage information is exactly what it vouches for."""
        alice_key = self._key_for(self.alice)

        def signers_for(content: JsonDict) -> list[str]:
            return self._signers(
                {
                    "type": EventTypes.Member,
                    "room_id": ROOM_ID,
                    "sender": alice_key,
                    "state_key": alice_key,
                    "content": {"membership": "join", **content},
                }
            )

        ours = {
            EventContentFields.MSC4430_LOCALPART: "alice",
            EventContentFields.MSC4430_DOMAIN: self.hs.hostname,
        }
        self.assertEqual(
            signers_for(ours),
            [MemberKeys.SIGNATURE_ENTITY, MemberKeys.LINKAGE_SIGNATURE_ENTITY],
        )
        for wrong in (
            {**ours, EventContentFields.MSC4430_LOCALPART: "bob"},
            {EventContentFields.MSC4430_DOMAIN: self.hs.hostname},
            {**ours, EventContentFields.MSC4430_DOMAIN: "other.example"},
            {},
        ):
            self.assertEqual(signers_for(wrong), [MemberKeys.SIGNATURE_ENTITY], wrong)

        # Once erased, only the form without a localpart is ours.
        self.get_success(self.store.mark_user_erased(self.alice))
        self.assertEqual(signers_for(ours), [MemberKeys.SIGNATURE_ENTITY])
        self.assertEqual(
            signers_for({EventContentFields.MSC4430_DOMAIN: self.hs.hostname}),
            [MemberKeys.SIGNATURE_ENTITY, MemberKeys.LINKAGE_SIGNATURE_ENTITY],
        )

    def test_remote_additional_creator_is_refused(self) -> None:
        err = self.get_failure(
            self.handler.translate_event_dict(
                ROOM_VERSION,
                {
                    "type": EventTypes.Create,
                    "state_key": "",
                    "sender": self.alice,
                    "content": {
                        EventContentFields.ADDITIONAL_CREATORS: ["@c:other.example"]
                    },
                },
            ),
            SynapseError,
        )
        self.assertEqual(err.value.code, 400)

    def test_room_identity_for_user(self) -> None:
        """A user is called by their key where they have one, else by user ID."""
        self.assertEqual(
            self.get_success(
                self.handler.room_identity_for_user(ROOM_ID, self.alice, ROOM_VERSION)
            ),
            self.alice,
        )
        alice_key = self._key_for(self.alice)
        self.assertEqual(
            self.get_success(
                self.handler.room_identity_for_user(ROOM_ID, self.alice, ROOM_VERSION)
            ),
            alice_key,
        )
        self.assertEqual(
            self.get_success(
                self.handler.room_identity_for_user(
                    ROOM_ID, self.alice, RoomVersions.V12
                )
            ),
            self.alice,
        )
        self.assertEqual(
            self.get_success(self.handler.user_id_for_identity(ROOM_ID, alice_key)),
            self.alice,
        )
        self.assertIsNone(
            self.get_success(self.handler.user_id_for_identity(ROOM_ID, Member().key))
        )
        self.assertEqual(
            self.get_success(self.handler.domain_for_identity(ROOM_ID, alice_key)),
            self.hs.hostname,
        )
        self.assertIsNone(
            self.get_success(self.handler.domain_for_identity(ROOM_ID, Member().key))
        )
