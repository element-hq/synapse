#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""The fermion arbiter over federation: a member's homeserver relays its
submissions here (see `synapse.rest.client.fermion`)."""

from typing import TYPE_CHECKING, Literal

from synapse.api.urls import FEDERATION_UNSTABLE_PREFIX
from synapse.federation.transport.server._base import (
    Authenticator,
    BaseFederationServlet,
)
from synapse.http.servlet import parse_integer_from_args
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

PATH = "/org.matrix.experimental.fermion.v1/rooms/(?P<room_id>[^/]*)"


class FermionCommitsServlet(BaseFederationServlet):
    PREFIX = FEDERATION_UNSTABLE_PREFIX
    PATH = PATH + "/commits"
    CATEGORY = "Federation requests"

    def __init__(
        self,
        hs: "HomeServer",
        authenticator: Authenticator,
        ratelimiter: object,
        server_name: str,
    ):
        super().__init__(hs, authenticator, ratelimiter, server_name)  # type: ignore[arg-type]
        self.arbiter = hs.get_fermion_arbiter()

    async def on_PUT(
        self,
        origin: str,
        content: JsonDict,
        query: dict[bytes, list[bytes]],
        room_id: str,
    ) -> tuple[int, JsonDict]:
        return 200, self.arbiter.submit(room_id, content, origin)


class FermionRulingsServlet(BaseFederationServlet):
    PREFIX = FEDERATION_UNSTABLE_PREFIX
    PATH = PATH + "/rulings"
    CATEGORY = "Federation requests"

    def __init__(
        self,
        hs: "HomeServer",
        authenticator: Authenticator,
        ratelimiter: object,
        server_name: str,
    ):
        super().__init__(hs, authenticator, ratelimiter, server_name)  # type: ignore[arg-type]
        self.arbiter = hs.get_fermion_arbiter()

    async def on_GET(
        self,
        origin: str,
        content: Literal[None],
        query: dict[bytes, list[bytes]],
        room_id: str,
    ) -> tuple[int, JsonDict]:
        since = parse_integer_from_args(query, "from", default=0)
        return 200, {"rulings": self.arbiter.rulings(room_id, since)}


FERMION_SERVLET_CLASSES: tuple[type[BaseFederationServlet], ...] = (
    FermionCommitsServlet,
    FermionRulingsServlet,
)
