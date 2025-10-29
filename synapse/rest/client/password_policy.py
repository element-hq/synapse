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
from typing import TYPE_CHECKING

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.types import JsonDict

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class PasswordPolicyServlet(RestServlet):
    PATTERNS = client_patterns("/password_policy$")
    CATEGORY = "Registration/login requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()

        self.policy = hs.config.auth.password_policy
        self.enabled = hs.config.auth.password_policy_enabled

    def on_GET(self, request: Request) -> tuple[int, JsonDict]:
        if not self.enabled or not self.policy:
            return 200, {}

        policy = {}

        for param in [
            "minimum_length",
            "require_digit",
            "require_symbol",
            "require_lowercase",
            "require_uppercase",
        ]:
            if param in self.policy:
                policy["m.%s" % param] = self.policy[param]

        return 200, policy


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    PasswordPolicyServlet(hs).register(http_server)
