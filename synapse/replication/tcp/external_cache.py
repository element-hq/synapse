#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, Any, Optional

from prometheus_client import Counter, Histogram

from synapse.logging import opentracing
from synapse.logging.context import make_deferred_yieldable
from synapse.metrics import SERVER_NAME_LABEL
from synapse.util import json_decoder, json_encoder

if TYPE_CHECKING:
    from txredisapi import ConnectionHandler

    from synapse.server import HomeServer

set_counter = Counter(
    "synapse_external_cache_set",
    "Number of times we set a cache",
    labelnames=["cache_name", SERVER_NAME_LABEL],
)

get_counter = Counter(
    "synapse_external_cache_get",
    "Number of times we get a cache",
    labelnames=["cache_name", "hit", SERVER_NAME_LABEL],
)

response_timer = Histogram(
    "synapse_external_cache_response_time_seconds",
    "Time taken to get a response from Redis for a cache get/set request",
    labelnames=["method", SERVER_NAME_LABEL],
    buckets=(
        0.001,
        0.002,
        0.005,
        0.01,
        0.02,
        0.05,
    ),
)


logger = logging.getLogger(__name__)


class ExternalCache:
    """A cache backed by an external Redis. Does nothing if no Redis is
    configured.
    """

    def __init__(self, hs: "HomeServer"):
        self.server_name = hs.hostname

        if hs.config.redis.redis_enabled:
            self._redis_connection: Optional["ConnectionHandler"] = (
                hs.get_outbound_redis_connection()
            )
        else:
            self._redis_connection = None

    def _get_redis_key(self, cache_name: str, key: str) -> str:
        return "cache_v1:%s:%s" % (cache_name, key)

    def is_enabled(self) -> bool:
        """Whether the external cache is used or not.

        It's safe to use the cache when this returns false, the methods will
        just no-op, but the function is useful to avoid doing unnecessary work.
        """
        return self._redis_connection is not None

    async def set(self, cache_name: str, key: str, value: Any, expiry_ms: int) -> None:
        """Add the key/value to the named cache, with the expiry time given."""

        if self._redis_connection is None:
            return

        set_counter.labels(
            cache_name=cache_name, **{SERVER_NAME_LABEL: self.server_name}
        ).inc()

        # txredisapi requires the value to be string, bytes or numbers, so we
        # encode stuff in JSON.
        encoded_value = json_encoder.encode(value)

        logger.debug("Caching %s %s: %r", cache_name, key, encoded_value)

        with opentracing.start_active_span(
            "ExternalCache.set",
            tags={opentracing.SynapseTags.CACHE_NAME: cache_name},
        ):
            with response_timer.labels(
                method="set", **{SERVER_NAME_LABEL: self.server_name}
            ).time():
                return await make_deferred_yieldable(
                    self._redis_connection.set(
                        self._get_redis_key(cache_name, key),
                        encoded_value,
                        pexpire=expiry_ms,
                    )
                )

    async def get(self, cache_name: str, key: str) -> Optional[Any]:
        """Look up a key/value in the named cache."""

        if self._redis_connection is None:
            return None

        with opentracing.start_active_span(
            "ExternalCache.get",
            tags={opentracing.SynapseTags.CACHE_NAME: cache_name},
        ):
            with response_timer.labels(
                method="get", **{SERVER_NAME_LABEL: self.server_name}
            ).time():
                result = await make_deferred_yieldable(
                    self._redis_connection.get(self._get_redis_key(cache_name, key))
                )

        logger.debug("Got cache result %s %s: %r", cache_name, key, result)

        get_counter.labels(
            cache_name=cache_name,
            hit=result is not None,
            **{SERVER_NAME_LABEL: self.server_name},
        ).inc()

        if not result:
            return None

        # For some reason the integers get magically converted back to integers
        if isinstance(result, int):
            return result

        return json_decoder.decode(result)
