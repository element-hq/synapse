#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Quentin Gliech
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

from synapse.http.server import DirectServeHtmlResource
from synapse.http.site import SynapseRequest

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class OIDCCallbackResource(DirectServeHtmlResource):
    isLeaf = 1

    def __init__(self, hs: "HomeServer"):
        super().__init__(clock=hs.get_clock())
        self._oidc_handler = hs.get_oidc_handler()

    async def _async_render_GET(self, request: SynapseRequest) -> None:
        await self._oidc_handler.handle_oidc_callback(request)

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        # the auth response can be returned via an x-www-form-urlencoded form instead
        # of GET params, as per
        # https://openid.net/specs/oauth-v2-form-post-response-mode-1_0.html.
        await self._oidc_handler.handle_oidc_callback(request)
