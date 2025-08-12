#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl_3.0.html>.
#
#

import logging
from typing import TYPE_CHECKING

from synapse.api.errors import NotFoundError
from synapse.http.server import DirectServeJsonResource
from synapse.http.servlet import parse_integer, parse_string

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


class DownloadResource(DirectServeJsonResource):
    """
    Serves media from the media repository, with a temporary signed URL which
    expires after a set amount of time.

        GET /_synapse/media/download/{media_id}?exp={exp}&sig={sig}

    The intent of this resource is to allow the federation and client media APIs
    to issue redirects to a signed URL that can then be cached by a CDN. This
    endpoint doesn't require any extra header, and is authenticated using the
    signature in the URL parameters.
    """

    isLeaf = True

    def __init__(self, hs: "HomeServer"):
        assert hs.config.media.can_load_media_repo, (
            "This resource should only be mounted on workers that can load the media repo"
        )

        DirectServeJsonResource.__init__(self)

        self._clock = hs.get_clock()
        self._media_repository = hs.get_media_repository()

    async def _async_render_GET(self, request: "SynapseRequest") -> None:
        # Extract the media ID from the path
        if request.postpath is None or len(request.postpath) != 1:
            raise NotFoundError()
        media_id = request.postpath[0].decode("utf-8")

        # Get the `exp` and `sig` query parameters
        exp = parse_integer(request=request, name="exp", required=True, negative=False)
        sig = parse_string(request=request, name="sig", required=True)

        # Check that the signature is valid
        key = self._media_repository.download_media_key(media_id=media_id, exp=exp)
        if not self._media_repository.verify_media_request_signature(key, sig):
            logger.warning(
                "Invalid URL signature serving media %s. key: %r, sig: %r",
                media_id,
                key,
                sig,
            )
            raise NotFoundError()

        # Check the expiry time
        if exp < self._clock.time_msec():
            logger.info("Expired signed URL serving media %s", media_id)
            raise NotFoundError()

        # Reply with the media
        await self._media_repository.get_local_media(
            request=request,
            media_id=media_id,
            name=None,
            max_timeout_ms=0,  # If we got here, the media finished uploading
            federation=False,  # This changes the response to be multipart; we explicitly don't want that
        )
