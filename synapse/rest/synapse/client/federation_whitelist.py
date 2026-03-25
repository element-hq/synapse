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

import logging
from typing import TYPE_CHECKING

from synapse.http.server import DirectServeJsonResource
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class FederationWhitelistResource(DirectServeJsonResource):
    """Custom endpoint (disabled by default) to fetch the federation whitelist
    config.

    Only enabled if `federation_whitelist_endpoint_enabled` feature is enabled.

    Response format:

        {
            "whitelist_enabled": true,  // Whether the federation whitelist is being enforced
            "whitelist": [  // Which server names are allowed by the whitelist
                "example.com"
            ]
        }
    """

    PATH = "/_synapse/client/v1/config/federation_whitelist"

    def __init__(self, hs: "HomeServer"):
        super().__init__(clock=hs.get_clock())

        self._federation_whitelist = hs.config.federation.federation_domain_whitelist

        self._auth = hs.get_auth()

    async def _async_render_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await self._auth.get_user_by_req(request)

        whitelist = []
        if self._federation_whitelist:
            # federation_whitelist is actually a dict, not a list
            whitelist = list(self._federation_whitelist)

        return_dict: JsonDict = {
            "whitelist_enabled": self._federation_whitelist is not None,
            "whitelist": whitelist,
        }

        return 200, return_dict
