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

from typing import Awaitable, Mapping

class HttpClient:
    def __init__(self, user_agent: str) -> None: ...
    def get(self, url: str, response_limit: int) -> Awaitable[bytes]: ...
    def post(
        self,
        url: str,
        response_limit: int,
        headers: Mapping[str, str],
        request_body: str,
    ) -> Awaitable[bytes]: ...
