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

import logging
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    disable_created_metrics,
    generate_latest,
)

from twisted.web.resource import Resource
from twisted.web.server import Request

logger = logging.getLogger(__name__)

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


def _set_prometheus_client_use_created_metrics(new_value: bool) -> None:
    """
    Sets whether prometheus_client should expose `_created`-suffixed metrics for
    all gauges, histograms and summaries.

    There is no programmatic way to disable this without poking at internals;
    the proper way is to use an environment variable which prometheus_client
    loads at import time.

    The motivation for disabling these `_created` metrics is that they're
    a waste of space as they're not useful but they take up space in Prometheus.
    """
    import prometheus_client.metrics

    if hasattr(prometheus_client.metrics, "_use_created"):
        prometheus_client.metrics._use_created = new_value
    else:
        logger.error(
            "Can't disable `_created` metrics in prometheus_client (brittle hack broken?)"
        )


_set_prometheus_client_use_created_metrics(False)


class MetricsResource(Resource):
    """
    Twisted ``Resource`` that serves prometheus metrics.
    """

    isLeaf = True

    def __init__(self, registry: CollectorRegistry = REGISTRY):
        self.registry = registry

    def render_GET(self, request: Request) -> bytes:
        request.setHeader(b"Content-Type", CONTENT_TYPE_LATEST.encode("ascii"))
        response = generate_latest(self.registry)
        request.setHeader(b"Content-Length", str(len(response)))
        return response
