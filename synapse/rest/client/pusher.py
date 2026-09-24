#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.push import PusherConfigException
from synapse.rest.admin.experimental_features import ExperimentalFeature
from synapse.rest.client._base import client_patterns
from synapse.rest.synapse.client.unsubscribe import UnsubscribeResource
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class PushersRestServlet(RestServlet):
    PATTERNS = client_patterns("/pushers$", v1=True)

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self._store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        msc3881_enabled = await self._store.is_feature_enabled(
            user_id, ExperimentalFeature.MSC3881
        )

        pushers = await self.hs.get_datastores().main.get_pushers_by_user_id(user_id)

        pusher_dicts = [p.as_dict() for p in pushers]

        for pusher in pusher_dicts:
            if msc3881_enabled:
                pusher["org.matrix.msc3881.enabled"] = pusher["enabled"]
                pusher["org.matrix.msc3881.device_id"] = pusher["device_id"]
            del pusher["enabled"]
            del pusher["device_id"]

        return 200, {"pushers": pusher_dicts}


class PushersSetRestServlet(RestServlet):
    PATTERNS = client_patterns("/pushers/set$", v1=True)

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.notifier = hs.get_notifier()
        self.pusher_pool = self.hs.get_pusherpool()
        self._store = hs.get_datastores().main

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        msc3881_enabled = await self._store.is_feature_enabled(
            user_id, ExperimentalFeature.MSC3881
        )

        content = parse_json_object_from_request(request)

        if (
            "pushkey" in content
            and "app_id" in content
            and "kind" in content
            and content["kind"] is None
        ):
            await self.pusher_pool.remove_pusher(
                content["app_id"], content["pushkey"], user_id=user_id
            )
            return 200, {}

        assert_params_in_dict(
            content,
            [
                "kind",
                "app_id",
                "app_display_name",
                "device_display_name",
                "pushkey",
                "lang",
                "data",
            ],
        )

        logger.debug("set pushkey %s to kind %s", content["pushkey"], content["kind"])
        logger.debug("Got pushers request with body: %r", content)

        append = False
        if "append" in content:
            append = content["append"]

        enabled = True
        if msc3881_enabled and "org.matrix.msc3881.enabled" in content:
            enabled = content["org.matrix.msc3881.enabled"]

        if not append:
            await self.pusher_pool.remove_pushers_by_app_id_and_pushkey_not_user(
                app_id=content["app_id"],
                pushkey=content["pushkey"],
                not_user_id=user_id,
            )

        try:
            await self.pusher_pool.add_or_update_pusher(
                user_id=user_id,
                kind=content["kind"],
                app_id=content["app_id"],
                app_display_name=content["app_display_name"],
                device_display_name=content["device_display_name"],
                pushkey=content["pushkey"],
                lang=content["lang"],
                data=content["data"],
                profile_tag=content.get("profile_tag", ""),
                enabled=enabled,
                device_id=requester.device_id,
            )
        except PusherConfigException as pce:
            raise SynapseError(
                400, "Config Error: " + str(pce), errcode=Codes.MISSING_PARAM
            )

        self.notifier.on_new_replication_data()

        return 200, {}


class LegacyPushersRemoveRestServlet(UnsubscribeResource, RestServlet):
    """
    A servlet to handle legacy "email unsubscribe" links, forwarding requests to the ``UnsubscribeResource``

    This should be kept for some time, so unsubscribe links in past emails stay valid.
    """

    PATTERNS = client_patterns("/pushers/remove$", releases=[], v1=False, unstable=True)

    async def on_GET(self, request: SynapseRequest) -> None:
        # Forward the request to the UnsubscribeResource
        await self._async_render(request)


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    PushersRestServlet(hs).register(http_server)
    PushersSetRestServlet(hs).register(http_server)
    LegacyPushersRemoveRestServlet(hs).register(http_server)
