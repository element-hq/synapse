#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from typing import TYPE_CHECKING

import saml2.metadata

from twisted.web.resource import Resource
from twisted.web.server import Request

if TYPE_CHECKING:
    from synapse.server import HomeServer


class SAML2MetadataResource(Resource):
    """A Twisted web resource which renders the SAML metadata"""

    isLeaf = 1

    def __init__(self, hs: "HomeServer"):
        Resource.__init__(self)
        self.sp_config = hs.config.saml2.saml2_sp_config

    def render_GET(self, request: Request) -> bytes:
        metadata_xml = saml2.metadata.create_metadata_string(
            configfile=None, config=self.sp_config
        )
        request.setHeader(b"Content-Type", b"text/xml; charset=utf-8")
        return metadata_xml
