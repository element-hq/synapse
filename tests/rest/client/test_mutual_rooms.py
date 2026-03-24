#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Half-Shot
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
from urllib.parse import quote

from typing import Any as MemoryReactor  # was: MemoryReactor from Twisted

import synapse.rest.admin
from synapse.rest.client import login, mutual_rooms, room
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest
from tests.server import FakeChannel


class UserMutualRoomsTest(unittest.HomeserverTestCase):
    """
    Tests the UserMutualRoomsServlet.
    """

    servlets = [
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        mutual_rooms.register_servlets,
    ]

    async def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()
        return await self.setup_test_homeserver(config=config)

    async def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        mutual_rooms.MUTUAL_ROOMS_BATCH_LIMIT = 10

    async def _get_mutual_rooms(
        self, token: str, other_user: str, since_token: str | None = None
    ) -> FakeChannel:
        return await self.make_request(
            "GET",
            "/_matrix/client/v1/mutual_rooms"
            f"?user_id={quote(other_user)}"
            + (f"&from={quote(since_token)}" if since_token else ""),
            access_token=token,
        )

    async def test_shared_room_list_public(self) -> None:
        """
        A room should show up in the shared list of rooms between two users
        if it is public.
        """
        await self._check_mutual_rooms_with(room_one_is_public=True, room_two_is_public=True)

    async def test_shared_room_list_private(self) -> None:
        """
        A room should show up in the shared list of rooms between two users
        if it is private.
        """
        await self._check_mutual_rooms_with(
            room_one_is_public=False, room_two_is_public=False
        )

    async def test_shared_room_list_mixed(self) -> None:
        """
        The shared room list between two users should contain both public and private
        rooms.
        """
        await self._check_mutual_rooms_with(room_one_is_public=True, room_two_is_public=False)

    async def _check_mutual_rooms_with(
        self, room_one_is_public: bool, room_two_is_public: bool
    ) -> None:
        """Checks that shared public or private rooms between two users appear in
        their shared room lists
        """
        u1 = await self.register_user("user1", "pass")
        u1_token = await self.login(u1, "pass")
        u2 = await self.register_user("user2", "pass")
        u2_token = await self.login(u2, "pass")

        # Create a room. user1 invites user2, who joins
        room_id_one = await self.helper.create_room_as(
            u1, is_public=room_one_is_public, tok=u1_token
        )
        await self.helper.invite(room_id_one, src=u1, targ=u2, tok=u1_token)
        await self.helper.join(room_id_one, user=u2, tok=u2_token)

        # Check shared rooms from user1's perspective.
        # We should see the one room in common
        channel = await self._get_mutual_rooms(u1_token, u2)
        self.assertEqual(200, channel.code, channel.result)
        self.assertEqual(len(channel.json_body["joined"]), 1)
        self.assertEqual(channel.json_body["count"], 1)
        self.assertEqual(channel.json_body["joined"][0], room_id_one)

        # Create another room and invite user2 to it
        room_id_two = await self.helper.create_room_as(
            u1, is_public=room_two_is_public, tok=u1_token
        )
        await self.helper.invite(room_id_two, src=u1, targ=u2, tok=u1_token)
        await self.helper.join(room_id_two, user=u2, tok=u2_token)

        # Check shared rooms again. We should now see both rooms.
        channel = await self._get_mutual_rooms(u1_token, u2)
        self.assertEqual(200, channel.code, channel.result)
        self.assertEqual(len(channel.json_body["joined"]), 2)
        self.assertEqual(channel.json_body["count"], 2)
        for room_id_id in channel.json_body["joined"]:
            self.assertIn(room_id_id, [room_id_one, room_id_two])

    async def _create_rooms_for_pagination_test(
        self, count: int
    ) -> tuple[str, str, list[str]]:
        u1 = await self.register_user("user1", "pass")
        u1_token = await self.login(u1, "pass")
        u2 = await self.register_user("user2", "pass")
        u2_token = await self.login(u2, "pass")
        room_ids = []
        for i in range(count):
            room_id = await self.helper.create_room_as(u1, is_public=i % 2 == 0, tok=u1_token)
            await self.helper.invite(room_id, src=u1, targ=u2, tok=u1_token)
            await self.helper.join(room_id, user=u2, tok=u2_token)
            room_ids.append(room_id)
        room_ids.sort()
        return u1_token, u2, room_ids

    async def test_shared_room_list_pagination_two_pages(self) -> None:
        u1_token, u2, room_ids = await self._create_rooms_for_pagination_test(15)

        channel = await self._get_mutual_rooms(u1_token, u2)
        self.assertEqual(200, channel.code, channel.result)
        self.assertEqual(channel.json_body["joined"], room_ids[0:10])
        self.assertEqual(channel.json_body["count"], 15)
        self.assertIn("next_batch", channel.json_body)

        channel = await self._get_mutual_rooms(u1_token, u2, channel.json_body["next_batch"])
        self.assertEqual(200, channel.code, channel.result)
        self.assertEqual(channel.json_body["joined"], room_ids[10:20])
        self.assertEqual(channel.json_body["count"], 15)
        self.assertNotIn("next_batch", channel.json_body)

    async def test_shared_room_list_pagination_one_page(self) -> None:
        u1_token, u2, room_ids = await self._create_rooms_for_pagination_test(10)

        channel = await self._get_mutual_rooms(u1_token, u2)
        self.assertEqual(200, channel.code, channel.result)
        self.assertEqual(channel.json_body["joined"], room_ids)
        self.assertEqual(channel.json_body["count"], 10)
        self.assertNotIn("next_batch", channel.json_body)

    async def test_shared_room_list_pagination_invalid_token(self) -> None:
        u1_token, u2, room_ids = await self._create_rooms_for_pagination_test(10)

        channel = await self._get_mutual_rooms(u1_token, u2, "!<>##faketoken")
        self.assertEqual(400, channel.code, channel.result)
        self.assertEqual(
            "M_INVALID_PARAM", channel.json_body["errcode"], channel.result
        )

    async def test_shared_room_list_after_leave(self) -> None:
        """
        A room should no longer be considered shared if the other
        user has left it.
        """
        u1 = await self.register_user("user1", "pass")
        u1_token = await self.login(u1, "pass")
        u2 = await self.register_user("user2", "pass")
        u2_token = await self.login(u2, "pass")

        room = await self.helper.create_room_as(u1, is_public=True, tok=u1_token)
        await self.helper.invite(room, src=u1, targ=u2, tok=u1_token)
        await self.helper.join(room, user=u2, tok=u2_token)

        # Assert user directory is not empty
        channel = await self._get_mutual_rooms(u1_token, u2)
        self.assertEqual(200, channel.code, channel.result)
        self.assertEqual(len(channel.json_body["joined"]), 1)
        self.assertEqual(channel.json_body["count"], 1)
        self.assertEqual(channel.json_body["joined"][0], room)

        await self.helper.leave(room, user=u1, tok=u1_token)

        # Check user1's view of shared rooms with user2
        channel = await self._get_mutual_rooms(u1_token, u2)
        self.assertEqual(200, channel.code, channel.result)
        self.assertEqual(len(channel.json_body["joined"]), 0)
        self.assertEqual(channel.json_body["count"], 0)

        # Check user2's view of shared rooms with user1
        channel = await self._get_mutual_rooms(u2_token, u1)
        self.assertEqual(200, channel.code, channel.result)
        self.assertEqual(len(channel.json_body["joined"]), 0)
        self.assertEqual(channel.json_body["count"], 0)

    async def test_shared_room_list_nonexistent_user(self) -> None:
        u1 = await self.register_user("user1", "pass")
        u1_token = await self.login(u1, "pass")

        # Check shared rooms from user1's perspective.
        # We should see the one room in common
        channel = await self._get_mutual_rooms(u1_token, "@meow:example.com")
        self.assertEqual(200, channel.code, channel.result)
        self.assertEqual(len(channel.json_body["joined"]), 0)
        self.assertEqual(channel.json_body["count"], 0)
        self.assertNotIn("next_batch", channel.json_body)

    async def test_shared_room_list_invalid_user(self) -> None:
        u1 = await self.register_user("user1", "pass")
        u1_token = await self.login(u1, "pass")

        channel = await self._get_mutual_rooms(u1_token, "@:example.com")
        self.assertEqual(400, channel.code, channel.result)
        self.assertEqual(
            "M_INVALID_PARAM", channel.json_body["errcode"], channel.result
        )

        channel = await self._get_mutual_rooms(u1_token, "@" + "a" * 255 + ":example.com")
        self.assertEqual(400, channel.code, channel.result)
        self.assertEqual(
            "M_INVALID_PARAM", channel.json_body["errcode"], channel.result
        )

        channel = await self._get_mutual_rooms(u1_token, "@🐈️:example.com")
        self.assertEqual(400, channel.code, channel.result)
        self.assertEqual(
            "M_INVALID_PARAM", channel.json_body["errcode"], channel.result
        )
