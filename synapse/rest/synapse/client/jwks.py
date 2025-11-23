#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

from synapse.http.server import DirectServeJsonResource
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class JwksResource(DirectServeJsonResource):
    def __init__(self, hs: "HomeServer"):
        super().__init__(clock=hs.get_clock(), extract_context=True)

        # Parameters that are allowed to be exposed in the public key.
        # This is done manually, because authlib's private to public key conversion
        # is unreliable depending on the version. Instead, we just serialize the private
        # key and only keep the public parameters.
        # List from https://www.iana.org/assignments/jose/jose.xhtml#web-key-parameters
        public_parameters = {
            "kty",
            "use",
            "key_ops",
            "alg",
            "kid",
            "x5u",
            "x5c",
            "x5t",
            "x5t#S256",
            "crv",
            "x",
            "y",
            "n",
            "e",
            "ext",
        }

        key = hs.config.experimental.msc3861.jwk

        if key is not None:
            private_key = key.as_dict()
            public_key = {
                k: v for k, v in private_key.items() if k in public_parameters
            }
            keys = [public_key]
        else:
            keys = []

        self.res = {
            "keys": keys,
        }

    async def _async_render_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        return 200, self.res
