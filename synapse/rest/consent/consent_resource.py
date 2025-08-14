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

import hmac
import logging
from hashlib import sha256
from http import HTTPStatus
from os import path
from typing import TYPE_CHECKING, Any, Dict, List

import jinja2
from jinja2 import TemplateNotFound

from twisted.web.server import Request

from synapse.api.errors import NotFoundError, StoreError, SynapseError
from synapse.config import ConfigError
from synapse.http.server import DirectServeHtmlResource, respond_with_html
from synapse.http.servlet import parse_bytes_from_args, parse_string
from synapse.types import UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

# language to use for the templates. TODO: figure this out from Accept-Language
TEMPLATE_LANGUAGE = "en"

logger = logging.getLogger(__name__)


class ConsentResource(DirectServeHtmlResource):
    """A twisted Resource to display a privacy policy and gather consent to it

    When accessed via GET, returns the privacy policy via a template.

    When accessed via POST, records the user's consent in the database and
    displays a success page.

    The config should include a template_dir setting which contains templates
    for the HTML. The directory should contain one subdirectory per language
    (eg, 'en', 'fr'), and each language directory should contain the policy
    document (named as '<version>.html') and a success page (success.html).

    Both forms take a set of parameters from the browser. For the POST form,
    these are normally sent as form parameters (but may be query-params); for
    GET requests they must be query params. These are:

        u: the complete mxid, or the localpart of the user giving their
           consent. Required for both GET (where it is used as an input to the
           template) and for POST (where it is used to find the row in the db
           to update).

        h: hmac_sha256(secret, u), where 'secret' is the privacy_secret in the
           config file. If it doesn't match, the request is 403ed.

        v: the version of the privacy policy being agreed to.

           For GET: optional, and defaults to whatever was set in the config
           file. Used to choose the version of the policy to pick from the
           templates directory.

           For POST: required; gives the value to be recorded in the database
           against the user.
    """

    def __init__(self, hs: "HomeServer"):
        super().__init__(clock=hs.get_clock())

        self.hs = hs
        self.store = hs.get_datastores().main
        self.registration_handler = hs.get_registration_handler()

        # this is required by the request_handler wrapper
        self.clock = hs.get_clock()

        # Consent must be configured to create this resource.
        default_consent_version = hs.config.consent.user_consent_version
        consent_template_directory = hs.config.consent.user_consent_template_dir
        if default_consent_version is None or consent_template_directory is None:
            raise ConfigError(
                "Consent resource is enabled but user_consent section is "
                "missing in config file."
            )
        self._default_consent_version = default_consent_version

        # TODO: switch to synapse.util.templates.build_jinja_env
        loader = jinja2.FileSystemLoader(consent_template_directory)
        self._jinja_env = jinja2.Environment(
            loader=loader, autoescape=jinja2.select_autoescape(["html", "htm", "xml"])
        )

        if hs.config.key.form_secret is None:
            raise ConfigError(
                "Consent resource is enabled but form_secret is not set in "
                "config file. It should be set to an arbitrary secret string."
            )

        self._hmac_secret = hs.config.key.form_secret.encode("utf-8")

    async def _async_render_GET(self, request: Request) -> None:
        version = parse_string(request, "v", default=self._default_consent_version)
        username = parse_string(request, "u", default="")
        userhmac = None
        has_consented = False
        public_version = username == ""
        if not public_version:
            args: Dict[bytes, List[bytes]] = request.args  # type: ignore
            userhmac_bytes = parse_bytes_from_args(args, "h", required=True)

            self._check_hash(username, userhmac_bytes)

            if username.startswith("@"):
                qualified_user_id = username
            else:
                qualified_user_id = UserID(username, self.hs.hostname).to_string()

            u = await self.store.get_user_by_id(qualified_user_id)
            if u is None:
                raise NotFoundError("Unknown user")

            has_consented = u.consent_version == version
            userhmac = userhmac_bytes.decode("ascii")

        try:
            self._render_template(
                request,
                "%s.html" % (version,),
                user=username,
                userhmac=userhmac,
                version=version,
                has_consented=has_consented,
                public_version=public_version,
            )
        except TemplateNotFound:
            raise NotFoundError("Unknown policy version")

    async def _async_render_POST(self, request: Request) -> None:
        version = parse_string(request, "v", required=True)
        username = parse_string(request, "u", required=True)
        args: Dict[bytes, List[bytes]] = request.args  # type: ignore
        userhmac = parse_bytes_from_args(args, "h", required=True)

        self._check_hash(username, userhmac)

        if username.startswith("@"):
            qualified_user_id = username
        else:
            qualified_user_id = UserID(username, self.hs.hostname).to_string()

        try:
            await self.store.user_set_consent_version(qualified_user_id, version)
        except StoreError as e:
            if e.code != 404:
                raise
            raise NotFoundError("Unknown user")
        await self.registration_handler.post_consent_actions(qualified_user_id)

        try:
            self._render_template(request, "success.html")
        except TemplateNotFound:
            raise NotFoundError("success.html not found")

    def _render_template(
        self, request: Request, template_name: str, **template_args: Any
    ) -> None:
        # get_template checks for ".." so we don't need to worry too much
        # about path traversal here.
        template_html = self._jinja_env.get_template(
            path.join(TEMPLATE_LANGUAGE, template_name)
        )
        html = template_html.render(**template_args)
        respond_with_html(request, 200, html)

    def _check_hash(self, userid: str, userhmac: bytes) -> None:
        """
        Args:
            userid:
            userhmac:

        Raises:
              SynapseError if the hash doesn't match

        """
        want_mac = (
            hmac.new(
                key=self._hmac_secret, msg=userid.encode("utf-8"), digestmod=sha256
            )
            .hexdigest()
            .encode("ascii")
        )

        if not hmac.compare_digest(want_mac, userhmac):
            raise SynapseError(HTTPStatus.FORBIDDEN, "HMAC incorrect")
