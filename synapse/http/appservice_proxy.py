#
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
#

import logging
from typing import TYPE_CHECKING, Optional, cast

from twisted.python import failure
from twisted.web.http_headers import Headers
from twisted.web.iweb import IBodyProducer, IResponse

from synapse.appservice import ApplicationService
from synapse.http.proxy import (
    HOP_BY_HOP_HEADERS_LOWERCASE,
    _ProxyResponseBody,
    parse_connection_header_value,
)
from synapse.http.server import return_json_error, set_cors_headers
from synapse.http.site import SynapseRequest
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.util.async_helpers import timeout_deferred

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


async def proxy_request_to_appservice(
    request: SynapseRequest,
    hs: "HomeServer",
    appservice: ApplicationService,
    body_producer: Optional[IBodyProducer],
    extra_request_headers: dict[bytes, bytes] | None = None,
) -> None:
    """Forward the given request to an application service's proxy URL and stream
    the response back to the original caller unchanged.

    Args:
        request: The inbound request to forward.
        hs: The homeserver.
        appservice: The application service to forward the request to. Must have
            `proxy_url` and `hs_token` set.
        body_producer: A producer for the request body to forward, or None if the
            request has no body to forward.
        extra_request_headers: Additional headers to set on the outbound request,
            beyond those copied from the original request.
    """
    assert appservice.proxy_url is not None
    assert appservice.hs_token is not None
    target_uri = appservice.proxy_url.encode("ascii") + request.uri

    # Only forward the bare minimum of request headers an application service could
    # plausibly need.
    headers = Headers()
    for header_name, header_values in request.requestHeaders.getAllRawHeaders():
        if header_name.decode("ascii").lower() in {
            "content-type",
            "accept",
            "accept-language",
        }:
            headers.setRawHeaders(header_name, header_values)

    headers.setRawHeaders(
        b"Authorization", [b"Bearer " + appservice.hs_token.encode("ascii")]
    )

    if extra_request_headers:
        for header_name, header_value in extra_request_headers.items():
            headers.setRawHeaders(header_name, [header_value])

    agent = hs.get_proxied_http_client().agent
    request_deferred = run_in_background(
        agent.request,
        request.method,
        target_uri,
        headers=headers,
        bodyProducer=body_producer,
    )
    request_deferred = timeout_deferred(
        deferred=request_deferred,
        timeout=30,  # Give the application service at most 30s to respond.
        clock=hs.get_clock(),
    )

    try:
        response = await make_deferred_yieldable(request_deferred)
    except Exception:
        logger.warning(
            "Error proxying request to application service %s at %s",
            appservice.id,
            target_uri,
            exc_info=True,
        )
        return_json_error(failure.Failure(), request, None)
        return

    _send_response(request, response)


def _send_response(request: SynapseRequest, response: IResponse) -> None:
    response_headers = cast(Headers, response.headers)

    request.setResponseCode(response.code)
    set_cors_headers(request)

    # We strip the "hop-by-hop" headers as defined by RFC2616.
    headers_to_strip = set(HOP_BY_HOP_HEADERS_LOWERCASE)

    # The `Connection` header can define additional headers that should not be
    # copied over.
    connection_header = response_headers.getRawHeaders(b"connection")
    headers_to_strip |= parse_connection_header_value(
        connection_header[0] if connection_header else None
    )

    for header_name, header_values in response_headers.getAllRawHeaders():
        if header_name.decode("ascii").lower() in headers_to_strip:
            continue
        request.responseHeaders.setRawHeaders(header_name, header_values)

    response.deliverBody(_ProxyResponseBody(request))
