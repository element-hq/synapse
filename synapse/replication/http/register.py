#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

import logging
from typing import TYPE_CHECKING

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# FIXME(2025-07-22): Remove this on the next release, this may only be used
# during rollout to Synapse 1.134 and can be removed after that release.
class ReplicationRegisterServlet(ReplicationEndpoint):
    """Register a new user"""

    NAME = "register_user"
    PATH_ARGS = ("user_id",)

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.store = hs.get_datastores().main
        self.registration_handler = hs.get_registration_handler()

        # Default value if the worker that sent the replication request did not include
        # an 'approved' property.
        if (
            hs.config.experimental.msc3866.enabled
            and hs.config.experimental.msc3866.require_approval_for_new_accounts
        ):
            self._approval_default = False
        else:
            self._approval_default = True

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str,
        password_hash: str | None,
        was_guest: bool,
        make_guest: bool,
        appservice_id: str | None,
        create_profile_with_displayname: str | None,
        admin: bool,
        user_type: str | None,
        address: str | None,
        shadow_banned: bool,
        approved: bool,
    ) -> JsonDict:
        """
        Args:
            user_id: The desired user ID to register.
            password_hash: Optional. The password hash for this user.
            was_guest: Optional. Whether this is a guest account being upgraded
                to a non-guest account.
            make_guest: True if the the new user should be guest, false to add a
                regular user account.
            appservice_id: The ID of the appservice registering the user.
            create_profile_with_displayname: Optionally create a profile for the
                user, setting their displayname to the given value
            admin: is an admin user?
            user_type: type of user. One of the values from api.constants.UserTypes,
                or None for a normal user.
            address: the IP address used to perform the regitration.
            shadow_banned: Whether to shadow-ban the user
            approved: Whether the user should be considered already approved by an
                administrator.
        """
        return {
            "password_hash": password_hash,
            "was_guest": was_guest,
            "make_guest": make_guest,
            "appservice_id": appservice_id,
            "create_profile_with_displayname": create_profile_with_displayname,
            "admin": admin,
            "user_type": user_type,
            "address": address,
            "shadow_banned": shadow_banned,
            "approved": approved,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> tuple[int, JsonDict]:
        await self.registration_handler.check_registration_ratelimit(content["address"])

        # Always default admin users to approved (since it means they were created by
        # an admin).
        approved_default = self._approval_default
        if content["admin"]:
            approved_default = True

        await self.registration_handler.register_with_store(
            user_id=user_id,
            password_hash=content["password_hash"],
            was_guest=content["was_guest"],
            make_guest=content["make_guest"],
            appservice_id=content["appservice_id"],
            create_profile_with_displayname=content["create_profile_with_displayname"],
            admin=content["admin"],
            user_type=content["user_type"],
            address=content["address"],
            shadow_banned=content["shadow_banned"],
            approved=content.get("approved", approved_default),
        )

        return 200, {}


class ReplicationPostRegisterActionsServlet(ReplicationEndpoint):
    """Run any post registration actions"""

    NAME = "post_register"
    PATH_ARGS = ("user_id",)

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.store = hs.get_datastores().main
        self.registration_handler = hs.get_registration_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str, auth_result: JsonDict, access_token: str | None
    ) -> JsonDict:
        """
        Args:
            user_id: The user ID that consented
            auth_result: The authenticated credentials of the newly registered user.
            access_token: The access token of the newly logged in
                device, or None if `inhibit_login` enabled.
        """
        return {"auth_result": auth_result, "access_token": access_token}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> tuple[int, JsonDict]:
        auth_result = content["auth_result"]
        access_token = content["access_token"]

        await self.registration_handler.post_registration_actions(
            user_id=user_id, auth_result=auth_result, access_token=access_token
        )

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationRegisterServlet(hs).register(http_server)
    ReplicationPostRegisterActionsServlet(hs).register(http_server)
