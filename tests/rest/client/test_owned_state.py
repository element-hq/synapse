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
from http import HTTPStatus

from parameterized import parameterized_class

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.errors import Codes
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests.unittest import HomeserverTestCase

_STATE_EVENT_TEST_TYPE = "com.example.test"

# To stress-test parsing, include separator & sigil characters
_STATE_KEY_SUFFIX = "_state_key_suffix:!@#$123"


class OwnedStateBase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.admin_user_id = self.register_user("admin", "pass")
        self.admin_access_token = self.login("admin", "pass")
        self.user1_user_id = self.register_user("user1", "pass")
        self.user1_access_token = self.login("user1", "pass")

        self.room_id = self.helper.create_room_as(
            self.admin_user_id,
            tok=self.admin_access_token,
            is_public=True,
            extra_content={
                "power_level_content_override": {
                    "events": {
                        _STATE_EVENT_TEST_TYPE: 0,
                    },
                },
            },
        )

        self.helper.join(
            room=self.room_id, user=self.user1_user_id, tok=self.user1_access_token
        )


class WithoutOwnedStateTestCase(OwnedStateBase):
    def test_user_can_set_state_with_own_userid_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.user1_user_id}",
            tok=self.user1_access_token,
            expect_code=HTTPStatus.OK,
        )

    def test_admin_cannot_set_state_with_own_suffixed_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.admin_user_id}{_STATE_KEY_SUFFIX}",
            tok=self.admin_access_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_admin_cannot_set_state_with_other_userid_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.user1_user_id}",
            tok=self.admin_access_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_admin_cannot_set_state_with_other_suffixed_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.user1_user_id}{_STATE_KEY_SUFFIX}",
            tok=self.admin_access_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_admin_cannot_set_state_with_malformed_userid_key(self) -> None:
        body = self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key="@oops",
            tok=self.admin_access_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

        self.assertEqual(
            body["errcode"],
            Codes.FORBIDDEN,
            body,
        )


@parameterized_class(
    ("room_version",),
    [(i,) for i, v in KNOWN_ROOM_VERSIONS.items() if v.msc3757_enabled],
)
class MSC3757OwnedStateTestCase(OwnedStateBase):
    room_version: str

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["default_room_version"] = self.room_version
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.user2_user_id = self.register_user("user2", "pass")
        self.user2_access_token = self.login("user2", "pass")

        self.helper.join(
            room=self.room_id, user=self.user2_user_id, tok=self.user2_access_token
        )

    def test_user_can_set_state_with_own_suffixed_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.user1_user_id}{_STATE_KEY_SUFFIX}",
            tok=self.user1_access_token,
            expect_code=HTTPStatus.OK,
        )

    def test_admin_can_set_state_with_other_userid_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.user1_user_id}",
            tok=self.admin_access_token,
            expect_code=HTTPStatus.OK,
        )

    def test_admin_can_set_state_with_other_suffixed_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.user1_user_id}{_STATE_KEY_SUFFIX}",
            tok=self.admin_access_token,
            expect_code=HTTPStatus.OK,
        )

    def test_user_cannot_set_state_with_other_userid_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.user2_user_id}",
            tok=self.user1_access_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_user_cannot_set_state_with_other_suffixed_key(self) -> None:
        self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.user2_user_id}",
            tok=self.user1_access_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_admin_cannot_set_state_with_malformed_userid_key(self) -> None:
        body = self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key="@oops",
            tok=self.admin_access_token,
            expect_code=HTTPStatus.BAD_REQUEST,
        )

        self.assertEqual(
            body["errcode"],
            Codes.BAD_JSON,
            body,
        )

    def test_admin_cannot_set_state_with_improperly_suffixed_key(self) -> None:
        body = self.helper.send_state(
            self.room_id,
            _STATE_EVENT_TEST_TYPE,
            {},
            state_key=f"{self.admin_user_id}@{_STATE_KEY_SUFFIX[1:]}",
            tok=self.admin_access_token,
            expect_code=HTTPStatus.BAD_REQUEST,
        )

        self.assertEqual(
            body["errcode"],
            Codes.BAD_JSON,
            body,
        )
