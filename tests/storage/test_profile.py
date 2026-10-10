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

from http import HTTPStatus

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import ProfileFields
from synapse.api.errors import StoreError
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import PostgresEngine
from synapse.types import JsonValue, UserID
from synapse.util.clock import Clock

from tests import unittest


class ProfileStoreTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        self.u_frank = UserID.from_string("@frank:test")

    def test_displayname(self) -> None:
        self.get_success(self.store.create_profile(self.u_frank))

        self.get_success(
            self.store.set_profile_field(
                user_id=self.u_frank,
                field_name=ProfileFields.DISPLAYNAME,
                new_value="Frank",
            )
        )

        self.assertEqual(
            "Frank",
            (self.get_success(self.store.get_profile_displayname(self.u_frank))),
        )

        # test set to None
        self.get_success(
            self.store.set_profile_field(
                user_id=self.u_frank,
                field_name=ProfileFields.DISPLAYNAME,
                new_value=None,
            )
        )

        self.assertIsNone(
            self.get_success(self.store.get_profile_displayname(self.u_frank))
        )

    def test_avatar_url(self) -> None:
        self.get_success(self.store.create_profile(self.u_frank))

        self.get_success(
            self.store.set_profile_field(
                user_id=self.u_frank,
                field_name=ProfileFields.AVATAR_URL,
                new_value="http://my.site/here",
            )
        )

        self.assertEqual(
            "http://my.site/here",
            (self.get_success(self.store.get_profile_avatar_url(self.u_frank))),
        )

        # test set to None
        self.get_success(
            self.store.set_profile_field(
                user_id=self.u_frank,
                field_name=ProfileFields.AVATAR_URL,
                new_value=None,
            )
        )

        self.assertIsNone(
            self.get_success(self.store.get_profile_avatar_url(self.u_frank))
        )

    def test_get_profile_field_without_profile(self) -> None:
        """
        Getting a custom profile field for a user that has no row in the
        `profiles` table at all should raise a 404.

        Regression test (we previously would trigger an unhandled exception).
        Can happen for users whose profile was erased upon deactivation.
        """
        f = self.get_failure(
            self.store.get_profile_field(self.u_frank, "org.example.field"),
            StoreError,
        )
        self.assertEqual(f.value.code, HTTPStatus.NOT_FOUND)

    def test_set_profile_field_without_profile(self) -> None:
        """
        Setting a custom profile field for a user that has no row in the
        `profiles` table at all should create the row and store the field.

        Regression test (we previously would trigger an unhandled exception in
        the profile size check, and then store the field under a wrong key on
        SQLite). Can happen for users whose profile was erased upon
        deactivation.
        """
        self.get_success(
            self.store.set_profile_field(
                user_id=self.u_frank,
                field_name="org.example.field",
                new_value="test",
            )
        )

        self.assertEqual(
            "test",
            self.get_success(
                self.store.get_profile_field(self.u_frank, "org.example.field")
            ),
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

    def _set_up_profiles_for_field_filtering(self) -> tuple[UserID, UserID, UserID]:
        """Create three local profiles for the `get_profile_data_for_users` tests:

        - frank: displayname, avatar_url and custom fields with string, nested
          object and boolean values (so JSON types surviving the filter are
          checked, not just strings).
        - bob: displayname only.
        - carol: a profile row with nothing set.
        """
        u_bob = UserID.from_string("@bob:test")
        u_carol = UserID.from_string("@carol:test")
        for user_id in (self.u_frank, u_bob, u_carol):
            self.get_success(self.store.create_profile(user_id))

        frank_fields: list[tuple[str, JsonValue | dict[str, JsonValue]]] = [
            (ProfileFields.DISPLAYNAME, "Frank"),
            (ProfileFields.AVATAR_URL, "mxc://test/frank"),
            ("m.status", {"emoji": "💬", "text": "In a meeting"}),
            ("org.example.pronouns", "he/him"),
            ("org.example.verified", True),
        ]
        for field_name, value in frank_fields:
            self.get_success(
                self.store.set_profile_field(
                    user_id=self.u_frank, field_name=field_name, new_value=value
                )
            )
        self.get_success(
            self.store.set_profile_field(
                user_id=u_bob, field_name=ProfileFields.DISPLAYNAME, new_value="Bob"
            )
        )

        return self.u_frank, u_bob, u_carol

    def test_get_profile_data_for_users_all_fields(self) -> None:
        """Without `field_names`, every set field is returned, users with an
        empty profile map to `{}`, and users without a profile row are omitted.
        """
        u_frank, u_bob, u_carol = self._set_up_profiles_for_field_filtering()

        result = self.get_success(
            self.store.get_profile_data_for_users(
                [
                    u_frank.to_string(),
                    u_bob.to_string(),
                    u_carol.to_string(),
                    "@nobody:test",
                ]
            )
        )

        self.assertEqual(
            result,
            {
                "@frank:test": {
                    ProfileFields.DISPLAYNAME: "Frank",
                    ProfileFields.AVATAR_URL: "mxc://test/frank",
                    "m.status": {"emoji": "💬", "text": "In a meeting"},
                    "org.example.pronouns": "he/him",
                    "org.example.verified": True,
                },
                "@bob:test": {ProfileFields.DISPLAYNAME: "Bob"},
                "@carol:test": {},
            },
        )

    def test_get_profile_data_for_users_filters_fields(self) -> None:
        """`field_names` restricts the result to the named fields, covering the
        column-backed fields (`displayname`, `avatar_url`), custom fields, and
        names that aren't set for some or all users.
        """
        u_frank, u_bob, u_carol = self._set_up_profiles_for_field_filtering()
        user_ids = [u_frank.to_string(), u_bob.to_string(), u_carol.to_string()]

        # (field_names, expected result)
        cases: list[tuple[set[str], dict]] = [
            # Only a column-backed field.
            (
                {ProfileFields.DISPLAYNAME},
                {
                    "@frank:test": {ProfileFields.DISPLAYNAME: "Frank"},
                    "@bob:test": {ProfileFields.DISPLAYNAME: "Bob"},
                    "@carol:test": {},
                },
            ),
            # Only custom fields, including a nested object and a boolean.
            (
                {"m.status", "org.example.verified"},
                {
                    "@frank:test": {
                        "m.status": {"emoji": "💬", "text": "In a meeting"},
                        "org.example.verified": True,
                    },
                    "@bob:test": {},
                    "@carol:test": {},
                },
            ),
            # A mix of column-backed, custom and unknown fields.
            (
                {ProfileFields.AVATAR_URL, "org.example.pronouns", "org.example.unset"},
                {
                    "@frank:test": {
                        ProfileFields.AVATAR_URL: "mxc://test/frank",
                        "org.example.pronouns": "he/him",
                    },
                    "@bob:test": {},
                    "@carol:test": {},
                },
            ),
            # No fields at all: every user with a profile still appears, empty.
            (
                set(),
                {"@frank:test": {}, "@bob:test": {}, "@carol:test": {}},
            ),
        ]

        for field_names, expected in cases:
            with self.subTest(field_names=field_names):
                result = self.get_success(
                    self.store.get_profile_data_for_users(
                        user_ids, field_names=field_names
                    )
                )
                self.assertEqual(result, expected)
