#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

"""Contains the URL paths to prefix various aspects of the server with. """
import hmac
from hashlib import sha256
from urllib.parse import urlencode

from synapse.config import ConfigError
from synapse.config.homeserver import HomeServerConfig

SYNAPSE_CLIENT_API_PREFIX = "/_synapse/client"
CLIENT_API_PREFIX = "/_matrix/client"
FEDERATION_PREFIX = "/_matrix/federation"
FEDERATION_V1_PREFIX = FEDERATION_PREFIX + "/v1"
FEDERATION_V2_PREFIX = FEDERATION_PREFIX + "/v2"
FEDERATION_UNSTABLE_PREFIX = FEDERATION_PREFIX + "/unstable"
STATIC_PREFIX = "/_matrix/static"
SERVER_KEY_PREFIX = "/_matrix/key"
MEDIA_R0_PREFIX = "/_matrix/media/r0"
MEDIA_V3_PREFIX = "/_matrix/media/v3"
LEGACY_MEDIA_PREFIX = "/_matrix/media/v1"


class ConsentURIBuilder:
    def __init__(self, hs_config: HomeServerConfig):
        if hs_config.key.form_secret is None:
            raise ConfigError("form_secret not set in config")
        self._hmac_secret = hs_config.key.form_secret.encode("utf-8")
        self._public_baseurl = hs_config.server.public_baseurl

    def build_user_consent_uri(self, user_id: str) -> str:
        """Build a URI which we can give to the user to do their privacy
        policy consent

        Args:
            user_id: mxid or username of user

        Returns
            The URI where the user can do consent
        """
        mac = hmac.new(
            key=self._hmac_secret, msg=user_id.encode("ascii"), digestmod=sha256
        ).hexdigest()
        consent_uri = "%s_matrix/consent?%s" % (
            self._public_baseurl,
            urlencode({"u": user_id, "h": mac}),
        )
        return consent_uri
