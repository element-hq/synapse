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

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.databases.main.member_info import MEMBER_INFO_CHANGES_RETENTION
from synapse.util.clock import Clock

from tests import unittest


class MemberInfoChangesStoreTestCase(unittest.HomeserverTestCase):
    """MSC4428: the change table behind the member info changes stream."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def test_changes_in_rooms(self) -> None:
        start = self.store.get_member_info_changes_stream_token()

        # Nothing recorded: nothing to report, for any room.
        self.assertEqual(
            self.get_success(
                self.store.get_member_info_changes_in_rooms({"!a:test"}, start, start)
            ),
            {},
        )

        middle = self.get_success(
            self.store.add_member_info_changes({"!a:test": ["@u1:test"]})
        )
        end = self.get_success(
            self.store.add_member_info_changes(
                {"!a:test": ["@u2:test"], "!b:test": ["@u1:test"]}
            )
        )

        # Rooms we did not ask about are left out; the range is (from, to].
        self.assertEqual(
            self.get_success(
                self.store.get_member_info_changes_in_rooms({"!a:test"}, start, end)
            ),
            {"!a:test": {"@u1:test", "@u2:test"}},
        )
        self.assertEqual(
            self.get_success(
                self.store.get_member_info_changes_in_rooms(
                    {"!a:test", "!b:test"}, middle, end
                )
            ),
            {"!a:test": {"@u2:test"}, "!b:test": {"@u1:test"}},
        )
        self.assertEqual(
            self.get_success(
                self.store.get_member_info_changes_in_rooms({"!b:test"}, end, end)
            ),
            {},
        )

    def test_prune(self) -> None:
        start = self.store.get_member_info_changes_stream_token()
        self.get_success(self.store.add_member_info_changes({"!a:test": ["@u1:test"]}))
        self.reactor.advance(MEMBER_INFO_CHANGES_RETENTION.as_secs() + 1)
        after_old = self.store.get_member_info_changes_stream_token()
        end = self.get_success(
            self.store.add_member_info_changes({"!a:test": ["@u2:test"]})
        )

        self.get_success(
            self.store.prune_member_info_changes_before(
                self.clock.time_msec() - MEMBER_INFO_CHANGES_RETENTION.as_millis()
            )
        )

        # A range starting before the prune point cannot be answered.
        self.assertIsNone(
            self.get_success(
                self.store.get_member_info_changes_in_rooms({"!a:test"}, start, end)
            )
        )
        # One starting at it can, and only sees the surviving row.
        self.assertEqual(
            self.get_success(
                self.store.get_member_info_changes_in_rooms({"!a:test"}, after_old, end)
            ),
            {"!a:test": {"@u2:test"}},
        )
