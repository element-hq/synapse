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
import re
from http import HTTPStatus
from typing import Optional

from parameterized import parameterized_class

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.errors import Codes
from synapse.api.room_versions import RoomVersions
from synapse.events.utils import CANONICALJSON_MAX_INT, CANONICALJSON_MIN_INT
from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.util import Clock

from tests.unittest import HomeserverTestCase

_room_version_msc3757_re = re.compile(r"org.matrix.msc3757\b")


@parameterized_class(
    ("room_version",),
    [
        (None,) if rv is None else (rv.identifier,)
        for rv in [
            None,
            RoomVersions.MSC3757v9,
            RoomVersions.MSC3757v10,
            RoomVersions.MSC3757v11,
        ]
    ],
)
class PowerLevelsTestCase(HomeserverTestCase):
    """Tests that power levels are enforced in various situations"""

    room_version: Optional[str]
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()

        return self.setup_test_homeserver(config=config)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # register a room admin, moderator and regular user
        self.admin_user_id = self.register_user("admin", "pass")
        self.admin_access_token = self.login("admin", "pass")
        self.mod_user_id = self.register_user("mod", "pass")
        self.mod_access_token = self.login("mod", "pass")
        self.user_user_id = self.register_user("user", "pass")
        self.user_access_token = self.login("user", "pass")

        # Create a room
        self.room_id = self.helper.create_room_as(
            self.admin_user_id,
            tok=self.admin_access_token,
            room_version=self.room_version,
        )

        # Invite the other users
        self.helper.invite(
            room=self.room_id,
            src=self.admin_user_id,
            tok=self.admin_access_token,
            targ=self.mod_user_id,
        )
        self.helper.invite(
            room=self.room_id,
            src=self.admin_user_id,
            tok=self.admin_access_token,
            targ=self.user_user_id,
        )

        # Make the other users join the room
        self.helper.join(
            room=self.room_id, user=self.mod_user_id, tok=self.mod_access_token
        )
        self.helper.join(
            room=self.room_id, user=self.user_user_id, tok=self.user_access_token
        )

        # Mod the mod
        room_power_levels = self.helper.get_state(
            self.room_id,
            "m.room.power_levels",
            tok=self.admin_access_token,
        )

        # Update existing power levels with mod at PL50
        room_power_levels["users"].update({self.mod_user_id: 50})

        self.helper.send_state(
            self.room_id,
            "m.room.power_levels",
            room_power_levels,
            tok=self.admin_access_token,
        )

    @property
    def _allows_owned_state(self) -> bool:
        return self.room_version is not None and bool(
            _room_version_msc3757_re.match(self.room_version)
        )

    def test_non_admins_cannot_enable_room_encryption(self) -> None:
        # have the mod try to enable room encryption
        self.helper.send_state(
            self.room_id,
            "m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
            tok=self.mod_access_token,
            expect_code=403,  # expect failure
        )

        # have the user try to enable room encryption
        self.helper.send_state(
            self.room_id,
            "m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
            tok=self.user_access_token,
            expect_code=HTTPStatus.FORBIDDEN,  # expect failure
        )

    def test_non_admins_cannot_send_server_acl(self) -> None:
        # have the mod try to send a server ACL
        self.helper.send_state(
            self.room_id,
            "m.room.server_acl",
            {
                "allow": ["*"],
                "allow_ip_literals": False,
                "deny": ["*.evil.com", "evil.com"],
            },
            tok=self.mod_access_token,
            expect_code=HTTPStatus.FORBIDDEN,  # expect failure
        )

        # have the user try to send a server ACL
        self.helper.send_state(
            self.room_id,
            "m.room.server_acl",
            {
                "allow": ["*"],
                "allow_ip_literals": False,
                "deny": ["*.evil.com", "evil.com"],
            },
            tok=self.user_access_token,
            expect_code=HTTPStatus.FORBIDDEN,  # expect failure
        )

    def test_non_admins_cannot_tombstone_room(self) -> None:
        # Create another room that will serve as our "upgraded room"
        self.upgraded_room_id = self.helper.create_room_as(
            self.admin_user_id,
            tok=self.admin_access_token,
            room_version=self.room_version,
        )

        # have the mod try to send a tombstone event
        self.helper.send_state(
            self.room_id,
            "m.room.tombstone",
            {
                "body": "This room has been replaced",
                "replacement_room": self.upgraded_room_id,
            },
            tok=self.mod_access_token,
            expect_code=HTTPStatus.FORBIDDEN,  # expect failure
        )

        # have the user try to send a tombstone event
        self.helper.send_state(
            self.room_id,
            "m.room.tombstone",
            {
                "body": "This room has been replaced",
                "replacement_room": self.upgraded_room_id,
            },
            tok=self.user_access_token,
            expect_code=403,  # expect failure
        )

    def test_admins_can_enable_room_encryption(self) -> None:
        # have the admin try to enable room encryption
        self.helper.send_state(
            self.room_id,
            "m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
            tok=self.admin_access_token,
            expect_code=HTTPStatus.OK,  # expect success
        )

    def test_admins_can_send_server_acl(self) -> None:
        # have the admin try to send a server ACL
        self.helper.send_state(
            self.room_id,
            "m.room.server_acl",
            {
                "allow": ["*"],
                "allow_ip_literals": False,
                "deny": ["*.evil.com", "evil.com"],
            },
            tok=self.admin_access_token,
            expect_code=HTTPStatus.OK,  # expect success
        )

    def test_admins_can_tombstone_room(self) -> None:
        # Create another room that will serve as our "upgraded room"
        self.upgraded_room_id = self.helper.create_room_as(
            self.admin_user_id,
            tok=self.admin_access_token,
            room_version=self.room_version,
        )

        # have the admin try to send a tombstone event
        self.helper.send_state(
            self.room_id,
            "m.room.tombstone",
            {
                "body": "This room has been replaced",
                "replacement_room": self.upgraded_room_id,
            },
            tok=self.admin_access_token,
            expect_code=HTTPStatus.OK,  # expect success
        )

    def test_cannot_set_string_power_levels(self) -> None:
        room_power_levels = self.helper.get_state(
            self.room_id,
            "m.room.power_levels",
            tok=self.admin_access_token,
        )

        # Update existing power levels with user at PL "0"
        room_power_levels["users"].update({self.user_user_id: "0"})

        body = self.helper.send_state(
            self.room_id,
            "m.room.power_levels",
            room_power_levels,
            tok=self.admin_access_token,
            expect_code=HTTPStatus.BAD_REQUEST,  # expect failure
        )

        self.assertEqual(
            body["errcode"],
            Codes.BAD_JSON,
            body,
        )

    def test_cannot_set_unsafe_large_power_levels(self) -> None:
        room_power_levels = self.helper.get_state(
            self.room_id,
            "m.room.power_levels",
            tok=self.admin_access_token,
        )

        # Update existing power levels with user at PL above the max safe integer
        room_power_levels["users"].update(
            {self.user_user_id: CANONICALJSON_MAX_INT + 1}
        )

        body = self.helper.send_state(
            self.room_id,
            "m.room.power_levels",
            room_power_levels,
            tok=self.admin_access_token,
            expect_code=HTTPStatus.BAD_REQUEST,  # expect failure
        )

        self.assertEqual(
            body["errcode"],
            Codes.BAD_JSON,
            body,
        )

    def test_cannot_set_unsafe_small_power_levels(self) -> None:
        room_power_levels = self.helper.get_state(
            self.room_id,
            "m.room.power_levels",
            tok=self.admin_access_token,
        )

        # Update existing power levels with user at PL below the minimum safe integer
        room_power_levels["users"].update(
            {self.user_user_id: CANONICALJSON_MIN_INT - 1}
        )

        body = self.helper.send_state(
            self.room_id,
            "m.room.power_levels",
            room_power_levels,
            tok=self.admin_access_token,
            expect_code=HTTPStatus.BAD_REQUEST,  # expect failure
        )

        self.assertEqual(
            body["errcode"],
            Codes.BAD_JSON,
            body,
        )

    def test_can_set_own_state(self) -> None:
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc3757.test",
            {},
            state_key=f"{self.mod_user_id}_suffix",
            tok=self.mod_access_token,
            expect_code=(
                HTTPStatus.OK if self._allows_owned_state else HTTPStatus.FORBIDDEN
            ),
        )

    def test_admins_can_set_others_state(self) -> None:
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc3757.test",
            {},
            state_key=f"{self.mod_user_id}_suffix",
            tok=self.admin_access_token,
            expect_code=(
                HTTPStatus.OK if self._allows_owned_state else HTTPStatus.FORBIDDEN
            ),
        )

    def test_non_admins_cannot_set_others_state(self) -> None:
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc3757.test",
            {},
            state_key=f"{self.admin_user_id}_suffix",
            tok=self.mod_access_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_cannot_set_state_with_non_user_id_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc3757.test",
            {},
            state_key=f"{self.admin_user_id}@suffix",
            tok=self.admin_access_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )
