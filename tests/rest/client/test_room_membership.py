#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
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
from unittest.mock import AsyncMock, patch

from twisted.internet.testing import MemoryReactor

from synapse.appservice import SCOPE_QUERY_ROOM_MEMBERSHIP, ApplicationService
from synapse.rest import admin
from synapse.rest.client import login, room, room_membership
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID, create_requester
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils import event_injection
from tests.unittest import override_config

AS_TOKEN = "i_am_an_app_service"
AS_TOKEN_NO_SCOPE = "i_am_an_app_service_without_scope"


class AppserviceRoomMembershipRestServletTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        room.register_servlets,
        room_membership.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {
            "msc4502_enabled": True,
            **config.get("experimental_features", {}),
        }
        return config

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.creator = self.register_user("owner", "pass")
        self.creator_tok = self.login("owner", "pass")
        self.room_id = self.helper.create_room_as(self.creator, tok=self.creator_tok)

        self.joined_user = self.register_user("joined_user", "pass")
        self.joined_user_tok = self.login("joined_user", "pass")
        self.helper.join(self.room_id, self.joined_user, tok=self.joined_user_tok)

        self.not_joined_user = self.register_user("not_joined_user", "pass")
        self.not_joined_user_tok = self.login("not_joined_user", "pass")

        self.remote_server = "elsewhere.com"
        self.remote_user = UserID.from_string(f"@joined_user:{self.remote_server}")
        self.get_success(
            event_injection.inject_member_event(
                self.hs, self.room_id, self.remote_user.to_string(), "join"
            )
        )
        self.not_joined_remote_user = UserID.from_string(
            f"@not_joined_user:{self.remote_server}"
        )

        self.unknown_server = "unknown.org"
        self.unknown_room_id = "!unknown:unknown.org"

        main_store = self.hs.get_datastores().main
        main_store.services_cache.append(
            ApplicationService(
                AS_TOKEN,
                id="as_with_scope",
                sender=UserID.from_string("@as:test"),
                scopes=[SCOPE_QUERY_ROOM_MEMBERSHIP],
            )
        )
        main_store.services_cache.append(
            ApplicationService(
                AS_TOKEN_NO_SCOPE,
                id="as_without_scope",
                sender=UserID.from_string("@as2:test"),
            )
        )

    def _get_joined(
        self, room_id: str, params: str, access_token: str | None
    ) -> tuple[int, JsonDict]:
        channel = self.make_request(
            "GET",
            f"/_matrix/client/unstable/io.element.msc4502/rooms/{room_id}/is_joined?{params}",
            access_token=access_token,
        )
        return channel.code, channel.json_body

    def test_invalid_room_id_format(self) -> None:
        code, body = self._get_joined(
            "not-a-room-id", f"mxid={self.joined_user}", AS_TOKEN
        )
        self.assertEqual(code, HTTPStatus.BAD_REQUEST, body)
        self.assertEqual(body["errcode"], "M_INVALID_PARAM")

    def test_both_mxid_and_server_name_given(self) -> None:
        code, body = self._get_joined(
            self.room_id,
            f"mxid={self.joined_user}&server_name={self.hs.hostname}",
            AS_TOKEN,
        )
        self.assertEqual(code, HTTPStatus.BAD_REQUEST, body)
        self.assertEqual(body["errcode"], "M_MISSING_PARAM")

    def test_neither_mxid_nor_server_name_given(self) -> None:
        code, body = self._get_joined(self.room_id, "", AS_TOKEN)
        self.assertEqual(code, HTTPStatus.BAD_REQUEST, body)
        self.assertEqual(body["errcode"], "M_MISSING_PARAM")

    def test_invalid_mxid_format(self) -> None:
        code, body = self._get_joined(self.room_id, "mxid=not-a-userid", AS_TOKEN)
        self.assertEqual(code, HTTPStatus.BAD_REQUEST, body)
        self.assertEqual(body["errcode"], "M_INVALID_PARAM")

    def test_invalid_server_name_format(self) -> None:
        code, body = self._get_joined(self.room_id, "server_name=foo_bar", AS_TOKEN)
        self.assertEqual(code, HTTPStatus.BAD_REQUEST, body)
        self.assertEqual(body["errcode"], "M_INVALID_PARAM")

    def test_local_user_joined(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"mxid={self.joined_user}", AS_TOKEN
        )
        self.assertEqual(code, HTTPStatus.OK, body)
        self.assertEqual(body, {"joined": True})

    def test_local_user_not_joined(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"mxid={self.not_joined_user}", AS_TOKEN
        )
        self.assertEqual(code, HTTPStatus.OK, body)
        self.assertEqual(body, {"joined": False})

    def test_remote_user_joined(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"mxid={self.remote_user.to_string()}", AS_TOKEN
        )
        self.assertEqual(code, HTTPStatus.OK, body)
        self.assertEqual(body, {"joined": True})

    def test_remote_user_not_joined(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"mxid={self.not_joined_remote_user.to_string()}", AS_TOKEN
        )
        self.assertEqual(code, HTTPStatus.OK, body)
        self.assertEqual(body, {"joined": False})

    def test_local_server_name_joined(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"server_name={self.hs.hostname}", AS_TOKEN
        )
        self.assertEqual(code, HTTPStatus.OK, body)
        self.assertEqual(body, {"joined": True})

    def test_remote_server_name_joined(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"server_name={self.remote_server}", AS_TOKEN
        )
        self.assertEqual(code, HTTPStatus.OK, body)
        self.assertEqual(body, {"joined": True})

    def test_remote_server_name_not_joined(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"server_name={self.unknown_server}", AS_TOKEN
        )
        self.assertEqual(code, HTTPStatus.OK, body)
        self.assertEqual(body, {"joined": False})

    def test_nonexistent_room_returns_false(self) -> None:
        code, body = self._get_joined(
            self.unknown_room_id, f"server_name={self.unknown_server}", AS_TOKEN
        )
        self.assertEqual(code, HTTPStatus.OK, body)
        self.assertEqual(body, {"joined": False})

    def test_no_token_unauthorized(self) -> None:
        code, body = self._get_joined(self.room_id, f"mxid={self.joined_user}", None)
        self.assertEqual(code, HTTPStatus.UNAUTHORIZED, body)
        self.assertEqual(body["errcode"], "M_MISSING_TOKEN")

    def test_normal_user_token_forbidden(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"mxid={self.joined_user}", self.creator_tok
        )
        self.assertEqual(code, HTTPStatus.FORBIDDEN, body)
        self.assertEqual(body["errcode"], "M_FORBIDDEN")

    def test_same_user_token_forbidden(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"mxid={self.joined_user}", self.joined_user_tok
        )
        self.assertEqual(code, HTTPStatus.FORBIDDEN, body)
        self.assertEqual(body["errcode"], "M_FORBIDDEN")

    def test_user_with_oauth_scope_allowed(self) -> None:
        requester = create_requester(self.creator, scope={SCOPE_QUERY_ROOM_MEMBERSHIP})
        with patch.object(
            self.hs.get_auth(), "get_user_by_req", AsyncMock(return_value=requester)
        ):
            code, body = self._get_joined(
                self.room_id, f"mxid={self.joined_user}", "doesnt-matter"
            )
        self.assertEqual(code, HTTPStatus.OK, body)
        self.assertEqual(body, {"joined": True})

    def test_appservice_without_scope_forbidden(self) -> None:
        code, body = self._get_joined(
            self.room_id, f"mxid={self.joined_user}", AS_TOKEN_NO_SCOPE
        )
        self.assertEqual(code, HTTPStatus.FORBIDDEN, body)
        self.assertEqual(body["errcode"], "M_FORBIDDEN")

    @override_config({"experimental_features": {"msc4502_enabled": False}})
    def test_unreachable_when_experimental_flag_disabled(self) -> None:
        code, _ = self._get_joined(self.room_id, f"mxid={self.joined_user}", AS_TOKEN)
        self.assertEqual(code, HTTPStatus.NOT_FOUND)
