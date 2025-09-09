# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.


from typing import Mapping

from twisted.internet.defer import Deferred

from synapse.logging.context import make_deferred_yieldable
from synapse.synapse_rust.http_client import HttpClient as RustHttpClient
from synapse.types import ISynapseReactor


class HttpClient:
    def __init__(self, reactor: ISynapseReactor, user_agent: str) -> None:
        self._http_client = RustHttpClient(reactor, user_agent)

    def get(self, url: str, response_limit: int) -> Deferred[bytes]:
        deferred = self._http_client.get(url, response_limit)
        return make_deferred_yieldable(deferred)

    def post(
        self,
        url: str,
        response_limit: int,
        headers: Mapping[str, str],
        request_body: str,
    ) -> Deferred[bytes]:
        deferred = self._http_client.post(url, response_limit, headers, request_body)
        return make_deferred_yieldable(deferred)
