#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016-2021 The Matrix.org Foundation C.I.C.
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

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase


class EndToEndKeyStoreTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def test_key_without_device_name(self) -> None:
        now = 1470174257070
        json = {"key": "value"}

        self.get_success(self.store.store_device("user", "device", None))

        self.get_success(self.store.set_e2e_device_keys("user", "device", now, json))

        res = self.get_success(
            self.store.get_e2e_device_keys_for_cs_api((("user", "device"),))
        )
        self.assertIn("user", res)
        self.assertIn("device", res["user"])
        dev = res["user"]["device"]
        self.assertLessEqual(json.items(), dev.items())

    def test_reupload_key(self) -> None:
        now = 1470174257070
        json = {"key": "value"}

        self.get_success(self.store.store_device("user", "device", None))

        changed = self.get_success(
            self.store.set_e2e_device_keys("user", "device", now, json)
        )
        self.assertTrue(changed)

        # If we try to upload the same key then we should be told nothing
        # changed
        changed = self.get_success(
            self.store.set_e2e_device_keys("user", "device", now, json)
        )
        self.assertFalse(changed)

    def test_get_key_with_device_name(self) -> None:
        now = 1470174257070
        json = {"key": "value"}

        self.get_success(self.store.set_e2e_device_keys("user", "device", now, json))
        self.get_success(self.store.store_device("user", "device", "display_name"))

        res = self.get_success(
            self.store.get_e2e_device_keys_for_cs_api((("user", "device"),))
        )
        self.assertIn("user", res)
        self.assertIn("device", res["user"])
        dev = res["user"]["device"]
        self.assertLessEqual(
            {
                "key": "value",
                "unsigned": {"device_display_name": "display_name"},
            }.items(),
            dev.items(),
        )

    def test_multiple_devices(self) -> None:
        now = 1470174257070

        self.get_success(self.store.store_device("user1", "device1", None))
        self.get_success(self.store.store_device("user1", "device2", None))
        self.get_success(self.store.store_device("user2", "device1", None))
        self.get_success(self.store.store_device("user2", "device2", None))

        self.get_success(
            self.store.set_e2e_device_keys("user1", "device1", now, {"key": "json11"})
        )
        self.get_success(
            self.store.set_e2e_device_keys("user1", "device2", now, {"key": "json12"})
        )
        self.get_success(
            self.store.set_e2e_device_keys("user2", "device1", now, {"key": "json21"})
        )
        self.get_success(
            self.store.set_e2e_device_keys("user2", "device2", now, {"key": "json22"})
        )

        res = self.get_success(
            self.store.get_e2e_device_keys_for_cs_api(
                (("user1", "device1"), ("user2", "device2"))
            )
        )
        self.assertIn("user1", res)
        self.assertIn("device1", res["user1"])
        self.assertNotIn("device2", res["user1"])
        self.assertIn("user2", res)
        self.assertNotIn("device1", res["user2"])
        self.assertIn("device2", res["user2"])

    def test_bg_signatures_migration(self) -> None:
        updater = self.hs.get_datastores().main.db_pool.updates

        # drop the constraint so we can insert duplicate signatures
        def f(txn: LoggingTransaction) -> None:
            txn.execute("DROP INDEX e2e_cross_signing_signatures_idx3")

        self.get_success(self.store.db_pool.runInteraction("", f))

        # save multiple copies of the same key in the database
        for _i in range(2):
            self.get_success(
                self.store.db_pool.simple_insert(
                    "e2e_cross_signing_signatures",
                    {
                        "user_id": "@alice:example.org",
                        "key_id": "ed25519:abcdefg",
                        "target_user_id": "@alice:example.org",
                        "target_device_id": "hijklmnop",
                        "signature": "some+signature",
                    },
                )
            )

        for _i in range(2):
            self.get_success(
                self.store.db_pool.simple_insert(
                    "e2e_cross_signing_signatures",
                    {
                        "user_id": "@alice:example.org",
                        "key_id": "ed25519:hijklmnop",
                        "target_user_id": "@alice:example.org",
                        "target_device_id": "abcdefg",
                        "signature": "some+signature",
                    },
                )
            )

        # run the background task to remove duplicates
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={
                    "update_name": "e2e_cross_signing_signatures_remove_duplicates",
                    "progress_json": "{}",
                },
            )
        )

        self.get_success(
            updater.run_background_updates(False),
        )

        # re-add the unique index
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={
                    "update_name": "e2e_cross_signing_signatures_add_key_id_to_index",
                    "progress_json": "{}",
                },
            )
        )

        self.get_success(
            updater.run_background_updates(False),
        )

        # check that we only have one copy of each key
        expected_values = [
            (
                "@alice:example.org",
                "ed25519:abcdefg",
                "@alice:example.org",
                "hijklmnop",
                "some+signature",
            ),
            (
                "@alice:example.org",
                "ed25519:hijklmnop",
                "@alice:example.org",
                "abcdefg",
                "some+signature",
            ),
        ]

        res = self.get_success(
            self.store.db_pool.execute(
                "",
                "SELECT user_id, key_id, target_user_id, target_device_id, signature from e2e_cross_signing_signatures ORDER BY key_id",
            )
        )
        self.assertEqual(len(res), len(expected_values))
        self.assertEqual(res, expected_values)
