#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from http.client import TEMPORARY_REDIRECT
from typing import TYPE_CHECKING, Optional

from synapse.api.errors import Codes, SynapseError
from synapse.http.server import HttpServer, respond_with_json, respond_with_redirect
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.http.client import read_body_with_max_size
from synapse.rest.client._base import client_patterns
from synapse.synapse_rust.rendezvous import RendezVous

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class MSC3886RendezvousServlet(RestServlet):
    """
    This is a placeholder implementation of [MSC3886](https://github.com/matrix-org/matrix-spec-proposals/pull/3886)
    simple client rendezvous capability that is used by the "Sign in with QR" functionality.

    This implementation only serves as a 307 redirect to a configured server rather than being a full implementation.

    A module that implements the full functionality is available at: https://pypi.org/project/matrix-http-rendezvous-synapse/.

    Request:

    POST /rendezvous HTTP/1.1
    Content-Type: ...

    ...

    Response:

    HTTP/1.1 307
    Location: <configured endpoint>
    """

    PATTERNS = client_patterns(
        "/org.matrix.msc3886/rendezvous$", releases=[], v1=False, unstable=True
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        redirection_target: Optional[str] = hs.config.experimental.msc3886_endpoint
        assert (
            redirection_target is not None
        ), "Servlet is only registered if there is a redirection target"
        self.endpoint = redirection_target.encode("utf-8")

    async def on_POST(self, request: SynapseRequest) -> None:
        respond_with_redirect(
            request, self.endpoint, statusCode=TEMPORARY_REDIRECT, cors=True
        )

    # PUT, GET and DELETE are not implemented as they should be fulfilled by the redirect target.


class MSC4108RendezvousServlet(RestServlet):
    PATTERNS = client_patterns(
        "/org.matrix.msc4108/rendezvous$", releases=[], v1=False, unstable=True
    )

    def __init__(self, store: RendezVous) -> None:
        super().__init__()
        self._store = store

    def on_POST(self, request: SynapseRequest) -> None:
        self._store.handle_post(request)

class MSC4108RendezvousSessionServlet(RestServlet):
    PATTERNS = client_patterns(
        "/org.matrix.msc4108/rendezvous/(?P<session_id>[^/]+)$",
        releases=[],
        v1=False,
        unstable=True,
    )

    def __init__(self, store: RendezVous) -> None:
        super().__init__()
        self._store = store

    def on_GET(self, request: SynapseRequest, session_id: str) -> None:
        self._store.handle_get(request, session_id)

    def on_PUT(self, request: SynapseRequest, session_id: str) -> None:
        self._store.handle_put(request, session_id)

    def on_DELETE(self, request: SynapseRequest, session_id: str) -> None:
        self._store.handle_delete(request, session_id)


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.experimental.msc3886_endpoint is not None:
        MSC3886RendezvousServlet(hs).register(http_server)

    # TODO: gate this behind a feature flag and store the rendezvous object in the HS
    rendezvous = RendezVous()
    MSC4108RendezvousServlet(rendezvous).register(http_server)
    MSC4108RendezvousSessionServlet(rendezvous).register(http_server)
