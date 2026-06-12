#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from typing import TYPE_CHECKING

from synapse.http.server import DirectServeHtmlResource, respond_with_html
from synapse.http.site import SynapseRequest

if TYPE_CHECKING:
    from synapse.server import HomeServer

# The path at which the fallback media upload limit info page is served. This is
# used as the `info_uri` returned in the `M_USER_LIMIT_EXCEEDED` error when no
# `info_uri` has been configured for a media upload limit.
MEDIA_UPLOAD_LIMIT_PATH = "/_synapse/client/media_upload_limit"


class MediaUploadLimitResource(DirectServeHtmlResource):
    """Serves a static fallback page explaining that a media upload limit has
    been exceeded.

    This is used as the `info_uri` for the `M_USER_LIMIT_EXCEEDED` error when a
    media upload limit has been configured without an explicit `info_uri`.
    """

    def __init__(self, hs: "HomeServer"):
        super().__init__(clock=hs.get_clock())
        self._template = hs.config.media.media_upload_limit_exceeded_template

    async def _async_render_GET(self, request: SynapseRequest) -> None:
        assert self._template is not None
        respond_with_html(request, 200, self._template.render())
