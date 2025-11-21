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

"""This module contains REST servlets to do with profile: /profile/<paths>"""

import re
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.constants import ProfileFields
from synapse.api.errors import Codes, SynapseError
from synapse.handlers.profile import MAX_CUSTOM_FIELD_LEN
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_boolean,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict, JsonValue, UserID
from synapse.util.stringutils import is_namedspaced_grammar

if TYPE_CHECKING:
    from synapse.server import HomeServer


def _read_propagate(hs: "HomeServer", request: SynapseRequest) -> bool:
    # This will always be set by the time Twisted calls us.
    assert request.args is not None

    propagate = True
    if hs.config.experimental.msc4069_profile_inhibit_propagation:
        do_propagate = request.args.get(b"org.matrix.msc4069.propagate")
        if do_propagate is not None:
            propagate = parse_boolean(
                request, "org.matrix.msc4069.propagate", default=False
            )
    return propagate


class ProfileRestServlet(RestServlet):
    PATTERNS = client_patterns("/profile/(?P<user_id>[^/]*)", v1=True)
    CATEGORY = "Event sending requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.profile_handler = hs.get_profile_handler()
        self.auth = hs.get_auth()

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[int, JsonDict]:
        requester_user = None

        if self.hs.config.server.require_auth_for_profile_requests:
            requester = await self.auth.get_user_by_req(request)
            requester_user = requester.user

        if not UserID.is_valid(user_id):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid user id", Codes.INVALID_PARAM
            )

        user = UserID.from_string(user_id)
        await self.profile_handler.check_profile_query_allowed(user, requester_user)

        ret = await self.profile_handler.get_profile(user_id)

        return 200, ret


class ProfileFieldRestServlet(RestServlet):
    PATTERNS = [
        *client_patterns(
            "/profile/(?P<user_id>[^/]*)/(?P<field_name>displayname)", v1=True
        ),
        *client_patterns(
            "/profile/(?P<user_id>[^/]*)/(?P<field_name>avatar_url)", v1=True
        ),
        re.compile(
            r"^/_matrix/client/v3/profile/(?P<user_id>[^/]*)/(?P<field_name>[^/]*)"
        ),
    ]

    CATEGORY = "Event sending requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.profile_handler = hs.get_profile_handler()
        self.auth = hs.get_auth()
        if hs.config.experimental.msc4133_enabled:
            self.PATTERNS.append(
                re.compile(
                    r"^/_matrix/client/unstable/uk\.tcpip\.msc4133/profile/(?P<user_id>[^/]*)/(?P<field_name>[^/]*)"
                )
            )

    async def on_GET(
        self, request: SynapseRequest, user_id: str, field_name: str
    ) -> tuple[int, JsonDict]:
        requester_user = None

        if self.hs.config.server.require_auth_for_profile_requests:
            requester = await self.auth.get_user_by_req(request)
            requester_user = requester.user

        if not UserID.is_valid(user_id):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid user id", Codes.INVALID_PARAM
            )

        if not field_name:
            raise SynapseError(400, "Field name too short", errcode=Codes.INVALID_PARAM)

        if len(field_name.encode("utf-8")) > MAX_CUSTOM_FIELD_LEN:
            raise SynapseError(400, "Field name too long", errcode=Codes.KEY_TOO_LARGE)
        if not is_namedspaced_grammar(field_name):
            raise SynapseError(
                400,
                "Field name does not follow Common Namespaced Identifier Grammar",
                errcode=Codes.INVALID_PARAM,
            )

        user = UserID.from_string(user_id)
        await self.profile_handler.check_profile_query_allowed(user, requester_user)

        if field_name == ProfileFields.DISPLAYNAME:
            field_value: JsonValue = await self.profile_handler.get_displayname(user)
        elif field_name == ProfileFields.AVATAR_URL:
            field_value = await self.profile_handler.get_avatar_url(user)
        else:
            field_value = await self.profile_handler.get_profile_field(user, field_name)

        return 200, {field_name: field_value}

    async def on_PUT(
        self, request: SynapseRequest, user_id: str, field_name: str
    ) -> tuple[int, JsonDict]:
        if not UserID.is_valid(user_id):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid user id", Codes.INVALID_PARAM
            )

        # Guest users are able to set their own displayname.
        requester = await self.auth.get_user_by_req(
            request, allow_guest=field_name == ProfileFields.DISPLAYNAME
        )
        user = UserID.from_string(user_id)
        is_admin = await self.auth.is_server_admin(requester)

        if not field_name:
            raise SynapseError(400, "Field name too short", errcode=Codes.INVALID_PARAM)

        if len(field_name.encode("utf-8")) > MAX_CUSTOM_FIELD_LEN:
            raise SynapseError(400, "Field name too long", errcode=Codes.KEY_TOO_LARGE)
        if not is_namedspaced_grammar(field_name):
            raise SynapseError(
                400,
                "Field name does not follow Common Namespaced Identifier Grammar",
                errcode=Codes.INVALID_PARAM,
            )

        content = parse_json_object_from_request(request)
        try:
            new_value = content[field_name]
        except KeyError:
            raise SynapseError(
                400, f"Missing key '{field_name}'", errcode=Codes.MISSING_PARAM
            )

        propagate = _read_propagate(self.hs, request)

        requester_suspended = (
            await self.hs.get_datastores().main.get_user_suspended_status(
                requester.user.to_string()
            )
        )

        if requester_suspended:
            raise SynapseError(
                403,
                "Updating profile while account is suspended is not allowed.",
                Codes.USER_ACCOUNT_SUSPENDED,
            )

        if field_name == ProfileFields.DISPLAYNAME:
            await self.profile_handler.set_displayname(
                user, requester, new_value, is_admin, propagate=propagate
            )
        elif field_name == ProfileFields.AVATAR_URL:
            await self.profile_handler.set_avatar_url(
                user, requester, new_value, is_admin, propagate=propagate
            )
        else:
            await self.profile_handler.set_profile_field(
                user, requester, field_name, new_value, is_admin
            )

        return 200, {}

    async def on_DELETE(
        self, request: SynapseRequest, user_id: str, field_name: str
    ) -> tuple[int, JsonDict]:
        if not UserID.is_valid(user_id):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid user id", Codes.INVALID_PARAM
            )

        # Guest users are able to set their own displayname.
        requester = await self.auth.get_user_by_req(
            request, allow_guest=field_name == ProfileFields.DISPLAYNAME
        )
        user = UserID.from_string(user_id)
        is_admin = await self.auth.is_server_admin(requester)

        if not field_name:
            raise SynapseError(400, "Field name too short", errcode=Codes.INVALID_PARAM)

        if len(field_name.encode("utf-8")) > MAX_CUSTOM_FIELD_LEN:
            raise SynapseError(400, "Field name too long", errcode=Codes.KEY_TOO_LARGE)
        if not is_namedspaced_grammar(field_name):
            raise SynapseError(
                400,
                "Field name does not follow Common Namespaced Identifier Grammar",
                errcode=Codes.INVALID_PARAM,
            )

        propagate = _read_propagate(self.hs, request)

        requester_suspended = (
            await self.hs.get_datastores().main.get_user_suspended_status(
                requester.user.to_string()
            )
        )

        if requester_suspended:
            raise SynapseError(
                403,
                "Updating profile while account is suspended is not allowed.",
                Codes.USER_ACCOUNT_SUSPENDED,
            )

        if field_name == ProfileFields.DISPLAYNAME:
            await self.profile_handler.set_displayname(
                user, requester, "", is_admin, propagate=propagate
            )
        elif field_name == ProfileFields.AVATAR_URL:
            await self.profile_handler.set_avatar_url(
                user, requester, "", is_admin, propagate=propagate
            )
        else:
            await self.profile_handler.delete_profile_field(
                user, requester, field_name, is_admin
            )

        return 200, {}


class UnstableProfileFieldRestServlet(ProfileFieldRestServlet):
    re.compile(
        r"^/_matrix/client/unstable/uk\.tcpip\.msc4133/profile/(?P<user_id>[^/]*)/(?P<field_name>[^/]*)"
    )


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    # The specific field endpoint *must* appear before the generic profile endpoint.
    ProfileFieldRestServlet(hs).register(http_server)
    ProfileRestServlet(hs).register(http_server)
    if hs.config.experimental.msc4133_enabled:
        UnstableProfileFieldRestServlet(hs).register(http_server)
