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
"""MSC4430 member keys over federation, end to end: another server's member
joins a room of ours through `/make_join` and `/send_join`, and we learn from
their membership event who their key belongs to.

The remote half is built by hand — a member key and a server signing key — so
that each of the things a real server could do can be done here: vouch for its
member, decline to, vouch for a different member, or lie about one.

These stand in for the Complement tests of the same names (COMPLEMENT.md §3.19,
§3.20, §3.23, §3.30, §3.31) until the fake-server helpers they need exist.
"""

from http import HTTPStatus
from typing import Any
from unittest.mock import AsyncMock, patch

import signedjson.key
import signedjson.types
from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    MemberInfo,
    MemberKeys,
    Membership,
)
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.crypto.event_signing import (
    add_hashes_and_signatures_from_signers,
    compute_event_signature,
    compute_member_key_room_signature,
)
from synapse.events import EventBase, make_event_from_dict
from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.storage.keys import FetchKeyResult
from synapse.types import JsonDict, MemberKey
from synapse.util.clock import Clock

from tests import unittest

ROOM_VERSION = RoomVersions.MSC4430v12

OTHER = "other.example.com"
THIRD = "third.example.com"


class MemberKeyFederationTestCase(unittest.FederatingHomeserverTestCase):
    """A member of another server in one of our member key rooms."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {})["msc4430_enabled"] = True
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.store = hs.get_datastores().main
        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.room_id = self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            room_version=ROOM_VERSION.identifier,
            extra_content={
                "preset": "public_chat",
                # So that a member of another server can invite another of
                # theirs, as `test_a_third_party_who_holds_the_key...` does.
                "power_level_content_override": {"invite": 0},
            },
        )
        # Carol, of `other.example.com`: a member key, and her server's key,
        # which this homeserver already has (the base class stored it).
        self.carol_signing_key = signedjson.key.generate_signing_key(
            MemberKeys.KEY_VERSION
        )
        self.carol_key = MemberKey.from_signing_key(self.carol_signing_key).to_string()
        self.other_server_key = self.OTHER_SERVER_SIGNATURE_KEY
        # The base class publishes it as valid for ten seconds, which is too
        # short for a test that waits for the re-check of an unverified link.
        self._store_server_key(OTHER, self.other_server_key)

    # ------------------------------------------------------------------
    # The remote half
    # ------------------------------------------------------------------

    def _store_server_key(
        self, server_name: str, signing_key: signedjson.types.SigningKey
    ) -> None:
        """Publish a server's signing key, as fetching it over federation would."""
        verify_key = signedjson.key.get_verify_key(signing_key)
        verify_key_id = f"{verify_key.alg}:{verify_key.version}"
        self.get_success(
            self.store.store_server_keys_response(
                server_name,
                from_server=server_name,
                ts_added_ms=self.clock.time_msec(),
                verify_keys={
                    verify_key_id: FetchKeyResult(
                        verify_key=verify_key,
                        valid_until_ts=self.clock.time_msec() + 24 * 60 * 60 * 1000,
                    )
                },
                response_json={
                    "verify_keys": {
                        verify_key_id: {
                            "key": signedjson.key.encode_verify_key_base64(verify_key)
                        }
                    }
                },
            )
        )

    def _make_join(self, member_key: str, origin: str = OTHER) -> JsonDict:
        """Ask for a join template for a member key, as a joining server does
        when it does not yet know the room's version."""
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/make_join/{self.room_id}/@carol:{origin}"
            f"?ver={ROOM_VERSION.identifier}"
            f"&{MemberKeys.MAKE_MEMBERSHIP_PARAM}={member_key}",
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        return channel.json_body

    def _sign_as_member(
        self,
        event_dict: JsonDict,
        signing_key: signedjson.types.SigningKey,
        *,
        room_version: RoomVersion = ROOM_VERSION,
    ) -> JsonDict:
        add_hashes_and_signatures_from_signers(
            room_version,
            event_dict,
            [(MemberKeys.SIGNATURE_ENTITY, signing_key)],
            server_name=OTHER,
        )
        return event_dict

    def _cosign_as_server(
        self,
        event_dict: JsonDict,
        domain: str,
        signing_key: signedjson.types.SigningKey,
    ) -> JsonDict:
        """A server vouching for the linkage information of one of its members."""
        event_dict["signatures"] = compute_event_signature(
            ROOM_VERSION, event_dict, domain, signing_key
        )
        return event_dict

    def _join(
        self,
        *,
        member_key: str | None = None,
        signing_key: signedjson.types.SigningKey | None = None,
        localpart: str | None = "carol",
        domain: str = OTHER,
        cosign_with: signedjson.types.SigningKey | None = None,
        origin: str = OTHER,
        expect_code: int = HTTPStatus.OK,
    ) -> JsonDict:
        """Complete a `/make_join` template and send it, as the member's server.

        `cosign_with` is the server signing key that vouches for the linkage
        information; `None` leaves the claim unvouched for.
        """
        member_key = member_key or self.carol_key
        signing_key = signing_key or self.carol_signing_key
        template = self._make_join(member_key, origin=origin)["event"]
        self.assertEqual(template["sender"], member_key)
        self.assertEqual(template["state_key"], member_key)

        template["content"][EventContentFields.MSC4430_DOMAIN] = domain
        if localpart is not None:
            template["content"][EventContentFields.MSC4430_LOCALPART] = localpart
        self._sign_as_member(template, signing_key)
        if cosign_with is not None:
            self._cosign_as_server(template, domain, cosign_with)

        event = make_event_from_dict(template, ROOM_VERSION)
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v2/send_join/{self.room_id}/{event.event_id}",
            content=template,
        )
        self.assertEqual(expect_code, channel.code, channel.result)
        return template

    def _send_message(
        self,
        member_key: str,
        signing_key: signedjson.types.SigningKey,
        body: str,
        origin: str = OTHER,
    ) -> str:
        """Send a message into the room as a remote member key."""
        prev_event_ids = self.get_success(
            self.store.get_prev_events_for_room(self.room_id)
        )
        prev_state_events = list(
            self.get_success(self.store.get_state_dag_extremities(self.room_id))
        )
        depth = self.get_success(self.store.get_max_depth_of(prev_event_ids))[1]
        event_dict: JsonDict = {
            "type": EventTypes.Message,
            "room_id": self.room_id,
            "sender": member_key,
            "content": {"msgtype": "m.text", "body": body},
            "prev_events": prev_event_ids,
            "prev_state_events": prev_state_events,
            "depth": depth + 1,
            "origin_server_ts": self.clock.time_msec(),
        }
        self._sign_as_member(event_dict, signing_key)
        event = make_event_from_dict(event_dict, ROOM_VERSION)
        channel = self.make_signed_federation_request(
            "PUT",
            f"/_matrix/federation/v1/send/txn_{body}",
            content={
                "origin": origin,
                "origin_server_ts": self.clock.time_msec(),
                "pdus": [event_dict],
            },
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self._assert_pdus_accepted(channel.json_body)
        return event.event_id

    # ------------------------------------------------------------------
    # Inspecting the result
    # ------------------------------------------------------------------

    def _link(self, member_key: str | None = None) -> Any:
        return self.get_success(
            self.store.get_member_link(self.room_id, member_key or self.carol_key)
        )

    def _member_info(self) -> JsonDict:
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{self.room_id}/members",
            access_token=self.alice_tok,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        return channel.json_body

    def _alice_sees(self, event_id: str) -> bool:
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{self.room_id}/event/{event_id}",
            access_token=self.alice_tok,
        )
        return channel.code == HTTPStatus.OK

    def _hosts(self) -> set[str]:
        return set(self.get_success(self.store.get_current_hosts_in_room(self.room_id)))

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_join_with_verified_link(self) -> None:
        """A member whose server vouches for them is attributed to that user,
        their server is in the room, and their messages reach clients."""
        self._join(cosign_with=self.other_server_key)

        link = self._link()
        self.assertIsNotNone(link)
        self.assertEqual(link.domain, OTHER)
        self.assertEqual(link.localpart, "carol")
        self.assertTrue(link.verified)

        self.assertEqual(
            self._member_info()[MemberInfo.FIELD][self.carol_key],
            {MemberInfo.USER_ID: f"@carol:{OTHER}"},
        )
        self.assertIn(OTHER, self._hosts())
        self.assertTrue(
            self.get_success(self.store.is_host_joined(self.room_id, OTHER))
        )

        event_id = self._send_message(self.carol_key, self.carol_signing_key, "hello")
        self.assertTrue(self._alice_sees(event_id))

    def test_join_with_unverified_link(self) -> None:
        """A member nobody vouches for is in the room and routable, but is
        attributed to nobody, their server is not treated as a member server,
        and their messages are soft failed."""
        self._join(cosign_with=None)

        link = self._link()
        self.assertIsNotNone(link)
        self.assertEqual(link.domain, OTHER)
        self.assertFalse(link.verified)

        self.assertEqual(
            self._member_info()[MemberInfo.FIELD][self.carol_key],
            {MemberInfo.USER_ID: None},
        )
        # Routing follows the claim even though nothing vouches for it...
        self.assertIn(OTHER, self._hosts())
        # ... but being told about the room does not.
        self.assertFalse(
            self.get_success(self.store.is_host_joined(self.room_id, OTHER))
        )

        event_id = self._send_message(self.carol_key, self.carol_signing_key, "hello")
        self.assertFalse(self._alice_sees(event_id))

    def test_unverified_link_is_retried(self) -> None:
        """A claim we could not check when it arrived — the domain was
        unreachable, or had not published its key — is checked again, rather
        than leaving the member soft failed until they next send a membership
        event."""
        with patch.object(
            self.hs.get_keyring(),
            "verify_event_for_server",
            AsyncMock(side_effect=Exception("unreachable")),
        ):
            self._join(cosign_with=self.other_server_key)
        self.assertFalse(self._link().verified)

        self.reactor.advance(70)

        self.assertTrue(self._link().verified)
        self.assertEqual(
            self._member_info()[MemberInfo.FIELD][self.carol_key],
            {MemberInfo.USER_ID: f"@carol:{OTHER}"},
        )

    def test_member_moves_to_another_server(self) -> None:
        """The same member key, re-joining from a different server which vouches
        for it, is attributed to the new server: a member key belongs to whoever
        holds it, so moving it moves the member."""
        self._join(cosign_with=self.other_server_key)
        self.assertEqual(self._link().domain, OTHER)

        third_server_key = signedjson.key.generate_signing_key("third")
        self._store_server_key(THIRD, third_server_key)
        self._join(domain=THIRD, cosign_with=third_server_key, origin=THIRD)

        link = self._link()
        self.assertEqual(link.domain, THIRD)
        self.assertTrue(link.verified)
        self.assertEqual(
            self._member_info()[MemberInfo.FIELD][self.carol_key],
            {MemberInfo.USER_ID: f"@carol:{THIRD}"},
        )
        # The room now routes to the new server, and not to the old one.
        self.assertIn(THIRD, self._hosts())
        self.assertNotIn(OTHER, self._hosts())

    def test_a_third_party_cannot_deface_a_member(self) -> None:
        """A moderator kicking a member may not rewrite who that member is: the
        member's own key has not signed the claim, so it is ignored, and the
        departure forgets the link rather than re-pointing it."""
        self._join(cosign_with=self.other_server_key)
        self.assertEqual(self._link().localpart, "carol")

        self.helper.send_state(
            self.room_id,
            EventTypes.Member,
            {
                "membership": Membership.BAN,
                EventContentFields.MSC4430_LOCALPART: "abusive",
                EventContentFields.MSC4430_DOMAIN: "evil.example",
            },
            tok=self.alice_tok,
            state_key=self.carol_key,
        )

        self.assertIsNone(self._link())
        self.assertNotIn(THIRD, self._hosts())

    def test_a_third_party_who_holds_the_key_may_name_a_member(self) -> None:
        """Linkage information on somebody else's event counts when the member's
        own key has signed it too: that is how an invitee's server names them."""
        self._join(cosign_with=self.other_server_key)
        dave_signing_key = signedjson.key.generate_signing_key(MemberKeys.KEY_VERSION)
        dave_key = MemberKey.from_signing_key(dave_signing_key).to_string()

        # Carol invites Dave, and Dave's server (also `other.example.com`)
        # vouches for him with his key and its own.
        template = self._make_join(dave_key)["event"]
        template["sender"] = self.carol_key
        template["state_key"] = dave_key
        template["content"] = {
            "membership": Membership.INVITE,
            EventContentFields.MSC4430_LOCALPART: "dave",
            EventContentFields.MSC4430_DOMAIN: OTHER,
        }
        self._sign_as_member(template, self.carol_signing_key)
        template["signatures"] = compute_member_key_room_signature(
            ROOM_VERSION,
            template,
            (MemberKeys.STATE_KEY_SIGNATURE_ENTITY, dave_signing_key),
            server_name=OTHER,
        )
        self._cosign_as_server(template, OTHER, self.other_server_key)

        self._send_pdu(template)

        link = self._link(dave_key)
        self.assertIsNotNone(link)
        self.assertEqual(link.localpart, "dave")
        self.assertTrue(link.verified)

    def _send_pdu(self, event_dict: JsonDict) -> None:
        channel = self.make_signed_federation_request(
            "PUT",
            "/_matrix/federation/v1/send/txn_pdu",
            content={
                "origin": OTHER,
                "origin_server_ts": self.clock.time_msec(),
                "pdus": [event_dict],
            },
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self._assert_pdus_accepted(channel.json_body)

    def _assert_pdus_accepted(self, body: JsonDict) -> None:
        """A `/send` response reports one entry per PDU, empty when it was
        accepted and carrying `error` when it was not."""
        for event_id, result in body.get("pdus", {}).items():
            self.assertNotIn("error", result, f"{event_id}: {result}")

    @parameterized.expand(["make_join", "make_leave"])
    def test_make_membership_needs_a_member_key(self, endpoint: str) -> None:
        """A room that identifies its members by key cannot build a template for
        a user ID: the joining server must say which key it means."""
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/{endpoint}/{self.room_id}/@carol:{OTHER}"
            f"?ver={ROOM_VERSION.identifier}",
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, channel.code, channel.result)


class OutboundMemberKeyJoinTestCase(unittest.FederatingHomeserverTestCase):
    """The join *we* send when one of our users joins another server's member
    key room: it must name our user and be signed for, or the room we join will
    know nothing about who we are."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {})["msc4430_enabled"] = True
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.store = hs.get_datastores().main
        self.federation_client = hs.get_federation_client()
        self.member_key_handler = hs.get_member_key_handler()
        self.alice = self.register_user("alice", "pass")
        self.room_id = "!room:" + OTHER

    def _make_membership_event(self, content: JsonDict | None = None) -> EventBase:
        """Drive the outbound half of a join: the remote returns a template for
        our member key, and we complete and sign it."""
        member_key = self.get_success(
            self.member_key_handler.member_key_for_remote_join(self.room_id, self.alice)
        )

        async def template(
            destination: str,
            room_id: str,
            user_id: str,
            membership: str,
            params: object,
        ) -> JsonDict:
            # What a resident of a member key room answers `/make_join` with.
            self.assertEqual(
                params[MemberKeys.MAKE_MEMBERSHIP_PARAM],  # type: ignore[index]
                member_key,
            )
            return {
                "room_version": ROOM_VERSION.identifier,
                "event": {
                    "type": EventTypes.Member,
                    "room_id": room_id,
                    "sender": member_key,
                    "state_key": member_key,
                    "content": {"membership": Membership.JOIN},
                    "depth": 12,
                    "prev_events": [],
                    "prev_state_events": [],
                    "origin_server_ts": self.clock.time_msec(),
                },
            }

        self.federation_client.transport_layer.make_membership_event = template  # type: ignore[method-assign]
        _, event, _ = self.get_success(
            self.federation_client.make_membership_event(
                [OTHER],
                self.room_id,
                self.alice,
                Membership.JOIN,
                content if content is not None else {},
                params={MemberKeys.MAKE_MEMBERSHIP_PARAM: member_key},
                member_key=member_key,
            )
        )
        return event

    def test_join_names_our_user_and_is_signed_for(self) -> None:
        event = self._make_membership_event()

        self.assertEqual(event.content[EventContentFields.MSC4430_LOCALPART], "alice")
        self.assertEqual(
            event.content[EventContentFields.MSC4430_DOMAIN], self.hs.hostname
        )
        # Signed by the member key, and vouched for by us.
        self.assertEqual(
            set(event.signatures.as_dict()),
            {MemberKeys.SIGNATURE_ENTITY, self.hs.hostname},
        )
        # The key is kept, so a later event in that room is signed by the same one.
        local_key = self.get_success(
            self.store.get_local_member_key(self.room_id, self.alice)
        )
        assert local_key is not None
        self.assertEqual(local_key.member_key, event.sender)

    def test_clients_cannot_assert_linkage_on_a_remote_join(self) -> None:
        event = self._make_membership_event(
            {
                EventContentFields.MSC4430_LOCALPART: "mallory",
                EventContentFields.MSC4430_DOMAIN: "evil.example",
            }
        )
        self.assertEqual(event.content[EventContentFields.MSC4430_LOCALPART], "alice")
        self.assertEqual(
            event.content[EventContentFields.MSC4430_DOMAIN], self.hs.hostname
        )

    def test_erased_user_joins_without_a_localpart(self) -> None:
        self.get_success(self.store.mark_user_erased(self.alice))
        event = self._make_membership_event()
        self.assertNotIn(EventContentFields.MSC4430_LOCALPART, event.content)
        self.assertEqual(
            event.content[EventContentFields.MSC4430_DOMAIN], self.hs.hostname
        )
