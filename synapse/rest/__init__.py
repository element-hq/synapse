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
from typing import TYPE_CHECKING, Callable

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
    report_event,
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

if TYPE_CHECKING:
    from synapse.server import HomeServer

RegisterServletsFunc = Callable[["HomeServer", HttpServer], None]


class ClientRestResource(JsonResource):
    """Matrix Client API REST resource.

    This gets mounted at various points under /_matrix/client, including:
       * /_matrix/client/r0
       * /_matrix/client/api/v1
       * /_matrix/client/unstable
       * etc
    """

    def __init__(self, hs: "HomeServer"):
        JsonResource.__init__(self, hs, canonical_json=False)
        self.register_servlets(self, hs)

    @staticmethod
    def register_servlets(client_resource: HttpServer, hs: "HomeServer") -> None:
        # Some servlets are only registered on the main process (and not worker
        # processes).
        is_main_process = hs.config.worker.worker_app is None

        versions.register_servlets(hs, client_resource)

        # Deprecated in r0
        initial_sync.register_servlets(hs, client_resource)
        room.register_deprecated_servlets(hs, client_resource)

        # Partially deprecated in r0
        events.register_servlets(hs, client_resource)

        room.register_servlets(hs, client_resource)
        login.register_servlets(hs, client_resource)
        profile.register_servlets(hs, client_resource)
        presence.register_servlets(hs, client_resource)
        directory.register_servlets(hs, client_resource)
        voip.register_servlets(hs, client_resource)
        if is_main_process:
            pusher.register_servlets(hs, client_resource)
        push_rule.register_servlets(hs, client_resource)
        if is_main_process:
            logout.register_servlets(hs, client_resource)
        sync.register_servlets(hs, client_resource)
        filter.register_servlets(hs, client_resource)
        account.register_servlets(hs, client_resource)
        register.register_servlets(hs, client_resource)
        if is_main_process:
            auth.register_servlets(hs, client_resource)
        receipts.register_servlets(hs, client_resource)
        read_marker.register_servlets(hs, client_resource)
        room_keys.register_servlets(hs, client_resource)
        keys.register_servlets(hs, client_resource)
        if is_main_process:
            tokenrefresh.register_servlets(hs, client_resource)
        tags.register_servlets(hs, client_resource)
        account_data.register_servlets(hs, client_resource)
        if is_main_process:
            report_event.register_servlets(hs, client_resource)
            openid.register_servlets(hs, client_resource)
        notifications.register_servlets(hs, client_resource)
        devices.register_servlets(hs, client_resource)
        if is_main_process:
            thirdparty.register_servlets(hs, client_resource)
        sendtodevice.register_servlets(hs, client_resource)
        user_directory.register_servlets(hs, client_resource)
        if is_main_process:
            room_upgrade_rest_servlet.register_servlets(hs, client_resource)
        capabilities.register_servlets(hs, client_resource)
        if is_main_process:
            account_validity.register_servlets(hs, client_resource)
        relations.register_servlets(hs, client_resource)
        password_policy.register_servlets(hs, client_resource)
        knock.register_servlets(hs, client_resource)
        appservice_ping.register_servlets(hs, client_resource)

        # moving to /_synapse/admin
        if is_main_process:
            admin.register_servlets_for_client_rest_resource(hs, client_resource)

        # unstable
        if is_main_process:
            mutual_rooms.register_servlets(hs, client_resource)
            login_token_request.register_servlets(hs, client_resource)
            rendezvous.register_servlets(hs, client_resource)
            auth_issuer.register_servlets(hs, client_resource)
