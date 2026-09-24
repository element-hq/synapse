#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
# Copyright 2017 Vector Creations Ltd
# Copyright 2016 OpenMarket Ltd
# Copyright (C) 2023-2024 New Vector, Ltd
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
import re
from typing import TYPE_CHECKING

from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class VersionsRestServlet(RestServlet):
    PATTERNS = [re.compile("^/_matrix/client/versions$")]
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.config = hs.config
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.rust_handlers = hs.get_rust_handlers()

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        user_id = None
        if self.auth.has_access_token(request):
            requester = await self.auth.get_user_by_req(
                request,
                allow_guest=True,
                allow_locked=True,
                allow_expired=True,
            )
            user_id = requester.user.to_string()
        else:
            # Allow caching of unauthenticated responses, as they only depend
            # on server configuration which rarely changes.
            #
            # - `public` means it can be cached both in the browser and in caching proxies
            # - `max-age` controls how long we cache on the browser side. 10m is sane enough
            # - `s-maxage` controls how long we cache on the proxy side. Since caching
            #   proxies usually have a way to purge caches, it is fine to cache there for
            #   longer (1h), and issue cache invalidations in case we need it
            # - `stale-while-revalidate` allows caching proxies to serve stale content while
            #   revalidating in the background. This is useful for making this request always
            #   'snappy' to end users whilst still keeping it fresh
            request.setHeader(
                b"Cache-Control",
                b"public, max-age=600, s-maxage=3600, stale-while-revalidate=600",
            )

        # Tell caches to vary on the Authorization header, so that
        # authenticated responses are not served from cache.
        request.setHeader(b"Vary", b"Authorization")

        versions_response_body = await self.rust_handlers.versions.get_versions(user_id)

        return (
            200,
            versions_response_body,
        )


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    VersionsRestServlet(hs).register(http_server)
