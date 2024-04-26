# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from twisted.web.iweb import IRequest

from synapse.server import HomeServer

class RendezvousHandler:
    def __init__(
        self,
        homeserver: HomeServer,
        /,
        capacity: int = 100,
        max_content_length: int = 4 * 1024,  # MSC4108 specifies 4KB
        eviction_interval: int = 60 * 1000,
        ttl: int = 60 * 1000,
    ) -> None: ...
    def handle_post(self, request: IRequest) -> None: ...
    def handle_get(self, request: IRequest, session_id: str) -> None: ...
    def handle_put(self, request: IRequest, session_id: str) -> None: ...
    def handle_delete(self, request: IRequest, session_id: str) -> None: ...
