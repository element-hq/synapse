#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2018-2022 The Matrix.org Foundation C.I.C.
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

import string
import unittest
from collections import namedtuple
from typing import Any, Collection, Iterable

from parameterized import parameterized
from signedjson.key import generate_signing_key, get_verify_key
from signedjson.sign import sign_json
from unpaddedbase64 import encode_base64

from synapse import event_auth
from synapse.api.constants import EventContentFields, MemberKeys, RejectedReason
from synapse.api.errors import AuthError, SynapseError
from synapse.api.room_versions import EventFormatVersions, RoomVersion, RoomVersions
from synapse.crypto.event_signing import (
    EventSigner,
    add_hashes_and_signatures,
    add_hashes_and_signatures_from_signers,
)
from synapse.events import (
    EventBase,
    event_exists_in_state_dag,
    make_event_from_dict,
    redaction_sender_matches,
)
from synapse.storage.databases.main.events_worker import EventRedactBehaviour
from synapse.types import JsonDict, MemberKey, get_domain_from_id

from tests.test_utils import get_awaitable_result
from tests.test_utils.event_builders import make_test_event


class _StubEventSourceStore:
    """A stub implementation of the EventSourceStore"""

    def __init__(self) -> None:
        self._store: dict[str, EventBase] = {}

    def add_event(self, event: EventBase) -> None:
        self._store[event.event_id] = event

    def add_events(self, events: Iterable[EventBase]) -> None:
        for event in events:
            self._store[event.event_id] = event

    async def get_events(
        self,
        event_ids: Collection[str],
        redact_behaviour: EventRedactBehaviour,
        get_prev_content: bool = False,
        allow_rejected: bool = False,
    ) -> dict[str, EventBase]:
        assert allow_rejected
        assert not get_prev_content
        assert redact_behaviour == EventRedactBehaviour.as_is
        results = {}
        for e in event_ids:
            if e in self._store:
                results[e] = self._store[e]
        return results


class EventAuthTestCase(unittest.TestCase):
    def test_rejected_auth_events(self) -> None:
        """
        Events that refer to rejected events in their auth events are rejected
        """
        creator = "@creator:example.com"
        auth_events = [
            _create_event(RoomVersions.V9, creator),
            _join_event(RoomVersions.V9, creator),
        ]

        event_store = _StubEventSourceStore()
        event_store.add_events(auth_events)

        # creator should be able to send state
        event = _random_state_event(RoomVersions.V9, creator, auth_events)
        get_awaitable_result(
            event_auth.check_state_independent_auth_rules(event_store, event)
        )
        event_auth.check_state_dependent_auth_rules(event, auth_events)

        # ... but a rejected join_rules event should cause it to be rejected
        rejected_join_rules = _join_rules_event(
            RoomVersions.V9,
            creator,
            "public",
            rejected_reason="stinky",
        )
        auth_events.append(rejected_join_rules)
        event_store.add_event(rejected_join_rules)

        with self.assertRaises(AuthError):
            get_awaitable_result(
                event_auth.check_state_independent_auth_rules(
                    event_store,
                    _random_state_event(RoomVersions.V9, creator),
                )
            )

        # ... even if there is *also* a good join rules
        auth_events.append(_join_rules_event(RoomVersions.V9, creator, "public"))
        event_store.add_event(rejected_join_rules)

        with self.assertRaises(AuthError):
            get_awaitable_result(
                event_auth.check_state_independent_auth_rules(
                    event_store,
                    _random_state_event(RoomVersions.V9, creator),
                )
            )

    def test_create_event_with_prev_events(self) -> None:
        """A create event with prev_events should be rejected

        https://spec.matrix.org/v1.3/rooms/v9/#authorization-rules
        1: If type is m.room.create:
            1. If it has any previous events, reject.
        """
        creator = f"@creator:{TEST_DOMAIN}"

        # we make both a good event and a bad event, to check that we are rejecting
        # the bad event for the reason we think we are.
        good_event = make_test_event(
            {
                "room_id": TEST_ROOM_ID,
                "type": "m.room.create",
                "state_key": "",
                "sender": creator,
                "content": {
                    "creator": creator,
                    "room_version": RoomVersions.V9.identifier,
                },
                "auth_events": [],
                "prev_events": [],
            },
            room_version=RoomVersions.V9,
        )
        bad_event = make_test_event(
            {**good_event.get_dict(), "prev_events": ["$fakeevent"]},
            room_version=RoomVersions.V9,
        )

        event_store = _StubEventSourceStore()

        get_awaitable_result(
            event_auth.check_state_independent_auth_rules(event_store, good_event)
        )
        with self.assertRaises(AuthError):
            get_awaitable_result(
                event_auth.check_state_independent_auth_rules(event_store, bad_event)
            )

    def test_duplicate_auth_events(self) -> None:
        """Events with duplicate auth_events should be rejected

        https://spec.matrix.org/v1.3/rooms/v9/#authorization-rules
        2. Reject if event has auth_events that:
            1. have duplicate entries for a given type and state_key pair
        """
        creator = "@creator:example.com"

        create_event = _create_event(RoomVersions.V9, creator)
        join_event1 = _join_event(RoomVersions.V9, creator)
        pl_event = _power_levels_event(
            RoomVersions.V9,
            creator,
            {"state_default": 30, "users": {"creator": 100}},
        )

        # create a second join event, so that we can make a duplicate
        join_event2 = _join_event(RoomVersions.V9, creator)

        event_store = _StubEventSourceStore()
        event_store.add_events([create_event, join_event1, join_event2, pl_event])

        good_event = _random_state_event(
            RoomVersions.V9, creator, [create_event, join_event2, pl_event]
        )
        bad_event = _random_state_event(
            RoomVersions.V9, creator, [create_event, join_event1, join_event2, pl_event]
        )
        # a variation: two instances of the *same* event
        bad_event2 = _random_state_event(
            RoomVersions.V9, creator, [create_event, join_event2, join_event2, pl_event]
        )

        get_awaitable_result(
            event_auth.check_state_independent_auth_rules(event_store, good_event)
        )
        with self.assertRaises(AuthError):
            get_awaitable_result(
                event_auth.check_state_independent_auth_rules(event_store, bad_event)
            )
        with self.assertRaises(AuthError):
            get_awaitable_result(
                event_auth.check_state_independent_auth_rules(event_store, bad_event2)
            )

    def test_unexpected_auth_events(self) -> None:
        """Events with excess auth_events should be rejected

        https://spec.matrix.org/v1.3/rooms/v9/#authorization-rules
        2. Reject if event has auth_events that:
           2. have entries whose type and state_key don’t match those specified by the
              auth events selection algorithm described in the server specification.
        """
        creator = "@creator:example.com"

        create_event = _create_event(RoomVersions.V9, creator)
        join_event = _join_event(RoomVersions.V9, creator)
        pl_event = _power_levels_event(
            RoomVersions.V9,
            creator,
            {"state_default": 30, "users": {"creator": 100}},
        )
        join_rules_event = _join_rules_event(RoomVersions.V9, creator, "public")

        event_store = _StubEventSourceStore()
        event_store.add_events([create_event, join_event, pl_event, join_rules_event])

        good_event = _random_state_event(
            RoomVersions.V9, creator, [create_event, join_event, pl_event]
        )
        # join rules should *not* be included in the auth events.
        bad_event = _random_state_event(
            RoomVersions.V9,
            creator,
            [create_event, join_event, pl_event, join_rules_event],
        )

        get_awaitable_result(
            event_auth.check_state_independent_auth_rules(event_store, good_event)
        )
        with self.assertRaises(AuthError):
            get_awaitable_result(
                event_auth.check_state_independent_auth_rules(event_store, bad_event)
            )

    def test_random_users_cannot_send_state_before_first_pl(self) -> None:
        """
        Check that, before the first PL lands, the creator is the only user
        that can send a state event.
        """
        creator = "@creator:example.com"
        joiner = "@joiner:example.com"
        auth_events = [
            _create_event(RoomVersions.V1, creator),
            _join_event(RoomVersions.V1, creator),
            _join_event(RoomVersions.V1, joiner),
        ]

        # creator should be able to send state
        event_auth.check_state_dependent_auth_rules(
            _random_state_event(RoomVersions.V1, creator),
            auth_events,
        )

        # joiner should not be able to send state
        self.assertRaises(
            AuthError,
            event_auth.check_state_dependent_auth_rules,
            _random_state_event(RoomVersions.V1, joiner),
            auth_events,
        )

    def test_state_default_level(self) -> None:
        """
        Check that users above the state_default level can send state and
        those below cannot
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"
        king = "@joiner2:example.com"

        auth_events = [
            _create_event(RoomVersions.V1, creator),
            _join_event(RoomVersions.V1, creator),
            _power_levels_event(
                RoomVersions.V1,
                creator,
                {"state_default": "30", "users": {pleb: "29", king: "30"}},
            ),
            _join_event(RoomVersions.V1, pleb),
            _join_event(RoomVersions.V1, king),
        ]

        # pleb should not be able to send state
        (
            self.assertRaises(
                AuthError,
                event_auth.check_state_dependent_auth_rules,
                _random_state_event(RoomVersions.V1, pleb),
                auth_events,
            ),
        )

        # king should be able to send state
        event_auth.check_state_dependent_auth_rules(
            _random_state_event(RoomVersions.V1, king),
            auth_events,
        )

    def test_alias_event(self) -> None:
        """Alias events have special behavior up through room version 6."""
        creator = "@creator:example.com"
        other = "@other:example.com"
        auth_events = [
            _create_event(RoomVersions.V1, creator),
            _join_event(RoomVersions.V1, creator),
        ]

        # creator should be able to send aliases
        event_auth.check_state_dependent_auth_rules(
            _alias_event(RoomVersions.V1, creator),
            auth_events,
        )

        # Reject an event with no state key.
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _alias_event(RoomVersions.V1, creator, state_key=""),
                auth_events,
            )

        # If the domain of the sender does not match the state key, reject.
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _alias_event(RoomVersions.V1, creator, state_key="test.com"),
                auth_events,
            )

        # Note that the member does *not* need to be in the room.
        event_auth.check_state_dependent_auth_rules(
            _alias_event(RoomVersions.V1, other),
            auth_events,
        )

    def test_msc2432_alias_event(self) -> None:
        """After MSC2432, alias events have no special behavior."""
        creator = "@creator:example.com"
        other = "@other:example.com"
        auth_events = [
            _create_event(RoomVersions.V6, creator),
            _join_event(RoomVersions.V6, creator),
        ]

        # creator should be able to send aliases
        event_auth.check_state_dependent_auth_rules(
            _alias_event(RoomVersions.V6, creator),
            auth_events,
        )

        # No particular checks are done on the state key.
        event_auth.check_state_dependent_auth_rules(
            _alias_event(RoomVersions.V6, creator, state_key=""),
            auth_events,
        )
        event_auth.check_state_dependent_auth_rules(
            _alias_event(RoomVersions.V6, creator, state_key="test.com"),
            auth_events,
        )

        # Per standard auth rules, the member must be in the room.
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _alias_event(RoomVersions.V6, other),
                auth_events,
            )

    def test_msc4242_state_dag_rules(self) -> None:
        """Tests additional rules in place for state DAG rooms.

        1. m.room.create => if it has any prev_state_events, reject.
        2. Considering the event's prev_state_events:
         i. If there are entries which do not belong in the same room, reject.
         ii. If there are entries which do not have a state_key, reject.
         iii. If there are entries which were themselves rejected under the checks performed on receipt of a PDU, reject.
        """
        creator = "@creator:example.com"
        room_version = RoomVersions.MSC4242v12

        create_event = make_test_event(
            {
                "type": "m.room.create",
                "sender": creator,
                "state_key": "",
                "content": {"creator": creator},
                "prev_events": [],
                "prev_state_events": [],
            },
            room_version,
        )
        create_event_2 = make_test_event(
            {
                "type": "m.room.create",
                "sender": creator,
                "state_key": "",
                "content": {"creator": creator, "another": "room"},
                "prev_events": [],
                "prev_state_events": [],
            },
            room_version,
        )
        room_id = create_event.room_id
        another_room_id = create_event_2.room_id
        join_event = make_test_event(
            {
                "room_id": room_id,
                "type": "m.room.member",
                "sender": creator,
                "state_key": creator,
                "content": {"membership": "join"},
                "prev_events": [create_event.event_id],
                "prev_state_events": [create_event.event_id],
            },
            room_version,
            {"calculated_auth_event_ids": [create_event.event_id]},
        )
        event_in_another_room = make_test_event(
            {
                "room_id": another_room_id,
                "type": "m.room.join_rules",
                "sender": creator,
                "state_key": "",
                "content": {"join_rule": "public"},
                "prev_events": [join_event.event_id],
                "prev_state_events": [join_event.event_id],
            },
            room_version,
            {"calculated_auth_event_ids": [create_event.event_id, join_event.event_id]},
        )
        msg_event = make_test_event(
            {
                "room_id": room_id,
                "type": "m.room.message",
                "sender": creator,
                "content": {"msgtype": "m.text", "body": "I am a message"},
                "prev_events": [join_event.event_id],
                "prev_state_events": [join_event.event_id],
            },
            room_version,
            {"calculated_auth_event_ids": [create_event.event_id, join_event.event_id]},
        )
        rejected_event = make_test_event(
            {
                "room_id": room_id,
                "type": "m.room.name",
                "sender": creator,
                "state_key": "",
                "content": {"name": "REJECTED"},
                "prev_events": [join_event.event_id],
                "prev_state_events": [join_event.event_id],
            },
            room_version,
            {"calculated_auth_event_ids": [create_event.event_id, join_event.event_id]},
            rejected_reason=RejectedReason.AUTH_ERROR,
        )
        RejectingTestCase = namedtuple(
            "RejectingTestCase", "name events_in_store test_event"
        )
        rejecting_test_cases = [
            RejectingTestCase(
                name="create event has prev_state_events",
                events_in_store=[],
                test_event=make_test_event(
                    {
                        "type": "m.room.create",
                        "sender": creator,
                        "state_key": "",
                        "content": {"creator": creator},
                        "prev_events": [],
                        "prev_state_events": [create_event.event_id],
                    },
                    room_version,
                    {},
                ),
            ),
            RejectingTestCase(
                name="prev_state_event belongs in a different room",
                events_in_store=[create_event, join_event, event_in_another_room],
                test_event=make_test_event(
                    {
                        "room_id": room_id,
                        "type": "m.room.name",
                        "sender": creator,
                        "state_key": "",
                        "content": {"name": "prev_state_event is in another room"},
                        "prev_events": [join_event.event_id],
                        "prev_state_events": [event_in_another_room.event_id],
                    },
                    room_version,
                    {
                        "calculated_auth_event_ids": [
                            create_event.event_id,
                            join_event.event_id,
                        ]
                    },
                ),
            ),
            RejectingTestCase(
                name="prev_state_event is a message event",
                events_in_store=[create_event, join_event, msg_event],
                test_event=make_test_event(
                    {
                        "room_id": room_id,
                        "type": "m.room.name",
                        "sender": creator,
                        "state_key": "",
                        "content": {"name": "prev state event is a message"},
                        "prev_events": [msg_event.event_id],
                        "prev_state_events": [msg_event.event_id],
                    },
                    room_version,
                    {
                        "calculated_auth_event_ids": [
                            create_event.event_id,
                            join_event.event_id,
                        ]
                    },
                ),
            ),
            RejectingTestCase(
                name="prev_state_event was rejected",
                events_in_store=[create_event, join_event, rejected_event],
                test_event=make_test_event(
                    {
                        "room_id": room_id,
                        "type": "m.room.name",
                        "sender": creator,
                        "state_key": "",
                        "content": {"name": "prev state event was rejected"},
                        "prev_events": [join_event.event_id],
                        "prev_state_events": [rejected_event.event_id],
                    },
                    room_version,
                    {
                        "calculated_auth_event_ids": [
                            create_event.event_id,
                            join_event.event_id,
                        ]
                    },
                ),
            ),
        ]

        for test_case in rejecting_test_cases:
            event_store = _StubEventSourceStore()
            event_store.add_events(test_case.events_in_store)

            with self.assertRaises(
                AuthError, msg=f"test case {test_case.name} was not rejected"
            ):
                get_awaitable_result(
                    event_auth.check_state_independent_auth_rules(
                        event_store, test_case.test_event
                    )
                )

    @parameterized.expand([(RoomVersions.V1, True), (RoomVersions.V6, False)])
    def test_notifications(
        self, room_version: RoomVersion, allow_modification: bool
    ) -> None:
        """
        Notifications power levels get checked due to MSC2209.
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = [
            _create_event(room_version, creator),
            _join_event(room_version, creator),
            _power_levels_event(
                room_version, creator, {"state_default": "30", "users": {pleb: "30"}}
            ),
            _join_event(room_version, pleb),
        ]

        pl_event = _power_levels_event(
            room_version, pleb, {"notifications": {"room": 100}}
        )

        # on room V1, pleb should be able to modify the notifications power level.
        if allow_modification:
            event_auth.check_state_dependent_auth_rules(pl_event, auth_events)

        else:
            # But an MSC2209 room rejects this change.
            with self.assertRaises(AuthError):
                event_auth.check_state_dependent_auth_rules(pl_event, auth_events)

    def test_join_rules_public(self) -> None:
        """
        Test joining a public room.
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = {
            ("m.room.create", ""): _create_event(RoomVersions.V6, creator),
            ("m.room.member", creator): _join_event(RoomVersions.V6, creator),
            ("m.room.join_rules", ""): _join_rules_event(
                RoomVersions.V6, creator, "public"
            ),
        }

        # Check join.
        event_auth.check_state_dependent_auth_rules(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

        # A user cannot be force-joined to a room.
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _member_event(RoomVersions.V6, pleb, "join", sender=creator),
                auth_events.values(),
            )

        # Banned should be rejected.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "ban"
        )
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

        # A user who left can re-join.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "leave"
        )
        event_auth.check_state_dependent_auth_rules(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

        # A user can send a join if they're in the room.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "join"
        )
        event_auth.check_state_dependent_auth_rules(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

        # A user can accept an invite.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "invite", sender=creator
        )
        event_auth.check_state_dependent_auth_rules(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

    def test_join_rules_invite(self) -> None:
        """
        Test joining an invite only room.
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = {
            ("m.room.create", ""): _create_event(RoomVersions.V6, creator),
            ("m.room.member", creator): _join_event(RoomVersions.V6, creator),
            ("m.room.join_rules", ""): _join_rules_event(
                RoomVersions.V6, creator, "invite"
            ),
        }

        # A join without an invite is rejected.
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

        # A user cannot be force-joined to a room.
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _member_event(RoomVersions.V6, pleb, "join", sender=creator),
                auth_events.values(),
            )

        # Banned should be rejected.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "ban"
        )
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

        # A user who left cannot re-join.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "leave"
        )
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

        # A user can send a join if they're in the room.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "join"
        )
        event_auth.check_state_dependent_auth_rules(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

        # A user can accept an invite.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "invite", sender=creator
        )
        event_auth.check_state_dependent_auth_rules(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

    def test_join_rules_restricted_old_room(self) -> None:
        """Old room versions should reject joins to restricted rooms"""
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = {
            ("m.room.create", ""): _create_event(RoomVersions.V6, creator),
            ("m.room.member", creator): _join_event(RoomVersions.V6, creator),
            ("m.room.power_levels", ""): _power_levels_event(
                RoomVersions.V6, creator, {"invite": 0}
            ),
            ("m.room.join_rules", ""): _join_rules_event(
                RoomVersions.V6, creator, "restricted"
            ),
        }

        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

    def test_join_rules_msc3083_restricted(self) -> None:
        """
        Test joining a restricted room from MSC3083.

        This is similar to the public test, but has some additional checks on
        signatures.

        The checks which care about signatures fake them by simply adding an
        object of the proper form, not generating valid signatures.
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = {
            ("m.room.create", ""): _create_event(RoomVersions.V8, creator),
            ("m.room.member", creator): _join_event(RoomVersions.V8, creator),
            ("m.room.power_levels", ""): _power_levels_event(
                RoomVersions.V8, creator, {"invite": 0}
            ),
            ("m.room.join_rules", ""): _join_rules_event(
                RoomVersions.V8, creator, "restricted"
            ),
        }

        # A properly formatted join event should work.
        authorised_join_event = _join_event(
            RoomVersions.V8,
            pleb,
            additional_content={
                EventContentFields.AUTHORISING_USER: "@creator:example.com"
            },
        )
        event_auth.check_state_dependent_auth_rules(
            authorised_join_event,
            auth_events.values(),
        )

        # A join issued by a specific user works (i.e. the power level checks
        # are done properly).
        pl_auth_events = auth_events.copy()
        pl_auth_events[("m.room.power_levels", "")] = _power_levels_event(
            RoomVersions.V8,
            creator,
            {"invite": 100, "users": {"@inviter:foo.test": 150}},
        )
        pl_auth_events[("m.room.member", "@inviter:foo.test")] = _join_event(
            RoomVersions.V8, "@inviter:foo.test"
        )
        event_auth.check_state_dependent_auth_rules(
            _join_event(
                RoomVersions.V8,
                pleb,
                additional_content={
                    EventContentFields.AUTHORISING_USER: "@inviter:foo.test"
                },
            ),
            pl_auth_events.values(),
        )

        # A join which is missing an authorised server is rejected.
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _join_event(RoomVersions.V8, pleb),
                auth_events.values(),
            )

        # An join authorised by a user who is not in the room is rejected.
        pl_auth_events = auth_events.copy()
        pl_auth_events[("m.room.power_levels", "")] = _power_levels_event(
            RoomVersions.V8,
            creator,
            {"invite": 100, "users": {"@other:example.com": 150}},
        )
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _join_event(
                    RoomVersions.V8,
                    pleb,
                    additional_content={
                        EventContentFields.AUTHORISING_USER: "@other:example.com"
                    },
                ),
                auth_events.values(),
            )

        # A user cannot be force-joined to a room. (This uses an event which
        # *would* be valid, but is sent be a different user.)
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                _member_event(
                    RoomVersions.V8,
                    pleb,
                    "join",
                    sender=creator,
                    additional_content={
                        EventContentFields.AUTHORISING_USER: "@inviter:foo.test"
                    },
                ),
                auth_events.values(),
            )

        # Banned should be rejected.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V8, pleb, "ban"
        )
        with self.assertRaises(AuthError):
            event_auth.check_state_dependent_auth_rules(
                authorised_join_event,
                auth_events.values(),
            )

        # A user who left can re-join.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V8, pleb, "leave"
        )
        event_auth.check_state_dependent_auth_rules(
            authorised_join_event,
            auth_events.values(),
        )

        # A user can send a join if they're in the room. (This doesn't need to
        # be authorised since the user is already joined.)
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V8, pleb, "join"
        )
        event_auth.check_state_dependent_auth_rules(
            _join_event(RoomVersions.V8, pleb),
            auth_events.values(),
        )

        # A user can accept an invite. (This doesn't need to be authorised since
        # the user was invited.)
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V8, pleb, "invite", sender=creator
        )
        event_auth.check_state_dependent_auth_rules(
            _join_event(RoomVersions.V8, pleb),
            auth_events.values(),
        )

    def test_room_v10_rejects_string_power_levels(self) -> None:
        pl_event_content = {"users_default": "42"}
        pl_event = make_test_event(
            {
                "room_id": TEST_ROOM_ID,
                **_maybe_get_event_id_dict_for_room_version(RoomVersions.V10),
                "type": "m.room.power_levels",
                "sender": "@test:test.com",
                "state_key": "",
                "content": pl_event_content,
                "signatures": {"test.com": {"ed25519:0": "some9signature"}},
            },
            room_version=RoomVersions.V10,
        )

        pl_event2_content = {"events": {"m.room.name": "42", "m.room.power_levels": 42}}
        pl_event2 = make_test_event(
            {
                "room_id": TEST_ROOM_ID,
                **_maybe_get_event_id_dict_for_room_version(RoomVersions.V10),
                "type": "m.room.power_levels",
                "sender": "@test:test.com",
                "state_key": "",
                "content": pl_event2_content,
                "signatures": {"test.com": {"ed25519:0": "some9signature"}},
            },
            room_version=RoomVersions.V10,
        )

        with self.assertRaises(SynapseError):
            event_auth._check_power_levels(
                pl_event.room_version, pl_event, {("fake_type", "fake_key"): pl_event2}
            )

        with self.assertRaises(SynapseError):
            event_auth._check_power_levels(
                pl_event.room_version, pl_event2, {("fake_type", "fake_key"): pl_event}
            )

    def test_room_v10_rejects_other_non_integer_power_levels(self) -> None:
        """We should reject PLs that are non-integer, non-string JSON values.

        test_room_v10_rejects_string_power_levels above handles the string case.
        """

        def create_event(pl_event_content: dict[str, Any]) -> EventBase:
            return make_test_event(
                {
                    "room_id": TEST_ROOM_ID,
                    **_maybe_get_event_id_dict_for_room_version(RoomVersions.V10),
                    "type": "m.room.power_levels",
                    "sender": "@test:test.com",
                    "state_key": "",
                    "content": pl_event_content,
                    "signatures": {"test.com": {"ed25519:0": "some9signature"}},
                },
                room_version=RoomVersions.V10,
            )

        contents: Iterable[dict[str, Any]] = [
            {"notifications": {"room": None}},
            {"users": {"@alice:wonderland": []}},
            {"users_default": {}},
        ]
        for content in contents:
            event = create_event(content)
            with self.assertRaises(SynapseError):
                event_auth._check_power_levels(event.room_version, event, {})

    def test_event_exists_in_state_dag(self) -> None:
        events_that_exist_in_state_dag = [
            {
                "type": "m.room.create",
                "state_key": "",
                "content": {},
            },
            {
                "type": "m.room.join_rules",
                "state_key": "",
                "content": {},
            },
            {
                "type": "m.room.power_levels",
                "state_key": "",
                "content": {},
            },
            {
                "type": "m.room.server_acl",
                "state_key": "",
                "content": {},
            },
            {
                "type": "m.room.member",
                "state_key": "@alice:somewhere",
                "content": {},
            },
            {
                "type": "m.room.third_party_invite",
                "state_key": "flibble",
                "content": {},
            },
            {
                "type": "m.room.create",
                "state_key": " ",
                "content": {},
            },
            {
                "type": "m.room.join_rules",
                "state_key": " ",
                "content": {},
            },
            {
                "type": "m.room.power_levels",
                "state_key": " ",
                "content": {},
            },
            {
                "type": "m.room.name",
                "state_key": "",
                "content": {},
            },
            {
                "type": "m.room.member",
                "state_key": "",
                "content": {},
            },
            {
                "type": "m.room.member",
                "state_key": "hello_world",
                "content": {},
            },
        ]
        events_that_dont_exist_in_state_dag = [
            {
                "type": "m.room.message",
                "content": {},
            },
            {
                "type": "m.room.create",
                "content": {},
            },
            {
                "type": "m.room.join_rules",
                "content": {},
            },
            {
                "type": "m.room.power_levels",
                "content": {},
            },
        ]

        def check_events(events: list[dict], should_exist: bool) -> None:
            for ev in events:
                base = {
                    "room_id": TEST_ROOM_ID,
                    "sender": "@test:test.com",
                    "signatures": {"test.com": {"ed25519:0": "some9signature"}},
                }
                base.update(ev)
                event = make_test_event(base, RoomVersions.V10)
                got = event_exists_in_state_dag(event)
                self.assertEqual(
                    got, should_exist, f"{ev} should_exist={should_exist} but got {got}"
                )

        check_events(events_that_exist_in_state_dag, should_exist=True)
        check_events(events_that_dont_exist_in_state_dag, should_exist=False)


# helpers for making events
TEST_DOMAIN = "example.com"
TEST_ROOM_ID = f"!test_room:{TEST_DOMAIN}"


def _create_event(
    room_version: RoomVersion,
    user_id: str,
) -> EventBase:
    return make_test_event(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "m.room.create",
            "state_key": "",
            "sender": user_id,
            "content": {"creator": user_id},
            "auth_events": [],
        },
        room_version=room_version,
    )


def _member_event(
    room_version: RoomVersion,
    user_id: str,
    membership: str,
    sender: str | None = None,
    additional_content: dict | None = None,
) -> EventBase:
    return make_test_event(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "m.room.member",
            "sender": sender or user_id,
            "state_key": user_id,
            "content": {"membership": membership, **(additional_content or {})},
            "auth_events": [],
            "prev_events": [],
        },
        room_version=room_version,
    )


def _join_event(
    room_version: RoomVersion,
    user_id: str,
    additional_content: dict | None = None,
) -> EventBase:
    return _member_event(
        room_version,
        user_id,
        "join",
        additional_content=additional_content,
    )


def _power_levels_event(
    room_version: RoomVersion,
    sender: str,
    content: JsonDict,
) -> EventBase:
    return make_test_event(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "m.room.power_levels",
            "sender": sender,
            "state_key": "",
            "content": content,
        },
        room_version=room_version,
    )


def _alias_event(room_version: RoomVersion, sender: str, **kwargs: Any) -> EventBase:
    data = {
        "room_id": TEST_ROOM_ID,
        **_maybe_get_event_id_dict_for_room_version(room_version),
        "type": "m.room.aliases",
        "sender": sender,
        "state_key": get_domain_from_id(sender),
        "content": {"aliases": []},
    }
    data.update(**kwargs)
    return make_test_event(data, room_version=room_version)


def _build_auth_dict_for_room_version(
    room_version: RoomVersion, auth_events: Iterable[EventBase]
) -> list:
    if room_version.event_format == EventFormatVersions.ROOM_V1_V2:
        return [(e.event_id, "not_used") for e in auth_events]
    else:
        return [e.event_id for e in auth_events]


def _random_state_event(
    room_version: RoomVersion,
    sender: str,
    auth_events: Iterable[EventBase] | None = None,
) -> EventBase:
    if auth_events is None:
        auth_events = []
    return make_test_event(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "test.state",
            "sender": sender,
            "state_key": "",
            "content": {"membership": "join"},
            "auth_events": _build_auth_dict_for_room_version(room_version, auth_events),
        },
        room_version=room_version,
    )


def _join_rules_event(
    room_version: RoomVersion,
    sender: str,
    join_rule: str,
    rejected_reason: str | None = None,
) -> EventBase:
    return make_test_event(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "m.room.join_rules",
            "sender": sender,
            "state_key": "",
            "content": {
                "join_rule": join_rule,
            },
        },
        room_version=room_version,
        rejected_reason=rejected_reason,
    )


event_count = 0


def _maybe_get_event_id_dict_for_room_version(room_version: RoomVersion) -> dict:
    """If this room version needs it, generate an event id"""
    if room_version.event_format != EventFormatVersions.ROOM_V1_V2:
        return {}

    global event_count
    c = event_count
    event_count += 1
    return {"event_id": "!%i:example.com" % (c,)}


MEMBER_KEY_ROOM_VERSION = RoomVersions.MSC4430v12


class _Member:
    """An identity in a member key room: a signing key and its public form."""

    def __init__(self) -> None:
        self.signing_key = generate_signing_key(MemberKeys.KEY_VERSION)
        self.key = MemberKey.from_signing_key(self.signing_key).to_string()

    @property
    def signer(self) -> EventSigner:
        """Signs as the event's sender."""
        return (MemberKeys.SIGNATURE_ENTITY, self.signing_key)

    @property
    def authorising_signer(self) -> EventSigner:
        """Signs as the authorising member of a restricted join."""
        return (self.key, self.signing_key)


def _member_key_event(event_dict: JsonDict, *signers: EventSigner) -> EventBase:
    """Build a signed event in a member key room. `room_id` must be given except
    for create events."""
    event_dict = {
        "depth": 1,
        "origin_server_ts": 1,
        "prev_events": [],
        "prev_state_events": [],
        **event_dict,
    }
    add_hashes_and_signatures_from_signers(
        MEMBER_KEY_ROOM_VERSION, event_dict, signers, server_name=TEST_DOMAIN
    )
    return make_event_from_dict(event_dict, MEMBER_KEY_ROOM_VERSION)


class MemberKeyRoomAuthTestCase(unittest.TestCase):
    """MSC4430: the auth rules in a room whose members are member keys. Every
    event is hand built and signed; nothing here touches storage or network."""

    def setUp(self) -> None:
        self.creator = _Member()
        self.member = _Member()
        self.other = _Member()
        self.create = _member_key_event(
            {
                "type": "m.room.create",
                "state_key": "",
                "sender": self.creator.key,
                "content": {"room_version": MEMBER_KEY_ROOM_VERSION.identifier},
            },
            self.creator.signer,
        )
        self.room_id = self.create.room_id
        self.creator_join = self._member_event(
            self.creator, "join", prev_events=[self.create.event_id]
        )
        self.public = self._event(
            {
                "type": "m.room.join_rules",
                "state_key": "",
                "sender": self.creator.key,
                "content": {"join_rule": "public"},
            },
            self.creator.signer,
        )

    def _event(self, event_dict: JsonDict, *signers: EventSigner) -> EventBase:
        return _member_key_event({"room_id": self.room_id, **event_dict}, *signers)

    def _member_event(
        self,
        target: _Member,
        membership: str,
        sender: _Member | None = None,
        content: JsonDict | None = None,
        prev_events: list[str] | None = None,
        *extra_signers: EventSigner,
    ) -> EventBase:
        sender = sender or target
        return self._event(
            {
                "type": "m.room.member",
                "state_key": target.key,
                "sender": sender.key,
                "content": {"membership": membership, **(content or {})},
                "prev_events": prev_events or [],
            },
            sender.signer,
            *extra_signers,
        )

    def _assert_rejected(self, event: EventBase, reason: str) -> None:
        with self.assertRaises(AuthError) as cm:
            event_auth.validate_event_for_room_version(event)
        self.assertEqual(cm.exception.code, 403)
        self.assertIn(reason, cm.exception.msg)

    def test_validate_sender_and_signature(self) -> None:
        """An event's sender is a canonical member key, and the event is signed by
        it under the `sender` entity."""
        message = self._event(
            {"type": "m.room.message", "sender": self.member.key, "content": {}},
            self.member.signer,
        )
        event_auth.validate_event_for_room_version(message)

        # A user ID, signed by its server as in every other room version.
        legacy_dict = {
            "room_id": self.room_id,
            "type": "m.room.message",
            "sender": f"@alice:{TEST_DOMAIN}",
            "content": {},
            "depth": 1,
            "origin_server_ts": 1,
            "prev_events": [],
            "prev_state_events": [],
        }
        add_hashes_and_signatures(
            MEMBER_KEY_ROOM_VERSION, legacy_dict, TEST_DOMAIN, generate_signing_key("x")
        )
        self._assert_rejected(
            make_event_from_dict(legacy_dict, MEMBER_KEY_ROOM_VERSION),
            "not a valid member key",
        )

        # A non-canonical encoding of the member's key (the same bytes with a
        # spare bit of the last character set) is not the member.
        alphabet = (
            string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
        )
        non_canonical = message.get_pdu_json()
        non_canonical["sender"] = (
            self.member.key[:-1] + alphabet[alphabet.index(self.member.key[-1]) + 1]
        )
        self._assert_rejected(
            make_event_from_dict(non_canonical, MEMBER_KEY_ROOM_VERSION),
            "not a valid member key",
        )

        # No signature by the sender (a template, or a server-only signature).
        unsigned = self._event(
            {"type": "m.room.message", "sender": self.member.key, "content": {}}
        )
        self._assert_rejected(unsigned, "not signed by its sender's member key")

    def test_validate_membership_state_key(self) -> None:
        invite = self._member_event(self.other, "invite", sender=self.creator)
        event_auth.validate_event_for_room_version(invite)

        bad = invite.get_pdu_json()
        bad["state_key"] = f"@bob:{TEST_DOMAIN}"
        self._assert_rejected(
            make_event_from_dict(bad, MEMBER_KEY_ROOM_VERSION),
            "state_key is not a valid member key",
        )

    def test_validate_restricted_join_authoriser(self) -> None:
        """A restricted join names the authorising member key, which signs under
        the member key string itself."""
        authorised = {EventContentFields.AUTHORISING_USER: self.member.key}
        join = self._member_event(
            self.other,
            "join",
            None,
            authorised,
            None,
            self.member.authorising_signer,
        )
        event_auth.validate_event_for_room_version(join)
        self.assertEqual(
            set(join.get_pdu_json()["signatures"]),
            {MemberKeys.SIGNATURE_ENTITY, self.member.key},
        )

        # Signed by the joiner only.
        self._assert_rejected(
            self._member_event(self.other, "join", None, authorised),
            "not signed by authorising member key",
        )

        # The authoriser is not a member key, or not even a string.
        for not_a_key in (f"@mod:{TEST_DOMAIN}", 42):
            bad = join.get_pdu_json()
            bad["content"][EventContentFields.AUTHORISING_USER] = not_a_key
            self._assert_rejected(
                make_event_from_dict(bad, MEMBER_KEY_ROOM_VERSION),
                "Authorising user is not a valid member key",
            )

    def test_state_dependent_rules(self) -> None:
        """The state-dependent rules work on member keys: membership, power levels
        and sender checks compare keys, and the `m.federate` domain rule, which
        has nothing to compare, is not applied."""
        auth_events = [self.create, self.creator_join, self.public]

        # The creator's first join, and a join to a public room.
        event_auth.check_state_dependent_auth_rules(self.creator_join, [self.create])
        member_join = self._member_event(self.member, "join")
        event_auth.check_state_dependent_auth_rules(member_join, auth_events)
        auth_events.append(member_join)

        # Members can send; non-members cannot.
        event_auth.check_state_dependent_auth_rules(
            self._event(
                {"type": "m.room.message", "sender": self.member.key, "content": {}},
                self.member.signer,
            ),
            auth_events,
        )
        with self.assertRaises(AuthError) as cm:
            event_auth.check_state_dependent_auth_rules(
                self._event(
                    {"type": "m.room.message", "sender": self.other.key, "content": {}},
                    self.other.signer,
                ),
                auth_events,
            )
        self.assertIn("not in room", cm.exception.msg)

        # The creator (creator power) can invite and kick by key.
        invite = self._member_event(self.other, "invite", sender=self.creator)
        event_auth.check_state_dependent_auth_rules(invite, auth_events)
        kick = self._member_event(self.member, "leave", sender=self.creator)
        event_auth.check_state_dependent_auth_rules(kick, auth_events)
        with self.assertRaises(AuthError) as cm:
            event_auth.check_state_dependent_auth_rules(
                self._member_event(self.creator, "leave", sender=self.member),
                auth_events,
            )
        self.assertIn("cannot kick", cm.exception.msg)

        # Power levels grant power to member keys, never to user IDs.
        def power_levels(users: JsonDict) -> EventBase:
            return self._event(
                {
                    "type": "m.room.power_levels",
                    "state_key": "",
                    "sender": self.creator.key,
                    "content": {"users": users},
                },
                self.creator.signer,
            )

        event_auth.check_state_dependent_auth_rules(
            power_levels({self.member.key: 50}), auth_events
        )
        with self.assertRaises(SynapseError) as bad_users:
            event_auth.check_state_dependent_auth_rules(
                power_levels({f"@alice:{TEST_DOMAIN}": 50}), auth_events
            )
        self.assertEqual(bad_users.exception.code, 400)
        self.assertIn("Not a valid member key", bad_users.exception.msg)

    def test_restricted_join_rules(self) -> None:
        """A join to a restricted room is authorised by a member key: that member
        must be in the room with invite power."""
        restricted = self._event(
            {
                "type": "m.room.join_rules",
                "state_key": "",
                "sender": self.creator.key,
                "content": {"join_rule": "restricted", "allow": []},
            },
            self.creator.signer,
        )
        member_join = self._member_event(self.member, "join")
        join = self._member_event(
            self.other,
            "join",
            None,
            {EventContentFields.AUTHORISING_USER: self.member.key},
            None,
            self.member.authorising_signer,
        )

        event_auth.check_state_dependent_auth_rules(
            join, [self.create, self.creator_join, restricted, member_join]
        )

        # No authoriser named.
        with self.assertRaises(AuthError) as cm:
            event_auth.check_state_dependent_auth_rules(
                self._member_event(self.other, "join"),
                [self.create, self.creator_join, restricted, member_join],
            )
        self.assertIn("missing authorising user", cm.exception.msg)

        # The authoriser is not in the room.
        with self.assertRaises(AuthError) as cm:
            event_auth.check_state_dependent_auth_rules(
                join, [self.create, self.creator_join, restricted]
            )
        self.assertIn("not in room", cm.exception.msg)

        # The authoriser lacks invite power.
        invite_level = self._event(
            {
                "type": "m.room.power_levels",
                "state_key": "",
                "sender": self.creator.key,
                "content": {"invite": 50, "users": {}},
            },
            self.creator.signer,
        )
        with self.assertRaises(AuthError) as cm:
            event_auth.check_state_dependent_auth_rules(
                join,
                [self.create, self.creator_join, restricted, member_join, invite_level],
            )
        self.assertIn("authorised by invalid server", cm.exception.msg)

    def test_federate_rule_not_applied(self) -> None:
        """`m.federate: false` cannot be evaluated against member keys (no domain),
        so a join by any key is allowed; the same room in the underlying version
        rejects a join from another server."""
        unfederatable = _member_key_event(
            {
                "type": "m.room.create",
                "state_key": "",
                "sender": self.creator.key,
                "content": {
                    "room_version": MEMBER_KEY_ROOM_VERSION.identifier,
                    EventContentFields.FEDERATE: False,
                },
            },
            self.creator.signer,
        )
        room_id = unfederatable.room_id
        creator_join = _member_key_event(
            {
                "room_id": room_id,
                "type": "m.room.member",
                "state_key": self.creator.key,
                "sender": self.creator.key,
                "content": {"membership": "join"},
                "prev_events": [unfederatable.event_id],
            },
            self.creator.signer,
        )
        public = _member_key_event(
            {
                "room_id": room_id,
                "type": "m.room.join_rules",
                "state_key": "",
                "sender": self.creator.key,
                "content": {"join_rule": "public"},
            },
            self.creator.signer,
        )
        join = _member_key_event(
            {
                "room_id": room_id,
                "type": "m.room.member",
                "state_key": self.member.key,
                "sender": self.member.key,
                "content": {"membership": "join"},
            },
            self.member.signer,
        )
        event_auth.check_state_dependent_auth_rules(
            join, [unfederatable, creator_join, public]
        )

        # Contrast: in the underlying room version the rule applies to user IDs.
        legacy = RoomVersions.MSC4242v12
        legacy_create = make_test_event(
            {
                "type": "m.room.create",
                "state_key": "",
                "sender": "@creator:one.example",
                "content": {EventContentFields.FEDERATE: False},
                "prev_events": [],
                "prev_state_events": [],
            },
            legacy,
        )
        legacy_room_id = legacy_create.room_id
        legacy_auth = [
            legacy_create,
            make_test_event(
                {
                    "room_id": legacy_room_id,
                    "type": "m.room.member",
                    "state_key": "@creator:one.example",
                    "sender": "@creator:one.example",
                    "content": {"membership": "join"},
                },
                legacy,
            ),
            make_test_event(
                {
                    "room_id": legacy_room_id,
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "sender": "@creator:one.example",
                    "content": {"join_rule": "public"},
                },
                legacy,
            ),
        ]
        with self.assertRaises(AuthError) as cm:
            event_auth.check_state_dependent_auth_rules(
                make_test_event(
                    {
                        "room_id": legacy_room_id,
                        "type": "m.room.member",
                        "state_key": "@other:two.example",
                        "sender": "@other:two.example",
                        "content": {"membership": "join"},
                    },
                    legacy,
                ),
                legacy_auth,
            )
        self.assertIn("unfederatable", cm.exception.msg)

    def test_additional_creators(self) -> None:
        """`additional_creators` in the create event are member keys."""
        store = _StubEventSourceStore()

        def create(additional_creators: list) -> EventBase:
            # `content.room_version` is left out: the create rules require it to
            # name a registered version, and the member key version is not
            # registered yet. The event itself is still in a member key room.
            return _member_key_event(
                {
                    "type": "m.room.create",
                    "state_key": "",
                    "sender": self.creator.key,
                    "content": {
                        EventContentFields.ADDITIONAL_CREATORS: additional_creators,
                    },
                },
                self.creator.signer,
            )

        get_awaitable_result(
            event_auth.check_state_independent_auth_rules(
                store, create([self.member.key])
            )
        )
        with self.assertRaises(AuthError) as cm:
            get_awaitable_result(
                event_auth.check_state_independent_auth_rules(
                    store, create([f"@alice:{TEST_DOMAIN}"])
                )
            )
        self.assertIn("not a valid member key", cm.exception.msg)

        # The underlying room version wants user IDs.
        event_auth.check_valid_additional_creators(
            [f"@alice:{TEST_DOMAIN}"], RoomVersions.MSC4242v12
        )
        with self.assertRaises(AuthError):
            event_auth.check_valid_additional_creators(
                [self.member.key], RoomVersions.MSC4242v12
            )

    def test_third_party_invite(self) -> None:
        """The third-party invite rule is identity-agnostic: the key that signs
        `signed` comes from the inviter's `m.room.third_party_invite` event, and
        `signed.mxid` must equal the `state_key`. So an invite whose signed mxid is
        the member key is accepted, and one naming a user ID is not. (How an
        identity server comes to sign a member key is a separate question.)"""
        id_signing_key = generate_signing_key("0")
        token = "token"
        third_party_invite = self._event(
            {
                "type": "m.room.third_party_invite",
                "state_key": token,
                "sender": self.creator.key,
                "content": {
                    "display_name": "bob",
                    "key_validity_url": "https://id.example/_matrix/identity/v2/pubkey/isvalid",
                    "public_key": encode_base64(
                        get_verify_key(id_signing_key).encode()
                    ),
                },
            },
            self.creator.signer,
        )
        auth_events = [self.create, self.creator_join, self.public, third_party_invite]

        def invite(mxid: str) -> EventBase:
            signed = sign_json(
                {"mxid": mxid, "token": token}, "id.example", id_signing_key
            )
            return self._member_event(
                self.other,
                "invite",
                self.creator,
                {"third_party_invite": {"display_name": "bob", "signed": signed}},
            )

        event_auth.check_state_dependent_auth_rules(invite(self.other.key), auth_events)

        with self.assertRaises(AuthError) as cm:
            event_auth.check_state_dependent_auth_rules(
                invite(f"@bob:{TEST_DOMAIN}"), auth_events
            )
        self.assertIn("not invited", cm.exception.msg)


class RedactionSenderMatchesTestCase(unittest.TestCase):
    def test_servers_match(self) -> None:
        self.assertTrue(
            redaction_sender_matches(RoomVersions.V12, "@a:x.example", "@b:x.example")
        )
        self.assertFalse(
            redaction_sender_matches(RoomVersions.V12, "@a:x.example", "@a:y.example")
        )

    def test_member_keys_match(self) -> None:
        a, b = _Member(), _Member()
        self.assertTrue(redaction_sender_matches(MEMBER_KEY_ROOM_VERSION, a.key, a.key))
        self.assertFalse(
            redaction_sender_matches(MEMBER_KEY_ROOM_VERSION, a.key, b.key)
        )
