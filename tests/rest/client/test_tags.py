#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

"""Tests REST events for /tags paths."""

from twisted.test.proto_helpers import MemoryReactor

from synapse.rest.client import room, tags
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests import unittest

PATH_PREFIX = "/_matrix/client/api/v1"


class RoomTaggingTestCase(unittest.HomeserverTestCase):
    """Tests /user/$user_id/rooms/$room_id/tags/$tag REST API."""

    user_id = "@sid:red"
    servlets = [room.register_servlets, tags.register_servlets]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver("red")
        self.room_member_handler = hs.get_room_member_handler()
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.room_id = self.helper.create_room_as(self.user_id)

    def test_put_tag_checks_room_membership(self) -> None:
        tag = "test_tag"
        # Make the request
        channel = self.make_request(
            "PUT",
            f"/user/{self.user_id}/rooms/{self.room_id}/tags/{tag}",
            content={"order": 0.5},
            access_token=self.get_success(
                self.hs.get_auth_handler().create_access_token_for_user_id(
                    self.user_id, device_id=None, valid_until_ms=None
                )
            ),
        )
        # Check that the request was successful
        self.assertEqual(channel.code, 200, channel.result)

    def test_put_tag_fails_if_not_in_room(self) -> None:
        room_id = "!nonexistent:test"
        tag = "test_tag"

        # Make the request
        channel = self.make_request(
            "PUT",
            f"/user/{self.user_id}/rooms/{room_id}/tags/{tag}",
            content={"order": 0.5},
            access_token=self.get_success(
                self.hs.get_auth_handler().create_access_token_for_user_id(
                    self.user_id, device_id=None, valid_until_ms=None
                )
            ),
        )
        # Check that the request failed with the correct error
        self.assertEqual(channel.code, 403, channel.result)