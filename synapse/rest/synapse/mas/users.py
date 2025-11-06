#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl_3.0.html>.
#
#

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, TypedDict

from pydantic import StrictBool, StrictStr, model_validator

from synapse.api.errors import NotFoundError, SynapseError
from synapse.http.servlet import (
    parse_and_validate_json_object_from_request,
    parse_string,
)
from synapse.types import JsonDict, UserID, UserInfo, create_requester
from synapse.types.rest import RequestBodyModel

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


from ._base import MasBaseResource

logger = logging.getLogger(__name__)


class MasQueryUserResource(MasBaseResource):
    """
    Endpoint for MAS to query user information by localpart.

    Takes a localpart parameter and returns user profile data including display name,
    avatar URL, and account status (suspended/deactivated).

    GET /_synapse/mas/query_user?localpart=alice
    """

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

    class Response(TypedDict):
        user_id: str
        display_name: str | None
        avatar_url: str | None
        is_suspended: bool
        is_deactivated: bool

    async def _async_render_GET(
        self, request: "SynapseRequest"
    ) -> tuple[int, Response]:
        self.assert_request_is_from_mas(request)

        localpart = parse_string(request, "localpart", required=True)
        user_id = UserID(localpart, self.hostname)

        user: UserInfo | None = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        profile = await self.store.get_profileinfo(user_id=user_id)

        return HTTPStatus.OK, self.Response(
            user_id=user_id.to_string(),
            display_name=profile.display_name,
            avatar_url=profile.avatar_url,
            is_suspended=user.suspended,
            is_deactivated=user.is_deactivated,
        )


class MasProvisionUserResource(MasBaseResource):
    """
    Endpoint for MAS to create or update user accounts and their profile data.

    Takes a localpart and optional profile fields (display name, avatar URL, email addresses).
    Can create new users or update existing ones by setting or unsetting profile fields.

    POST /_synapse/mas/provision_user
    {"localpart": "alice", "set_displayname": "Alice", "set_emails": ["alice@example.com"]}
    """

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)
        self.registration_handler = hs.get_registration_handler()
        self.identity_handler = hs.get_identity_handler()
        self.auth_handler = hs.get_auth_handler()
        self.profile_handler = hs.get_profile_handler()
        self.clock = hs.get_clock()
        self.auth = hs.get_auth()

    class PostBody(RequestBodyModel):
        localpart: StrictStr

        unset_displayname: StrictBool = False
        set_displayname: StrictStr | None = None

        unset_avatar_url: StrictBool = False
        set_avatar_url: StrictStr | None = None

        unset_emails: StrictBool = False
        set_emails: list[StrictStr] | None = None

        @model_validator(mode="before")
        @classmethod
        def validate_exclusive(cls, values: Any) -> Any:
            if "unset_displayname" in values and "set_displayname" in values:
                raise ValueError(
                    "Cannot specify both unset_displayname and set_displayname"
                )
            if "unset_avatar_url" in values and "set_avatar_url" in values:
                raise ValueError(
                    "Cannot specify both unset_avatar_url and set_avatar_url"
                )
            if "unset_emails" in values and "set_emails" in values:
                raise ValueError("Cannot specify both unset_emails and set_emails")

            return values

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)

        localpart = body.localpart
        user_id = UserID(localpart, self.hostname)

        requester = create_requester(user_id=user_id)
        existing_user = await self.store.get_user_by_id(user_id=str(user_id))
        if existing_user is None:
            created = True
            await self.registration_handler.register_user(
                localpart=localpart,
                default_display_name=body.set_displayname,
                bind_emails=body.set_emails,
                by_admin=True,
            )
        else:
            created = False
            if body.unset_displayname:
                await self.profile_handler.set_displayname(
                    target_user=user_id,
                    requester=requester,
                    new_displayname="",
                    by_admin=True,
                )
            elif body.set_displayname is not None:
                await self.profile_handler.set_displayname(
                    target_user=user_id,
                    requester=requester,
                    new_displayname=body.set_displayname,
                    by_admin=True,
                )

            new_email_list: set[str] | None = None
            if body.unset_emails:
                new_email_list = set()
            elif body.set_emails is not None:
                new_email_list = set(body.set_emails)

            if new_email_list is not None:
                medium = "email"
                current_threepid_list = await self.store.user_get_threepids(
                    user_id=user_id.to_string()
                )
                current_email_list = {
                    t.address for t in current_threepid_list if t.medium == medium
                }

                to_delete = current_email_list - new_email_list
                to_add = new_email_list - current_email_list

                for address in to_delete:
                    await self.identity_handler.try_unbind_threepid(
                        mxid=user_id.to_string(),
                        medium=medium,
                        address=address,
                        id_server=None,
                    )

                    await self.auth_handler.delete_local_threepid(
                        user_id=user_id.to_string(),
                        medium=medium,
                        address=address,
                    )

                current_time = self.clock.time_msec()
                for address in to_add:
                    await self.auth_handler.add_threepid(
                        user_id=user_id.to_string(),
                        medium=medium,
                        address=address,
                        validated_at=current_time,
                    )

        if body.unset_avatar_url:
            await self.profile_handler.set_avatar_url(
                target_user=user_id,
                requester=requester,
                new_avatar_url="",
                by_admin=True,
            )
        elif body.set_avatar_url is not None:
            await self.profile_handler.set_avatar_url(
                target_user=user_id,
                requester=requester,
                new_avatar_url=body.set_avatar_url,
                by_admin=True,
            )

        return HTTPStatus.CREATED if created else HTTPStatus.OK, {}


class MasIsLocalpartAvailableResource(MasBaseResource):
    """
    Endpoint for MAS to check if a localpart is available for user registration.

    Takes a localpart parameter and validates its format and availability,
    checking for conflicts with existing users or application service namespaces.

    GET /_synapse/mas/is_localpart_available?localpart=alice
    """

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.registration_handler = hs.get_registration_handler()

    async def _async_render_GET(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)
        localpart = parse_string(request, "localpart")
        if localpart is None:
            raise SynapseError(400, "Missing localpart")

        await self.registration_handler.check_username(localpart)

        return HTTPStatus.OK, {}


class MasDeleteUserResource(MasBaseResource):
    """
    Endpoint for MAS to delete/deactivate user accounts.

    Takes a localpart and an erase flag to determine whether to deactivate
    the account and optionally erase user data for compliance purposes.

    POST /_synapse/mas/delete_user
    {"localpart": "alice", "erase": true}
    """

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.deactivate_account_handler = hs.get_deactivate_account_handler()

    class PostBody(RequestBodyModel):
        localpart: StrictStr
        erase: StrictBool

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)
        user_id = UserID(body.localpart, self.hostname)

        # Check the user exists
        user = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        await self.deactivate_account_handler.deactivate_account(
            user_id=user_id.to_string(),
            erase_data=body.erase,
            requester=create_requester(user_id=user_id),
        )

        return HTTPStatus.OK, {}


class MasReactivateUserResource(MasBaseResource):
    """
    Endpoint for MAS to reactivate previously deactivated user accounts.

    Takes a localpart parameter to restore access to deactivated accounts.

    POST /_synapse/mas/reactivate_user
    {"localpart": "alice"}
    """

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.deactivate_account_handler = hs.get_deactivate_account_handler()

    class PostBody(RequestBodyModel):
        localpart: StrictStr

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)
        user_id = UserID(body.localpart, self.hostname)

        # Check the user exists
        user = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        await self.deactivate_account_handler.activate_account(user_id=str(user_id))

        return HTTPStatus.OK, {}


class MasSetDisplayNameResource(MasBaseResource):
    """
    Endpoint for MAS to set a user's display name.

    Takes a localpart and display name to update the user's profile.

    POST /_synapse/mas/set_displayname
    {"localpart": "alice", "displayname": "Alice"}
    """

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.profile_handler = hs.get_profile_handler()
        self.auth_handler = hs.get_auth_handler()

    class PostBody(RequestBodyModel):
        localpart: StrictStr
        displayname: StrictStr

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)
        user_id = UserID(body.localpart, self.hostname)

        # Check the user exists
        user = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        requester = create_requester(user_id=user_id)

        await self.profile_handler.set_displayname(
            target_user=requester.user,
            requester=requester,
            new_displayname=body.displayname,
            by_admin=True,
        )

        return HTTPStatus.OK, {}


class MasUnsetDisplayNameResource(MasBaseResource):
    """
    Endpoint for MAS to clear a user's display name.

    Takes a localpart parameter to remove the display name for the specified user.

    POST /_synapse/mas/unset_displayname
    {"localpart": "alice"}
    """

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.profile_handler = hs.get_profile_handler()
        self.auth_handler = hs.get_auth_handler()

    class PostBody(RequestBodyModel):
        localpart: StrictStr

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)
        user_id = UserID(body.localpart, self.hostname)

        # Check the user exists
        user = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        requester = create_requester(user_id=user_id)

        await self.profile_handler.set_displayname(
            target_user=requester.user,
            requester=requester,
            new_displayname="",
            by_admin=True,
        )

        return HTTPStatus.OK, {}


class MasAllowCrossSigningResetResource(MasBaseResource):
    """
    Endpoint for MAS to allow cross-signing key reset without user interaction.

    Takes a localpart parameter to temporarily allow cross-signing key replacement
    without requiring User-Interactive Authentication (UIA).

    POST /_synapse/mas/allow_cross_signing_reset
    {"localpart": "alice"}
    """

    REPLACEMENT_PERIOD_MS = 10 * 60 * 1000  # 10 minutes

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.auth_handler = hs.get_auth_handler()

    class PostBody(RequestBodyModel):
        localpart: StrictStr

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)
        user_id = UserID(body.localpart, self.hostname)

        # Check the user exists
        user = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        timestamp = (
            await self.store.allow_master_cross_signing_key_replacement_without_uia(
                user_id=str(user_id),
                duration_ms=self.REPLACEMENT_PERIOD_MS,
            )
        )

        if timestamp is None:
            # If there are no cross-signing keys, this is a no-op, but we should log
            logger.warning(
                "User %s has no master cross-signing key", user_id.to_string()
            )

        return HTTPStatus.OK, {}
