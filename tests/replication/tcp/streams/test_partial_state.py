#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from twisted.internet.defer import ensureDeferred

from synapse.rest.client import room

from tests.replication._base import BaseMultiWorkerStreamTestCase


class PartialStateStreamsTestCase(BaseMultiWorkerStreamTestCase):
    servlets = [room.register_servlets]
    hijack_auth = True
    user_id = "@bob:test"

    def setUp(self) -> None:
        super().setUp()
        self.store = self.hs.get_datastores().main

    def test_un_partial_stated_room_unblocks_over_replication(self) -> None:
        """
        Tests that, when a room is un-partial-stated on another worker,
        pending calls to `await_full_state` get unblocked.
        """

        # Make a room.
        room_id = self.helper.create_room_as("@bob:test")
        # Mark the room as partial-stated.
        self.get_success(
            self.store.store_partial_state_room(room_id, {"serv1", "serv2"}, 0, "serv1")
        )

        worker = self.make_worker_hs("synapse.app.generic_worker")

        # On the worker, attempt to get the current hosts in the room
        d = ensureDeferred(
            worker.get_storage_controllers().state.get_current_hosts_in_room(room_id)
        )

        self.reactor.advance(0.1)

        # This should block
        self.assertFalse(
            d.called, "get_current_hosts_in_room/await_full_state did not block"
        )

        # On the master, clear the partial state flag.
        self.get_success(self.store.clear_partial_state_room(room_id))

        self.reactor.advance(0.1)

        # The worker should have unblocked
        self.assertTrue(
            d.called, "get_current_hosts_in_room/await_full_state did not unblock"
        )
