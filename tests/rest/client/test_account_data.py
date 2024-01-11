#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C
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
from unittest.mock import AsyncMock

from synapse.rest import admin
from synapse.rest.client import account_data, login, room

from tests import unittest


class AccountDataTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        account_data.register_servlets,
    ]

    def test_on_account_data_updated_callback(self) -> None:
        """Tests that the on_account_data_updated module callback is called correctly when
        a user's account data changes.
        """
        mocked_callback = AsyncMock(return_value=None)
        self.hs.get_account_data_handler()._on_account_data_updated_callbacks.append(
            mocked_callback
        )

        user_id = self.register_user("user", "password")
        tok = self.login("user", "password")
        account_data_type = "org.matrix.foo"
        account_data_content = {"bar": "baz"}

        # Change the user's global account data.
        channel = self.make_request(
            "PUT",
            f"/user/{user_id}/account_data/{account_data_type}",
            account_data_content,
            access_token=tok,
        )

        # Test that the callback is called with the user ID, the new account data, and
        # None as the room ID.
        self.assertEqual(channel.code, 200, channel.result)
        mocked_callback.assert_called_once_with(
            user_id, None, account_data_type, account_data_content
        )

        # Change the user's room-specific account data.
        room_id = self.helper.create_room_as(user_id, tok=tok)
        channel = self.make_request(
            "PUT",
            f"/user/{user_id}/rooms/{room_id}/account_data/{account_data_type}",
            account_data_content,
            access_token=tok,
        )

        # Test that the callback is called with the user ID, the room ID and the new
        # account data.
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(mocked_callback.call_count, 2)
        mocked_callback.assert_called_with(
            user_id, room_id, account_data_type, account_data_content
        )
