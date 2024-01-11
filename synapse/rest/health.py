#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from twisted.web.resource import Resource
from twisted.web.server import Request


class HealthResource(Resource):
    """A resource that does nothing except return a 200 with a body of `OK`,
    which can be used as a health check.

    Note: `SynapseRequest._should_log_request` ensures that requests to
    `/health` do not get logged at INFO.
    """

    isLeaf = 1

    def render_GET(self, request: Request) -> bytes:
        request.setHeader(b"Content-Type", b"text/plain")
        return b"OK"
