# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import Any

from twisted.web.iweb import IRequest

from synapse.server import HomeServer

class MSC4388RendezvousHandler:
    def __init__(
        self,
        homeserver: HomeServer,
        /,
        soft_limit: int = 100,  # On each background eviction run sessions will be removed until we're under this limit
        hard_limit: int = 200,  # If this limit is reached an immediate eviction will be triggered
        max_content_length: int = 4 * 1024,  # MSC4388 specifies maximum of 4KB
        eviction_interval: int = 60 * 1000,
        ttl: int = 2 * 60 * 1000,  # MSC4388 specifies minimum of 120 seconds
    ) -> None: ...
    def handle_post(self, request: IRequest) -> tuple[int, Any]: ...
    def handle_get(self, session_id: str, request: IRequest) -> tuple[int, Any]: ...
    def handle_put(self, session_id: str, request: IRequest) -> tuple[int, Any]: ...
    def handle_delete(self, session_id: str) -> tuple[int, Any]: ...
