#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 Matrix.org Foundation C.I.C.
# Copyright 2015-2019 Prometheus Python Client Developers
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

from prometheus_client import REGISTRY, generate_latest

from twisted.web.resource import Resource
from twisted.web.server import Request

if TYPE_CHECKING:
    from synapse.metrics.homeserver_metrics_manager import HomeserverMetricsManager

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


class MetricsResource(Resource):
    """
    Twisted ``Resource`` that serves prometheus metrics.
    """

    isLeaf = True

    def __init__(self, metrics_manager: "HomeserverMetricsManager"):
        self.metrics_manager = metrics_manager

    def render_GET(self, request: Request) -> bytes:
        request.setHeader(b"Content-Type", CONTENT_TYPE_LATEST.encode("ascii"))
        # While we're in the middle of the refactor of metrics in Synapse, we need to
        # merge the metrics from the global registry and the homeserver specific metrics
        # collector registry.
        #
        # TODO: Remove `generate_latest(REGISTRY)` once all homeserver metrics have been
        # migrated to the homeserver specific metrics collector registry, see
        # https://github.com/element-hq/synapse/issues/18592
        response = generate_latest(REGISTRY) + generate_latest(
            self.metrics_manager.metrics_collector_registry
        )
        request.setHeader(b"Content-Length", str(len(response)))
        return response
