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
#

import logging
from typing import TYPE_CHECKING

from synapse.api.errors import UnrecognizedRequestError
from synapse.http.server import DirectServeJsonResource
from synapse.http.site import SynapseRequest

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class MSC4108RendezvousSessionResource(DirectServeJsonResource):
    isLeaf = True

    def __init__(self, hs: "HomeServer") -> None:
        super().__init__(clock=hs.get_clock())
        self._handler = hs.get_rendezvous_handler()

    async def _async_render_GET(self, request: SynapseRequest) -> None:
        postpath: list[bytes] = request.postpath  # type: ignore
        if len(postpath) != 1:
            raise UnrecognizedRequestError()
        session_id = postpath[0].decode("ascii")

        self._handler.handle_get(request, session_id)

    def _async_render_PUT(self, request: SynapseRequest) -> None:
        postpath: list[bytes] = request.postpath  # type: ignore
        if len(postpath) != 1:
            raise UnrecognizedRequestError()
        session_id = postpath[0].decode("ascii")

        self._handler.handle_put(request, session_id)

    def _async_render_DELETE(self, request: SynapseRequest) -> None:
        postpath: list[bytes] = request.postpath  # type: ignore
        if len(postpath) != 1:
            raise UnrecognizedRequestError()
        session_id = postpath[0].decode("ascii")

        self._handler.handle_delete(request, session_id)
