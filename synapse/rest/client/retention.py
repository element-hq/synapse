#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from http import HTTPStatus
from typing import TYPE_CHECKING

from typing_extensions import TypedDict

from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer


class RetentionPolicyDict(TypedDict, total=False):
    min_lifetime: int
    max_lifetime: int


class LifetimeBoundsDict(TypedDict, total=False):
    min: int
    max: int


class RetentionLimitsDict(TypedDict, total=False):
    max_lifetime: LifetimeBoundsDict


class RetentionConfigurationResponse(TypedDict):
    policies: dict[str, RetentionPolicyDict]
    limits: RetentionLimitsDict


class RetentionConfigurationServlet(RestServlet):
    """Implements MSC1763: /_matrix/client/unstable/org.matrix.msc1763/retention/configuration"""

    PATTERNS = client_patterns(
        "/org.matrix.msc1763/retention/configuration$",
        releases=[],
        v1=False,
        unstable=True,
    )
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self._retention_config = hs.config.retention

    async def on_GET(
        self, request: SynapseRequest
    ) -> tuple[int, RetentionConfigurationResponse]:
        await self.auth.get_user_by_req(request)

        default_policy: RetentionPolicyDict = {}
        if self._retention_config.retention_default_min_lifetime is not None:
            default_policy["min_lifetime"] = (
                self._retention_config.retention_default_min_lifetime
            )
        if self._retention_config.retention_default_max_lifetime is not None:
            default_policy["max_lifetime"] = (
                self._retention_config.retention_default_max_lifetime
            )

        max_lifetime_limits: LifetimeBoundsDict = {}
        if self._retention_config.retention_allowed_lifetime_min is not None:
            max_lifetime_limits["min"] = (
                self._retention_config.retention_allowed_lifetime_min
            )
        if self._retention_config.retention_allowed_lifetime_max is not None:
            max_lifetime_limits["max"] = (
                self._retention_config.retention_allowed_lifetime_max
            )

        limits: RetentionLimitsDict = {}
        if max_lifetime_limits:
            limits["max_lifetime"] = max_lifetime_limits

        policies: dict[str, RetentionPolicyDict] = {}
        if default_policy:
            policies["*"] = default_policy

        return HTTPStatus.OK, {"policies": policies, "limits": limits}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.retention.retention_enabled and hs.config.experimental.msc1763_enabled:
        RetentionConfigurationServlet(hs).register(http_server)
