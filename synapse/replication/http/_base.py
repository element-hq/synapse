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

import abc
import logging
import re
import urllib.parse
from inspect import signature
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ClassVar, Dict, List, Tuple

from prometheus_client import Counter, Gauge

from twisted.internet.error import ConnectError, DNSLookupError
from twisted.web.server import Request

from synapse.api.errors import HttpResponseException, SynapseError
from synapse.config.workers import MAIN_PROCESS_INSTANCE_NAME
from synapse.http import RequestTimedOutError
from synapse.http.server import HttpServer
from synapse.http.servlet import parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.logging import opentracing
from synapse.logging.opentracing import trace_with_opname
from synapse.types import JsonDict
from synapse.util.caches.response_cache import ResponseCache
from synapse.util.cancellation import is_function_cancellable
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

_pending_outgoing_requests = Gauge(
    "synapse_pending_outgoing_replication_requests",
    "Number of active outgoing replication requests, by replication method name",
    ["name"],
)

_outgoing_request_counter = Counter(
    "synapse_outgoing_replication_requests",
    "Number of outgoing replication requests, by replication method name and result",
    ["name", "code"],
)


_STREAM_POSITION_KEY = "_INT_STREAM_POS"


class ReplicationEndpoint(metaclass=abc.ABCMeta):
    """Helper base class for defining new replication HTTP endpoints.

    This creates an endpoint under `/_synapse/replication/:NAME/:PATH_ARGS..`
    (with a `/:txn_id` suffix for cached requests), where NAME is a name,
    PATH_ARGS are a tuple of parameters to be encoded in the URL.

    For example, if `NAME` is "send_event" and `PATH_ARGS` is `("event_id",)`,
    with `CACHE` set to true then this generates an endpoint:

        /_synapse/replication/send_event/:event_id/:txn_id

    For POST/PUT requests the payload is serialized to json and sent as the
    body, while for GET requests the payload is added as query parameters. See
    `_serialize_payload` for details.

    Incoming requests are handled by overriding `_handle_request`. Servers
    must call `register` to register the path with the HTTP server.

    Requests can be sent by calling the client returned by `make_client`.
    Requests are sent to master process by default, but can be sent to other
    named processes by specifying an `instance_name` keyword argument.

    Attributes:
        NAME (str): A name for the endpoint, added to the path as well as used
            in logging and metrics.
        PATH_ARGS (tuple[str]): A list of parameters to be added to the path.
            Adding parameters to the path (rather than payload) can make it
            easier to follow along in the log files.
        METHOD (str): The method of the HTTP request, defaults to POST. Can be
            one of POST, PUT or GET. If GET then the payload is sent as query
            parameters rather than a JSON body.
        CACHE (bool): Whether server should cache the result of the request/
            If true then transparently adds a txn_id to all requests, and
            `_handle_request` must return a Deferred.
        RETRY_ON_TIMEOUT(bool): Whether or not to retry the request when a 504
            is received.
        RETRY_ON_CONNECT_ERROR (bool): Whether or not to retry the request when
            a connection error is received.
        RETRY_ON_CONNECT_ERROR_ATTEMPTS (int): Number of attempts to retry when
            receiving connection errors, each will backoff exponentially longer.
        WAIT_FOR_STREAMS (bool): Whether to wait for replication streams to
            catch up before processing the request and/or response. Defaults to
            True.
    """

    NAME: str = abc.abstractproperty()  # type: ignore
    PATH_ARGS: Tuple[str, ...] = abc.abstractproperty()  # type: ignore
    METHOD = "POST"
    CACHE = True
    RETRY_ON_TIMEOUT = True
    RETRY_ON_CONNECT_ERROR = True
    RETRY_ON_CONNECT_ERROR_ATTEMPTS = 5  # =63s (2^6-1)

    WAIT_FOR_STREAMS: ClassVar[bool] = True

    def __init__(self, hs: "HomeServer"):
        if self.CACHE:
            self.response_cache: ResponseCache[str] = ResponseCache(
                hs.get_clock(), "repl." + self.NAME, timeout_ms=30 * 60 * 1000
            )

        # We reserve `instance_name` as a parameter to sending requests, so we
        # assert here that sub classes don't try and use the name.
        assert "instance_name" not in self.PATH_ARGS, (
            "`instance_name` is a reserved parameter name"
        )
        assert (
            "instance_name"
            not in signature(self.__class__._serialize_payload).parameters
        ), "`instance_name` is a reserved parameter name"

        assert self.METHOD in ("PUT", "POST", "GET")

        self._replication_secret = None
        if hs.config.worker.worker_replication_secret:
            self._replication_secret = hs.config.worker.worker_replication_secret

        self._streams = hs.get_replication_command_handler().get_streams_to_replicate()
        self._replication = hs.get_replication_data_handler()
        self._instance_name = hs.get_instance_name()

    def _check_auth(self, request: Request) -> None:
        # Get the authorization header.
        auth_headers = request.requestHeaders.getRawHeaders(b"Authorization")

        if not auth_headers:
            raise RuntimeError("Missing Authorization header.")
        if len(auth_headers) > 1:
            raise RuntimeError("Too many Authorization headers.")
        parts = auth_headers[0].split(b" ")
        if parts[0] == b"Bearer" and len(parts) == 2:
            received_secret = parts[1].decode("ascii")
            if self._replication_secret == received_secret:
                # Success!
                return

        raise RuntimeError("Invalid Authorization header.")

    @abc.abstractmethod
    async def _serialize_payload(**kwargs) -> JsonDict:
        """Static method that is called when creating a request.

        Concrete implementations should have explicit parameters (rather than
        kwargs) so that an appropriate exception is raised if the client is
        called with unexpected parameters. All PATH_ARGS must appear in
        argument list.

        Returns:
            If POST/PUT request then dictionary must be JSON serialisable,
            otherwise must be appropriate for adding as query args.
        """
        return {}

    @abc.abstractmethod
    async def _handle_request(
        self, request: Request, content: JsonDict, **kwargs: Any
    ) -> Tuple[int, JsonDict]:
        """Handle incoming request.

        This is called with the request object and PATH_ARGS.

        Returns:
            HTTP status code and a JSON serialisable dict to be used as response
            body of request.
        """

    @classmethod
    def make_client(cls, hs: "HomeServer") -> Callable:
        """Create a client that makes requests.

        Returns a callable that accepts the same parameters as
        `_serialize_payload`, and also accepts an optional `instance_name`
        parameter to specify which instance to hit (the instance must be in
        the `instance_map` config).
        """
        clock = hs.get_clock()
        client = hs.get_replication_client()
        local_instance_name = hs.get_instance_name()

        instance_map = hs.config.worker.instance_map

        outgoing_gauge = _pending_outgoing_requests.labels(cls.NAME)

        replication_secret = None
        if hs.config.worker.worker_replication_secret:
            replication_secret = hs.config.worker.worker_replication_secret.encode(
                "ascii"
            )

        @trace_with_opname("outgoing_replication_request")
        async def send_request(
            *, instance_name: str = MAIN_PROCESS_INSTANCE_NAME, **kwargs: Any
        ) -> Any:
            # We have to pull these out here to avoid circular dependencies...
            streams = hs.get_replication_command_handler().get_streams_to_replicate()
            replication = hs.get_replication_data_handler()

            with outgoing_gauge.track_inprogress():
                if instance_name == local_instance_name:
                    raise Exception("Trying to send HTTP request to self")
                if instance_name not in instance_map:
                    raise Exception(
                        "Instance %r not in 'instance_map' config" % (instance_name,)
                    )

                data = await cls._serialize_payload(**kwargs)

                if cls.METHOD != "GET" and cls.WAIT_FOR_STREAMS:
                    # Include the current stream positions that we write to. We
                    # don't do this for GETs as they don't have a body, and we
                    # generally assume that a GET won't rely on data we have
                    # written.
                    if _STREAM_POSITION_KEY in data:
                        raise Exception(
                            "data to send contains %r key", _STREAM_POSITION_KEY
                        )

                    data[_STREAM_POSITION_KEY] = {
                        "streams": {
                            stream.NAME: stream.minimal_local_current_token()
                            for stream in streams
                        },
                        "instance_name": local_instance_name,
                    }

                url_args = [
                    urllib.parse.quote(kwargs[name], safe="") for name in cls.PATH_ARGS
                ]

                if cls.CACHE:
                    txn_id = random_string(10)
                    url_args.append(txn_id)

                if cls.METHOD == "POST":
                    request_func: Callable[..., Awaitable[Any]] = (
                        client.post_json_get_json
                    )
                elif cls.METHOD == "PUT":
                    request_func = client.put_json
                elif cls.METHOD == "GET":
                    request_func = client.get_json
                else:
                    # We have already asserted in the constructor that a
                    # compatible was picked, but lets be paranoid.
                    raise Exception(
                        "Unknown METHOD on %s replication endpoint" % (cls.NAME,)
                    )

                # Hard code a special scheme to show this only used for replication. The
                # instance_name will be passed into the ReplicationEndpointFactory to
                # determine connection details from the instance_map.
                uri = "synapse-replication://%s/_synapse/replication/%s/%s" % (
                    instance_name,
                    cls.NAME,
                    "/".join(url_args),
                )

                headers: Dict[bytes, List[bytes]] = {}
                # Add an authorization header, if configured.
                if replication_secret:
                    headers[b"Authorization"] = [b"Bearer " + replication_secret]
                opentracing.inject_header_dict(headers, check_destination=False)

                try:
                    # Keep track of attempts made so we can bail if we don't manage to
                    # connect to the target after N tries.
                    attempts = 0
                    # We keep retrying the same request for timeouts. This is so that we
                    # have a good idea that the request has either succeeded or failed
                    # on the master, and so whether we should clean up or not.
                    while True:
                        try:
                            result = await request_func(uri, data, headers=headers)
                            break
                        except RequestTimedOutError:
                            if not cls.RETRY_ON_TIMEOUT:
                                raise

                            logger.warning("%s request timed out; retrying", cls.NAME)

                            # If we timed out we probably don't need to worry about backing
                            # off too much, but lets just wait a little anyway.
                            await clock.sleep(1)
                        except (ConnectError, DNSLookupError) as e:
                            if not cls.RETRY_ON_CONNECT_ERROR:
                                raise
                            if attempts > cls.RETRY_ON_CONNECT_ERROR_ATTEMPTS:
                                raise

                            delay = 2**attempts
                            logger.warning(
                                "%s request connection failed; retrying in %ds: %r",
                                cls.NAME,
                                delay,
                                e,
                            )

                            await clock.sleep(delay)
                            attempts += 1
                except HttpResponseException as e:
                    # We convert to SynapseError as we know that it was a SynapseError
                    # on the main process that we should send to the client. (And
                    # importantly, not stack traces everywhere)
                    _outgoing_request_counter.labels(cls.NAME, e.code).inc()
                    raise e.to_synapse_error()
                except Exception as e:
                    _outgoing_request_counter.labels(cls.NAME, "ERR").inc()
                    raise SynapseError(
                        502, f"Failed to talk to {instance_name} process"
                    ) from e

                _outgoing_request_counter.labels(cls.NAME, 200).inc()

                # Wait on any streams that the remote may have written to.
                for stream_name, position in result.pop(
                    _STREAM_POSITION_KEY, {}
                ).items():
                    await replication.wait_for_stream_position(
                        instance_name=instance_name,
                        stream_name=stream_name,
                        position=position,
                    )

                return result

        return send_request

    def register(self, http_server: HttpServer) -> None:
        """Called by the server to register this as a handler to the
        appropriate path.
        """

        url_args = list(self.PATH_ARGS)
        method = self.METHOD

        if self.CACHE and is_function_cancellable(self._handle_request):
            raise Exception(
                f"{self.__class__.__name__} has been marked as cancellable, but CACHE "
                "is set. The cancellable flag would have no effect."
            )

        if self.CACHE:
            url_args.append("txn_id")

        args = "/".join("(?P<%s>[^/]+)" % (arg,) for arg in url_args)
        pattern = re.compile("^/_synapse/replication/%s/%s$" % (self.NAME, args))

        http_server.register_paths(
            method,
            [pattern],
            self._check_auth_and_handle,
            self.__class__.__name__,
        )

    async def _check_auth_and_handle(
        self, request: SynapseRequest, **kwargs: Any
    ) -> Tuple[int, JsonDict]:
        """Called on new incoming requests when caching is enabled. Checks
        if there is a cached response for the request and returns that,
        otherwise calls `_handle_request` and caches its response.
        """
        # We just use the txn_id here, but we probably also want to use the
        # other PATH_ARGS as well.

        # Check the authorization headers before handling the request.
        if self._replication_secret:
            self._check_auth(request)

        if self.METHOD == "GET":
            # GET APIs always have an empty body.
            content = {}
        else:
            content = parse_json_object_from_request(request)

        # Wait on any streams that the remote may have written to.
        for stream_name, position in content.get(_STREAM_POSITION_KEY, {"streams": {}})[
            "streams"
        ].items():
            await self._replication.wait_for_stream_position(
                instance_name=content[_STREAM_POSITION_KEY]["instance_name"],
                stream_name=stream_name,
                position=position,
            )

        if self.CACHE:
            txn_id = kwargs.pop("txn_id")

            # We ignore the `@cancellable` flag, since cancellation wouldn't interupt
            # `_handle_request` and `ResponseCache` does not handle cancellation
            # correctly yet. In particular, there may be issues to do with logging
            # context lifetimes.

            code, response = await self.response_cache.wrap(
                txn_id, self._handle_request, request, content, **kwargs
            )
            # Take a copy so we don't mutate things in the cache.
            response = dict(response)
        else:
            # The `@cancellable` decorator may be applied to `_handle_request`. But we
            # told `HttpServer.register_paths` that our handler is `_check_auth_and_handle`,
            # so we have to set up the cancellable flag ourselves.
            request.is_render_cancellable = is_function_cancellable(
                self._handle_request
            )

            code, response = await self._handle_request(request, content, **kwargs)

        # Return streams we may have written to in the course of processing this
        # request.
        if _STREAM_POSITION_KEY in response:
            raise Exception("data to send contains %r key", _STREAM_POSITION_KEY)

        if self.WAIT_FOR_STREAMS:
            response[_STREAM_POSITION_KEY] = {
                stream.NAME: stream.minimal_local_current_token()
                for stream in self._streams
            }

        return code, response
