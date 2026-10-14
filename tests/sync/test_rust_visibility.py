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

"""The per-event checks of `synapse.visibility` against `rust/src/visibility.rs`,
on random events, state and flags."""

import random

from parameterized import parameterized

from synapse.api.constants import EventTypes, HistoryVisibility, Membership
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.synapse_rust.visibility import check_event_for_client_for_test
from synapse.types import (
    JsonDict,
    RetentionPolicy,
    StateKey,
    StateMap,
)
from synapse.visibility import _check_event_for_client

from tests import unittest
from tests.sync.fixtures import (
    ME,
    SEEDS,
    USERS,
)
from tests.test_utils.event_builders import make_test_event


class _FakeClock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def time_msec(self) -> int:
        return self.now_ms


class VisibilityTestCase(unittest.TestCase):
    """The per-event checks of `synapse.visibility` against
    `rust/src/visibility.rs`."""

    VISIBILITIES = [
        HistoryVisibility.WORLD_READABLE,
        HistoryVisibility.SHARED,
        HistoryVisibility.INVITED,
        HistoryVisibility.JOINED,
        "bogus",
        None,
    ]
    MEMBERSHIPS = [
        Membership.JOIN,
        Membership.INVITE,
        Membership.KNOCK,
        Membership.LEAVE,
        Membership.BAN,
        "bogus",
        None,
    ]

    @classmethod
    def _state_event(
        cls, rng: random.Random, event_type: str, state_key: str, content: JsonDict
    ) -> EventBase:
        unsigned: JsonDict = {}
        if rng.random() < 0.5:
            prev: JsonDict = {}
            if event_type == EventTypes.Member:
                prev_membership = rng.choice(cls.MEMBERSHIPS)
                if prev_membership is not None:
                    prev["membership"] = prev_membership
            elif event_type == EventTypes.RoomHistoryVisibility:
                prev_visibility = rng.choice(cls.VISIBILITIES)
                if prev_visibility is not None:
                    prev["history_visibility"] = prev_visibility
            unsigned["prev_content"] = prev
        return make_test_event(
            {
                "type": event_type,
                "state_key": state_key,
                "sender": rng.choice(USERS),
                "room_id": "!a:test",
                "content": content,
                "unsigned": unsigned,
            },
            room_version=RoomVersions.V10,
        )

    @classmethod
    def _random_state(cls, rng: random.Random) -> StateMap[EventBase]:
        state: dict[StateKey, EventBase] = {}
        visibility = rng.choice(cls.VISIBILITIES)
        if visibility is not None:
            content = {"history_visibility": visibility}
            if rng.random() < 0.1:
                content = {}
            state[(EventTypes.RoomHistoryVisibility, "")] = cls._state_event(
                rng, EventTypes.RoomHistoryVisibility, "", content
            )
        membership = rng.choice(cls.MEMBERSHIPS)
        if membership is not None:
            state[(EventTypes.Member, ME)] = cls._state_event(
                rng, EventTypes.Member, ME, {"membership": membership}
            )
        if rng.random() < 0.5:
            state[(EventTypes.JoinRules, "")] = cls._state_event(
                rng,
                EventTypes.JoinRules,
                "",
                {"join_rule": rng.choice(["public", "invite"])},
            )
        return state

    @classmethod
    def _random_event(cls, rng: random.Random, now_ms: int) -> EventBase:
        kind = rng.choice(
            [
                "message",
                "own_member",
                "other_member",
                "history_vis",
                "call",
                "dummy",
                "aliases",
                "sticky",
            ]
        )
        fields: JsonDict = {
            "sender": rng.choice(USERS),
            "room_id": "!a:test",
            "origin_server_ts": now_ms - rng.choice([0, 1000, 100_000, 10_000_000]),
            "content": {},
        }
        unsigned: JsonDict = {}
        if kind == "message":
            fields["type"] = EventTypes.Message
            fields["content"] = {"body": "hi"}
        elif kind in ("own_member", "other_member"):
            fields["type"] = EventTypes.Member
            fields["state_key"] = ME if kind == "own_member" else "@alice:test"
            # Always with a membership: the auth rules require one, and the
            # Python raises on an event without.
            fields["content"] = {"membership": rng.choice(cls.MEMBERSHIPS) or "bogus"}
            prev = rng.choice(cls.MEMBERSHIPS)
            if rng.random() < 0.7:
                unsigned["prev_content"] = {"membership": prev} if prev else {}
        elif kind == "history_vis":
            fields["type"] = EventTypes.RoomHistoryVisibility
            fields["state_key"] = ""
            visibility = rng.choice(cls.VISIBILITIES)
            fields["content"] = {"history_visibility": visibility} if visibility else {}
            prev = rng.choice(cls.VISIBILITIES)
            if rng.random() < 0.7:
                unsigned["prev_content"] = {"history_visibility": prev} if prev else {}
        elif kind == "call":
            fields["type"] = EventTypes.CallInvite
            if rng.random() < 0.2:
                fields["state_key"] = ""
        elif kind == "dummy":
            fields["type"] = EventTypes.Dummy
        elif kind == "aliases":
            fields["type"] = EventTypes.Aliases
            fields["state_key"] = "test"
        elif kind == "sticky":
            fields["type"] = EventTypes.Message
            fields["msc4354_sticky"] = {
                "duration_ms": rng.choice([0, 60_000, 10_000_000])
            }
        fields["unsigned"] = unsigned
        internal_metadata: JsonDict = {}
        if kind == "sticky" or rng.random() < 0.3:
            internal_metadata["received_ts"] = now_ms - rng.choice(
                [0, 30_000, 7_200_000]
            )
        if kind in ("own_member", "other_member", "message") and rng.random() < 0.2:
            internal_metadata["outlier"] = True
        return make_test_event(
            fields,
            room_version=RoomVersions.V10,
            internal_metadata_dict=internal_metadata,
        )

    @parameterized.expand(SEEDS)
    def test_random_events_agree(self, seed: int) -> None:
        rng = random.Random(seed)
        now_ms = 1_700_000_000_000
        for _ in range(200):
            event = self._random_event(rng, now_ms)
            state = (
                None
                if event.internal_metadata.is_outlier()
                else self._random_state(rng)
            )
            msc4354_enabled = rng.random() < 0.5
            filter_send_to_client = rng.random() < 0.8
            is_peeking = rng.random() < 0.3
            always_include_ids = frozenset(
                [event.event_id] if rng.random() < 0.1 else []
            )
            sender_ignored = rng.random() < 0.2
            max_lifetime = rng.choice([None, 500, 1_000_000])
            sender_erased = rng.random() < 0.2
            context = (
                f"event={event} state={state} msc4354={msc4354_enabled} "
                f"send={filter_send_to_client} peeking={is_peeking} "
                f"ignored={sender_ignored} max_lifetime={max_lifetime} erased={sender_erased}"
            )

            expected = _check_event_for_client(
                user_id=ME,
                event=event,
                state_after_event=state,
                clock=_FakeClock(now_ms),  # type: ignore[arg-type]
                msc4354_enabled=msc4354_enabled,
                filter_send_to_client=filter_send_to_client,
                sender_ignored=sender_ignored,
                always_include_ids=always_include_ids,
                retention_policy=RetentionPolicy(max_lifetime=max_lifetime),
                is_peeking=is_peeking,
                sender_erased=sender_erased,
            )
            actual = check_event_for_client_for_test(
                ME,
                event,
                state,
                now_ms=now_ms,
                msc4354_enabled=msc4354_enabled,
                filter_send_to_client=filter_send_to_client,
                is_peeking=is_peeking,
                always_include_ids=always_include_ids,
                sender_ignored=sender_ignored,
                retention_max_lifetime=max_lifetime,
                sender_erased=sender_erased,
            )
            if expected is None:
                self.assertIsNone(actual, context)
                continue
            self.assertIsNotNone(actual, context)
            assert actual is not None
            actual_event, actual_membership = actual
            self.assertEqual(actual_event.event_id, expected.event.event_id, context)
            self.assertEqual(
                actual_event.internal_metadata.is_redacted(),
                expected.event.internal_metadata.is_redacted(),
                context,
            )
            self.assertEqual(
                actual_event.get_dict(), expected.event.get_dict(), context
            )
            self.assertEqual(actual_membership, expected.membership, context)
