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
import logging
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

from synapse.http.server import HttpServer, JsonResource
from synapse.rest import admin
from synapse.rest.client import (
    account,
    account_data,
    account_validity,
    appservice_ping,
    auth,
    auth_issuer,
    capabilities,
    devices,
    directory,
    events,
    filter,
    initial_sync,
    keys,
    knock,
    login,
    login_token_request,
    logout,
    mutual_rooms,
    notifications,
    openid,
    password_policy,
    presence,
    profile,
    push_rule,
    pusher,
    read_marker,
    receipts,
    register,
    relations,
    rendezvous,
    reporting,
    room,
    room_keys,
    room_upgrade_rest_servlet,
    sendtodevice,
    sync,
    tags,
    thirdparty,
    tokenrefresh,
    user_directory,
    versions,
    voip,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from synapse.server import HomeServer

RegisterServletsFunc = Callable[["HomeServer", HttpServer], None]

CLIENT_SERVLET_FUNCTIONS: Tuple[RegisterServletsFunc, ...] = (
    versions.register_servlets,
    initial_sync.register_servlets,
    room.register_deprecated_servlets,
    events.register_servlets,
    room.register_servlets,
    login.register_servlets,
    profile.register_servlets,
    presence.register_servlets,
    directory.register_servlets,
    voip.register_servlets,
    pusher.register_servlets,
    push_rule.register_servlets,
    logout.register_servlets,
    sync.register_servlets,
    filter.register_servlets,
    account.register_servlets,
    register.register_servlets,
    auth.register_servlets,
    receipts.register_servlets,
    read_marker.register_servlets,
    room_keys.register_servlets,
    keys.register_servlets,
    tokenrefresh.register_servlets,
    tags.register_servlets,
    account_data.register_servlets,
    reporting.register_servlets,
    openid.register_servlets,
    notifications.register_servlets,
    devices.register_servlets,
    thirdparty.register_servlets,
    sendtodevice.register_servlets,
    user_directory.register_servlets,
    room_upgrade_rest_servlet.register_servlets,
    capabilities.register_servlets,
    account_validity.register_servlets,
    relations.register_servlets,
    password_policy.register_servlets,
    knock.register_servlets,
    appservice_ping.register_servlets,
    admin.register_servlets_for_client_rest_resource,
    mutual_rooms.register_servlets,
    login_token_request.register_servlets,
    rendezvous.register_servlets,
    auth_issuer.register_servlets,
)

SERVLET_GROUPS: Dict[str, Iterable[RegisterServletsFunc]] = {
    "client": CLIENT_SERVLET_FUNCTIONS,
}


class ClientRestResource(JsonResource):
    """Matrix Client API REST resource.

    This gets mounted at various points under /_matrix/client, including:
       * /_matrix/client/r0
       * /_matrix/client/api/v1
       * /_matrix/client/unstable
       * etc
    """

    def __init__(self, hs: "HomeServer", servlet_groups: Optional[List[str]] = None):
        JsonResource.__init__(self, hs, canonical_json=False)
        if hs.config.media.can_load_media_repo:
            # This import is here to prevent a circular import failure
            from synapse.rest.client import media

            SERVLET_GROUPS["media"] = (media.register_servlets,)
        self.register_servlets(self, hs, servlet_groups)

    @staticmethod
    def register_servlets(
        client_resource: HttpServer,
        hs: "HomeServer",
        servlet_groups: Optional[Iterable[str]] = None,
    ) -> None:
        # Some servlets are only registered on the main process (and not worker
        # processes).
        is_main_process = hs.config.worker.worker_app is None

        if not servlet_groups:
            servlet_groups = SERVLET_GROUPS.keys()

        for servlet_group in servlet_groups:
            # Fail on unknown servlet groups.
            if servlet_group not in SERVLET_GROUPS:
                if servlet_group == "media":
                    logger.warn(
                        "media.can_load_media_repo needs to be configured for the media servlet to be available"
                    )
                raise RuntimeError(
                    f"Attempting to register unknown client servlet: '{servlet_group}'"
                )

            for servletfunc in SERVLET_GROUPS[servlet_group]:
                if not is_main_process and servletfunc in [
                    pusher.register_servlets,
                    logout.register_servlets,
                    auth.register_servlets,
                    tokenrefresh.register_servlets,
                    reporting.register_servlets,
                    openid.register_servlets,
                    thirdparty.register_servlets,
                    room_upgrade_rest_servlet.register_servlets,
                    account_validity.register_servlets,
                    admin.register_servlets_for_client_rest_resource,
                    mutual_rooms.register_servlets,
                    login_token_request.register_servlets,
                    rendezvous.register_servlets,
                    auth_issuer.register_servlets,
                ]:
                    continue

                servletfunc(hs, client_resource)
