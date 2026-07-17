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

import logging
import re
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.http.appservice_proxy import send_federation_request_from_appservice
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

ALLOWED_METHODS = ("GET", "PUT", "POST", "DELETE")


class AppserviceFederationProxyRestServlet(RestServlet):
    PATTERNS = [
        re.compile(
            r"^/_matrix/client/unstable/io.element.msc4512/appservice/fed_proxy$"
        )
    ]

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)

        app_service = (
            self.store.get_app_service_by_id(requester.app_service_id)
            if requester.app_service_id
            else None
        )

        if not app_service:
            raise SynapseError(
                HTTPStatus.FORBIDDEN,
                "Only application services can use this endpoint",
                Codes.FORBIDDEN,
            )

        content = parse_json_object_from_request(request)

        destination = content.get("destination")
        if not isinstance(destination, str) or not destination:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Missing or invalid destination",
                Codes.MISSING_PARAM,
            )

        method = content.get("method")
        if method not in ALLOWED_METHODS:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                f"method must be one of {ALLOWED_METHODS}",
                Codes.INVALID_PARAM,
            )

        path = content.get("path")
        if not isinstance(path, str) or not path:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Missing or invalid path",
                Codes.MISSING_PARAM,
            )

        body = content.get("body")
        if body is not None and not isinstance(body, dict):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "body must be an object",
                Codes.INVALID_PARAM,
            )
        if body is not None and method in ("GET", "DELETE"):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                f"'body' is not supported for {method}",
                Codes.INVALID_PARAM,
            )

        query = content.get("query")
        if query is not None and not isinstance(query, dict):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "query must be an object",
                Codes.INVALID_PARAM,
            )
        if query is not None and any(
            not isinstance(value, str) for value in query.values()
        ):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "'query' values must be strings",
                Codes.INVALID_PARAM,
            )

        status, response_content = await send_federation_request_from_appservice(
            self.hs,
            app_service,
            method,
            destination,
            path,
            body,
            query,
        )

        return HTTPStatus.OK, {"status": status, "content": response_content}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if not hs.config.experimental.msc4512_enabled:
        return

    AppserviceFederationProxyRestServlet(hs).register(http_server)
