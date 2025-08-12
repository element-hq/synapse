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

from twisted.web.resource import Resource

from .download import DownloadResource

if TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


class MediaResource(Resource):
    """
    Provides endpoints for signed media downloads and thumbnails.

    All endpoints are mounted under the path `/_synapse/media/` and only work
    on the media worker.
    """

    def __init__(self, hs: "HomeServer"):
        assert hs.config.media.can_load_media_repo, (
            "This resource should only be mounted on workers that can load the media repo"
        )

        Resource.__init__(self)
        self.putChild(b"download", DownloadResource(hs))
