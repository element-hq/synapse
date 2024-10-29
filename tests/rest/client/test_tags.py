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

"""Tests REST events for /tags paths."""

from http import HTTPStatus

import synapse.rest.admin
from synapse.rest.client import login, room, tags

from tests import unittest


class RoomTaggingTestCase(unittest.HomeserverTestCase):
    """Tests /user/$user_id/rooms/$room_id/tags/$tag REST API."""

    servlets = [
        room.register_servlets,
        tags.register_servlets,
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
    ]

    def test_put_tag_checks_room_membership(self) -> None:
        """
        Test that a user can add a tag to a room if they have membership to the room.

        Args:
            None

        Returns:
            None
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(
            user1_id, "pass"
        )  # login(username: str, password: str) -> str
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        tag = "test_tag"
        # Make the request
        channel = self.make_request(
            "PUT",
            f"/user/{user1_id}/rooms/{room_id}/tags/{tag}",
            content={"order": 0.5},
            access_token=user1_tok,
        )
        # Check that the request was successful
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_put_tag_fails_if_not_in_room(self) -> None:
        """
        Test that a user cannot add a tag to a room if they don't have membership to the room.

        Args:
            None

        Returns:
            None
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id = "!nonexistent:test"
        tag = "test_tag"
        # Make the request
        channel = self.make_request(
            "PUT",
            f"/user/{user1_id}/rooms/{room_id}/tags/{tag}",
            content={"order": 0.5},
            access_token=user1_tok,
        )
        # Check that the request failed with the correct error
        self.assertEqual(channel.code, HTTPStatus.FORBIDDEN, channel.result)
