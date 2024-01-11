#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, Tuple

from twisted.web.server import Request

from synapse.api.errors import SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import parse_integer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationGetStreamUpdates(ReplicationEndpoint):
    """Fetches stream updates from a server. Used for streams not persisted to
    the database, e.g. typing notifications.

    The API looks like:

        GET /_synapse/replication/get_repl_stream_updates/<stream name>?from_token=0&to_token=10

        200 OK

        {
            updates: [ ... ],
            upto_token: 10,
            limited: False,
        }

    If there are more rows than can sensibly be returned in one lump, `limited` will be
    set to true, and the caller should call again with a new `from_token`.

    """

    NAME = "get_repl_stream_updates"
    PATH_ARGS = ("stream_name",)
    METHOD = "GET"

    # We don't want to wait for replication streams to catch up, as this gets
    # called in the process of catching replication streams up.
    WAIT_FOR_STREAMS = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self._instance_name = hs.get_instance_name()
        self.streams = hs.get_replication_streams()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        stream_name: str, from_token: int, upto_token: int
    ) -> JsonDict:
        return {"from_token": from_token, "upto_token": upto_token}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, stream_name: str
    ) -> Tuple[int, JsonDict]:
        stream = self.streams.get(stream_name)
        if stream is None:
            raise SynapseError(400, "Unknown stream")

        from_token = parse_integer(request, "from_token", required=True)
        upto_token = parse_integer(request, "upto_token", required=True)

        updates, upto_token, limited = await stream.get_updates_since(
            self._instance_name, from_token, upto_token
        )

        return (
            200,
            {"updates": updates, "upto_token": upto_token, "limited": limited},
        )


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationGetStreamUpdates(hs).register(http_server)
