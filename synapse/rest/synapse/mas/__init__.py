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
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE_2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#


import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Optional, Tuple, cast

from pydantic.v1.types import StrictBool, StrictStr

from twisted.web.resource import Resource

from synapse._pydantic_compat import BaseModel, root_validator
from synapse.api.auth.msc3861_delegated import MSC3861DelegatedAuth
from synapse.api.errors import SynapseError
from synapse.http.server import DirectServeJsonResource
from synapse.http.servlet import (
    parse_and_validate_json_object_from_request,
    parse_string,
)
from synapse.types import JsonDict, UserID, UserInfo
from synapse.types.rest import RequestBodyModel

if TYPE_CHECKING:
    from synapse.app.generic_worker import GenericWorkerStore
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


class MasResource(Resource):
    def __init__(self, hs: "HomeServer"):
        Resource.__init__(self)
        self.putChild(b"query_user", MasQueryUserResource(hs))
        self.putChild(b"provision_user", MasProvisionUserResource(hs))
        # self.putChild(b"is_localpart_available", MasIsLocalpartAvailableResource(hs))
        # self.putChild(b"create_device", MasCreateDeviceResource(hs))
        # self.putChild(
        # b"update_device_display_name", MasUpdateDeviceDisplayNameResource(hs)
        # )
        # self.putChild(b"sync_devices", MasSyncDevicesResource(hs))
        # self.putChild(b"delete_user", MasDeleteUserResource(hs))
        # self.putChild(b"reactivate_user", MasReactivateUserResource(hs))
        # self.putChild(b"set_displayname", MasSetDisplayNameResource(hs))
        # self.putChild(b"unset_displayname", MasUnsetDisplayNameResource(hs))
        # self.putChild(
        # b"allow_cross_signing_reset", MasAllowCrossSigningResetResource(hs)
        # )


class MasBaseResource(DirectServeJsonResource):
    def __init__(self, hs: "HomeServer"):
        DirectServeJsonResource.__init__(self, extract_context=True)
        auth = hs.get_auth()
        assert isinstance(auth, MSC3861DelegatedAuth)
        self.msc3861_auth = auth
        self.store = cast("GenericWorkerStore", hs.get_datastores().main)
        self.hostname = hs.hostname

    def assert_mas_request(self, request: "SynapseRequest") -> None:
        if not self.msc3861_auth.is_request_using_the_admin_token(request):
            raise SynapseError(403, "This endpoint must only be called by MAS")


class MasQueryUserResource(MasBaseResource):
    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

    class Response(BaseModel):
        user_id: StrictStr
        display_name: Optional[StrictStr]
        avatar_url: Optional[StrictStr]
        is_suspended: StrictBool
        is_deactivated: StrictBool

    async def _async_render_GET(
        self, request: "SynapseRequest"
    ) -> Tuple[int, JsonDict]:
        self.assert_mas_request(request)

        localpart = parse_string(request, "localpart", required=True)
        user_id = UserID(localpart, self.hostname)
        user: Optional[UserInfo] = await self.store.get_user_by_id(
            user_id=user_id.to_string()
        )

        if user is None:
            raise SynapseError(404, "User not found")

        profile = await self.store.get_profileinfo(user_id=user_id)

        return HTTPStatus.OK, self.Response(
            user_id=user_id.to_string(),
            display_name=profile.display_name,
            avatar_url=profile.avatar_url,
            is_suspended=user.suspended,
            is_deactivated=user.is_deactivated,
        ).dict()


class MasProvisionUserResource(MasBaseResource):
    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)
        self.registration_handler = hs.get_registration_handler()
        self.identity_handler = hs.get_identity_handler()
        self.auth_handler = hs.get_auth_handler()
        self.clock = hs.get_clock()
        self.auth = hs.get_auth()

    class PostBody(RequestBodyModel):
        localpart: StrictStr

        unset_displayname: StrictBool = False
        set_displayname: Optional[StrictStr] = None

        unset_avatar_url: StrictBool = False
        set_avatar_url: Optional[StrictStr] = None

        unset_emails: StrictBool = False
        set_emails: Optional[list[StrictStr]] = None

        @root_validator
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
    ) -> Tuple[int, JsonDict]:
        self.assert_mas_request(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)

        localpart = body.localpart
        user_id = UserID(localpart, self.hostname)

        user = await self.store.get_user_by_id(user_id=user_id.to_string())
        if user is None:
            created = True
            await self.registration_handler.register_user(
                localpart=localpart,
                default_display_name=body.set_displayname,
                bind_emails=body.set_emails,
            )
        else:
            created = False
            if body.unset_displayname:
                await self.store.set_profile_displayname(user_id, None)
            elif body.set_displayname is not None:
                await self.store.set_profile_displayname(user_id, body.set_displayname)

            new_email_list: Optional[set[str]] = None
            if body.unset_emails:
                new_email_list = set()
            elif body.set_emails is not None:
                new_email_list = set(body.set_emails)

            if new_email_list is not None:
                current_threepid_list = await self.store.user_get_threepids(
                    user_id=user_id.to_string()
                )
                current_email_list = {
                    t.address for t in current_threepid_list if t.medium == "email"
                }

                to_delete = current_email_list - new_email_list
                to_add = new_email_list - current_email_list

                for address in to_delete:
                    await self.identity_handler.try_unbind_threepid(
                        mxid=user_id.to_string(),
                        medium="email",
                        address=address,
                        id_server=None,
                    )

                    await self.auth_handler.delete_local_threepid(
                        user_id=user_id.to_string(),
                        medium="email",
                        address=address,
                    )

                current_time = self.clock.time_msec()
                for address in to_add:
                    await self.auth_handler.add_threepid(
                        user_id=user_id.to_string(),
                        medium="email",
                        address=address,
                        validated_at=current_time,
                    )

        if body.unset_avatar_url:
            await self.store.set_profile_avatar_url(user_id, None)
        elif body.set_avatar_url is not None:
            await self.store.set_profile_avatar_url(user_id, body.set_avatar_url)

        return HTTPStatus.CREATED if created else HTTPStatus.OK, {}
