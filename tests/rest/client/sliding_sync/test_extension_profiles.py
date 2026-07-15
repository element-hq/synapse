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
import logging

from parameterized import parameterized, parameterized_class

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import login, profile, room, sync
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.unittest import override_config

logger = logging.getLogger(__name__)


# FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
# foreground update for
# `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
# https://github.com/element-hq/synapse/issues/17623)
@parameterized_class(
    ("use_new_tables",),
    [
        (True,),
        (False,),
    ],
    class_name_func=lambda cls,
    num,
    params_dict: f"{cls.__name__}_{'new' if params_dict['use_new_tables'] else 'fallback'}",
)
class SlidingSyncProfilesTestCase(SlidingSyncBase):
    """Tests for the profile updates sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        profile.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        super().prepare(reactor, clock, hs)

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    def test_no_data_when_not_enabled(self, is_initial: bool) -> None:
        """
        Test that no profile extension response is returned
        if the feature is not enabled.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                },
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        self.assertIsNone(response_body["extensions"].get("org.matrix.msc4262.profiles"))

        if not is_initial:
            # Make an incremental Sliding Sync request
            response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

            self.assertIsNone(response_body["extensions"].get("org.matrix.msc4262.profiles"))

    @override_config({"include_profile_updates_in_sync": True})
    def test_no_data_initial_sync(self) -> None:
        """
        Test that enabling the profiles extension works during an initial sync,
        even if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                },
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIsNone(response_body["extensions"].get("org.matrix.msc4262.profiles"))

    @override_config({"include_profile_updates_in_sync": True})
    def test_no_data_incremental_sync(self) -> None:
        """
        Test that enabling profiles extension works during an incremental sync, even
        if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the profiles extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertIsNone(response_body["extensions"].get("org.matrix.msc4262.profiles"))
