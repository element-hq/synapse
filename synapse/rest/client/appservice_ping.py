#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 Tulir Asokan
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
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Dict, Tuple

from synapse.api.errors import (
    CodeMessageException,
    Codes,
    HttpResponseException,
    SynapseError,
)
from synapse.http import RequestTimedOutError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class AppservicePingRestServlet(RestServlet):
    PATTERNS = client_patterns(
        "/appservice/(?P<appservice_id>[^/]*)/ping",
        releases=("v1",),
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.as_api = hs.get_application_service_api()
        self.auth = hs.get_auth()

    async def on_POST(
        self, request: SynapseRequest, appservice_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)

        if not requester.app_service:
            raise SynapseError(
                HTTPStatus.FORBIDDEN,
                "Only application services can use the /appservice/ping endpoint",
                Codes.FORBIDDEN,
            )
        elif requester.app_service.id != appservice_id:
            raise SynapseError(
                HTTPStatus.FORBIDDEN,
                "Mismatching application service ID in path",
                Codes.FORBIDDEN,
            )
        elif not requester.app_service.url:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "The application service does not have a URL set",
                Codes.AS_PING_URL_NOT_SET,
            )

        content = parse_json_object_from_request(request)
        txn_id = content.get("transaction_id", None)

        start = time.monotonic()
        try:
            await self.as_api.ping(requester.app_service, txn_id)
        except RequestTimedOutError as e:
            raise SynapseError(
                HTTPStatus.GATEWAY_TIMEOUT,
                e.msg,
                Codes.AS_PING_CONNECTION_TIMEOUT,
            )
        except CodeMessageException as e:
            additional_fields: Dict[str, Any] = {"status": e.code}
            if isinstance(e, HttpResponseException):
                try:
                    additional_fields["body"] = e.response.decode("utf-8")
                except UnicodeDecodeError:
                    pass
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY,
                f"HTTP {e.code} {e.msg}",
                Codes.AS_PING_BAD_STATUS,
                additional_fields=additional_fields,
            )
        except Exception as e:
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY,
                f"{type(e).__name__}: {e}",
                Codes.AS_PING_CONNECTION_FAILED,
            )

        duration = time.monotonic() - start

        return HTTPStatus.OK, {"duration_ms": int(duration * 1000)}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    AppservicePingRestServlet(hs).register(http_server)
