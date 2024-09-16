#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from synapse.api.constants import EduTypes

from tests import unittest
from tests.unittest import DEBUG, override_config


class RoomDirectoryFederationTests(unittest.FederatingHomeserverTestCase):
    @override_config({"allow_public_rooms_over_federation": False})
    def test_blocked_public_room_list_over_federation(self) -> None:
        """Test that unauthenticated requests to the public rooms directory 403 when
        allow_public_rooms_over_federation is False.
        """
        channel = self.make_signed_federation_request(
            "GET",
            "/_matrix/federation/v1/publicRooms",
        )
        self.assertEqual(403, channel.code)

    @override_config({"allow_public_rooms_over_federation": True})
    def test_open_public_room_list_over_federation(self) -> None:
        """Test that unauthenticated requests to the public rooms directory 200 when
        allow_public_rooms_over_federation is True.
        """
        channel = self.make_signed_federation_request(
            "GET",
            "/_matrix/federation/v1/publicRooms",
        )
        self.assertEqual(200, channel.code)

    @DEBUG
    def test_edu_debugging_doesnt_explode(self) -> None:
        """Sanity check incoming federation succeeds with `synapse.debug_8631` enabled.

        Remove this when we strip out issue_8631_logger.
        """
        channel = self.make_signed_federation_request(
            "PUT",
            "/_matrix/federation/v1/send/txn_id_1234/",
            content={
                "edus": [
                    {
                        "edu_type": EduTypes.DEVICE_LIST_UPDATE,
                        "content": {
                            "device_id": "QBUAZIFURK",
                            "stream_id": 0,
                            "user_id": "@user:id",
                        },
                    },
                ],
                "pdus": [],
            },
        )
        self.assertEqual(200, channel.code)
