#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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

from synapse.api.errors import SynapseError
from synapse.api.ratelimiting import Ratelimiter
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonMapping

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class UserDirectorySearchRestServlet(RestServlet):
    PATTERNS = client_patterns("/user_directory/search$")
    CATEGORY = "User directory search requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.user_directory_handler = hs.get_user_directory_handler()

        self._per_user_limiter = Ratelimiter(
            store=hs.get_datastores().main,
            clock=hs.get_clock(),
            cfg=hs.config.ratelimiting.rc_user_directory,
        )
        self._msc4258_enabled = hs.config.experimental.msc4258_enabled

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonMapping]:
        """Searches for users in directory

        Returns:
            dict of the form::

                {
                    "limited": <bool>,  # whether there were more results or not
                    "results": [  # Ordered by best match first
                        {
                            "user_id": <user_id>,
                            "display_name": <display_name>,
                            "avatar_url": <avatar_url>
                        }
                    ]
                }
        """
        requester = await self.auth.get_user_by_req(request, allow_guest=False)
        user_id = requester.user.to_string()

        if not self.hs.config.userdirectory.user_directory_search_enabled:
            return 200, {"limited": False, "results": []}

        await self._per_user_limiter.ratelimit(requester)

        body = parse_json_object_from_request(request)

        limit = int(body.get("limit", 10))
        limit = max(min(limit, 50), 0)

        try:
            search_term = body["search_term"]
        except Exception:
            raise SynapseError(400, "`search_term` is required field")

        # MSC4258 federated search parameters. `search_scope` is from the MSC
        # (local/restricted searches must not leave this server); `server` is
        # a targeted extension letting the client search one specific remote
        # server's directory (analogous to /publicRooms?server=).
        search_scope = body.get("search_scope", "remote")
        if search_scope not in ("local", "restricted", "remote"):
            raise SynapseError(400, "Invalid search_scope")
        server = body.get("server")
        if server is not None and not isinstance(server, str):
            raise SynapseError(400, "Invalid server")
        if server is not None and self.hs.is_mine_server_name(server):
            server = None

        federate = (
            self._msc4258_enabled
            and search_scope == "remote"
            # Mirror the remote side's anti-enumeration threshold rather than
            # sending queries that will be refused.
            and len(search_term) >= 4
        )

        if federate and server is not None:
            # The client asked for one specific server's directory: return
            # that server's results alone.
            results = await self.user_directory_handler.search_remote_users(
                user_id, search_term, limit, server
            )
            return 200, results

        results = await self.user_directory_handler.search_users(
            user_id, search_term, limit
        )

        if federate:
            remote_results = await self.user_directory_handler.search_remote_users(
                user_id, search_term, limit, None
            )
            return 200, _merge_search_results(results, remote_results, limit)

        return 200, results


def _merge_search_results(
    local_results: JsonMapping, remote_results: JsonMapping, limit: int
) -> JsonMapping:
    """Concatenate local and remote user directory results, deduplicating by
    user ID. Local results come first, preserving their relevance ordering.
    """
    seen = set()
    results = []
    for user in list(local_results["results"]) + list(remote_results["results"]):
        if user["user_id"] not in seen:
            seen.add(user["user_id"])
            results.append(user)

    limited = bool(local_results.get("limited")) or bool(remote_results.get("limited"))
    if len(results) > limit:
        results = results[:limit]
        limited = True

    return {"limited": limited, "results": results}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    UserDirectorySearchRestServlet(hs).register(http_server)
