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
from typing import TYPE_CHECKING

from synapse.api.ratelimiting import RequestRatelimiter
from synapse.appservice import ApplicationService
from synapse.http import QuieterFileBodyProducer
from synapse.http.appservice_proxy import proxy_request_to_appservice
from synapse.http.server import HttpServer, ServletCallback
from synapse.http.site import SynapseRequest

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


def _make_proxy_callback(
    hs: "HomeServer",
    ratelimiter: RequestRatelimiter,
    appservice: ApplicationService,
) -> ServletCallback:
    async def _proxy(request: SynapseRequest, **kwargs: str) -> None:
        requester = await hs.get_auth().get_user_by_req(request)

        await ratelimiter.ratelimit(requester)

        await proxy_request_to_appservice(
            request,
            hs,
            appservice,
            QuieterFileBodyProducer(request.content),
            extra_request_headers={
                b"X-Matrix-User-Identifier": requester.user.to_string().encode("ascii")
            },
        )

    return _proxy


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    """Registers blanket reverse-proxy routes for each application service that has
    configured a proxy prefix. This forwards requests under
    /_matrix/client/<version>/<prefix>/* (where <version> is either "vN" or
    "unstable/<prefix>") to the same path under the application service's
    configured proxy URL after verifying request authentication.
    """
    if not hs.config.experimental.msc4512_enabled:
        return

    ratelimiter = hs.get_request_ratelimiter()
    for appservice in hs.get_datastores().main.get_app_services():
        if appservice.proxy_prefix is None:
            continue

        pattern = re.compile(
            r"^/_matrix/client/(?:unstable/[^/]+|v[^/]+)/%s(/.*)?$"
            % (re.escape(appservice.proxy_prefix),)
        )
        callback = _make_proxy_callback(hs, ratelimiter, appservice)

        for method in ("GET", "POST", "PUT", "DELETE"):
            http_server.register_paths(
                method,
                (pattern,),
                callback,
                "ApplicationServiceClientProxy",
            )
