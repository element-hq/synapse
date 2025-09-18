#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from synapse.api.urls import LoginSSORedirectURIBuilder
from synapse.http.server import (
    DirectServeHtmlResource,
    finish_request,
    respond_with_html,
)
from synapse.http.servlet import parse_string
from synapse.http.site import SynapseRequest

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class PickIdpResource(DirectServeHtmlResource):
    """IdP picker resource.

    This resource gets mounted under /_synapse/client/pick_idp. It serves an HTML page
    which prompts the user to choose an Identity Provider from the list.
    """

    def __init__(self, hs: "HomeServer"):
        super().__init__(clock=hs.get_clock())
        self._sso_handler = hs.get_sso_handler()
        self._sso_login_idp_picker_template = (
            hs.config.sso.sso_login_idp_picker_template
        )
        self._server_name = hs.hostname
        self._public_baseurl = hs.config.server.public_baseurl
        self._login_sso_redirect_url_builder = LoginSSORedirectURIBuilder(hs.config)

    async def _async_render_GET(self, request: SynapseRequest) -> None:
        client_redirect_url = parse_string(
            request, "redirectUrl", required=True, encoding="utf-8"
        )
        idp = parse_string(request, "idp", required=False)

        # If we need to pick an IdP, do so
        if not idp:
            return await self._serve_id_picker(request, client_redirect_url)

        # Validate the `idp` query parameter. We should only be working with known IdPs.
        # No need waste further effort if we don't know about it.
        #
        # Although, we primarily prevent open redirect attacks by URL encoding all of
        # the parameters we use in the redirect URL below, this validation also helps
        # prevent Synapse from crafting arbitrary URLs and being used in open redirect
        # attacks (defense in depth).
        providers = self._sso_handler.get_identity_providers()
        auth_provider = providers.get(idp)
        if not auth_provider:
            logger.info("Unknown idp %r", idp)
            self._sso_handler.render_error(
                request, "unknown_idp", "Unknown identity provider ID"
            )
            return

        # Otherwise, redirect to the login SSO redirect endpoint for the given IdP
        # (which will in turn take us to the the IdP's redirect URI).
        #
        # We could go directly to the IdP's redirect URI, but this way we ensure that
        # the user goes through the same logic as normal flow. Additionally, if a proxy
        # needs to intercept the request, it only needs to intercept the one endpoint.
        sso_login_redirect_url = (
            self._login_sso_redirect_url_builder.build_login_sso_redirect_uri(
                idp_id=idp, client_redirect_url=client_redirect_url
            )
        )
        logger.info("Redirecting to %s", sso_login_redirect_url)
        request.redirect(sso_login_redirect_url)
        finish_request(request)

    async def _serve_id_picker(
        self, request: SynapseRequest, client_redirect_url: str
    ) -> None:
        # otherwise, serve up the IdP picker
        providers = self._sso_handler.get_identity_providers()
        html = self._sso_login_idp_picker_template.render(
            redirect_url=client_redirect_url,
            server_name=self._server_name,
            providers=providers.values(),
        )
        respond_with_html(request, 200, html)
