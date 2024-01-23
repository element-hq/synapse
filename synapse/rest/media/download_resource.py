#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020-2021 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
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
import re
from typing import TYPE_CHECKING, Optional

from synapse.http.server import set_corp_headers, set_cors_headers
from synapse.http.servlet import RestServlet, parse_boolean, parse_integer
from synapse.http.site import SynapseRequest
from synapse.media._base import (
    DEFAULT_MAX_TIMEOUT_MS,
    MAXIMUM_ALLOWED_MAX_TIMEOUT_MS,
    respond_404,
)
from synapse.util.stringutils import parse_and_validate_server_name

if TYPE_CHECKING:
    from synapse.media.media_repository import MediaRepository
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class DownloadResource(RestServlet):
    PATTERNS = [
        re.compile(
            "/_matrix/media/(r0|v3|v1)/download/(?P<server_name>[^/]*)/(?P<media_id>[^/]*)(/(?P<file_name>[^/]*))?$"
        )
    ]

    def __init__(self, hs: "HomeServer", media_repo: "MediaRepository"):
        super().__init__()
        self.media_repo = media_repo
        self._is_mine_server_name = hs.is_mine_server_name

    async def on_GET(
        self,
        request: SynapseRequest,
        server_name: str,
        media_id: str,
        file_name: Optional[str] = None,
    ) -> None:
        # Validate the server name, raising if invalid
        parse_and_validate_server_name(server_name)

        set_cors_headers(request)
        set_corp_headers(request)
        request.setHeader(
            b"Content-Security-Policy",
            b"sandbox;"
            b" default-src 'none';"
            b" script-src 'none';"
            b" plugin-types application/pdf;"
            b" style-src 'unsafe-inline';"
            b" media-src 'self';"
            b" object-src 'self';",
        )
        # Limited non-standard form of CSP for IE11
        request.setHeader(b"X-Content-Security-Policy", b"sandbox;")
        request.setHeader(b"Referrer-Policy", b"no-referrer")
        max_timeout_ms = parse_integer(
            request, "timeout_ms", default=DEFAULT_MAX_TIMEOUT_MS
        )
        max_timeout_ms = min(max_timeout_ms, MAXIMUM_ALLOWED_MAX_TIMEOUT_MS)

        if self._is_mine_server_name(server_name):
            await self.media_repo.get_local_media(
                request, media_id, file_name, max_timeout_ms
            )
        else:
            allow_remote = parse_boolean(request, "allow_remote", default=True)
            if not allow_remote:
                logger.info(
                    "Rejecting request for remote media %s/%s due to allow_remote",
                    server_name,
                    media_id,
                )
                respond_404(request)
                return

            await self.media_repo.get_remote_media(
                request, server_name, media_id, file_name, max_timeout_ms
            )
