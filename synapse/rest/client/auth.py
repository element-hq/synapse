#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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

from twisted.web.server import Request

from synapse.api.auth.mas import MasDelegatedAuth
from synapse.api.constants import LoginType
from synapse.api.errors import LoginError, SynapseError
from synapse.api.urls import CLIENT_API_PREFIX
from synapse.http.server import HttpServer, respond_with_html, respond_with_redirect
from synapse.http.servlet import RestServlet, parse_string
from synapse.http.site import SynapseRequest

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class AuthRestServlet(RestServlet):
    """
    Handles Client / Server API authentication in any situations where it
    cannot be handled in the normal flow (with requests to the same endpoint).
    Current use is for web fallback auth.
    """

    PATTERNS = client_patterns(r"/auth/(?P<stagetype>[\w\.]*)/fallback/web")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_auth_handler()
        self.registration_handler = hs.get_registration_handler()
        self.recaptcha_template = hs.config.captcha.recaptcha_template
        self.terms_template = hs.config.consent.terms_template
        self.registration_token_template = (
            hs.config.registration.registration_token_template
        )
        self.success_template = hs.config.registration.fallback_success_template

    async def on_GET(self, request: SynapseRequest, stagetype: str) -> None:
        session = parse_string(request, "session")
        if not session:
            raise SynapseError(400, "No session supplied")

        if stagetype == "org.matrix.cross_signing_reset":
            if self.hs.config.mas.enabled:
                assert isinstance(self.auth, MasDelegatedAuth)

                url = await self.auth.account_management_url()
                url = f"{url}?action=org.matrix.cross_signing_reset"
                return respond_with_redirect(
                    request,
                    url.encode(),
                )

            elif self.hs.config.experimental.msc3861.enabled:
                # If MSC3861 is enabled, we can assume self._auth is an instance of MSC3861DelegatedAuth
                # We import lazily here because of the authlib requirement
                from synapse.api.auth.msc3861_delegated import MSC3861DelegatedAuth

                assert isinstance(self.auth, MSC3861DelegatedAuth)

                base = await self.auth.account_management_url()
                if base is not None:
                    url = f"{base}?action=org.matrix.cross_signing_reset"
                else:
                    url = await self.auth.issuer()
                return respond_with_redirect(request, url.encode())

        if stagetype == LoginType.RECAPTCHA:
            html = self.recaptcha_template.render(
                session=session,
                myurl="%s/v3/auth/%s/fallback/web"
                % (CLIENT_API_PREFIX, LoginType.RECAPTCHA),
                sitekey=self.hs.config.captcha.recaptcha_public_key,
            )
        elif stagetype == LoginType.TERMS:
            html = self.terms_template.render(
                session=session,
                terms_url="%s_matrix/consent?v=%s"
                % (
                    self.hs.config.server.public_baseurl,
                    self.hs.config.consent.user_consent_version,
                ),
                myurl="%s/v3/auth/%s/fallback/web"
                % (CLIENT_API_PREFIX, LoginType.TERMS),
            )

        elif stagetype == LoginType.SSO:
            # Display a confirmation page which prompts the user to
            # re-authenticate with their SSO provider.
            html = await self.auth_handler.start_sso_ui_auth(request, session)

        elif stagetype == LoginType.REGISTRATION_TOKEN:
            html = self.registration_token_template.render(
                session=session,
                myurl=f"{CLIENT_API_PREFIX}/r0/auth/{LoginType.REGISTRATION_TOKEN}/fallback/web",
            )

        else:
            raise SynapseError(404, "Unknown auth stage type")

        # Render the HTML and return.
        respond_with_html(request, 200, html)
        return None

    async def on_POST(self, request: Request, stagetype: str) -> None:
        session = parse_string(request, "session")
        if not session:
            raise SynapseError(400, "No session supplied")

        if stagetype == LoginType.RECAPTCHA:
            response = parse_string(request, "g-recaptcha-response")

            if not response:
                raise SynapseError(400, "No captcha response supplied")

            authdict = {"response": response, "session": session}

            try:
                await self.auth_handler.add_oob_auth(
                    LoginType.RECAPTCHA, authdict, request.getClientAddress().host
                )
            except LoginError as e:
                # Authentication failed, let user try again
                html = self.recaptcha_template.render(
                    session=session,
                    myurl="%s/v3/auth/%s/fallback/web"
                    % (CLIENT_API_PREFIX, LoginType.RECAPTCHA),
                    sitekey=self.hs.config.captcha.recaptcha_public_key,
                    error=e.msg,
                )
            else:
                # No LoginError was raised, so authentication was successful
                html = self.success_template.render()

        elif stagetype == LoginType.TERMS:
            authdict = {"session": session}

            try:
                await self.auth_handler.add_oob_auth(
                    LoginType.TERMS, authdict, request.getClientAddress().host
                )
            except LoginError as e:
                # Authentication failed, let user try again
                html = self.terms_template.render(
                    session=session,
                    terms_url="%s_matrix/consent?v=%s"
                    % (
                        self.hs.config.server.public_baseurl,
                        self.hs.config.consent.user_consent_version,
                    ),
                    myurl="%s/v3/auth/%s/fallback/web"
                    % (CLIENT_API_PREFIX, LoginType.TERMS),
                    error=e.msg,
                )
            else:
                # No LoginError was raised, so authentication was successful
                html = self.success_template.render()

        elif stagetype == LoginType.SSO:
            # The SSO fallback workflow should not post here,
            raise SynapseError(404, "Fallback SSO auth does not support POST requests.")

        elif stagetype == LoginType.REGISTRATION_TOKEN:
            token = parse_string(request, "token", required=True)
            authdict = {"session": session, "token": token}

            try:
                await self.auth_handler.add_oob_auth(
                    LoginType.REGISTRATION_TOKEN,
                    authdict,
                    request.getClientAddress().host,
                )
            except LoginError as e:
                html = self.registration_token_template.render(
                    session=session,
                    myurl=f"{CLIENT_API_PREFIX}/r0/auth/{LoginType.REGISTRATION_TOKEN}/fallback/web",
                    error=e.msg,
                )
            else:
                html = self.success_template.render()

        else:
            raise SynapseError(404, "Unknown auth stage type")

        # Render the HTML and return.
        respond_with_html(request, 200, html)
        return None


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    AuthRestServlet(hs).register(http_server)
