# Copyright 2023 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import typing
from typing import cast

from synapse.api.auth.mas import MasDelegatedAuth
from synapse.api.errors import Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict

if typing.TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


class AuthIssuerServlet(RestServlet):
    """
    Advertises what OpenID Connect issuer clients should use to authorise users.
    This endpoint was defined in a previous iteration of MSC2965, and is still
    used by some clients.
    """

    PATTERNS = client_patterns(
        "/org.matrix.msc2965/auth_issuer$",
        unstable=True,
        releases=(),
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._config = hs.config
        self._auth = hs.get_auth()

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        if self._config.mas.enabled:
            assert isinstance(self._auth, MasDelegatedAuth)
            return 200, {"issuer": await self._auth.issuer()}

        elif self._config.experimental.msc3861.enabled:
            # If MSC3861 is enabled, we can assume self._auth is an instance of MSC3861DelegatedAuth
            # We import lazily here because of the authlib requirement
            from synapse.api.auth.msc3861_delegated import MSC3861DelegatedAuth

            assert isinstance(self._auth, MSC3861DelegatedAuth)
            return 200, {"issuer": await self._auth.issuer()}

        else:
            # Wouldn't expect this to be reached: the servelet shouldn't have been
            # registered. Still, fail gracefully if we are registered for some reason.
            raise SynapseError(
                404,
                "OIDC discovery has not been configured on this homeserver",
                Codes.NOT_FOUND,
            )


class AuthMetadataServlet(RestServlet):
    """
    Advertises the OAuth 2.0 server metadata for the homeserver.
    """

    PATTERNS = [
        *client_patterns(
            "/auth_metadata$",
            releases=("v1",),
        ),
        *client_patterns(
            "/org.matrix.msc2965/auth_metadata$",
            unstable=True,
            releases=(),
        ),
    ]

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._config = hs.config
        self._auth = hs.get_auth()

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        if self._config.mas.enabled:
            assert isinstance(self._auth, MasDelegatedAuth)
            return 200, await self._auth.auth_metadata()

        elif self._config.experimental.msc3861.enabled:
            # If MSC3861 is enabled, we can assume self._auth is an instance of MSC3861DelegatedAuth
            # We import lazily here because of the authlib requirement
            from synapse.api.auth.msc3861_delegated import MSC3861DelegatedAuth

            auth = cast(MSC3861DelegatedAuth, self._auth)
            return 200, await auth.auth_metadata()

        else:
            # Wouldn't expect this to be reached: the servlet shouldn't have been
            # registered. Still, fail gracefully if we are registered for some reason.
            raise SynapseError(
                404,
                "OIDC discovery has not been configured on this homeserver",
                Codes.NOT_FOUND,
            )


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.mas.enabled or hs.config.experimental.msc3861.enabled:
        AuthIssuerServlet(hs).register(http_server)
        AuthMetadataServlet(hs).register(http_server)
