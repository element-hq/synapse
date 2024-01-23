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

from typing import TYPE_CHECKING

from synapse.api.errors import StoreError
from synapse.http.server import DirectServeHtmlResource, respond_with_html_bytes
from synapse.http.servlet import parse_string
from synapse.http.site import SynapseRequest

if TYPE_CHECKING:
    from synapse.server import HomeServer


class UnsubscribeResource(DirectServeHtmlResource):
    """
    To allow pusher to be delete by clicking a link (ie. GET request)
    """

    SUCCESS_HTML = b"<html><body>You have been unsubscribed</body><html>"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.notifier = hs.get_notifier()
        self.auth = hs.get_auth()
        self.pusher_pool = hs.get_pusherpool()
        self.macaroon_generator = hs.get_macaroon_generator()

    async def _async_render_GET(self, request: SynapseRequest) -> None:
        """
        Handle a user opening an unsubscribe link in the browser, either via an
        HTML/Text email or via the List-Unsubscribe header.
        """
        token = parse_string(request, "access_token", required=True)
        app_id = parse_string(request, "app_id", required=True)
        pushkey = parse_string(request, "pushkey", required=True)

        user_id = self.macaroon_generator.verify_delete_pusher_token(
            token, app_id, pushkey
        )

        try:
            await self.pusher_pool.remove_pusher(
                app_id=app_id, pushkey=pushkey, user_id=user_id
            )
        except StoreError as se:
            if se.code != 404:
                # This is fine: they're already unsubscribed
                raise

        self.notifier.on_new_replication_data()

        respond_with_html_bytes(
            request,
            200,
            UnsubscribeResource.SUCCESS_HTML,
        )

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        """
        Handle a mail user agent POSTing to the unsubscribe URL via the
        List-Unsubscribe & List-Unsubscribe-Post headers.
        """

        # TODO Assert that the body has a single field

        # Assert the body has form encoded key/value pair of
        # List-Unsubscribe=One-Click.

        await self._async_render_GET(request)
