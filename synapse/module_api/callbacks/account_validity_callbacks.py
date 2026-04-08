#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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
from typing import Awaitable, Callable

from twisted.web.http import Request

logger = logging.getLogger(__name__)

# Types for callbacks to be registered via the module api
IS_USER_EXPIRED_CALLBACK = Callable[[str], Awaitable[bool | None]]
ON_USER_REGISTRATION_CALLBACK = Callable[[str], Awaitable]
ON_USER_LOGIN_CALLBACK = Callable[[str, str | None, str | None], Awaitable]
# Temporary hooks to allow for a transition from `/_matrix/client` endpoints
# to `/_synapse/client/account_validity`. See `register_callbacks` below.
ON_LEGACY_SEND_MAIL_CALLBACK = Callable[[str], Awaitable]
ON_LEGACY_RENEW_CALLBACK = Callable[[str], Awaitable[tuple[bool, bool, int]]]
ON_LEGACY_ADMIN_REQUEST = Callable[[Request], Awaitable]


class AccountValidityModuleApiCallbacks:
    def __init__(self) -> None:
        self.is_user_expired_callbacks: list[IS_USER_EXPIRED_CALLBACK] = []
        self.on_user_registration_callbacks: list[ON_USER_REGISTRATION_CALLBACK] = []
        self.on_user_login_callbacks: list[ON_USER_LOGIN_CALLBACK] = []
        self.on_legacy_send_mail_callback: ON_LEGACY_SEND_MAIL_CALLBACK | None = None
        self.on_legacy_renew_callback: ON_LEGACY_RENEW_CALLBACK | None = None

        # The legacy admin requests callback isn't a protected attribute because we need
        # to access it from the admin servlet, which is outside of this handler.
        self.on_legacy_admin_request_callback: ON_LEGACY_ADMIN_REQUEST | None = None

    def register_callbacks(
        self,
        is_user_expired: IS_USER_EXPIRED_CALLBACK | None = None,
        on_user_registration: ON_USER_REGISTRATION_CALLBACK | None = None,
        on_user_login: ON_USER_LOGIN_CALLBACK | None = None,
        on_legacy_send_mail: ON_LEGACY_SEND_MAIL_CALLBACK | None = None,
        on_legacy_renew: ON_LEGACY_RENEW_CALLBACK | None = None,
        on_legacy_admin_request: ON_LEGACY_ADMIN_REQUEST | None = None,
    ) -> None:
        """Register callbacks from module for each hook."""
        if is_user_expired is not None:
            self.is_user_expired_callbacks.append(is_user_expired)

        if on_user_registration is not None:
            self.on_user_registration_callbacks.append(on_user_registration)

        if on_user_login is not None:
            self.on_user_login_callbacks.append(on_user_login)

        # The builtin account validity feature exposes 3 endpoints (send_mail, renew, and
        # an admin one). As part of moving the feature into a module, we need to change
        # the path from /_matrix/client/unstable/account_validity/... to
        # /_synapse/client/account_validity, because:
        #
        #   * the feature isn't part of the Matrix spec thus shouldn't live under /_matrix
        #   * the way we register servlets means that modules can't register resources
        #     under /_matrix/client
        #
        # We need to allow for a transition period between the old and new endpoints
        # in order to allow for clients to update (and for emails to be processed).
        #
        # Once the email-account-validity module is loaded, it will take control of account
        # validity by moving the rows from our `account_validity` table into its own table.
        #
        # Therefore, we need to allow modules (in practice just the one implementing the
        # email-based account validity) to temporarily hook into the legacy endpoints so we
        # can route the traffic coming into the old endpoints into the module, which is
        # why we have the following three temporary hooks.
        if on_legacy_send_mail is not None:
            if self.on_legacy_send_mail_callback is not None:
                raise RuntimeError("Tried to register on_legacy_send_mail twice")

            self.on_legacy_send_mail_callback = on_legacy_send_mail

        if on_legacy_renew is not None:
            if self.on_legacy_renew_callback is not None:
                raise RuntimeError("Tried to register on_legacy_renew twice")

            self.on_legacy_renew_callback = on_legacy_renew

        if on_legacy_admin_request is not None:
            if self.on_legacy_admin_request_callback is not None:
                raise RuntimeError("Tried to register on_legacy_admin_request twice")

            self.on_legacy_admin_request_callback = on_legacy_admin_request
