#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Tuple

from twisted.web.server import Request

from synapse.http.server import DirectServeJsonResource

if TYPE_CHECKING:
    from synapse.server import HomeServer


class AdditionalResource(DirectServeJsonResource):
    """Resource wrapper for additional_resources

    If the user has configured additional_resources, we need to wrap the
    handler class with a Resource so that we can map it into the resource tree.

    This class is also where we wrap the request handler with logging, metrics,
    and exception handling.
    """

    def __init__(
        self,
        hs: "HomeServer",
        handler: Callable[[Request], Awaitable[Optional[Tuple[int, Any]]]],
    ):
        """Initialise AdditionalResource

        The ``handler`` should return a deferred which completes when it has
        done handling the request. It should write a response with
        ``request.write()``, and call ``request.finish()``.

        Args:
            hs: homeserver
            handler: function to be called to handle the request.
        """
        super().__init__()
        self._handler = handler

    async def _async_render(self, request: Request) -> Optional[Tuple[int, Any]]:
        # Cheekily pass the result straight through, so we don't need to worry
        # if its an awaitable or not.
        return await self._handler(request)
