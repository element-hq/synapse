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

from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.util import Clock

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
