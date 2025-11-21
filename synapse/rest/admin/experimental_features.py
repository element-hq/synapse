#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C
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


from enum import Enum
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import SynapseError
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.rest.admin import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from typing_extensions import assert_never

    from synapse.server import HomeServer, HomeServerConfig


class ExperimentalFeature(str, Enum):
    """
    Currently supported per-user features
    """

    MSC3881 = "msc3881"
    MSC3575 = "msc3575"
    MSC4222 = "msc4222"

    def is_globally_enabled(self, config: "HomeServerConfig") -> bool:
        if self is ExperimentalFeature.MSC3881:
            return config.experimental.msc3881_enabled
        if self is ExperimentalFeature.MSC3575:
            return config.experimental.msc3575_enabled
        if self is ExperimentalFeature.MSC4222:
            return config.experimental.msc4222_enabled

        assert_never(self)


class ExperimentalFeaturesRestServlet(RestServlet):
    """
    Enable or disable experimental features for a user or determine which features are enabled
    for a given user
    """

    PATTERNS = admin_patterns("/experimental_features/(?P<user_id>[^/]*)")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.is_mine = hs.is_mine

    async def on_GET(
        self,
        request: SynapseRequest,
        user_id: str,
    ) -> tuple[int, JsonDict]:
        """
        List which features are enabled for a given user
        """
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)
        if not self.is_mine(target_user):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "User must be local to check what experimental features are enabled.",
            )

        enabled_features = await self.store.list_enabled_features(user_id)

        user_features = {}
        for feature in ExperimentalFeature:
            if feature in enabled_features:
                user_features[feature.value] = True
            else:
                user_features[feature.value] = False
        return HTTPStatus.OK, {"features": user_features}

    async def on_PUT(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[HTTPStatus, dict]:
        """
        Enable or disable the provided features for the requester
        """
        await assert_requester_is_admin(self.auth, request)

        body = parse_json_object_from_request(request)

        target_user = UserID.from_string(user_id)
        if not self.is_mine(target_user):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "User must be local to enable experimental features.",
            )

        features = body.get("features")
        if not features:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "You must provide features to set."
            )

        # validate the provided features
        validated_features = {}
        for feature, enabled in features.items():
            try:
                validated_feature = ExperimentalFeature(feature)
                validated_features[validated_feature] = enabled
            except ValueError:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    f"{feature!r} is not recognised as a valid experimental feature.",
                )

        await self.store.set_features_for_user(user_id, validated_features)

        return HTTPStatus.OK, {}
