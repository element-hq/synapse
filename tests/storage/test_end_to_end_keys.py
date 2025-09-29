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

from synapse.handlers.e2e_keys import DeviceKeys
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class EndToEndKeyStoreTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.now_ms = 1470174257070
        self.test_user_id = "@alice:test"
        self.test_device_id = "TEST_DEVICE"
        self.test_device_keys = self._create_test_device_keys(self.test_user_id, self.test_device_id)

    def _create_test_device_keys(self, user_id: str, device_id: str, public_key: str = "test_public_key") -> DeviceKeys:
        """Create and return a test `DeviceKeys` object."""
        return DeviceKeys(
            algorithms=["ed25519"],
            device_id=device_id,
            keys={
                f"ed25519:{device_id}": public_key,
            },
            signatures={},
            user_id=UserID.from_string(user_id),
        )

    def test_key_without_device_name(self) -> None:
        self.get_success(self.store.store_device(self.test_user_id, self.test_device_id, None))

        self.get_success(self.store.set_e2e_device_keys(self.test_user_id, self.test_device_id, self.now_ms, self.test_device_keys))

        res = self.get_success(
            self.store.get_e2e_device_keys_for_cs_api(((self.test_user_id, self.test_device_id),))
        )
        self.assertIn(self.test_user_id, res)
        self.assertIn(self.test_device_id, res[self.test_user_id])
        device_keys = res[self.test_user_id][self.test_device_id]

        print(device_keys)

    def test_reupload_key(self) -> None:
        self.get_success(self.store.store_device("user", "device", None))

        changed = self.get_success(
            self.store.set_e2e_device_keys("user", "device", self.now_ms, self.test_device_keys)
        )
        self.assertTrue(changed)

        # If we try to upload the same key then we should be told nothing
        # changed
        changed = self.get_success(
            self.store.set_e2e_device_keys("user", "device", self.now_ms, self.test_device_keys)
        )
        self.assertFalse(changed)

    def test_get_key_with_device_name(self) -> None:
        self.get_success(self.store.set_e2e_device_keys(self.test_user_id, self.test_device_id, self.now_ms, self.test_device_keys))
        self.get_success(self.store.store_device(self.test_user_id, self.test_device_id, "display_name"))

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
        user_one = "@user1:test"
        user_two = "@user2:test"
        device_id_one = "DEVICE_ID_1"
        device_id_two = "DEVICE_ID_2"

        self.get_success(self.store.store_device(user_one, device_id_one, None))
        self.get_success(self.store.store_device(user_one, device_id_two, None))
        self.get_success(self.store.store_device(user_two, device_id_one, None))
        self.get_success(self.store.store_device(user_two, device_id_two, None))

        self.get_success(
            self.store.set_e2e_device_keys(user_one, device_id_one, self.now_ms, self._create_test_device_keys(user_one, device_id_one, "json11"))
        )
        self.get_success(
            self.store.set_e2e_device_keys(user_one, device_id_two, self.now_ms, self._create_test_device_keys(user_one, device_id_two, "json12"))
        )
        self.get_success(
            self.store.set_e2e_device_keys(user_two, device_id_one, self.now_ms, self._create_test_device_keys(user_two, device_id_one, "json21"))
        )
        self.get_success(
            self.store.set_e2e_device_keys(user_two, device_id_two, self.now_ms, self._create_test_device_keys(user_two, device_id_two, "json22"))
        )

        res = self.get_success(
            self.store.get_e2e_device_keys_for_cs_api(
                ((user_one, device_id_one), (user_two, device_id_two))
            )
        )
        self.assertIn(user_one, res)
        self.assertIn(device_id_one, res[user_one])
        self.assertNotIn(device_id_two, res[user_one])
        self.assertIn(user_two, res)
        self.assertNotIn(device_id_one, res[user_two])
        self.assertIn(device_id_two, res[user_two])
