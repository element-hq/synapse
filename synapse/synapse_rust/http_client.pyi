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

from synapse.types import ISynapseReactor

class HttpClient:
    """
    The returned deferreds follow Synapse logcontext rules.
    """

    def __init__(
        self,
        reactor: ISynapseReactor,
        user_agent: str,
        http2_only: bool = False,
    ) -> None:
        """
        Create a new HTTP client backed by reqwest.

        Args:
            reactor: The Twisted reactor to coordinate with
            user_agent: The user agent to use for requests
            http2_only: Whether to use HTTP/2 only, even on unencrypted connections. By
                default, it will always use HTTP/1.1 over unencrypted connections, and
                rely on TLS ALPN to negotiate HTTP/2.

                Ensure the upstream server supports HTTP/2 before enabling this.
        """

    def get(self, url: str, response_limit: int) -> Deferred[bytes]: ...
    def post(
        self,
        url: str,
        response_limit: int,
        headers: Mapping[str, str],
        request_body: str,
    ) -> Deferred[bytes]: ...
