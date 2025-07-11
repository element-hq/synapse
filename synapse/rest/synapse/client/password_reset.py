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
from typing import TYPE_CHECKING, Tuple

from twisted.web.server import Request

from synapse.api.errors import ThreepidValidationError
from synapse.http.server import DirectServeHtmlResource
from synapse.http.servlet import parse_string
from synapse.util.stringutils import assert_valid_client_secret

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class PasswordResetSubmitTokenResource(DirectServeHtmlResource):
    """Handles 3PID validation token submission

    This resource gets mounted under /_synapse/client/password_reset/email/submit_token
    """

    isLeaf = 1

    def __init__(self, hs: "HomeServer"):
        """
        Args:
            hs: server
        """
        super().__init__(clock=hs.get_clock())

        self.clock = hs.get_clock()
        self.store = hs.get_datastores().main

        self._confirmation_email_template = (
            hs.config.email.email_password_reset_template_confirmation_html
        )
        self._email_password_reset_template_success_html = (
            hs.config.email.email_password_reset_template_success_html_content
        )
        self._failure_email_template = (
            hs.config.email.email_password_reset_template_failure_html
        )

        # This resource should only be mounted if email validation is enabled
        assert hs.config.email.can_verify_email

    async def _async_render_GET(self, request: Request) -> Tuple[int, bytes]:
        sid = parse_string(request, "sid", required=True)
        token = parse_string(request, "token", required=True)
        client_secret = parse_string(request, "client_secret", required=True)
        assert_valid_client_secret(client_secret)

        # Show a confirmation page, just in case someone accidentally clicked this link when
        # they didn't mean to
        template_vars = {
            "sid": sid,
            "token": token,
            "client_secret": client_secret,
        }
        return (
            200,
            self._confirmation_email_template.render(**template_vars).encode("utf-8"),
        )

    async def _async_render_POST(self, request: Request) -> Tuple[int, bytes]:
        sid = parse_string(request, "sid", required=True)
        token = parse_string(request, "token", required=True)
        client_secret = parse_string(request, "client_secret", required=True)

        # Attempt to validate a 3PID session
        try:
            # Mark the session as valid
            next_link = await self.store.validate_threepid_session(
                sid, client_secret, token, self.clock.time_msec()
            )

            # Perform a 302 redirect if next_link is set
            if next_link:
                if next_link.startswith("file:///"):
                    logger.warning(
                        "Not redirecting to next_link as it is a local file: address"
                    )
                else:
                    next_link_bytes = next_link.encode("utf-8")
                    request.setHeader("Location", next_link_bytes)
                    return (
                        302,
                        (
                            b'You are being redirected to <a href="%s">%s</a>.'
                            % (next_link_bytes, next_link_bytes)
                        ),
                    )

            # Otherwise show the success template
            html_bytes = self._email_password_reset_template_success_html.encode(
                "utf-8"
            )
            status_code = 200
        except ThreepidValidationError as e:
            status_code = e.code

            # Show a failure page with a reason
            template_vars = {"failure_reason": e.msg}
            html_bytes = self._failure_email_template.render(**template_vars).encode(
                "utf-8"
            )

        return status_code, html_bytes
