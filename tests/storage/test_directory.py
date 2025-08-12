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

from synapse.server import HomeServer
from synapse.types import RoomAlias, RoomID
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class DirectoryStoreTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        self.room = RoomID.from_string("!abcde:test")
        self.alias = RoomAlias.from_string("#my-room:test")

    def test_room_to_alias(self) -> None:
        self.get_success(
            self.store.create_room_alias_association(
                room_alias=self.alias, room_id=self.room.to_string(), servers=["test"]
            )
        )

        self.assertEqual(
            ["#my-room:test"],
            (self.get_success(self.store.get_aliases_for_room(self.room.to_string()))),
        )

    def test_alias_to_room(self) -> None:
        self.get_success(
            self.store.create_room_alias_association(
                room_alias=self.alias, room_id=self.room.to_string(), servers=["test"]
            )
        )

        self.assertObjectHasAttributes(
            {"room_id": self.room.to_string(), "servers": ["test"]},
            (self.get_success(self.store.get_association_from_room_alias(self.alias))),
        )

    def test_delete_alias(self) -> None:
        self.get_success(
            self.store.create_room_alias_association(
                room_alias=self.alias, room_id=self.room.to_string(), servers=["test"]
            )
        )

        room_id = self.get_success(self.store.delete_room_alias(self.alias))
        self.assertEqual(self.room.to_string(), room_id)

        self.assertIsNone(
            self.get_success(self.store.get_association_from_room_alias(self.alias))
        )
