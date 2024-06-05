#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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
#

""" This module contains REST servlets to do with futures: /futures/<paths> """
import logging
from typing import TYPE_CHECKING, List, Tuple

from synapse.api.errors import NotFoundError
from synapse.api.ratelimiting import Ratelimiter
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# TODO: Needs unit testing
class FuturesTokenServlet(RestServlet):
    PATTERNS = client_patterns(
        r"/org\.matrix\.msc4140/futures/(?P<token>[^/]*)$", releases=(), v1=False
    )
    CATEGORY = "Future management requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.futures_handler = hs.get_futures_handler()

        self._ratelimiter = Ratelimiter(
            store=hs.get_datastores().main,
            clock=hs.get_clock(),
            cfg=hs.config.ratelimiting.rc_future_token_validity,
        )

    async def on_POST(
        self, request: SynapseRequest, token: str
    ) -> Tuple[int, JsonDict]:
        # Ratelimit by address, since this is an unauthenticated request
        ratelimit_key = (request.getClientAddress().host,)
        # Check if we should be ratelimited due to too many previous failed attempts
        await self._ratelimiter.ratelimit(None, ratelimit_key, update=False)

        try:
            await self.futures_handler.use_future_token(token)
            return 200, {}
        except NotFoundError as e:
            # Update the ratelimiter to say we failed (`can_do_action` doesn't raise).
            await self._ratelimiter.can_do_action(None, ratelimit_key)
            # TODO: Decide if the error code should be left at 404, instead of 410 as per the MSC
            e.code = 410
            raise


# TODO: Needs unit testing
class FuturesServlet(RestServlet):
    PATTERNS = client_patterns(r"/org\.matrix\.msc4140/futures$", releases=(), v1=False)
    CATEGORY = "Future management requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.futures_handler = hs.get_futures_handler()

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, List[JsonDict]]:
        requester = await self.auth.get_user_by_req(request)
        return 200, await self.futures_handler.get_all_futures_for_user(requester)


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    FuturesTokenServlet(hs).register(http_server)
    FuturesServlet(hs).register(http_server)
