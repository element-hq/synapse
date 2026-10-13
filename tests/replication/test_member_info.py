#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import MemberInfo
from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.server import make_request


class MemberInfoWorkerTestCase(BaseMultiWorkerStreamTestCase):
    """MSC4428: a `/sync` served by a worker sees an erasure recorded on the
    main process, with nothing in the room to anchor it."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4428_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")
        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)

    def test_erasure_reaches_sync_on_worker(self) -> None:
        worker_hs = self.make_worker_hs("synapse.app.generic_worker")
        site = self._hs_to_site[worker_hs]

        channel = make_request(
            self.reactor, site, "GET", "/sync", access_token=self.alice_tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertNotIn(
            MemberInfo.FIELD, channel.json_body["rooms"]["join"][self.room_id]
        )
        since = channel.json_body["next_batch"]

        # Erase Bob on the main process, without an event in the room.
        self.get_success(self.store.mark_user_erased(self.bob))
        self.get_success(self.hs.get_member_info_handler().on_user_erased(self.bob))
        self.replicate()

        channel = make_request(
            self.reactor,
            site,
            "GET",
            f"/sync?since={since}",
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        room = channel.json_body["rooms"]["join"][self.room_id]
        self.assertEqual(room["timeline"]["events"], [])
        self.assertEqual(room[MemberInfo.FIELD], {self.bob: {MemberInfo.USER_ID: None}})
