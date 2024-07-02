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
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import SynapseError
from synapse.http.server import HttpServer, JsonResource
from synapse.http.servlet import (
    RestServlet,
    parse_integer,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import assert_requester_is_admin, assert_user_is_admin
from synapse.types import JsonDict, UserID

from .scim_constants import (
    RESOURCE_TYPE_USER,
    SCHEMA_RESOURCE_TYPE,
    SCHEMA_SCHEMA,
    SCHEMA_SERVICE_PROVIDER_CONFIG,
    SCHEMA_USER,
    SCIM_SERVICE_PROVIDER_CONFIG,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

SCIM_PREFIX = "_matrix/client/unstable/coop.yaal/scim"

logger = logging.getLogger(__name__)


class SCIMResource(JsonResource):
    """The REST resource which gets mounted at
    /_matrix/client/unstable/coop.yaal/scim"""

    def __init__(self, hs: "HomeServer"):
        JsonResource.__init__(self, hs, canonical_json=False)
        register_servlets(hs, self)


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    SchemaListServlet(hs).register(http_server)
    SchemaServlet(hs).register(http_server)
    ResourceTypeListServlet(hs).register(http_server)
    ResourceTypeServlet(hs).register(http_server)
    ServiceProviderConfigServlet(hs).register(http_server)

    UserListServlet(hs).register(http_server)
    UserServlet(hs).register(http_server)


# TODO: test requests with additional/wrong attributes
# TODO: take inspiration from tests/rest/admin/test_user.py
# TODO: test user passwords after creation/update


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

    def absolute_meta_location(self, payload: JsonDict) -> JsonDict:
        prefix = self.config.server.public_baseurl + SCIM_PREFIX
        if not payload["meta"]["location"].startswith(prefix):
            payload["meta"]["location"] = prefix + payload["meta"]["location"]
        return payload

    def make_list_response_payload(
        self, items, start_index=1, count=None, total_results=None
    ):
        return {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
            "totalResults": total_results or len(items),
            "itemsPerPage": count or len(items),
            "startIndex": start_index,
            "Resources": items,
        }

    def make_error_response(self, status, message):
        return status, {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "status": status.value if isinstance(status, HTTPStatus) else status,
            "detail": message,
        }

    def parse_pagination_params(self, request):
        start_index = parse_integer(request, "startIndex", default=1, negative=True)
        count = parse_integer(
            request, "count", default=self.default_nb_items_per_page, negative=True
        )

        # RFC7644 §3.4.2.4
        #    A value less than 1 SHALL be interpreted as 1.
        #
        # https://datatracker.ietf.org/doc/html/rfc7644#section-3.4.2.4
        if start_index < 1:
            start_index = 1

        # RFC7644 §3.4.2.4
        #    A negative value SHALL be interpreted as 0.
        #
        # https://datatracker.ietf.org/doc/html/rfc7644#section-3.4.2.4
        if count < 0:
            count = 0

        return start_index, count

    async def get_user_data(self, user_id: str):
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

        location = f"{self.config.server.public_baseurl}{SCIM_PREFIX}/Users/{user_id}"
        creation_datetime = datetime.datetime.fromtimestamp(user.creation_ts)
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "meta": {
                "resourceType": "User",
                "created": creation_datetime.isoformat(),
                "lastModified": creation_datetime.isoformat(),
                "location": location,
            },
            "id": user_id,
            "externalId": user_id,
            "userName": user_id_obj.localpart,
            "active": not user.is_deactivated,
        }

        for threepid in threepids:
            if threepid.medium == "email":
                payload.setdefault("emails", []).append({"value": threepid.address})

            if threepid.medium == "msisdn":
                payload.setdefault("phoneNumbers", []).append(
                    {"value": threepid.address}
                )

        if profile.display_name:
            payload["displayName"] = profile.display_name

        if profile.avatar_url:
            payload["photos"] = [{
                "type": "photo",
                "primary": True,
                "value": profile.avatar_url,
            }]

        return payload


class UserServlet(SCIMServlet):
    PATTERNS = [re.compile(f"^/{SCIM_PREFIX}/Users/(?P<user_id>[^/]*)")]

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        try:
            payload = await self.get_user_data(user_id)
            return HTTPStatus.OK, payload
        except SynapseError as exc:
            return self.make_error_response(exc.code, exc.msg)

    async def on_DELETE(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester)
        deactivate_account_handler = self.hs.get_deactivate_account_handler()
        is_admin = await self.auth.is_server_admin(requester)
        try:
            await deactivate_account_handler.deactivate_account(
                user_id, erase_data=True, requester=requester, by_admin=is_admin
            )
        except SynapseError as exc:
            return self.make_error_response(exc.code, exc.msg)

        return HTTPStatus.NO_CONTENT, ""

    async def on_PUT(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester)

        body = parse_json_object_from_request(request)
        try:
            user_id_obj = UserID.from_string(user_id)

            threepids = await self.store.user_get_threepids(user_id)

            default_display_name = body.get("displayName", "")
            await self.profile_handler.set_displayname(
                user_id_obj, requester, default_display_name, True
            )

            avatar_url = body["photos"][0]["value"] if body.get("photos") else ""
            await self.profile_handler.set_avatar_url(
                user_id_obj, requester, avatar_url, True
            )

            if threepids is not None:
                new_threepids = {
                    ("email", email["value"]) for email in body["emails"]
                } | {
                    ("msisdn", phone_number["value"])
                    for phone_number in body["phoneNumbers"]
                }
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

            payload = await self.get_user_data(user_id)
            return HTTPStatus.OK, payload

        except SynapseError as exc:
            return self.make_error_response(exc.code, exc.msg)


class UserListServlet(SCIMServlet):
    PATTERNS = [re.compile(f"^/{SCIM_PREFIX}/Users/?$")]

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        try:
            await assert_requester_is_admin(self.auth, request)
            start_index, count = self.parse_pagination_params(request)

            items, total = await self.store.get_users_paginate(
                start=start_index - 1,
                limit=count,
            )
            users = [await self.get_user_data(item.name) for item in items]
            payload = self.make_list_response_payload(
                users, start_index=start_index, count=count, total_results=total
            )
            return HTTPStatus.OK, payload

        except SynapseError as exc:
            return self.make_error_response(exc.code, exc.msg)

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        try:
            requester = await self.auth.get_user_by_req(request)
            await assert_user_is_admin(self.auth, requester)

            body = parse_json_object_from_request(request)

            from synapse.rest.client.register import RegisterRestServlet

            register = RegisterRestServlet(self.hs)

            registration_arguments = {
                "by_admin": True,
                "approved": True,
                "localpart": body["userName"],
            }

            if password := body.get("password"):
                registration_arguments["password_hash"] = await self.auth_handler.hash(
                    password
                )

            if display_name := body.get("displayName"):
                registration_arguments["default_display_name"] = display_name

            user_id = await register.registration_handler.register_user(
                **registration_arguments
            )

            await register._create_registration_details(
                user_id,
                body,
                should_issue_refresh_token=True,
            )

            now_ts = self.hs.get_clock().time_msec()
            for email in body.get("emails", []):
                await self.store.user_add_threepid(
                    user_id, "email", email["value"], now_ts, now_ts
                )

            for phone_number in body.get("phoneNumbers", []):
                await self.store.user_add_threepid(
                    user_id, "msisdn", phone_number["value"], now_ts, now_ts
                )

            avatar_url = body["photos"][0]["value"] if body.get("photos") else None
            if avatar_url:
                await self.profile_handler.set_avatar_url(
                    UserID.from_string(user_id), requester, avatar_url, True
                )

            payload = await self.get_user_data(user_id)
            return HTTPStatus.CREATED, payload

        except SynapseError as exc:
            return self.make_error_response(exc.code, exc.msg)


class ServiceProviderConfigServlet(SCIMServlet):
    PATTERNS = [re.compile(f"^/{SCIM_PREFIX}/ServiceProviderConfig$")]

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        return HTTPStatus.OK, SCIM_SERVICE_PROVIDER_CONFIG


class SchemaListServlet(SCIMServlet):
    PATTERNS = [re.compile(f"^/{SCIM_PREFIX}/Schemas$")]

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        start_index, count = self.parse_pagination_params(request)
        resources = [
            self.absolute_meta_location(SCHEMA_SERVICE_PROVIDER_CONFIG),
            self.absolute_meta_location(SCHEMA_RESOURCE_TYPE),
            self.absolute_meta_location(SCHEMA_SCHEMA),
            self.absolute_meta_location(SCHEMA_USER),
        ]
        return HTTPStatus.OK, self.make_list_response_payload(
            resources, start_index=start_index, count=count
        )


class SchemaServlet(SCIMServlet):
    PATTERNS = [re.compile(f"^/{SCIM_PREFIX}/Schemas/(?P<schema_id>[^/]*)$")]

    async def on_GET(
        self, request: SynapseRequest, schema_id: str
    ) -> Tuple[int, JsonDict]:
        schemas = {
            "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig": SCHEMA_SERVICE_PROVIDER_CONFIG,
            "urn:ietf:params:scim:schemas:core:2.0:ResourceType": SCHEMA_RESOURCE_TYPE,
            "urn:ietf:params:scim:schemas:core:2.0:Schema": SCHEMA_SCHEMA,
            "urn:ietf:params:scim:schemas:core:2.0:User": SCHEMA_USER,
        }
        try:
            return HTTPStatus.OK, self.absolute_meta_location(schemas[schema_id])
        except KeyError:
            return self.make_error_response(HTTPStatus.NOT_FOUND, "Object not found")


class ResourceTypeListServlet(SCIMServlet):
    PATTERNS = [re.compile(f"^/{SCIM_PREFIX}/ResourceTypes$")]

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        start_index, count = self.parse_pagination_params(request)
        resources = [self.absolute_meta_location(RESOURCE_TYPE_USER)]
        return HTTPStatus.OK, self.make_list_response_payload(
            resources, start_index=start_index, count=count
        )


class ResourceTypeServlet(SCIMServlet):
    PATTERNS = [re.compile(f"^/{SCIM_PREFIX}/ResourceTypes/(?P<resource_type>[^/]*)$")]

    async def on_GET(
        self, request: SynapseRequest, resource_type: str
    ) -> Tuple[int, JsonDict]:
        resource_types = {
            "User": RESOURCE_TYPE_USER,
        }
        try:
            return HTTPStatus.OK, self.absolute_meta_location(
                resource_types[resource_type]
            )
        except KeyError:
            return self.make_error_response(HTTPStatus.NOT_FOUND, "Object not found")
