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

import json
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from twisted.web.http_headers import Headers
from twisted.web.iweb import IBodyProducer, IResponse

from synapse.api.errors import (
    Codes,
    FederationDeniedError,
    HttpResponseException,
    RequestSendFailed,
    SynapseError,
)
from synapse.appservice import ApplicationService
from synapse.http.proxy import (
    HOP_BY_HOP_HEADERS_LOWERCASE,
    _ProxyResponseBody,
    parse_connection_header_value,
)
from synapse.http.server import set_cors_headers
from synapse.http.site import SynapseRequest
from synapse.http.types import QueryParams
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.types import JsonDict
from synapse.util.async_helpers import timeout_deferred
from synapse.util.json import json_decoder
from synapse.util.retryutils import NotRetryingDestination

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


async def proxy_request_to_appservice(
    request: SynapseRequest,
    hs: "HomeServer",
    appservice: ApplicationService,
    body_producer: IBodyProducer,
    extra_request_headers: dict[bytes, bytes] | None = None,
) -> None:
    """Forward the given request to an application service's proxy URL and stream
    the response back to the original caller unchanged.

    Args:
        request: The inbound request to forward.
        hs: The homeserver.
        appservice: The application service to forward the request to. Must have
            `proxy_url` set.
        body_producer: A producer for the request body to forward.
        extra_request_headers: Additional headers to set on the outbound request,
            beyond those copied from the original request.
    """
    assert appservice.proxy_url is not None
    target_uri = appservice.proxy_url.encode("ascii") + request.uri

    # Other than the "hop-by-hop" headers as defined by RFC2616 we also strip:
    # - Host and Content-Length (twisted adds these on its own)
    # - Authorization (because the app service shouldn't need to be concerned with it)
    headers_to_strip = HOP_BY_HOP_HEADERS_LOWERCASE | {
        "host",
        "content-length",
        "authorization",
    }

    # The `Connection` header can define additional headers that should not be
    # copied over.
    connection_header = request.requestHeaders.getRawHeaders(b"connection")
    headers_to_strip |= parse_connection_header_value(
        connection_header[0] if connection_header else None
    )

    headers = Headers()
    for header_name, header_values in request.requestHeaders.getAllRawHeaders():
        if header_name.decode("ascii").lower() in headers_to_strip:
            continue
        headers.setRawHeaders(header_name, header_values)
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
        _send_error_response(request)
        return

    _send_response(request, response)


def _send_response(request: SynapseRequest, response: IResponse) -> None:
    response_headers = cast(Headers, response.headers)

    request.setResponseCode(response.code)
    set_cors_headers(request)

    # We strip the "hop-by-hop" headers as defined by RFC2616.
    headers_to_strip = HOP_BY_HOP_HEADERS_LOWERCASE

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


def _send_error_response(request: SynapseRequest) -> None:
    request.setResponseCode(404)
    set_cors_headers(request)
    request.setHeader(b"Content-Type", b"application/json")
    request.write(
        json.dumps(
            {"errcode": Codes.UNRECOGNIZED, "error": "Unrecognized request"}
        ).encode()
    )
    request.finish()


async def send_federation_request_from_appservice(
    hs: "HomeServer",
    appservice: ApplicationService,
    method: str,
    destination: str,
    path: str,
    data: JsonDict | None,
    args: QueryParams | None,
) -> tuple[int, JsonDict | None]:
    """Sign and send a federation request on behalf of an appservice.

    Returns:
        A `(status, content)` tuple describing the destination's actual HTTP response.
    """
    _check_path_allowed_for_appservice(appservice, path)

    if destination == hs.hostname:
        raise SynapseError(
            HTTPStatus.FORBIDDEN,
            "Cannot target this homeserver itself",
            Codes.AS_FEDPROXY_DESTINATION_DENIED,
        )

    client = hs.get_federation_http_client()

    try:
        if method == "GET":
            content = await client.get_json(destination, path, args=args)
        elif method == "PUT":
            content = await client.put_json(destination, path, args=args, data=data)
        elif method == "POST":
            content = await client.post_json(destination, path, args=args, data=data)
        elif method == "DELETE":
            content = await client.delete_json(destination, path, args=args)
        else:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                f"Unsupported method {method}",
                Codes.INVALID_PARAM,
            )
        return HTTPStatus.OK, content
    except HttpResponseException as e:
        try:
            content = json_decoder.decode(e.response.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            content = None
        return e.code, content
    except FederationDeniedError as e:
        raise SynapseError(
            HTTPStatus.FORBIDDEN,
            e.msg,
            Codes.AS_FEDPROXY_DESTINATION_DENIED,
        )
    except (RequestSendFailed, NotRetryingDestination) as e:
        raise SynapseError(
            HTTPStatus.BAD_GATEWAY,
            str(e),
            Codes.AS_FEDPROXY_CONNECTION_FAILED,
        )


def _check_path_allowed_for_appservice(
    appservice: ApplicationService, path: str
) -> None:
    # Deny relative paths.
    if any(segment in (".", "..") for segment in path.split("/")):
        raise SynapseError(
            HTTPStatus.FORBIDDEN,
            "Path must not contain '.' or '..' segments",
            Codes.AS_FEDPROXY_PATH_NOT_ALLOWED,
        )

    # Ensure the path is under the appservice's own proxy prefix.
    if appservice.proxy_prefix is None:
        raise SynapseError(
            HTTPStatus.BAD_REQUEST,
            "Application service does not have a proxy prefix",
            Codes.AS_FEDPROXY_NO_PROXY_PREFIX,
        )
    allowed_root = f"/_matrix/federation/{appservice.proxy_prefix}"
    if path != allowed_root and not path.startswith(allowed_root + "/"):
        raise SynapseError(
            HTTPStatus.FORBIDDEN,
            f"Path must be under {allowed_root}",
            Codes.AS_FEDPROXY_PATH_NOT_ALLOWED,
        )
