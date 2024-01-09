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

import logging
from typing import TYPE_CHECKING

from twisted.web.resource import Resource

from synapse.rest.synapse.client.saml2.metadata_resource import SAML2MetadataResource
from synapse.rest.synapse.client.saml2.response_resource import SAML2ResponseResource

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class SAML2Resource(Resource):
    def __init__(self, hs: "HomeServer"):
        Resource.__init__(self)
        self.putChild(b"metadata.xml", SAML2MetadataResource(hs))
        self.putChild(b"authn_response", SAML2ResponseResource(hs))


__all__ = ["SAML2Resource"]
