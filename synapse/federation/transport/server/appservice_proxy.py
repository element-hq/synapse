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
from io import BytesIO
from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.appservice import ApplicationService
from synapse.federation.transport.server._base import Authenticator
from synapse.http import QuieterFileBodyProducer
from synapse.http.appservice_proxy import proxy_request_to_appservice
from synapse.http.server import HttpServer, ServletCallback
from synapse.http.site import SynapseRequest
from synapse.util.json import json_decoder
from synapse.util.ratelimitutils import FederationRateLimiter

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


def _make_proxy_callback(
    hs: "HomeServer",
    authenticator: Authenticator,
    ratelimiter: FederationRateLimiter,
    appservice: ApplicationService,
) -> ServletCallback:
    async def _proxy(request: SynapseRequest, **kwargs: str) -> None:
        raw_body = request.content.read()  # type: ignore[union-attr]

        content = None
        if request.method in (b"PUT", b"POST"):
            try:
                content = json_decoder.decode(raw_body.decode("utf-8"))
            except Exception:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST, "Content not JSON.", Codes.NOT_JSON
                )

        origin = await authenticator.authenticate_request(request, content)

        # Apply the same per-origin rate limiting that every other federation endpoint gets.
        with ratelimiter.ratelimit(origin) as d:
            await d
            if request._disconnected:
                logger.warning(
                    "client disconnected before we started processing request"
                )
                return

            await proxy_request_to_appservice(
                request,
                hs,
                appservice,
                QuieterFileBodyProducer(BytesIO(raw_body)),
                extra_request_headers={b"X-Matrix-Origin": origin.encode("ascii")},
            )

    return _proxy


def register_servlets(
    hs: "HomeServer",
    resource: HttpServer,
    authenticator: Authenticator,
    ratelimiter: FederationRateLimiter,
) -> None:
    """Registers blanket reverse-proxy routes for each application service that has
    configured a proxy prefix. This forwards requests under
    /_matrix/federation/<version>/<prefix>/* (where <version> is either "vN"
    or "unstable/<prefix>") to the same path under the application service's
    proxy URL after verifying request authentication.
    """
    if not hs.config.experimental.msc4512_enabled:
        return

    for appservice in hs.get_datastores().main.get_app_services():
        if appservice.proxy_prefix is None or appservice.proxy_url is None:
            continue

        pattern = re.compile(
            r"^/_matrix/federation/(?:unstable/[^/]+|v[^/]+)/%s(/.*)?$"
            % (re.escape(appservice.proxy_prefix),)
        )
        callback = _make_proxy_callback(hs, authenticator, ratelimiter, appservice)

        for method in ("GET", "POST", "PUT", "DELETE"):
            resource.register_paths(
                method,
                (pattern,),
                callback,
                "ApplicationServiceFederationProxy",
            )
