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

"""`synapse.api.filtering` against `rust/src/sync/filter.rs`: random filters
and random events, and the two must agree."""

import random

from parameterized import parameterized

from synapse.api.constants import EduTypes, EventTypes
from synapse.api.filtering import FilterCollection
from synapse.api.presence import UserPresenceState
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.synapse_rust.sync import (
    FilterCollection as RustFilterCollection,
)
from synapse.types import JsonDict

from tests import unittest
from tests.sync.fixtures import (
    ME,
    ROOMS,
    SEEDS,
    TYPES,
    USERS,
    pick_list,
)
from tests.test_utils.event_builders import make_test_event
from tests.unittest import override_config


class FilterCollectionTestCase(unittest.HomeserverTestCase):
    """`synapse.api.filtering` against `rust/src/sync/filter.rs`."""

    def _rust(self, filter_json: JsonDict) -> RustFilterCollection:
        config = self.hs.config
        return RustFilterCollection(
            filter_json,
            msc3773_enabled=config.experimental.msc3773_enabled,
            msc3874_enabled=config.experimental.msc3874_enabled,
            include_profile_updates_in_sync=config.server.include_profile_updates_in_sync,
        )

    @staticmethod
    def _random_filter(rng: random.Random) -> JsonDict:
        """One `room_event_filter`, with each field present at random. The
        `related_by_*` fields are left out: they need the database."""
        f: JsonDict = {}
        if rng.random() < 0.5:
            f["types"] = pick_list(rng, TYPES + ["m.room.*", "m.*", "org.*"], "*")
        if rng.random() < 0.4:
            f["not_types"] = pick_list(rng, TYPES + ["m.room.*"], "*")
        if rng.random() < 0.4:
            f["senders"] = pick_list(rng, USERS)
        if rng.random() < 0.4:
            f["not_senders"] = pick_list(rng, USERS, "*")
        if rng.random() < 0.4:
            f["rooms"] = pick_list(rng, ROOMS)
        if rng.random() < 0.4:
            f["not_rooms"] = pick_list(rng, ROOMS, "*")
        if rng.random() < 0.3:
            f["contains_url"] = rng.choice([True, False])
        if rng.random() < 0.3:
            f["org.matrix.labels"] = pick_list(rng, ["#fun", "#work"])
        if rng.random() < 0.3:
            f["org.matrix.not_labels"] = pick_list(rng, ["#fun", "#work"])
        if rng.random() < 0.3:
            f["org.matrix.msc3874.rel_types"] = pick_list(
                rng, ["m.thread", "m.replace"]
            )
        if rng.random() < 0.3:
            f["org.matrix.msc3874.not_rel_types"] = pick_list(
                rng, ["m.thread", "m.replace"]
            )
        if rng.random() < 0.3:
            f["limit"] = rng.choice([0, 1, 5, 20])
        if rng.random() < 0.3:
            f["lazy_load_members"] = rng.choice([True, False])
        if rng.random() < 0.3:
            f["include_redundant_members"] = rng.choice([True, False])
        if rng.random() < 0.3:
            f["unread_thread_notifications"] = rng.choice([True, False])
        if rng.random() < 0.3:
            f["org.matrix.msc3773.unread_thread_notifications"] = rng.choice(
                [True, False]
            )
        return f

    @classmethod
    def _random_filter_json(cls, rng: random.Random) -> JsonDict:
        filter_json: JsonDict = {}
        if rng.random() < 0.9:
            room = cls._random_filter(rng)
            for section in ("timeline", "state", "ephemeral", "account_data"):
                if rng.random() < 0.7:
                    room[section] = cls._random_filter(rng)
            if rng.random() < 0.3:
                room["include_leave"] = rng.choice([True, False])
            filter_json["room"] = room
        if rng.random() < 0.5:
            filter_json["presence"] = cls._random_filter(rng)
        if rng.random() < 0.5:
            filter_json["account_data"] = cls._random_filter(rng)
        if rng.random() < 0.3:
            filter_json["event_fields"] = pick_list(rng, ["type", "content", "sender"])
        if rng.random() < 0.3:
            filter_json["event_format"] = rng.choice(["client", "federation"])
        if rng.random() < 0.3:
            filter_json["org.matrix.msc4429.profile_fields"] = {
                "ids": pick_list(rng, ["displayname", "avatar_url"])
            }
        return filter_json

    @staticmethod
    def _random_content(rng: random.Random) -> JsonDict:
        content: JsonDict = {"body": "hi"}
        if rng.random() < 0.3:
            content["url"] = rng.choice(["mxc://test/abc", 5])
        if rng.random() < 0.3:
            content["org.matrix.labels"] = pick_list(rng, ["#fun", "#work", "#other"])
        if rng.random() < 0.3:
            content["m.relates_to"] = rng.choice(
                [
                    {"rel_type": "m.thread", "event_id": "$root"},
                    {"rel_type": "m.replace", "event_id": "$root"},
                    {"rel_type": "m.thread"},
                    {"event_id": "$root"},
                    "not a relation",
                ]
            )
        return content

    @classmethod
    def _random_event(cls, rng: random.Random) -> EventBase:
        event_type = rng.choice(TYPES)
        fields: JsonDict = {
            "type": event_type,
            "sender": rng.choice(USERS),
            "room_id": rng.choice(ROOMS),
            "content": cls._random_content(rng),
        }
        if event_type in (EventTypes.Member, EventTypes.Topic) or rng.random() < 0.2:
            fields["state_key"] = rng.choice(["", ME, "@alice:test"])
        return make_test_event(fields, room_version=RoomVersions.V10)

    @classmethod
    def _random_json_event(cls, rng: random.Random) -> JsonDict:
        """An ephemeral event or account data entry."""
        event: JsonDict = {
            "type": rng.choice(TYPES + [EduTypes.PRESENCE]),
            "content": rng.choice(
                [
                    cls._random_content(rng),
                    {"user_id": rng.choice(USERS)},
                    "not a dict",
                    [],
                ]
            ),
        }
        if rng.random() < 0.6:
            event["sender"] = rng.choice(USERS + [""])
        if rng.random() < 0.6:
            event["room_id"] = rng.choice(ROOMS)
        return event

    def _check_agree(self, rng: random.Random, filter_json: JsonDict) -> None:
        python = FilterCollection(self.hs, filter_json)
        rust = self._rust(filter_json)
        context = f"filter={filter_json!r}"

        # The accessors.
        self.assertEqual(python.timeline_limit(), rust.timeline_limit(), context)
        self.assertEqual(python.presence_limit(), rust.presence_limit(), context)
        self.assertEqual(python.ephemeral_limit(), rust.ephemeral_limit(), context)
        self.assertEqual(python.lazy_load_members(), rust.lazy_load_members(), context)
        self.assertEqual(
            python.include_redundant_members(),
            rust.include_redundant_members(),
            context,
        )
        self.assertEqual(
            python.unread_thread_notifications(),
            rust.unread_thread_notifications(),
            context,
        )
        self.assertEqual(python.include_leave, rust.include_leave, context)
        self.assertEqual(list(python.event_fields), rust.event_fields, context)
        self.assertEqual(python.event_format, rust.event_format, context)
        self.assertEqual(python.profile_fields, rust.profile_fields, context)
        for name in (
            "blocks_all_rooms",
            "blocks_all_presence",
            "blocks_all_global_account_data",
            "blocks_all_room_ephemeral",
            "blocks_all_room_account_data",
            "blocks_all_room_timeline",
        ):
            self.assertEqual(
                getattr(python, name)(), getattr(rust, name)(), f"{name} {context}"
            )

        # Events through the timeline and state filters.
        events = [self._random_event(rng) for _ in range(30)]
        python_timeline = self.get_success(python.filter_room_timeline(events))
        rust_timeline = [e for e in events if rust.check_room_timeline(e)]
        self.assertEqual(
            [e.event_id for e in python_timeline],
            [e.event_id for e in rust_timeline],
            context,
        )
        python_state = self.get_success(python.filter_room_state(events))
        rust_state = [e for e in events if rust.check_room_state(e)]
        self.assertEqual(
            [e.event_id for e in python_state],
            [e.event_id for e in rust_state],
            context,
        )

        # JSON events through the ephemeral and account data filters.
        json_events = [self._random_json_event(rng) for _ in range(30)]
        for python_filter, rust_check in (
            (python.filter_room_ephemeral, rust.check_room_ephemeral),
            (python.filter_room_account_data, rust.check_room_account_data),
            (python.filter_global_account_data, rust.check_global_account_data),
        ):
            expected = self.get_success(python_filter(json_events))
            actual = [e for e in json_events if rust_check(e)]
            self.assertEqual(expected, actual, f"{python_filter.__name__} {context}")

        # Presence.
        presence = [
            UserPresenceState(
                user_id=user_id,
                state="online",
                last_active_ts=0,
                last_federation_update_ts=0,
                last_user_sync_ts=0,
                status_msg=None,
                currently_active=False,
            )
            for user_id in USERS
        ]
        expected_presence = self.get_success(python.filter_presence(presence))
        actual_presence = [p for p in presence if rust.check_presence(p.user_id)]
        self.assertEqual(expected_presence, actual_presence, context)

    @parameterized.expand(SEEDS)
    def test_random_filters_agree(self, seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(25):
            self._check_agree(rng, self._random_filter_json(rng))

    @parameterized.expand(SEEDS)
    @override_config(
        {
            "experimental_features": {"msc3773_enabled": True, "msc3874_enabled": True},
            "include_profile_updates_in_sync": True,
        }
    )
    def test_random_filters_agree_with_mscs_enabled(self, seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(25):
            self._check_agree(rng, self._random_filter_json(rng))

    def test_edge_cases(self) -> None:
        rng = random.Random(0)
        filter_json: JsonDict
        for filter_json in (
            {},
            {"room": {"timeline": {"types": []}}},
            {"room": {"not_rooms": ["*"]}},
            {"room": {"timeline": {"limit": 3}}},
            {"presence": {"types": ["m.*"]}},
            {"account_data": {"senders": []}},
            {"room": {"rooms": ["!a:test"], "state": {"not_senders": ["*"]}}},
            {"event_format": "federation", "event_fields": [r"content.foo\.bar"]},
        ):
            self._check_agree(rng, filter_json)
        # The relation checks are the one part the Rust leaves to the store.
        rust = self._rust({"room": {"timeline": {"related_by_senders": [ME]}}})
        self.assertTrue(rust.timeline_needs_relation_check())
        self.assertFalse(self._rust({}).timeline_needs_relation_check())
