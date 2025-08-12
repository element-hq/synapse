#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2021 The Matrix.org Foundation C.I.C.
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

from synapse.api.room_versions import RoomVersions
from synapse.server import HomeServer
from synapse.types import RoomAlias, RoomID, UserID
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class RoomStoreTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # We can't test RoomStore on its own without the DirectoryStore, for
        # management of the 'room_aliases' table
        self.store = hs.get_datastores().main

        self.room = RoomID.from_string("!abcde:test")
        self.alias = RoomAlias.from_string("#a-room-name:test")
        self.u_creator = UserID.from_string("@creator:test")

        self.get_success(
            self.store.store_room(
                self.room.to_string(),
                room_creator_user_id=self.u_creator.to_string(),
                is_public=True,
                room_version=RoomVersions.V1,
            )
        )

    def test_get_room(self) -> None:
        room = self.get_success(self.store.get_room(self.room.to_string()))
        assert room is not None
        self.assertTrue(room[0])

    def test_get_room_unknown_room(self) -> None:
        self.assertIsNone(self.get_success(self.store.get_room("!uknown:test")))

    def test_get_room_with_stats(self) -> None:
        res = self.get_success(self.store.get_room_with_stats(self.room.to_string()))
        assert res is not None
        self.assertEqual(res.room_id, self.room.to_string())
        self.assertEqual(res.creator, self.u_creator.to_string())
        self.assertTrue(res.public)

    def test_get_room_with_stats_unknown_room(self) -> None:
        self.assertIsNone(
            self.get_success(self.store.get_room_with_stats("!uknown:test"))
        )
