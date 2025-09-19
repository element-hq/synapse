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

from unittest.mock import patch

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import devices
from synapse.server import HomeServer
from synapse.storage.databases.main.deviceinbox import (
    DEVICE_FEDERATION_INBOX_CLEANUP_DELAY_MS,
)
from synapse.util.clock import Clock

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


class DeviceInboxFederationInboxCleanupTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        devices.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.db_pool = self.store.db_pool

        # Advance time to ensure we are past the cleanup delay
        self.reactor.advance(DEVICE_FEDERATION_INBOX_CLEANUP_DELAY_MS * 2 / 1000)

    def test_delete_old_federation_inbox_rows_skips_if_no_index(self) -> None:
        """Test that we don't delete rows if the index hasn't been created yet."""

        # Insert some test data into device_federation_inbox
        for i in range(5):
            self.get_success(
                self.db_pool.simple_insert(
                    "device_federation_inbox",
                    {
                        "origin": "example.com",
                        "message_id": f"msg_{i}",
                        "received_ts": 0,
                    },
                )
            )

        # Mock to report the update as not completed
        with patch(
            "synapse.storage.background_updates.BackgroundUpdater.has_completed_background_update"
        ) as mock:
            mock.return_value = False

            self.get_success(self.store._delete_old_federation_inbox_rows())

        # Check that no rows were deleted
        rows = self.get_success(
            self.db_pool.simple_select_list(
                "device_federation_inbox",
                keyvalues={},
                retcols=["origin", "message_id", "received_ts"],
            )
        )
        self.assertEqual(
            len(rows), 5, "Expected no rows to be deleted when index is missing"
        )

    def test_delete_old_federation_inbox_rows(self) -> None:
        """Test that old rows are deleted from device_federation_inbox."""

        # Insert old messages
        for i in range(5):
            self.get_success(
                self.db_pool.simple_insert(
                    "device_federation_inbox",
                    {
                        "origin": "old.example.com",
                        "message_id": f"old_msg_{i}",
                        "received_ts": self.clock.time_msec(),
                    },
                )
            )

        self.reactor.advance(2 * DEVICE_FEDERATION_INBOX_CLEANUP_DELAY_MS / 1000)

        # Insert new messages
        for i in range(5):
            self.get_success(
                self.db_pool.simple_insert(
                    "device_federation_inbox",
                    {
                        "origin": "new.example.com",
                        "message_id": f"new_msg_{i}",
                        "received_ts": self.clock.time_msec(),
                    },
                )
            )

        # Run the cleanup
        self.get_success(self.store._delete_old_federation_inbox_rows())

        # Check that only old messages were deleted
        rows = self.get_success(
            self.db_pool.simple_select_onecol(
                "device_federation_inbox",
                keyvalues={},
                retcol="origin",
            )
        )

        self.assertEqual(len(rows), 5, "Expected only 5 new messages to remain")
        for origin in rows:
            self.assertEqual(origin, "new.example.com")

    def test_delete_old_federation_inbox_rows_batch_limit(self) -> None:
        """Test that the deletion happens in batches."""

        # Insert 10 old messages (more than the 5 batch limit)
        for i in range(10):
            self.get_success(
                self.db_pool.simple_insert(
                    "device_federation_inbox",
                    {
                        "origin": "old.example.com",
                        "message_id": f"old_msg_{i}",
                        "received_ts": self.clock.time_msec(),
                    },
                )
            )

        # Advance time to ensure we are past the cleanup delay
        self.reactor.advance(2 * DEVICE_FEDERATION_INBOX_CLEANUP_DELAY_MS / 1000)

        # Run the cleanup - it should delete in batches and sleep between them
        deferred = defer.ensureDeferred(
            self.store._delete_old_federation_inbox_rows(batch_size=5)
        )

        # Check that the deferred doesn't resolve immediately
        self.assertFalse(deferred.called)

        # Advance the reactor to allow the cleanup to continue and complete
        self.reactor.advance(2)
        self.get_success(deferred)

        # Check that all messages were deleted after multiple batches
        rows = self.get_success(
            self.db_pool.simple_select_list(
                "device_federation_inbox",
                keyvalues={},
                retcols=["origin", "message_id"],
            )
        )

        self.assertEqual(
            len(rows),
            0,
            "Expected all messages to be deleted after multiple batches",
        )
