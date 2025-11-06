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

"""Contains the URL paths to prefix various aspects of the server with."""

import hmac
import urllib.parse
from hashlib import sha256
from urllib.parse import urlencode, urljoin

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


class LoginSSORedirectURIBuilder:
    def __init__(self, hs_config: HomeServerConfig):
        self._public_baseurl = hs_config.server.public_baseurl

    def build_login_sso_redirect_uri(
        self, *, idp_id: str | None, client_redirect_url: str
    ) -> str:
        """Build a `/login/sso/redirect` URI for the given identity provider.

        Builds `/_matrix/client/v3/login/sso/redirect/{idpId}?redirectUrl=xxx` when `idp_id` is specified.
        Otherwise, builds `/_matrix/client/v3/login/sso/redirect?redirectUrl=xxx` when `idp_id` is `None`.

        Args:
            idp_id: Optional ID of the identity provider
            client_redirect_url: URL to redirect the user to after login

        Returns
            The URI to follow when choosing a specific identity provider.
        """
        base_url = urljoin(
            self._public_baseurl,
            f"{CLIENT_API_PREFIX}/v3/login/sso/redirect",
        )

        serialized_query_parameters = urlencode({"redirectUrl": client_redirect_url})

        if idp_id:
            # Since this is a user-controlled string, make it safe to include in a URL path.
            url_encoded_idp_id = urllib.parse.quote(
                idp_id,
                # Since this defaults to `safe="/"`, we have to override it. We're
                # working with an individual URL path parameter so there shouldn't be
                # any slashes in it which could change the request path.
                safe="",
                encoding="utf8",
            )

            resultant_url = urljoin(
                # We have to add a trailing slash to the base URL to ensure that the
                # last path segment is not stripped away when joining with another path.
                f"{base_url}/",
                f"{url_encoded_idp_id}?{serialized_query_parameters}",
            )
        else:
            resultant_url = f"{base_url}?{serialized_query_parameters}"

        return resultant_url
