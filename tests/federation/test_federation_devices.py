#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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

import logging
from unittest.mock import AsyncMock, Mock

from twisted.internet.testing import MemoryReactor

from synapse.handlers.device import DeviceListUpdater
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock
from synapse.util.retryutils import NotRetryingDestination

from tests import unittest

logger = logging.getLogger(__name__)


class DeviceListResyncTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main

    def test_retry_device_list_resync(self) -> None:
        """Tests that device lists are marked as stale if they couldn't be synced, and
        that stale device lists are retried periodically.
        """
        remote_user_id = "@john:test_remote"
        remote_origin = "test_remote"

        # Track the number of attempts to resync the user's device list.
        self.resync_attempts = 0

        # When this function is called, increment the number of resync attempts (only if
        # we're querying devices for the right user ID), then raise a
        # NotRetryingDestination error to fail the resync gracefully.
        def query_user_devices(
            destination: str, user_id: str, timeout: int = 30000
        ) -> JsonDict:
            if user_id == remote_user_id:
                self.resync_attempts += 1

            raise NotRetryingDestination(0, 0, destination)

        # Register the mock on the federation client.
        federation_client = self.hs.get_federation_client()
        federation_client.query_user_devices = Mock(side_effect=query_user_devices)  # type: ignore[method-assign]

        # Register a mock on the store so that the incoming update doesn't fail because
        # we don't share a room with the user.
        self.store.get_rooms_for_user = AsyncMock(return_value=["!someroom:test"])

        # Manually inject a fake device list update. We need this update to include at
        # least one prev_id so that the user's device list will need to be retried.
        device_list_updater = self.hs.get_device_handler().device_list_updater
        assert isinstance(device_list_updater, DeviceListUpdater)
        self.get_success(
            device_list_updater.incoming_device_list_update(
                origin=remote_origin,
                edu_content={
                    "deleted": False,
                    "device_display_name": "Mobile",
                    "device_id": "QBUAZIFURK",
                    "prev_id": [5],
                    "stream_id": 6,
                    "user_id": remote_user_id,
                },
            )
        )

        # Check that there was one resync attempt.
        self.assertEqual(self.resync_attempts, 1)

        # Check that the resync attempt failed and caused the user's device list to be
        # marked as stale.
        need_resync = self.get_success(
            self.store.get_user_ids_requiring_device_list_resync()
        )
        self.assertIn(remote_user_id, need_resync)

        # Check that waiting for 30 seconds caused Synapse to retry resyncing the device
        # list.
        self.reactor.advance(30)
        self.assertEqual(self.resync_attempts, 2)

    def test_cross_signing_keys_retry(self) -> None:
        """Tests that resyncing a device list correctly processes cross-signing keys from
        the remote server.
        """
        remote_user_id = "@john:test_remote"
        remote_master_key = "85T7JXPFBAySB/jwby4S3lBPTqY3+Zg53nYuGmu1ggY"
        remote_self_signing_key = "QeIiFEjluPBtI7WQdG365QKZcFs9kqmHir6RBD0//nQ"

        # Register mock device list retrieval on the federation client.
        federation_client = self.hs.get_federation_client()
        federation_client.query_user_devices = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "user_id": remote_user_id,
                "stream_id": 1,
                "devices": [],
                "master_key": {
                    "user_id": remote_user_id,
                    "usage": ["master"],
                    "keys": {"ed25519:" + remote_master_key: remote_master_key},
                },
                "self_signing_key": {
                    "user_id": remote_user_id,
                    "usage": ["self_signing"],
                    "keys": {
                        "ed25519:" + remote_self_signing_key: remote_self_signing_key
                    },
                },
            }
        )

        # Resync the device list.
        device_handler = self.hs.get_device_handler()
        self.get_success(
            device_handler.device_list_updater.multi_user_device_resync(
                [remote_user_id]
            ),
        )

        # Retrieve the cross-signing keys for this user.
        keys = self.get_success(
            self.store.get_e2e_cross_signing_keys_bulk(user_ids=[remote_user_id]),
        )
        self.assertIn(remote_user_id, keys)
        key = keys[remote_user_id]
        assert key is not None

        # Check that the master key is the one returned by the mock.
        master_key = key["master"]
        self.assertEqual(len(master_key["keys"]), 1)
        self.assertTrue("ed25519:" + remote_master_key in master_key["keys"].keys())
        self.assertTrue(remote_master_key in master_key["keys"].values())

        # Check that the self-signing key is the one returned by the mock.
        self_signing_key = key["self_signing"]
        self.assertEqual(len(self_signing_key["keys"]), 1)
        self.assertTrue(
            "ed25519:" + remote_self_signing_key in self_signing_key["keys"].keys(),
        )
        self.assertTrue(remote_self_signing_key in self_signing_key["keys"].values())
