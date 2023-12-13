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
import logging
from typing import TYPE_CHECKING, Optional

from twisted.web.resource import Resource
from twisted.web.server import Request

from synapse.http.server import set_cors_headers
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict
from synapse.util import json_encoder
from synapse.util.stringutils import parse_server_name

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class WellKnownBuilder:
    def __init__(self, hs: "HomeServer"):
        self._config = hs.config

    def get_well_known(self) -> Optional[JsonDict]:
        if not self._config.server.serve_client_wellknown:
            return None

        result = {"m.homeserver": {"base_url": self._config.server.public_baseurl}}

        if self._config.registration.default_identity_server:
            result["m.identity_server"] = {
                "base_url": self._config.registration.default_identity_server
            }

        # We use the MSC3861 values as they are used by multiple MSCs
        if self._config.experimental.msc3861.enabled:
            result["org.matrix.msc2965.authentication"] = {
                "issuer": self._config.experimental.msc3861.issuer
            }
            if self._config.experimental.msc3861.account_management_url is not None:
                result["org.matrix.msc2965.authentication"][
                    "account"
                ] = self._config.experimental.msc3861.account_management_url

        if self._config.server.extra_well_known_client_content:
            for (
                key,
                value,
            ) in self._config.server.extra_well_known_client_content.items():
                if key not in result:
                    result[key] = value

        return result


class ClientWellKnownResource(Resource):
    """A Twisted web resource which renders the .well-known/matrix/client file"""

    isLeaf = 1

    def __init__(self, hs: "HomeServer"):
        Resource.__init__(self)
        self._well_known_builder = WellKnownBuilder(hs)

    def render_GET(self, request: SynapseRequest) -> bytes:
        set_cors_headers(request)
        r = self._well_known_builder.get_well_known()
        if not r:
            request.setResponseCode(404)
            request.setHeader(b"Content-Type", b"text/plain")
            return b".well-known not available"

        logger.debug("returning: %s", r)
        request.setHeader(b"Content-Type", b"application/json")
        return json_encoder.encode(r).encode("utf-8")


class ServerWellKnownResource(Resource):
    """Resource for .well-known/matrix/server, redirecting to port 443"""

    isLeaf = 1

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._serve_server_wellknown = hs.config.server.serve_server_wellknown

        host, port = parse_server_name(hs.config.server.server_name)

        # If we've got this far, then https://<server_name>/ must route to us, so
        # we just redirect the traffic to port 443 instead of 8448.
        if port is None:
            port = 443

        self._response = json_encoder.encode({"m.server": f"{host}:{port}"}).encode(
            "utf-8"
        )

    def render_GET(self, request: Request) -> bytes:
        if not self._serve_server_wellknown:
            request.setResponseCode(404)
            request.setHeader(b"Content-Type", b"text/plain")
            return b"404. Is anything ever truly *well* known?\n"

        request.setHeader(b"Content-Type", b"application/json")
        return self._response


def well_known_resource(hs: "HomeServer") -> Resource:
    """Returns a Twisted web resource which handles '.well-known' requests"""
    res = Resource()
    matrix_resource = Resource()
    res.putChild(b"matrix", matrix_resource)

    matrix_resource.putChild(b"server", ServerWellKnownResource(hs))
    matrix_resource.putChild(b"client", ClientWellKnownResource(hs))

    return res
