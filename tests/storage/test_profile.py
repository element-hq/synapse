#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2021 The Matrix.org Foundation C.I.C.
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
import itertools
from unittest.mock import patch

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import ProfileUpdateAction
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main.profile import PRUNE_PROFILE_UPDATES_AGE
from synapse.storage.engines import PostgresEngine
from synapse.types import UserID
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest


class ProfileStoreTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        self.u_frank = UserID.from_string("@frank:test")

    def test_displayname(self) -> None:
        self.get_success(self.store.create_profile(self.u_frank))

        self.get_success(self.store.set_profile_displayname(self.u_frank, "Frank"))

        self.assertEqual(
            "Frank",
            (self.get_success(self.store.get_profile_displayname(self.u_frank))),
        )

        # test set to None
        self.get_success(self.store.set_profile_displayname(self.u_frank, None))

        self.assertIsNone(
            self.get_success(self.store.get_profile_displayname(self.u_frank))
        )

    def test_avatar_url(self) -> None:
        self.get_success(self.store.create_profile(self.u_frank))

        self.get_success(
            self.store.set_profile_avatar_url(self.u_frank, "http://my.site/here")
        )

        self.assertEqual(
            "http://my.site/here",
            (self.get_success(self.store.get_profile_avatar_url(self.u_frank))),
        )

        # test set to None
        self.get_success(self.store.set_profile_avatar_url(self.u_frank, None))

        self.assertIsNone(
            self.get_success(self.store.get_profile_avatar_url(self.u_frank))
        )

    def test_profiles_bg_migration(self) -> None:
        """
        Test background job that copies entries from column user_id to full_user_id, adding
        the hostname in the process.
        """
        updater = self.hs.get_datastores().main.db_pool.updates

        # drop the constraint so we can insert nulls in full_user_id to populate the test
        if isinstance(self.store.database_engine, PostgresEngine):

            def f(txn: LoggingTransaction) -> None:
                txn.execute(
                    "ALTER TABLE profiles DROP CONSTRAINT full_user_id_not_null"
                )

            self.get_success(self.store.db_pool.runInteraction("", f))

        for i in range(70):
            self.get_success(
                self.store.db_pool.simple_insert(
                    "profiles",
                    {"user_id": f"hello{i:02}"},
                )
            )

        # re-add the constraint so that when it's validated it actually exists
        if isinstance(self.store.database_engine, PostgresEngine):

            def f(txn: LoggingTransaction) -> None:
                txn.execute(
                    "ALTER TABLE profiles ADD CONSTRAINT full_user_id_not_null CHECK (full_user_id IS NOT NULL) NOT VALID"
                )

            self.get_success(self.store.db_pool.runInteraction("", f))

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={
                    "update_name": "populate_full_user_id_profiles",
                    "progress_json": "{}",
                },
            )
        )

        self.get_success(
            updater.run_background_updates(False),
        )

        expected_values = []
        for i in range(70):
            expected_values.append((f"@hello{i:02}:{self.hs.hostname}",))

        res = self.get_success(
            self.store.db_pool.execute(
                "", "SELECT full_user_id from profiles ORDER BY full_user_id"
            )
        )
        self.assertEqual(len(res), len(expected_values))
        self.assertEqual(res, expected_values)

    @patch("synapse.storage.databases.main.profile.PRUNE_PROFILE_UPDATES_BATCH_SIZE", 5)
    def test_prune_profile_updates(self) -> None:
        """Test that old entries in the `profile_updates` and `profile_updates_per_user`
        tables are pruned properly."""

        # Create a generator for field names so we can easily create many unique
        # field names without having to keep track of the count ourselves.
        field_name_gen = (f"field{i}" for i in itertools.count())

        def get_profile_updates_status() -> tuple[int, str]:
            """Helper function to get the count of entries in the
            `profile_updates` table."""
            return self.get_success(
                self.store.db_pool.simple_select_one(
                    table="profile_updates",
                    keyvalues={},
                    retcols=("COUNT(*)", "MIN(field_name)"),
                )
            )

        def get_profile_updates_per_user_status() -> tuple[int]:
            """Helper function to get the count of entries in the
            `profile_updates_per_user` table."""
            return self.get_success(
                self.store.db_pool.simple_select_one(
                    table="profile_updates_per_user",
                    keyvalues={},
                    retcols=("COUNT(*)",),
                )
            )

        # First add some entries
        for _ in range(10):
            stream_id = self.get_success(
                self.store.add_profile_updates(
                    user_id=UserID.from_string("@user:test"),
                    updated_fields={next(field_name_gen)},
                    action=ProfileUpdateAction.UPDATE,
                )
            )
            self.get_success(
                self.store.track_profile_updates_per_user(
                    stream_id=stream_id,
                    user_ids={"@alice:test", "@bob:test"},
                )
            )

        # Advance the reactor a while, but not long enough to trigger pruning.
        self.reactor.advance(Duration(hours=1).as_secs())

        # The `profile_updates_per_user` table should now have 10 * 2 entries.
        per_user_count = get_profile_updates_per_user_status()
        self.assertEqual(per_user_count[0], 20)
        # The `profile_updates` table should have 10 entries.
        # and the minimum field name should be `field0`.
        updates_count, min_field_name = get_profile_updates_status()
        self.assertEqual(updates_count, 10)
        self.assertEqual(min_field_name, "field0")

        # Now we add some more entries
        for _ in range(10):
            stream_id = self.get_success(
                self.store.add_profile_updates(
                    user_id=UserID.from_string("@user:test"),
                    updated_fields={next(field_name_gen)},
                    action=ProfileUpdateAction.UPDATE,
                )
            )
            self.get_success(
                self.store.track_profile_updates_per_user(
                    stream_id=stream_id,
                    user_ids={"@alice:test", "@bob:test"},
                )
            )

        # Advance the reactor a while more, so that the first batch of entries is
        # now old enough to be pruned.
        self.reactor.advance(
            (PRUNE_PROFILE_UPDATES_AGE - Duration(minutes=30)).as_secs()
        )

        # Advance repeatedly a bit so that the pruning process can run to completion.
        for _ in range(10):
            self.reactor.advance(Duration(milliseconds=110).as_secs())

        # Check that the old entries have been pruned, and the new entries are still there.
        # The `profile_updates_per_user` table should now have 10 * 2 entries.
        per_user_count = get_profile_updates_per_user_status()
        self.assertEqual(per_user_count[0], 20)
        # The `profile_updates` table should have 10 entries.
        # and the minimum field name should be `field10`.
        updates_count, min_field_name = get_profile_updates_status()
        self.assertEqual(updates_count, 10)
        self.assertEqual(min_field_name, "field10")

        # We should always keep the most recent entries, even if they are old enough to be pruned.
        self.reactor.advance(
            (PRUNE_PROFILE_UPDATES_AGE + Duration(minutes=30)).as_secs()
        )

        # Advance repeatedly a bit so that the pruning process can run to completion.
        for _ in range(10):
            self.reactor.advance(Duration(milliseconds=110).as_secs())

        # The `profile_updates_per_user` table should now have 2 entries.
        per_user_count = get_profile_updates_per_user_status()
        self.assertEqual(per_user_count[0], 2)
        # The `profile_updates` table should have 1 entry.
        # and the minimum field name should be `field19`.
        updates_count, min_field_name = get_profile_updates_status()
        self.assertEqual(updates_count, 1)
        self.assertEqual(min_field_name, "field19")
