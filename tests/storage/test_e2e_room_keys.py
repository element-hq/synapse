#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

from synapse.server import HomeServer
from synapse.storage.databases.main.e2e_room_keys import RoomKey
from synapse.util.clock import Clock

from tests import unittest

# sample room_key data for use in the tests
room_key: RoomKey = {
    "first_message_index": 1,
    "forwarded_count": 1,
    "is_verified": False,
    "session_data": "SSBBTSBBIEZJU0gK",
}


class E2eRoomKeysHandlerTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver("server")
        self.store = hs.get_datastores().main
        return hs

    def test_room_keys_version_delete(self) -> None:
        # test that deleting a room key backup deletes the keys
        version1 = self.get_success(
            self.store.create_e2e_room_keys_version(
                "user_id", {"algorithm": "rot13", "auth_data": {}}
            )
        )

        self.get_success(
            self.store.add_e2e_room_keys(
                "user_id", version1, [("room", "session", room_key)]
            )
        )

        version2 = self.get_success(
            self.store.create_e2e_room_keys_version(
                "user_id", {"algorithm": "rot13", "auth_data": {}}
            )
        )

        self.get_success(
            self.store.add_e2e_room_keys(
                "user_id", version2, [("room", "session", room_key)]
            )
        )

        # make sure the keys were stored properly
        keys = self.get_success(self.store.get_e2e_room_keys("user_id", version1))
        self.assertEqual(len(keys["rooms"]), 1)

        keys = self.get_success(self.store.get_e2e_room_keys("user_id", version2))
        self.assertEqual(len(keys["rooms"]), 1)

        # delete version1
        self.get_success(self.store.delete_e2e_room_keys_version("user_id", version1))

        # make sure the key from version1 is gone, and the key from version2 is
        # still there
        keys = self.get_success(self.store.get_e2e_room_keys("user_id", version1))
        self.assertEqual(len(keys["rooms"]), 0)

        keys = self.get_success(self.store.get_e2e_room_keys("user_id", version2))
        self.assertEqual(len(keys["rooms"]), 1)
