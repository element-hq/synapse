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
from urllib.parse import parse_qs

from synapse.api.errors import NotFoundError
from synapse.http.server import (
    DirectServeJsonResource,
    set_headers_for_media_response,
)
from synapse.http.servlet import (
    parse_integer,
    parse_integer_from_args,
    parse_string,
    parse_string_from_args,
)
from synapse.media.thumbnailer import ThumbnailProvider

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


class ThumbnailResource(DirectServeJsonResource):
    """
    Serves thumbnails from the media repository, with a temporary signed URL
    which expires after a set amount of time.

        GET /_synapse/media/thumbnail/{media_id}/height={height}&width={width}&...?exp={exp}&sig={sig}

    The intent of this resource is to allow the federation and client media APIs
    to issue redirects to a signed URL that can then be cached by a CDN. This
    endpoint doesn't require any extra header, and is authenticated using the
    signature in the URL parameters.

    The reason the `height`, `width` and other parameters are not part of the
    query string but instead part of the path is to ignoring the query string
    when caching the URL, which is possible with some CDNs.
    """

    isLeaf = True

    def __init__(self, hs: "HomeServer"):
        assert hs.config.media.can_load_media_repo, (
            "This resource should only be mounted on workers that can load the media repo"
        )

        DirectServeJsonResource.__init__(
            self,
            # It is useful to have the tracing context on this endpoint as it
            # can help debug federation issues
            extract_context=True,
        )

        self._clock = hs.get_clock()
        self._media_repository = hs.get_media_repository()
        self._dynamic_thumbnails = hs.config.media.dynamic_thumbnails
        self._thumbnailer = ThumbnailProvider(
            hs, self._media_repository, self._media_repository.media_storage
        )

    async def _async_render_GET(self, request: "SynapseRequest") -> None:
        set_headers_for_media_response(request)

        # Extract the media ID and parameters from the path
        if request.postpath is None or len(request.postpath) != 2:
            raise NotFoundError()

        media_id = request.postpath[0].decode("utf-8")
        parameters = request.postpath[1]

        # Get the `exp` and `sig` query parameters
        exp = parse_integer(request=request, name="exp", required=True, negative=False)
        sig = parse_string(request=request, name="sig", required=True)

        # Check that the signature is valid
        key = self._media_repository.thumbnail_media_key(
            media_id=media_id,
            parameters=parameters.decode("utf-8"),
            exp=exp,
        )
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

        # Now parse and check the parameters
        args = parse_qs(parameters)
        width = parse_integer_from_args(args, "width", required=True)
        height = parse_integer_from_args(args, "height", required=True)
        method = parse_string_from_args(args, "method", "scale")
        m_type = parse_string_from_args(args, "type", "image/png")

        # Reply with the thumbnail
        if self._dynamic_thumbnails:
            await self._thumbnailer.select_or_generate_local_thumbnail(
                request,
                media_id,
                width,
                height,
                method,
                m_type,
                max_timeout_ms=0,  # If we got here, the media finished uploading
                for_federation=False,  # This changes the response to be multipart; we explicitly don't want that
                may_redirect=False,  # We're already on the redirected URL, we don't want to redirect again
            )
        else:
            await self._thumbnailer.respond_local_thumbnail(
                request,
                media_id,
                width,
                height,
                method,
                m_type,
                max_timeout_ms=0,  # If we got here, the media finished uploading
                for_federation=False,  # This changes the response to be multipart; we explicitly don't want that
                may_redirect=False,  # We're already on the redirected URL, we don't want to redirect again
            )
