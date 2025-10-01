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
#
import logging

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

logger = logging.getLogger(__name__)


# The name of the extension. Currently unstable-prefixed.
EXT_NAME = "io.element.msc4360.threads"


class SlidingSyncThreadsExtensionTestCase(SlidingSyncBase):
    """
    Test the threads extension in the Sliding Sync API.
    """

    maxDiff = None

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        # TODO:
        # threads.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4360_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        super().prepare(reactor, clock, hs)

    def test_no_data_initial_sync(self) -> None:
        """
        Test enabling threads extension during initial sync with no data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        sync_body = {
            "lists": {},
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }

        # Sync
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        self.assertNotIn(EXT_NAME, response_body["extensions"])

    def test_no_data_incremental_sync(self) -> None:
        """
        Test enabling threads extension during incremental sync with no data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        initial_sync_body: JsonDict = {
            "lists": {},
        }

        # Initial sync
        response_body, sync_pos = self.do_sync(initial_sync_body, tok=user1_tok)

        # Incremental sync with extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert
        self.assertNotIn(
            EXT_NAME,
            response_body["extensions"],
            response_body,
        )
