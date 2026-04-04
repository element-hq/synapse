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

import base64
import hashlib
import hmac
import logging
from typing import TYPE_CHECKING
from urllib.parse import quote

from synapse.http.client import SimpleHttpClient
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


def _validate_turn_credentials_response(
    response: JsonDict, requested_ttl: int | None = None
) -> JsonDict:
    """Validate a Matrix TURN credentials response shape."""

    username = response.get("username")
    password = response.get("password")
    ttl = response.get("ttl")
    uris = response.get("uris")

    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("TURN credentials response did not include credentials")

    if not isinstance(ttl, int) or ttl <= 0:
        raise ValueError("TURN credentials response did not include a valid ttl")

    if requested_ttl is not None and ttl > requested_ttl:
        raise ValueError("TURN credentials response ttl exceeded the requested ttl")

    if not isinstance(uris, list) or not uris or not all(
        isinstance(uri, str) and uri.startswith(("turn:", "turns:")) for uri in uris
    ):
        raise ValueError("TURN credentials response did not include TURN URIs")

    return {
        "username": username,
        "password": password,
        "ttl": ttl,
        "uris": uris,
    }


def _parse_cloudflare_turn_response(response: JsonDict, ttl: int) -> JsonDict:
    """Convert Cloudflare TURN API credentials into the Matrix VoIP response shape."""

    ice_servers = response.get("iceServers")
    if not isinstance(ice_servers, list):
        raise ValueError("Cloudflare TURN response did not include an iceServers list")

    turn_uris: list[str] = []
    username: str | None = None
    password: str | None = None

    for ice_server in ice_servers:
        if not isinstance(ice_server, dict):
            continue

        urls = ice_server.get("urls")
        if isinstance(urls, str):
            candidate_urls = [urls]
        elif isinstance(urls, list):
            candidate_urls = [url for url in urls if isinstance(url, str)]
        else:
            continue

        candidate_turn_uris = [
            url
            for url in candidate_urls
            if url.startswith(("turn:", "turns:"))
            and ":53" not in url.split("?", 1)[0]
        ]
        if not candidate_turn_uris:
            continue

        ice_username = ice_server.get("username")
        ice_password = ice_server.get("credential")
        if not isinstance(ice_username, str) or not isinstance(ice_password, str):
            continue

        if username is None:
            username = ice_username
            password = ice_password
        elif username != ice_username or password != ice_password:
            raise ValueError("Cloudflare TURN response contained multiple credentials")

        for uri in candidate_turn_uris:
            if uri not in turn_uris:
                turn_uris.append(uri)

    if username is None or password is None or not turn_uris:
        raise ValueError("Cloudflare TURN response did not include TURN credentials")

    return {
        "username": username,
        "password": password,
        "ttl": ttl,
        "uris": turn_uris,
    }


class VoipRestServlet(RestServlet):
    PATTERNS = client_patterns("/voip/turnServer$", v1=True)
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.http_client: SimpleHttpClient = hs.get_proxied_http_client()

    async def _get_turn_broker_credentials(self, ttl: int) -> JsonDict | None:
        if not self.hs.config.voip.turn_federation_deployment:
            return None

        broker_url = self.hs.config.voip.turn_broker_url
        if not broker_url:
            return None

        api_token = self.hs.config.voip.turn_broker_api_token
        headers = None
        if api_token:
            headers = {b"Authorization": [f"Bearer {api_token}".encode("ascii")]}

        response = await self.http_client.post_json_get_json(
            broker_url,
            {"ttl": ttl},
            headers=headers,
        )

        if not isinstance(response, dict):
            raise ValueError("TURN broker returned a non-object response")

        return _validate_turn_credentials_response(response, ttl)

    async def _get_cloudflare_turn_credentials(self, ttl: int) -> JsonDict | None:
        if not self.hs.config.voip.turn_cloudflare_enabled:
            return None

        key_id = self.hs.config.voip.turn_cloudflare_key_id
        api_token = self.hs.config.voip.turn_cloudflare_api_token
        if not key_id or not api_token:
            return None

        uri = (
            f"{self.hs.config.voip.turn_cloudflare_api_base_url}/turn/keys/"
            f"{quote(key_id, safe='')}/credentials/generate-ice-servers"
        )
        response = await self.http_client.post_json_get_json(
            uri,
            {"ttl": ttl},
            headers={b"Authorization": [f"Bearer {api_token}".encode("ascii")]},
        )

        if not isinstance(response, dict):
            raise ValueError("Cloudflare TURN API returned a non-object response")

        return _parse_cloudflare_turn_response(response, ttl)

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(
            request, self.hs.config.voip.turn_allow_guests
        )

        turnUris = self.hs.config.voip.turn_uris
        turnSecret = self.hs.config.voip.turn_shared_secret
        turnUsername = self.hs.config.voip.turn_username
        turnPassword = self.hs.config.voip.turn_password
        userLifetime = self.hs.config.voip.turn_user_lifetime

        if self.hs.config.voip.turn_federation_deployment:
            try:
                broker_response = await self._get_turn_broker_credentials(
                    userLifetime // 1000
                )
            except Exception:
                logger.warning(
                    "Failed to fetch TURN credentials from the configured broker; "
                    "falling back to the configured TURN server",
                    exc_info=True,
                )
            else:
                if broker_response is not None:
                    return 200, broker_response
        elif self.hs.config.voip.turn_cloudflare_enabled:
            try:
                cloudflare_response = await self._get_cloudflare_turn_credentials(
                    userLifetime // 1000
                )
            except Exception:
                logger.warning(
                    "Failed to fetch Cloudflare TURN credentials; falling back to "
                    "the configured TURN server",
                    exc_info=True,
                )
            else:
                if cloudflare_response is not None:
                    return 200, cloudflare_response

        if turnUris and turnSecret and userLifetime:
            expiry = (self.hs.get_clock().time_msec() + userLifetime) / 1000
            username = "%d:%s" % (expiry, requester.user.to_string())

            mac = hmac.new(
                turnSecret.encode(), msg=username.encode(), digestmod=hashlib.sha1
            )
            # We need to use standard padded base64 encoding here
            # encode_base64 because we need to add the standard padding to get the
            # same result as the TURN server.
            password = base64.b64encode(mac.digest()).decode("ascii")

        elif turnUris and turnUsername and turnPassword and userLifetime:
            username = turnUsername
            password = turnPassword

        else:
            return 200, {}

        return (
            200,
            {
                "username": username,
                "password": password,
                "ttl": userLifetime // 1000,
                "uris": turnUris,
            },
        )


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    VoipRestServlet(hs).register(http_server)
