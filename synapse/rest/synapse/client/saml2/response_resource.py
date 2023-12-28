#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from typing import TYPE_CHECKING

from twisted.web.server import Request

from synapse.http.server import DirectServeHtmlResource
from synapse.http.site import SynapseRequest

if TYPE_CHECKING:
    from synapse.server import HomeServer


class SAML2ResponseResource(DirectServeHtmlResource):
    """A Twisted web resource which handles the SAML response"""

    isLeaf = 1

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._saml_handler = hs.get_saml_handler()
        self._sso_handler = hs.get_sso_handler()

    async def _async_render_GET(self, request: Request) -> None:
        # We're not expecting any GET request on that resource if everything goes right,
        # but some IdPs sometimes end up responding with a 302 redirect on this endpoint.
        # In this case, just tell the user that something went wrong and they should
        # try to authenticate again.
        self._sso_handler.render_error(
            request, "unexpected_get", "Unexpected GET request on /saml2/authn_response"
        )

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        await self._saml_handler.handle_saml_response(request)
