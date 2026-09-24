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

from twisted.web.server import Request

from synapse.http.server import HttpServer, respond_with_html
from synapse.http.servlet import RestServlet, parse_string
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class AccountValidityRenewServlet(RestServlet):
    PATTERNS = client_patterns("/account_validity/renew$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()

        self.hs = hs
        self.account_activity_handler = hs.get_account_validity_handler()
        self.auth = hs.get_auth()
        self.account_renewed_template = (
            hs.config.account_validity.account_validity_account_renewed_template
        )
        self.account_previously_renewed_template = hs.config.account_validity.account_validity_account_previously_renewed_template
        self.invalid_token_template = (
            hs.config.account_validity.account_validity_invalid_token_template
        )

    async def on_GET(self, request: Request) -> None:
        renewal_token = parse_string(request, "token", required=True)

        (
            token_valid,
            token_stale,
            expiration_ts,
        ) = await self.account_activity_handler.renew_account(renewal_token)

        if token_valid:
            status_code = 200
            response = self.account_renewed_template.render(expiration_ts=expiration_ts)
        elif token_stale:
            status_code = 200
            response = self.account_previously_renewed_template.render(
                expiration_ts=expiration_ts
            )
        else:
            status_code = 404
            response = self.invalid_token_template.render(expiration_ts=expiration_ts)

        respond_with_html(request, status_code, response)


class AccountValiditySendMailServlet(RestServlet):
    PATTERNS = client_patterns("/account_validity/send_mail$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()

        self.hs = hs
        self.account_activity_handler = hs.get_account_validity_handler()
        self.auth = hs.get_auth()
        self.account_validity_renew_by_email_enabled = (
            hs.config.account_validity.account_validity_renew_by_email_enabled
        )

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_expired=True)
        user_id = requester.user.to_string()
        await self.account_activity_handler.send_renewal_email_to_user(user_id)

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    AccountValidityRenewServlet(hs).register(http_server)
    AccountValiditySendMailServlet(hs).register(http_server)
