"""This module implements a subset of the SCIM user provisioning protocol,
as proposed in the MSC4098.

The implemented endpoints are:
- /User (GET, POST, PUT, DELETE)
- /ServiceProviderConfig (GET)
- /Schemas (GET)
- /ResourceTypes (GET)

The supported SCIM User attributes are:
- userName
- password
- emails
- phoneNumbers
- displayName
- photos
- active

References:
https://github.com/matrix-org/matrix-spec-proposals/pull/4098
https://datatracker.ietf.org/doc/html/rfc7642
https://datatracker.ietf.org/doc/html/rfc7643
https://datatracker.ietf.org/doc/html/rfc7644
"""

import datetime
import logging
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, TypeVar, Union

from synapse.api.errors import SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_integer,
    parse_json_object_from_request,
    parse_strings_from_args,
)
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import assert_requester_is_admin, assert_user_is_admin
from synapse.rest.client.register import RegisterRestServlet
from synapse.types import JsonDict, UserID
from synapse.util.templates import mxc_to_http

try:
    from scim2_models import (
        AuthenticationScheme,
        Bulk,
        ChangePassword,
        Context,
        Email,
        Error,
        ETag,
        Filter,
        ListResponse,
        Meta,
        Patch,
        PhoneNumber,
        Photo,
        ResourceType,
        Schema,
        SearchRequest,
        ServiceProviderConfig,
        Sort,
        User,
    )

    HAS_SCIM2 = True

except ImportError:
    HAS_SCIM2 = False

if TYPE_CHECKING:
    from synapse.server import HomeServer

SCIM_PREFIX = "/_synapse/admin/scim/v2"
SCIM_DEFAULT_IDP_ID = "__scim__"

logger = logging.getLogger(__name__)


def register_scim_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if not hs.config.experimental.scim.enabled:
        return

    SchemaListServlet(hs).register(http_server)
    SchemaServlet(hs).register(http_server)
    ResourceTypeListServlet(hs).register(http_server)
    ResourceTypeServlet(hs).register(http_server)
    ServiceProviderConfigServlet(hs).register(http_server)

    UserListServlet(hs).register(http_server)
    UserServlet(hs).register(http_server)


T = TypeVar("T")


class SCIMServlet(RestServlet):
    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.config = hs.config
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_auth_handler()
        self.is_mine = hs.is_mine
        self.profile_handler = hs.get_profile_handler()

        self.default_nb_items_per_page = 100
        self.max_nb_items_per_page = 1000

    def make_response(
        self,
        request: SynapseRequest,
        status: Union[int, HTTPStatus],
        payload: T,
    ) -> Tuple[Union[int, HTTPStatus], T]:
        """Create a SCIM response, and adds the expected headers."""
        request.setHeader(b"Content-Type", b"application/scim+json")
        return status, payload

    def make_error_response(
        self, request: SynapseRequest, status: Union[int, HTTPStatus], message: str
    ) -> Tuple[Union[int, HTTPStatus], JsonDict]:
        """Create a SCIM Error object intended to be returned as HTTP response."""
        return self.make_response(
            request,
            status,
            Error(
                status=status.value if isinstance(status, HTTPStatus) else status,
                detail=message,
            ).model_dump(),
        )

    def parse_search_request(self, request: SynapseRequest) -> "SearchRequest":
        """Build a SCIM SearchRequest object from the HTTP request arguments."""
        args: Dict[bytes, List[bytes]] = request.args  # type: ignore
        count = min(
            parse_integer(
                request, "count", default=self.default_nb_items_per_page, negative=False
            ),
            self.max_nb_items_per_page,
        )
        return SearchRequest(
            attributes=parse_strings_from_args(args, "attributes"),
            excluded_attributes=parse_strings_from_args(args, "excludedAttributes"),
            start_index=parse_integer(request, "startIndex", default=1, negative=False),
            count=count,
        )

    async def get_scim_external_id(self, user_id: str) -> Optional[str]:
        """Read the external id stored in the special SCIM IDP."""

        scim_idp_id = self.hs.config.experimental.scim.idp_id or SCIM_DEFAULT_IDP_ID
        external_ids = await self.store.get_external_ids_by_user(user_id)
        for idp_id, external_id in external_ids:
            if idp_id == scim_idp_id:
                return external_id

        return None

    async def get_scim_user(self, user_id: str) -> "User":
        """Create a SCIM User object from a synapse user_id.

        The objects are intended be used as HTTP responses."""

        user_id_obj = UserID.from_string(user_id)
        user = await self.store.get_user_by_id(user_id)
        profile = await self.store.get_profileinfo(user_id_obj)
        threepids = await self.store.user_get_threepids(user_id)

        if not user:
            raise SynapseError(
                HTTPStatus.NOT_FOUND,
                "User not found",
            )

        if not self.is_mine(user_id_obj):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Only local users can be admins of this homeserver",
            )

        creation_datetime = datetime.datetime.fromtimestamp(user.creation_ts)
        external_id = await self.get_scim_external_id(user_id)
        scim_user: User = User(
            meta=Meta(
                resource_type="User",
                created=creation_datetime,
                last_modified=creation_datetime,
                location=f"{self.config.server.public_baseurl}{SCIM_PREFIX[1:]}/Users/{user_id}",
            ),
            id=user_id,
            external_id=external_id,
            user_name=user_id_obj.localpart,
            display_name=profile.display_name,
            active=not user.is_deactivated,
            emails=[
                Email(value=threepid.address)
                for threepid in threepids
                if threepid.medium == "email"
            ]
            or None,
            phone_numbers=[
                PhoneNumber(value=threepid.address)
                for threepid in threepids
                if threepid.medium == "msisdn"
            ]
            or None,
        )

        if profile.avatar_url:
            http_url = mxc_to_http(
                self.hs.config.server.public_baseurl, profile.avatar_url
            )
            scim_user.photos = [
                Photo(
                    type=Photo.Type.photo,
                    primary=True,
                    value=http_url,
                )
            ]

        return scim_user


class UserServlet(SCIMServlet):
    """Servlet implementing the SCIM /Users/* endpoints.

    Details are available on RFC7644:
    https://datatracker.ietf.org/doc/html/rfc7644#section-3.2
    """

    PATTERNS = [re.compile(f"^{SCIM_PREFIX}/Users/(?P<user_id>[^/]*)")]

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[Union[int, HTTPStatus], JsonDict]:
        """Implement the RFC7644 'Retrieving a Known Resource' endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-3.4.1"""

        await assert_requester_is_admin(self.auth, request)
        try:
            user = await self.get_scim_user(user_id)
            req = self.parse_search_request(request)
            payload = user.model_dump(
                scim_ctx=Context.RESOURCE_QUERY_RESPONSE,
                attributes=req.attributes,
                excluded_attributes=req.excluded_attributes,
            )
            return self.make_response(request, HTTPStatus.OK, payload)
        except SynapseError as exc:
            return self.make_error_response(request, exc.code, exc.msg)

    async def on_DELETE(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[Union[int, HTTPStatus], Union[str, JsonDict]]:
        """Implement the RFC7644 resource deletion endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-3.6"""

        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester)
        deactivate_account_handler = self.hs.get_deactivate_account_handler()
        try:
            await deactivate_account_handler.deactivate_account(
                user_id, erase_data=True, requester=requester, by_admin=True
            )
        except SynapseError as exc:
            return self.make_error_response(request, exc.code, exc.msg)

        return self.make_response(request, HTTPStatus.NO_CONTENT, "")

    async def on_PUT(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        """Implement the RFC7644 resource replacement endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-3.5.1"""

        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester)

        try:
            body = parse_json_object_from_request(request)
            original_user = await self.get_scim_user(user_id)
            request_user: User = User.model_validate(
                body,
                scim_ctx=Context.RESOURCE_REPLACEMENT_REQUEST,
                original=original_user,
            )

            user_id_obj = UserID.from_string(user_id)

            threepids = await self.store.user_get_threepids(user_id)

            await self.profile_handler.set_displayname(
                user_id_obj, requester, request_user.display_name or "", True
            )

            external_id = await self.get_scim_external_id(user_id)
            if request_user.external_id != external_id:
                scim_idp_id = (
                    self.hs.config.experimental.scim.idp_id or SCIM_DEFAULT_IDP_ID
                )
                if external_id:
                    await self.store.remove_user_external_id(
                        scim_idp_id, external_id, user_id
                    )
                if request_user.external_id:
                    await self.store.record_user_external_id(
                        scim_idp_id, request_user.external_id, user_id
                    )

            if request_user.photos and request_user.photos[0].value:
                media_repo = (
                    self.hs.get_media_repository()
                    if self.hs.config.media.can_load_media_repo
                    else None
                )
                http_client = self.hs.get_proxied_blocklisted_http_client()

                await self.profile_handler.set_avatar_from_http_url(
                    user_id,
                    str(request_user.photos[0].value),
                    media_repo,
                    http_client,
                    "scim_",
                )

            if threepids is not None:
                new_email_threepids = {
                    ("email", email.value)
                    for email in request_user.emails or []
                    if email.value
                }
                new_phone_number_threepids = {
                    ("msisdn", phone_number.value)
                    for phone_number in request_user.phone_numbers or []
                    if phone_number.value
                }
                new_threepids = new_email_threepids | new_phone_number_threepids
                # get changed threepids (added and removed)
                cur_threepids = {
                    (threepid.medium, threepid.address)
                    for threepid in await self.store.user_get_threepids(user_id)
                }
                add_threepids = new_threepids - cur_threepids
                del_threepids = cur_threepids - new_threepids

                # remove old threepids
                for medium, address in del_threepids:
                    try:
                        # Attempt to remove any known bindings of this third-party ID
                        # and user ID from identity servers.
                        await self.hs.get_identity_handler().try_unbind_threepid(
                            user_id, medium, address, id_server=None
                        )
                    except Exception:
                        logger.exception("Failed to remove threepids")
                        raise SynapseError(500, "Failed to remove threepids")

                    # Delete the local association of this user ID and third-party ID.
                    await self.auth_handler.delete_local_threepid(
                        user_id, medium, address
                    )

                # add new threepids
                current_time = self.hs.get_clock().time_msec()
                for medium, address in add_threepids:
                    await self.auth_handler.add_threepid(
                        user_id, medium, address, current_time
                    )

            response_user = await self.get_scim_user(user_id)
            payload = response_user.model_dump(
                scim_ctx=Context.RESOURCE_REPLACEMENT_RESPONSE
            )
            return self.make_response(request, HTTPStatus.OK, payload)

        except SynapseError as exc:
            return self.make_error_response(request, exc.code, exc.msg)


class UserListServlet(SCIMServlet):
    """Servlet implementing the SCIM /Users endpoint.

    Details are available on RFC7644:
    https://datatracker.ietf.org/doc/html/rfc7644#section-3.2
    """

    PATTERNS = [re.compile(f"^{SCIM_PREFIX}/Users/?$")]

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        """Implement the RFC7644 resource query endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-3.4.2"""

        try:
            await assert_requester_is_admin(self.auth, request)
            req = self.parse_search_request(request)

            items, total = await self.store.get_users_paginate(
                start=(req.start_index or 0) - 1,
                limit=req.count or 0,
            )
            users = [await self.get_scim_user(item.name) for item in items]
            list_response = ListResponse[User](
                start_index=req.start_index,
                items_per_page=req.count,
                total_results=total,
                resources=users,
            )
            payload = list_response.model_dump(
                scim_ctx=Context.RESOURCE_QUERY_RESPONSE,
            )
            return self.make_response(request, HTTPStatus.OK, payload)

        except SynapseError as exc:
            return self.make_error_response(request, exc.code, exc.msg)

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        """Implement the RFC7644 resource creation endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-3.3"""

        try:
            requester = await self.auth.get_user_by_req(request)
            await assert_user_is_admin(self.auth, requester)

            body = parse_json_object_from_request(request)
            request_user: User = User.model_validate(
                body, scim_ctx=Context.RESOURCE_CREATION_REQUEST
            )

            register = RegisterRestServlet(self.hs)

            password_hash = (
                await self.auth_handler.hash(request_user.password)
                if request_user.password
                else None
            )
            user_id = await register.registration_handler.register_user(
                by_admin=True,
                approved=True,
                localpart=request_user.user_name,
                password_hash=password_hash,
                default_display_name=request_user.display_name,
            )

            if request_user.external_id:
                scim_idp_id = (
                    self.hs.config.experimental.scim.idp_id or SCIM_DEFAULT_IDP_ID
                )
                await self.store.record_user_external_id(
                    scim_idp_id, request_user.external_id, user_id
                )

            now_ts = self.hs.get_clock().time_msec()
            if request_user.emails:
                for email in request_user.emails:
                    if email.value:
                        await self.store.user_add_threepid(
                            user_id, "email", email.value, now_ts, now_ts
                        )

            if request_user.phone_numbers:
                for phone_number in request_user.phone_numbers:
                    if phone_number.value:
                        await self.store.user_add_threepid(
                            user_id, "msisdn", phone_number.value, now_ts, now_ts
                        )

            if request_user.photos and request_user.photos[0].value:
                media_repo = (
                    self.hs.get_media_repository()
                    if self.hs.config.media.can_load_media_repo
                    else None
                )
                http_client = self.hs.get_proxied_blocklisted_http_client()

                await self.profile_handler.set_avatar_from_http_url(
                    user_id,
                    str(request_user.photos[0].value),
                    media_repo,
                    http_client,
                    "scim_",
                )

            response_user = await self.get_scim_user(user_id)
            payload = response_user.model_dump(
                scim_ctx=Context.RESOURCE_CREATION_RESPONSE
            )
            return self.make_response(request, HTTPStatus.CREATED, payload)

        except SynapseError as exc:
            return self.make_error_response(request, exc.code, exc.msg)


class ServiceProviderConfigServlet(SCIMServlet):
    PATTERNS = [re.compile(f"^{SCIM_PREFIX}/ServiceProviderConfig$")]

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        """Implement the RFC7644 mandatory ServiceProviderConfig query endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-4"""

        spc = ServiceProviderConfig(
            meta=Meta(
                resource_type="ServiceProviderConfig",
                location=(
                    self.config.server.public_baseurl
                    + SCIM_PREFIX[1:]
                    + "/ServiceProviderConfig"
                ),
            ),
            documentation_uri="https://element-hq.github.io/synapse/latest/admin_api/scim_api.html",
            patch=Patch(supported=False),
            bulk=Bulk(supported=False, max_operations=0, max_payload_size=0),
            change_password=ChangePassword(supported=True),
            filter=Filter(supported=False, max_results=0),
            sort=Sort(supported=False),
            etag=ETag(supported=False),
            authentication_schemes=[
                AuthenticationScheme(
                    name="OAuth Bearer Token",
                    description="Authentication scheme using the OAuth Bearer Token Standard",
                    spec_uri="http://www.rfc-editor.org/info/rfc6750",
                    documentation_uri="https://element-hq.github.io/synapse/latest/openid.html",
                    type="oauthbearertoken",
                    primary=True,
                ),
            ],
        )
        return self.make_response(
            request,
            HTTPStatus.OK,
            spc.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE),
        )


class BaseSchemaServlet(SCIMServlet):
    schemas: Dict[str, "Schema"]

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.schemas = {
            "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig": ServiceProviderConfig.to_schema(),
            "urn:ietf:params:scim:schemas:core:2.0:ResourceType": ResourceType.to_schema(),
            "urn:ietf:params:scim:schemas:core:2.0:Schema": Schema.to_schema(),
            "urn:ietf:params:scim:schemas:core:2.0:User": User.to_schema(),
        }
        for schema_id, schema in self.schemas.items():
            schema_name = schema_id.split(":")[-1]
            schema.meta = Meta(
                resource_type=schema_name,
                location=(
                    self.config.server.public_baseurl
                    + SCIM_PREFIX[1:]
                    + "/Schemas/"
                    + schema_id
                ),
            )


class SchemaListServlet(BaseSchemaServlet):
    PATTERNS = [re.compile(f"^{SCIM_PREFIX}/Schemas$")]

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        """Implement the RFC7644 mandatory Schema list query endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-4"""

        req = self.parse_search_request(request)
        start_index = req.start_index or 0
        stop_index = start_index + req.count if req.count else None
        resources = list(self.schemas.values())
        response = ListResponse[Schema](
            total_results=len(resources),
            items_per_page=req.count or len(resources),
            start_index=start_index,
            resources=resources[start_index - 1 : stop_index],
        )
        return self.make_response(
            request,
            HTTPStatus.OK,
            response.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE),
        )


class SchemaServlet(BaseSchemaServlet):
    PATTERNS = [re.compile(f"^{SCIM_PREFIX}/Schemas/(?P<schema_id>[^/]*)$")]

    async def on_GET(
        self, request: SynapseRequest, schema_id: str
    ) -> Tuple[int, JsonDict]:
        """Implement the RFC7644 mandatory Schema query endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-4"""

        try:
            return self.make_response(
                request,
                HTTPStatus.OK,
                self.schemas[schema_id].model_dump(
                    scim_ctx=Context.RESOURCE_QUERY_RESPONSE
                ),
            )
        except KeyError:
            return self.make_error_response(
                request, HTTPStatus.NOT_FOUND, "Object not found"
            )


class BaseResourceTypeServlet(SCIMServlet):
    resource_type: "ResourceType"

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.resource_type = ResourceType(
            id="User",
            name="User",
            endpoint="/Users",
            description="User accounts",
            schema_="urn:ietf:params:scim:schemas:core:2.0:User",
            meta=Meta(
                resource_type="ResourceType",
                location=(
                    self.config.server.public_baseurl
                    + SCIM_PREFIX[1:]
                    + "/ResourceTypes/User"
                ),
            ),
        )


class ResourceTypeListServlet(BaseResourceTypeServlet):
    PATTERNS = [re.compile(f"^{SCIM_PREFIX}/ResourceTypes$")]

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        """Implement the RFC7644 mandatory ResourceType list query endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-4"""

        req = self.parse_search_request(request)
        start_index = req.start_index or 0
        stop_index = start_index + req.count if req.count else None
        resources = [
            self.resource_type.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE)
        ]
        response = ListResponse[ResourceType](
            total_results=len(resources),
            items_per_page=req.count or len(resources),
            start_index=start_index,
            resources=resources[start_index - 1 : stop_index],
        )
        return self.make_response(
            request,
            HTTPStatus.OK,
            response.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE),
        )


class ResourceTypeServlet(BaseResourceTypeServlet):
    PATTERNS = [re.compile(f"^{SCIM_PREFIX}/ResourceTypes/(?P<resource_type>[^/]*)$")]

    async def on_GET(
        self, request: SynapseRequest, resource_type: str
    ) -> Tuple[int, JsonDict]:
        """Implement the RFC7644 mandatory ResourceType query endpoint.

        As defined in:
        https://datatracker.ietf.org/doc/html/rfc7644#section-4"""

        resource_types = {
            "User": self.resource_type.model_dump(
                scim_ctx=Context.RESOURCE_QUERY_RESPONSE
            ),
        }

        try:
            return self.make_response(
                request, HTTPStatus.OK, resource_types[resource_type]
            )
        except KeyError:
            return self.make_error_response(
                request, HTTPStatus.NOT_FOUND, "Object not found"
            )
