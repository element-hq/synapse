#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util.clock import Clock

from tests.unittest import skip_unless
from tests.utils import HAS_AUTHLIB

from ._base import BaseTestCase


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasUpsertDeviceResource(BaseTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Create a user for testing
        self.alice_user_id = UserID("alice", "test")
        self.get_success(
            homeserver.get_registration_handler().register_user(
                localpart=self.alice_user_id.localpart,
            )
        )

    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/upsert_device",
            shorthand=False,
            access_token="other_token",
            content={
                "localpart": "alice",
                "device_id": "DEVICE1",
            },
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_upsert_device(self) -> None:
        store = self.hs.get_datastores().main

        channel = self.make_request(
            "POST",
            "/_synapse/mas/upsert_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "device_id": "DEVICE1",
            },
        )

        # This created a new device, hence the 201 status code
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify the device exists
        device = self.get_success(store.get_device(str(self.alice_user_id), "DEVICE1"))
        assert device is not None
        self.assertEqual(device["device_id"], "DEVICE1")
        self.assertIsNone(device["display_name"])

    def test_update_existing_device(self) -> None:
        store = self.hs.get_datastores().main
        device_handler = self.hs.get_device_handler()

        # Create an initial device
        self.get_success(
            device_handler.upsert_device(
                user_id=str(self.alice_user_id),
                device_id="DEVICE1",
                display_name="Old Name",
            )
        )

        channel = self.make_request(
            "POST",
            "/_synapse/mas/upsert_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "device_id": "DEVICE1",
                "display_name": "New Name",
            },
        )

        # This updated an existing device, hence the 200 status code
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify the device was updated
        device = self.get_success(store.get_device(str(self.alice_user_id), "DEVICE1"))
        assert device is not None
        self.assertEqual(device["display_name"], "New Name")

    def test_upsert_device_with_display_name(self) -> None:
        store = self.hs.get_datastores().main

        channel = self.make_request(
            "POST",
            "/_synapse/mas/upsert_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "device_id": "DEVICE1",
                "display_name": "Alice's Phone",
            },
        )

        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify the device exists with correct display name
        device = self.get_success(store.get_device(str(self.alice_user_id), "DEVICE1"))
        assert device is not None
        self.assertEqual(device["display_name"], "Alice's Phone")

    def test_upsert_device_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/upsert_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "device_id": "DEVICE1",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_upsert_device_missing_device_id(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/upsert_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_upsert_device_nonexistent_user(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/upsert_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "nonexistent",
                "device_id": "DEVICE1",
            },
        )

        # We get a 404 here as the user doesn't exist
        self.assertEqual(channel.code, 404, channel.json_body)


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasDeleteDeviceResource(BaseTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Create a user and device for testing
        self.alice_user_id = UserID("alice", "test")
        self.get_success(
            homeserver.get_registration_handler().register_user(
                localpart=self.alice_user_id.localpart,
            )
        )

        # Create a device
        device_handler = homeserver.get_device_handler()
        self.get_success(
            device_handler.upsert_device(
                user_id=str(self.alice_user_id),
                device_id="DEVICE1",
                display_name="Test Device",
            )
        )

    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_device",
            shorthand=False,
            access_token="other_token",
            content={
                "localpart": "alice",
                "device_id": "DEVICE1",
            },
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_delete_device(self) -> None:
        store = self.hs.get_datastores().main

        # Verify device exists before deletion
        device = self.get_success(store.get_device(str(self.alice_user_id), "DEVICE1"))
        assert device is not None

        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "device_id": "DEVICE1",
            },
        )

        self.assertEqual(channel.code, 204)

        # Verify the device no longer exists
        device = self.get_success(store.get_device(str(self.alice_user_id), "DEVICE1"))
        self.assertIsNone(device)

    def test_delete_nonexistent_device(self) -> None:
        # Deleting a non-existent device should be idempotent
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "device_id": "NONEXISTENT",
            },
        )

        self.assertEqual(channel.code, 204)

    def test_delete_device_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "device_id": "DEVICE1",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_delete_device_missing_device_id(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_delete_device_nonexistent_user(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_device",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "nonexistent",
                "device_id": "DEVICE1",
            },
        )

        # Should fail on a non-existent user
        self.assertEqual(channel.code, 404, channel.json_body)


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasUpdateDeviceDisplayNameResource(BaseTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Create a user and device for testing
        self.alice_user_id = UserID("alice", "test")
        self.get_success(
            homeserver.get_registration_handler().register_user(
                localpart=self.alice_user_id.localpart,
            )
        )

        # Create a device
        device_handler = homeserver.get_device_handler()
        self.get_success(
            device_handler.upsert_device(
                user_id=str(self.alice_user_id),
                device_id="DEVICE1",
                display_name="Old Name",
            )
        )

    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/update_device_display_name",
            shorthand=False,
            access_token="other_token",
            content={
                "localpart": "alice",
                "device_id": "DEVICE1",
                "display_name": "New Name",
            },
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_update_device_display_name(self) -> None:
        store = self.hs.get_datastores().main

        # Verify initial display name
        device = self.get_success(store.get_device(str(self.alice_user_id), "DEVICE1"))
        assert device is not None
        self.assertEqual(device["display_name"], "Old Name")

        channel = self.make_request(
            "POST",
            "/_synapse/mas/update_device_display_name",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "device_id": "DEVICE1",
                "display_name": "Updated Name",
            },
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify the display name was updated
        device = self.get_success(store.get_device(str(self.alice_user_id), "DEVICE1"))
        assert device is not None
        self.assertEqual(device["display_name"], "Updated Name")

    def test_update_nonexistent_device(self) -> None:
        # Updating a non-existent device should fail
        channel = self.make_request(
            "POST",
            "/_synapse/mas/update_device_display_name",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "device_id": "NONEXISTENT",
                "display_name": "New Name",
            },
        )

        self.assertEqual(channel.code, 404, channel.json_body)

    def test_update_device_display_name_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/update_device_display_name",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "device_id": "DEVICE1",
                "display_name": "New Name",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_update_device_display_name_missing_device_id(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/update_device_display_name",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "display_name": "New Name",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_update_device_display_name_missing_display_name(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/update_device_display_name",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "device_id": "DEVICE1",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_update_device_display_name_nonexistent_user(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/update_device_display_name",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "nonexistent",
                "device_id": "DEVICE1",
                "display_name": "New Name",
            },
        )

        self.assertEqual(channel.code, 404, channel.json_body)


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasSyncDevicesResource(BaseTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Create a user for testing
        self.alice_user_id = UserID("alice", "test")
        self.get_success(
            homeserver.get_registration_handler().register_user(
                localpart=self.alice_user_id.localpart,
            )
        )

        # Create some initial devices
        device_handler = homeserver.get_device_handler()
        for device_id in ["DEVICE1", "DEVICE2", "DEVICE3"]:
            self.get_success(
                device_handler.upsert_device(
                    user_id=str(self.alice_user_id),
                    device_id=device_id,
                    display_name=f"Device {device_id}",
                )
            )

    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token="other_token",
            content={
                "localpart": "alice",
                "devices": ["DEVICE1", "DEVICE2"],
            },
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_sync_devices_no_changes(self) -> None:
        # Sync with the same devices that already exist
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "devices": ["DEVICE1", "DEVICE2", "DEVICE3"],
            },
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify all devices still exist
        store = self.hs.get_datastores().main
        devices = self.get_success(store.get_devices_by_user(str(self.alice_user_id)))
        self.assertEqual(set(devices.keys()), {"DEVICE1", "DEVICE2", "DEVICE3"})

    def test_sync_devices_add_only(self) -> None:
        # Sync with additional devices
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "devices": ["DEVICE1", "DEVICE2", "DEVICE3", "DEVICE4", "DEVICE5"],
            },
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify new devices were added
        store = self.hs.get_datastores().main
        devices = self.get_success(store.get_devices_by_user(str(self.alice_user_id)))
        self.assertEqual(
            set(devices.keys()), {"DEVICE1", "DEVICE2", "DEVICE3", "DEVICE4", "DEVICE5"}
        )

    def test_sync_devices_delete_only(self) -> None:
        # Sync with fewer devices
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "devices": ["DEVICE1"],
            },
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify devices were deleted
        store = self.hs.get_datastores().main
        devices = self.get_success(store.get_devices_by_user(str(self.alice_user_id)))
        self.assertEqual(set(devices.keys()), {"DEVICE1"})

    def test_sync_devices_add_and_delete(self) -> None:
        # Sync with a mix of additions and deletions
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "devices": ["DEVICE1", "DEVICE4", "DEVICE5"],
            },
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify the correct devices exist
        store = self.hs.get_datastores().main
        devices = self.get_success(store.get_devices_by_user(str(self.alice_user_id)))
        self.assertEqual(set(devices.keys()), {"DEVICE1", "DEVICE4", "DEVICE5"})

    def test_sync_devices_empty_list(self) -> None:
        # Sync with empty device list (delete all devices)
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "devices": [],
            },
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify all devices were deleted
        store = self.hs.get_datastores().main
        devices = self.get_success(store.get_devices_by_user(str(self.alice_user_id)))
        self.assertEqual(devices, {})

    def test_sync_devices_for_new_user(self) -> None:
        # Test syncing devices for a user that doesn't have any devices yet
        bob_user_id = UserID("bob", "test")
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=bob_user_id.localpart,
            )
        )

        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "bob",
                "devices": ["DEVICE1", "DEVICE2"],
            },
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify devices were created
        store = self.hs.get_datastores().main
        devices = self.get_success(store.get_devices_by_user(str(bob_user_id)))
        self.assertEqual(set(devices.keys()), {"DEVICE1", "DEVICE2"})

    def test_sync_devices_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "devices": ["DEVICE1", "DEVICE2"],
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_sync_devices_missing_devices(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_sync_devices_invalid_devices_type(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "devices": "not_a_list",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_sync_devices_nonexistent_user(self) -> None:
        # Test syncing devices for a user that doesn't exist
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "nonexistent",
                "devices": ["DEVICE1", "DEVICE2"],
            },
        )

        self.assertEqual(channel.code, 404, channel.json_body)

    def test_sync_devices_duplicate_device_ids(self) -> None:
        # Test syncing with duplicate device IDs (sets should handle this)
        channel = self.make_request(
            "POST",
            "/_synapse/mas/sync_devices",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "devices": ["DEVICE1", "DEVICE1", "DEVICE2"],
            },
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Verify the correct devices exist (duplicates should be handled)
        store = self.hs.get_datastores().main
        devices = self.get_success(store.get_devices_by_user(str(self.alice_user_id)))
        self.assertEqual(sorted(devices.keys()), ["DEVICE1", "DEVICE2"])
