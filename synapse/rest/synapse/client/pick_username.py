#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, Generator

from twisted.web.resource import Resource
from twisted.web.server import Request

from synapse.api.errors import SynapseError
from synapse.handlers.sso import get_username_mapping_session_cookie_from_request
from synapse.http.server import (
    DirectServeHtmlResource,
    DirectServeJsonResource,
    respond_with_html,
)
from synapse.http.servlet import parse_boolean, parse_string
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict
from synapse.util.templates import build_jinja_env

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


def pick_username_resource(hs: "HomeServer") -> Resource:
    """Factory method to generate the username picker resource.

    This resource gets mounted under /_synapse/client/pick_username and has two
       children:

      * "account_details": renders the form and handles the POSTed response
      * "check": a JSON endpoint which checks if a userid is free.
    """

    res = Resource()
    res.putChild(b"account_details", AccountDetailsResource(hs))
    res.putChild(b"check", AvailabilityCheckResource(hs))

    return res


class AvailabilityCheckResource(DirectServeJsonResource):
    def __init__(self, hs: "HomeServer"):
        super().__init__(clock=hs.get_clock())
        self._sso_handler = hs.get_sso_handler()

    async def _async_render_GET(self, request: Request) -> tuple[int, JsonDict]:
        localpart = parse_string(request, "username", required=True)

        session_id = get_username_mapping_session_cookie_from_request(request)

        is_available = await self._sso_handler.check_username_availability(
            localpart, session_id
        )
        return 200, {"available": is_available}


class AccountDetailsResource(DirectServeHtmlResource):
    def __init__(self, hs: "HomeServer"):
        super().__init__(clock=hs.get_clock())
        self._sso_handler = hs.get_sso_handler()

        def template_search_dirs() -> Generator[str, None, None]:
            if hs.config.server.custom_template_directory:
                yield hs.config.server.custom_template_directory
            if hs.config.sso.sso_template_dir:
                yield hs.config.sso.sso_template_dir
            yield hs.config.sso.default_template_dir

        self._jinja_env = build_jinja_env(list(template_search_dirs()), hs.config)

    async def _async_render_GET(self, request: Request) -> None:
        try:
            session_id = get_username_mapping_session_cookie_from_request(request)
            session = self._sso_handler.get_mapping_session(session_id)
        except SynapseError as e:
            logger.warning("Error fetching session: %s", e)
            self._sso_handler.render_error(request, "bad_session", e.msg, code=e.code)
            return

        # The configuration might mandate going through this step to validate an
        # automatically generated localpart, so session.chosen_localpart might already
        # be set.
        localpart = ""
        if session.chosen_localpart is not None:
            localpart = session.chosen_localpart

        idp_id = session.auth_provider_id
        template_params = {
            "idp": self._sso_handler.get_identity_providers()[idp_id],
            "user_attributes": {
                "display_name": session.display_name,
                "emails": session.emails,
                "localpart": localpart,
                "avatar_url": session.avatar_url,
            },
        }

        template = self._jinja_env.get_template("sso_auth_account_details.html")
        html = template.render(template_params)
        respond_with_html(request, 200, html)

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        # This will always be set by the time Twisted calls us.
        assert request.args is not None

        try:
            session_id = get_username_mapping_session_cookie_from_request(request)
        except SynapseError as e:
            logger.warning("Error fetching session cookie: %s", e)
            self._sso_handler.render_error(request, "bad_session", e.msg, code=e.code)
            return

        try:
            localpart = parse_string(request, "username", required=True)
            use_display_name = parse_boolean(request, "use_display_name", default=False)
            use_avatar = parse_boolean(request, "use_avatar", default=False)

            try:
                emails_to_use: list[str] = [
                    val.decode("utf-8") for val in request.args.get(b"use_email", [])
                ]
            except ValueError:
                raise SynapseError(400, "Query parameter use_email must be utf-8")
        except SynapseError as e:
            logger.warning("[session %s] bad param: %s", session_id, e)
            self._sso_handler.render_error(request, "bad_param", e.msg, code=e.code)
            return

        await self._sso_handler.handle_submit_username_request(
            request, session_id, localpart, use_display_name, use_avatar, emails_to_use
        )
