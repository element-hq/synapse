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
from typing import TYPE_CHECKING, Tuple

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
from synapse.types import JsonDict, UserID

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


class ProfileDisplaynameRestServlet(RestServlet):
    PATTERNS = client_patterns("/profile/(?P<user_id>[^/]*)/displayname", v1=True)
    CATEGORY = "Event sending requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.profile_handler = hs.get_profile_handler()
        self.auth = hs.get_auth()

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
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

        displayname = await self.profile_handler.get_displayname(user)

        ret = {}
        if displayname is not None:
            ret["displayname"] = displayname

        return 200, ret

    async def on_PUT(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        user = UserID.from_string(user_id)
        is_admin = await self.auth.is_server_admin(requester)

        content = parse_json_object_from_request(request)

        try:
            new_name = content["displayname"]
        except Exception:
            raise SynapseError(
                code=400,
                msg="Unable to parse name",
                errcode=Codes.BAD_JSON,
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
                "Updating displayname while account is suspended is not allowed.",
                Codes.USER_ACCOUNT_SUSPENDED,
            )

        await self.profile_handler.set_displayname(
            user, requester, new_name, is_admin, propagate=propagate
        )

        return 200, {}


class ProfileAvatarURLRestServlet(RestServlet):
    PATTERNS = client_patterns("/profile/(?P<user_id>[^/]*)/avatar_url", v1=True)
    CATEGORY = "Event sending requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.profile_handler = hs.get_profile_handler()
        self.auth = hs.get_auth()

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
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

        avatar_url = await self.profile_handler.get_avatar_url(user)

        ret = {}
        if avatar_url is not None:
            ret["avatar_url"] = avatar_url

        return 200, ret

    async def on_PUT(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user = UserID.from_string(user_id)
        is_admin = await self.auth.is_server_admin(requester)

        content = parse_json_object_from_request(request)
        try:
            new_avatar_url = content["avatar_url"]
        except KeyError:
            raise SynapseError(
                400, "Missing key 'avatar_url'", errcode=Codes.MISSING_PARAM
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
                "Updating avatar URL while account is suspended is not allowed.",
                Codes.USER_ACCOUNT_SUSPENDED,
            )

        await self.profile_handler.set_avatar_url(
            user, requester, new_avatar_url, is_admin, propagate=propagate
        )

        return 200, {}


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
    ) -> Tuple[int, JsonDict]:
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

        displayname = await self.profile_handler.get_displayname(user)
        avatar_url = await self.profile_handler.get_avatar_url(user)

        ret = {}
        if displayname is not None:
            ret["displayname"] = displayname
        if avatar_url is not None:
            ret["avatar_url"] = avatar_url

        return 200, ret


class UnstableProfileRestServlet(RestServlet):
    PATTERNS = [
        re.compile(
            r"^/_matrix/client/unstable/uk\.tcpip\.msc4133/profile/(?P<user_id>[^/]*)"
        )
    ]
    CATEGORY = "Event sending requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.profile_handler = hs.get_profile_handler()
        self.auth = hs.get_auth()

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
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

        return 200, await self.profile_handler.get_profile(user_id)

    async def on_PATCH(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user = UserID.from_string(user_id)
        is_admin = await self.auth.is_server_admin(requester)

        content = parse_json_object_from_request(request)

        for field_name in content.keys():
            if not field_name:
                raise SynapseError(
                    400, "Field name too short", errcode=Codes.INVALID_PARAM
                )

            if len(field_name.encode("utf-8")) > MAX_CUSTOM_FIELD_LEN:
                raise SynapseError(
                    400, f"Field name too long: {field_name}", errcode=Codes.TOO_LARGE
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

        # TODO This isn't idempotent.
        for field_name, new_value in content.items():
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

    async def on_PUT(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user = UserID.from_string(user_id)
        is_admin = await self.auth.is_server_admin(requester)

        content = parse_json_object_from_request(request)

        for field_name in content.keys():
            if not field_name:
                raise SynapseError(
                    400, "Field name too short", errcode=Codes.INVALID_PARAM
                )

            if len(field_name.encode("utf-8")) > MAX_CUSTOM_FIELD_LEN:
                raise SynapseError(
                    400, f"Field name too long: {field_name}", errcode=Codes.TOO_LARGE
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

        # TODO This isn't idempotent.

        # Any fields which were not provided need to be deleted.
        current_fields = await self.profile_handler.get_profile(user_id)
        for field_name in (ProfileFields.DISPLAYNAME, ProfileFields.AVATAR_URL):
            # If no new value is given, then set the value to empty to delete it.
            if field_name not in content:
                content[field_name] = ""
        for field_name in current_fields.keys() - content.keys():
            await self.profile_handler.delete_profile_field(user, requester, field_name)

        # Remaining fields are created or updated.
        for field_name, new_value in content.items():
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


class UnstableProfileFieldRestServlet(RestServlet):
    PATTERNS = [
        re.compile(
            r"^/_matrix/client/unstable/uk\.tcpip\.msc4133/profile/(?P<user_id>[^/]*)/(?P<field_name>[^/]*)"
        )
    ]
    CATEGORY = "Event sending requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.profile_handler = hs.get_profile_handler()
        self.auth = hs.get_auth()

    async def on_GET(
        self, request: SynapseRequest, user_id: str, field_name: str
    ) -> Tuple[int, JsonDict]:
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

        if field_name == ProfileFields.DISPLAYNAME:
            field_value = await self.profile_handler.get_displayname(user)
        elif field_name == ProfileFields.AVATAR_URL:
            field_value = await self.profile_handler.get_avatar_url(user)
        else:
            field_value = await self.profile_handler.get_profile_field(user, field_name)

        return 200, {field_name: field_value}

    async def on_PUT(
        self, request: SynapseRequest, user_id: str, field_name: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user = UserID.from_string(user_id)
        is_admin = await self.auth.is_server_admin(requester)

        if not field_name:
            raise SynapseError(400, "Field name too short", errcode=Codes.INVALID_PARAM)

        if len(field_name.encode("utf-8")) > MAX_CUSTOM_FIELD_LEN:
            raise SynapseError(400, "Field name too long", errcode=Codes.INVALID_PARAM)

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
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user = UserID.from_string(user_id)
        is_admin = await self.auth.is_server_admin(requester)

        if not field_name:
            raise SynapseError(400, "Field name too short", errcode=Codes.INVALID_PARAM)

        if len(field_name.encode("utf-8")) > MAX_CUSTOM_FIELD_LEN:
            raise SynapseError(400, "Field name too long", errcode=Codes.TOO_LARGE)

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


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    # The specific displayname / avatar URL / custom field endpoints *must* appear
    # before their corresponding generic profile endpoint.
    ProfileDisplaynameRestServlet(hs).register(http_server)
    ProfileAvatarURLRestServlet(hs).register(http_server)
    ProfileRestServlet(hs).register(http_server)
    if hs.config.experimental.msc4133_enabled:
        UnstableProfileFieldRestServlet(hs).register(http_server)
        UnstableProfileRestServlet(hs).register(http_server)
