#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from synapse.rest import admin
from synapse.rest.client import devices
from synapse.server import HomeServer
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class DeviceInboxBackgroundUpdateStoreTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        devices.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.user_id = self.register_user("foo", "pass")

    def test_background_remove_deleted_devices_from_device_inbox(self) -> None:
        """Test that the background task to delete old device_inboxes works properly."""

        # create a valid device
        self.get_success(
            self.store.store_device(self.user_id, "cur_device", "display_name")
        )

        # Add device_inbox to devices
        self.get_success(
            self.store.db_pool.simple_insert(
                "device_inbox",
                {
                    "user_id": self.user_id,
                    "device_id": "cur_device",
                    "stream_id": 1,
                    "message_json": "{}",
                },
            )
        )
        self.get_success(
            self.store.db_pool.simple_insert(
                "device_inbox",
                {
                    "user_id": self.user_id,
                    "device_id": "old_device",
                    "stream_id": 2,
                    "message_json": "{}",
                },
            )
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": "remove_dead_devices_from_device_inbox",
                    "progress_json": "{}",
                },
            )
        )

        # ... and tell the DataStore that it hasn't finished all updates yet
        self.store.db_pool.updates._all_done = False

        self.wait_for_background_updates()

        # Make sure the background task deleted old device_inbox
        res = self.get_success(
            self.store.db_pool.simple_select_onecol(
                table="device_inbox",
                keyvalues={},
                retcol="device_id",
                desc="get_device_id_from_device_inbox",
            )
        )
        self.assertEqual(1, len(res))
        self.assertEqual(res[0], "cur_device")

    def test_background_remove_hidden_devices_from_device_inbox(self) -> None:
        """Test that the background task to delete hidden devices
        from device_inboxes works properly."""

        # create a valid device
        self.get_success(
            self.store.store_device(self.user_id, "cur_device", "display_name")
        )

        # create a hidden device
        self.get_success(
            self.store.db_pool.simple_insert(
                "devices",
                values={
                    "user_id": self.user_id,
                    "device_id": "hidden_device",
                    "display_name": "hidden_display_name",
                    "hidden": True,
                },
            )
        )

        # Add device_inbox to devices
        self.get_success(
            self.store.db_pool.simple_insert(
                "device_inbox",
                {
                    "user_id": self.user_id,
                    "device_id": "cur_device",
                    "stream_id": 1,
                    "message_json": "{}",
                },
            )
        )
        self.get_success(
            self.store.db_pool.simple_insert(
                "device_inbox",
                {
                    "user_id": self.user_id,
                    "device_id": "hidden_device",
                    "stream_id": 2,
                    "message_json": "{}",
                },
            )
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": "remove_dead_devices_from_device_inbox",
                    "progress_json": "{}",
                },
            )
        )

        # ... and tell the DataStore that it hasn't finished all updates yet
        self.store.db_pool.updates._all_done = False

        self.wait_for_background_updates()

        # Make sure the background task deleted hidden devices from device_inbox
        res = self.get_success(
            self.store.db_pool.simple_select_onecol(
                table="device_inbox",
                keyvalues={},
                retcol="device_id",
                desc="get_device_id_from_device_inbox",
            )
        )
        self.assertEqual(1, len(res))
        self.assertEqual(res[0], "cur_device")
