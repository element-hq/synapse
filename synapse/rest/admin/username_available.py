#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
from http import HTTPStatus
from typing import TYPE_CHECKING, Tuple

from synapse.http.servlet import RestServlet, parse_string
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class UsernameAvailableRestServlet(RestServlet):
    """An admin API to check if a given username is available, regardless of whether registration is enabled.

    Example:
        GET /_synapse/admin/v1/username_available?username=foo
        200 OK
        {
            "available": true
        }
    """

    PATTERNS = admin_patterns("/username_available$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.registration_handler = hs.get_registration_handler()

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        username = parse_string(request, "username", required=True)
        await self.registration_handler.check_username(username)
        return HTTPStatus.OK, {"available": True}
