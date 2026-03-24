#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2018-2021 The Matrix.org Foundation C.I.C.
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
import hashlib
import ipaddress
import json
import logging
import os
import os.path
import sqlite3
import time
import uuid
import warnings
import weakref
from collections import deque
from io import SEEK_END, BytesIO
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    MutableMapping,
    Optional,
    Sequence,
    TypeVar,
    Union,
    cast,
)
from unittest.mock import Mock, patch

import attr
from incremental import Version
from typing_extensions import ParamSpec
try:
    from zope.interface import Interface, implementer
except ImportError:
    class Interface:  # type: ignore[no-redef]
        pass
    def implementer(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        def decorator(cls: Any) -> Any:
            return cls
        return decorator

# Provide Twisted interfaces or stubs for test class decorators
try:
    from twisted.internet.interfaces import (
        IConsumer,
        IPushProducer,
        ITCPTransport,
        ITransport,
    )
except ImportError:
    class ITransport(Interface):  # type: ignore[no-redef]
        pass
    class IPushProducer(Interface):  # type: ignore[no-redef]
        pass
    class IConsumer(Interface):  # type: ignore[no-redef]
        pass
    class ITCPTransport(Interface):  # type: ignore[no-redef]
        pass

from synapse.http.aiohttp_shim import SynapseRequest as Request
from synapse.http.resource import Resource

# Twisted imports — optional, with asyncio fallbacks
try:
    from twisted.internet.testing import MemoryReactorClock
    from twisted.internet.interfaces import IReactorPluggableNameResolver
    from twisted.web.server import Site
    _HAS_TWISTED = True
except ImportError:
    MemoryReactorClock = object  # type: ignore[assignment,misc]
    IReactorPluggableNameResolver = object  # type: ignore[assignment,misc]
    Site = object  # type: ignore[assignment,misc]
    _HAS_TWISTED = False

# These are used as type annotations or in a few test utilities
IResource = Any
Failure = Exception  # type: ignore[assignment,misc]
Headers = Any  # type: ignore[assignment,misc]

from synapse.api.constants import MAX_REQUEST_SIZE
from synapse.config.database import DatabaseConnectionConfig
from synapse.config.homeserver import HomeServerConfig
from synapse.events.auto_accept_invites import InviteAutoAccepter
from synapse.events.presence_router import load_legacy_presence_router
from synapse.handlers.auth import load_legacy_password_auth_providers
from synapse.http.site import SynapseRequest
from synapse.logging.context import ContextResourceUsage
from synapse.module_api.callbacks.spamchecker_callbacks import load_legacy_spam_checkers
from synapse.module_api.callbacks.third_party_event_rules_callbacks import (
    load_legacy_third_party_event_rules,
)
from synapse.server import HomeServer
from synapse.server_notices.consent_server_notices import ConfigError
from synapse.storage import DataStore
from synapse.storage.database import LoggingDatabaseConnection, make_pool
from synapse.storage.engines import BaseDatabaseEngine, create_engine
from synapse.storage.prepare_database import prepare_database
from synapse.types import ISynapseReactor, JsonDict
from synapse.util.clock import Clock

from tests.utils import (
    LEAVE_DB,
    POSTGRES_BASE_DB,
    POSTGRES_HOST,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
    SQLITE_PERSIST_DB,
    USE_POSTGRES_FOR_TESTS,
    default_config,
)

logger = logging.getLogger(__name__)

R = TypeVar("R")
P = ParamSpec("P")

# the type of thing that can be passed into `make_request` in the headers list
CustomHeaderType = tuple[str | bytes, str | bytes]

# A pre-prepared SQLite DB that is used as a template when creating new SQLite
# DB each test run. This dramatically speeds up test set up when using SQLite.
PREPPED_SQLITE_DB_CONN: LoggingDatabaseConnection | None = None


class TimedOutException(Exception):
    """
    A web query timed out.
    """


@implementer(ITransport, IPushProducer, IConsumer)
@attr.s(auto_attribs=True)
class FakeChannel:
    """
    A fake Twisted Web Channel (the part that interfaces with the
    wire).

    See twisted.web.http.HTTPChannel.
    """

    site: Union[Site, "FakeSite"]
    _reactor: MemoryReactorClock
    _result: dict = attr.Factory(dict)
    _ip: str = "127.0.0.1"
    _producer: Optional[Union[IPullProducer, IPushProducer]] = None
    resource_usage: ContextResourceUsage | None = None
    _request: Request | None = None
    _clock: Any = None  # NativeClock, for advancing fake time

    @property
    def result(self) -> dict:
        """Return the result dict, populating from shim request if available."""
        if self._request is not None and hasattr(self._request, '_response_buffer'):
            return {
                "body": bytes(self._request._response_buffer),
                "code": self._request.code,
                "done": self._request.finished,
            }
        return self._result

    @result.setter
    def result(self, value: dict) -> None:
        self._result = value

    @property
    def request(self) -> Request:
        assert self._request is not None
        return self._request

    @request.setter
    def request(self, request: Request) -> None:
        assert self._request is None
        self._request = request

    @property
    def json_body(self) -> JsonDict:
        body = json.loads(self.text_body)
        assert isinstance(body, dict)
        return body

    @property
    def json_list(self) -> list[JsonDict]:
        body = json.loads(self.text_body)
        assert isinstance(body, list)
        return body

    @property
    def text_body(self) -> str:
        """The body of the result, utf-8-decoded.

        Raises an exception if the request has not yet completed.
        """
        if not self.is_finished():
            raise Exception("Request not yet completed")
        # Read from shim request's response buffer
        if self._request is not None and hasattr(self._request, '_response_buffer'):
            return bytes(self._request._response_buffer).decode("utf8")
        return self.result["body"].decode("utf8")

    def is_finished(self) -> bool:
        """check if the response has been completely received"""
        # Check the shim request's finished flag
        if self._request is not None and hasattr(self._request, 'finished'):
            return self._request.finished
        return self.result.get("done", False)

    @property
    def code(self) -> int:
        if self._request is not None and hasattr(self._request, 'code'):
            return int(self._request.code)
        if not self.result:
            raise Exception("No result yet.")
        return int(self.result["code"])

    @property
    def headers(self) -> Any:
        # Return the shim response headers if available
        if self._request is not None and hasattr(self._request, 'responseHeaders'):
            return self._request.responseHeaders
        if not self.result:
            raise Exception("No result yet.")

        h = self.result["headers"]
        return h

    def writeHeaders(
        self,
        version: bytes,
        code: bytes,
        reason: bytes,
        headers: Headers | list[tuple[bytes, bytes]],
    ) -> None:
        self.result["version"] = version
        self.result["code"] = code
        self.result["reason"] = reason

        if isinstance(headers, list):
            # Support prior to Twisted 24.7.0rc1
            new_headers = Headers()
            for k, v in headers:
                assert isinstance(k, bytes), f"key is not of type bytes: {k!r}"
                assert isinstance(v, bytes), f"value is not of type bytes: {v!r}"
                new_headers.addRawHeader(k, v)
            headers = new_headers

        assert isinstance(headers, Headers), (
            f"headers are of the wrong type: {headers!r}"
        )

        self.result["headers"] = headers

    def write(self, data: bytes) -> None:
        assert isinstance(data, bytes), "Should be bytes! " + repr(data)

        if "body" not in self.result:
            self.result["body"] = b""

        self.result["body"] += data

    def writeSequence(self, data: Iterable[bytes]) -> None:
        for x in data:
            self.write(x)

    def loseConnection(self) -> None:
        self.unregisterProducer()

    # Type ignore: mypy doesn't like the fact that producer isn't an IProducer.
    def registerProducer(self, producer: IProducer, streaming: bool) -> None:
        # TODO This should ensure that the IProducer is an IPushProducer or
        # IPullProducer, unfortunately twisted.protocols.basic.FileSender does
        # implement those, but doesn't declare it.
        self._producer = cast(Union[IPushProducer, IPullProducer], producer)
        self.producerStreaming = streaming

        def _produce() -> None:
            if self._producer:
                self._producer.resumeProducing()
                self._reactor.callLater(0.1, _produce)

        if not streaming:
            self._reactor.callLater(0.0, _produce)

    def unregisterProducer(self) -> None:
        if self._producer is None:
            return

        self._producer = None

    def stopProducing(self) -> None:
        if self._producer is not None:
            self._producer.stopProducing()

    def pauseProducing(self) -> None:
        raise NotImplementedError()

    def resumeProducing(self) -> None:
        raise NotImplementedError()

    def requestDone(self, _self: Request) -> None:
        self.result["done"] = True
        if isinstance(_self, SynapseRequest):
            assert _self.logcontext is not None
            self.resource_usage = _self.logcontext.get_resource_usage()

    def getPeer(self) -> IAddress:
        # We give an address so that getClientAddress/getClientIP returns a non null entry,
        # causing us to record the MAU
        return address.IPv4Address("TCP", self._ip, 3423)

    def getHost(self) -> IAddress:
        # this is called by Request.__init__ to configure Request.host.
        return address.IPv4Address("TCP", "127.0.0.1", 8888)

    def isSecure(self) -> bool:
        return False

    @property
    def transport(self) -> "FakeChannel":
        return self

    def await_result(self, timeout_ms: int = 5000) -> None:
        """
        Wait until the request is finished by driving the asyncio event loop.
        """
        import asyncio
        import time as _time
        from tests import pump_loop

        loop = asyncio.get_event_loop()
        deadline = _time.monotonic() + timeout_ms / 1000.0

        while not self.is_finished():
            if _time.monotonic() > deadline:
                raise TimedOutException("Timed out waiting for request to finish.")

            # Advance fake time OUTSIDE the event loop drive
            self._reactor.advance(0.1)

            # Drive the event loop briefly
            try:
                pump_loop(loop, timeout=0.05, tick=0.001)
            except TimeoutError:
                pass  # outer loop will retry

    def extract_cookies(self, cookies: MutableMapping[str, str]) -> None:
        """Process the contents of any Set-Cookie headers in the response

        Any cookines found are added to the given dict
        """
        headers = self.headers.getRawHeaders("Set-Cookie")
        if not headers:
            return

        for h in headers:
            parts = h.split(";")
            k, v = parts[0].split("=", maxsplit=1)
            cookies[k] = v


class FakeSite:
    """
    A fake Twisted Web Site, with mocks of the extra things that
    Synapse adds.
    """

    server_version_string = b"1"
    site_tag = "test"
    access_logger = logging.getLogger("synapse.access.http.fake")

    def __init__(
        self,
        resource: IResource,
        reactor: IReactorTime,
        *,
        parsePOSTFormSubmission: bool = True,
    ):
        """

        Args:
            resource: the resource to be used for rendering all requests
        """
        self._resource = resource
        self.reactor = reactor
        self._parsePOSTFormSubmission = parsePOSTFormSubmission

    def getResourceFor(self, request: Request) -> IResource:
        return self._resource


async def make_request(
    reactor: MemoryReactorClock,
    site: Site | FakeSite,
    method: bytes | str,
    path: bytes | str,
    content: bytes | str | JsonDict = b"",
    access_token: str | None = None,
    request: type[Request] = SynapseRequest,
    shorthand: bool = True,
    federation_auth_origin: bytes | None = None,
    content_type: bytes | None = None,
    content_is_form: bool = False,
    await_result: bool = True,
    custom_headers: Iterable[CustomHeaderType] | None = None,
    client_ip: str = "127.0.0.1",
    clock: Any = None,
) -> FakeChannel:
    """
    Make a web request using the given method, path and content, and render it

    Returns the fake Channel object which records the response to the request.

    Args:
        reactor:
        site: The twisted Site to use to render the request
        method: The HTTP request method ("verb").
        path: The HTTP path, suitably URL encoded (e.g. escaped UTF-8 & spaces and such).
        content: The body of the request. JSON-encoded, if a str of bytes.
        access_token: The access token to add as authorization for the request.
        request: The request class to create.
        shorthand: Whether to try and be helpful and prefix the given URL
            with the usual REST API path, if it doesn't contain it.
        federation_auth_origin: if set to not-None, we will add a fake
            Authorization header pretenting to be the given server name.
        content_type: The content-type to use for the request. If not set then will default to
            application/json unless content_is_form is true.
        content_is_form: Whether the content is URL encoded form data. Adds the
            'Content-Type': 'application/x-www-form-urlencoded' header.
        await_result: whether to wait for the request to complete rendering. If true,
             will pump the reactor until the the renderer tells the channel the request
             is finished.
        custom_headers: (name, value) pairs to add as request headers
        client_ip: The IP to use as the requesting IP. Useful for testing
            ratelimiting.

    Returns:
        channel
    """
    if not isinstance(method, bytes):
        method = method.encode("ascii")

    if not isinstance(path, bytes):
        path = path.encode("ascii")

    # Decorate it to be the full path, if we're using shorthand
    if (
        shorthand
        and not path.startswith(b"/_matrix")
        and not path.startswith(b"/_synapse")
    ):
        if path.startswith(b"/"):
            path = path[1:]
        path = b"/_matrix/client/r0/" + path

    if not path.startswith(b"/"):
        path = b"/" + path

    if isinstance(content, dict):
        content = json.dumps(content).encode("utf8")
    if isinstance(content, str):
        content = content.encode("utf8")

    channel = FakeChannel(site, reactor, ip=client_ip, clock=clock)

    # Create the request via the shim — it handles path/query splitting,
    # query arg parsing, etc.
    from synapse.http.aiohttp_shim import SynapseRequest as ShimRequest
    req = ShimRequest.for_testing(
        channel,
        site,
        method=method,
        uri=path,
        body=content,
        client_ip=client_ip,
        max_request_body_size=MAX_REQUEST_SIZE,
    )
    channel.request = req

    # Add standard headers (test-specific, not part of the shim)
    if custom_headers is None or not any(
        (k if isinstance(k, bytes) else k.encode("ascii")) == b"Content-Length"
        for k, _ in custom_headers
    ):
        req.requestHeaders.addRawHeader(
            b"Content-Length", str(len(content)).encode("ascii")
        )

    if access_token:
        req.requestHeaders.addRawHeader(
            b"Authorization", b"Bearer " + access_token.encode("ascii")
        )

    if federation_auth_origin is not None:
        req.requestHeaders.addRawHeader(
            b"Authorization",
            b"X-Matrix origin=%s,key=,sig=" % (federation_auth_origin,),
        )

    if content:
        if content_type is not None:
            req.requestHeaders.addRawHeader(b"Content-Type", content_type)
        elif content_is_form:
            req.requestHeaders.addRawHeader(
                b"Content-Type", b"application/x-www-form-urlencoded"
            )
        else:
            req.requestHeaders.addRawHeader(b"Content-Type", b"application/json")

    if custom_headers:
        for k, v in custom_headers:
            req.requestHeaders.addRawHeader(k, v)

    # Finalise request state (form body parsing, metrics, logcontext)
    # and dispatch through the resource tree.
    req.prepare_for_dispatch(content_is_form=content_is_form)

    root_resource = getattr(site, 'resource', None) or getattr(site, '_resource', None)
    if root_resource is not None:
        req.dispatch(root_resource)

    # Auto-detect clock from reactor if not explicitly provided.
    # This ensures fake time works even when callers don't pass clock=.
    if clock is None and hasattr(reactor, '_clock'):
        clock = reactor._clock

    if await_result and req.render_deferred is not None:
        import asyncio

        # Advance fake time in a background task so clock.sleep() calls
        # (ratelimit pauses) and call_later timers resolve. We advance
        # 1ms per event loop iteration — small enough to not refill
        # ratelimit buckets, large enough for pauses to complete.
        async def _advance_time() -> None:
            while not req.render_deferred.done():
                if clock is not None:
                    clock.advance(0.001)
                await asyncio.sleep(0)

        advancer = asyncio.ensure_future(_advance_time())
        try:
            await req.render_deferred
        finally:
            advancer.cancel()
            try:
                await advancer
            except asyncio.CancelledError:
                pass

    return channel


# ISynapseReactor implies IReactorPluggableNameResolver, but explicitly
# marking this as an implementer of the latter seems to keep mypy-zope happier.
class ThreadedMemoryReactorClock:
    """A minimal fake reactor for tests.

    Provides the subset of reactor methods that Synapse tests need:
    ``advance()``, ``callFromThread()``, ``getThreadPool()``, ``run()``.

    This replaces the Twisted ``MemoryReactorClock`` with a pure-asyncio
    implementation. Time advancement is delegated to a ``NativeClock``
    instance — call ``set_clock()`` after construction (done automatically
    by ``get_clock()``).
    """

    def __init__(self) -> None:
        self._clock: Any = None  # will be set to a NativeClock by get_clock()
        self._thread_callbacks: deque[Callable[..., Any]] = deque()
        self.lookups: dict[str, str] = {}
        self.triggers: dict[Any, Any] = {}
        self.threadpool = _FakeThreadPool()

    def set_clock(self, clock: Any) -> None:
        """Link this reactor to a NativeClock so advance() delegates to it."""
        self._clock = clock

    def seconds(self) -> float:
        """Return the current (fake) time."""
        if self._clock is not None:
            return self._clock.time()
        return time.time()

    def run(self) -> None:
        """No-op — the asyncio loop is managed elsewhere."""
        pass

    def advance(self, amount: float) -> None:
        """Advance fake time by *amount* seconds.

        Delegates to ``NativeClock.advance()`` to fire any pending
        ``clock.sleep()`` calls, then drains the ``callFromThread`` queue.
        """
        if self._clock is not None:
            self._clock.advance(amount)

        # Drain callFromThread callbacks
        while True:
            try:
                callback = self._thread_callbacks.popleft()
            except IndexError:
                break
            callback()

    def callFromThread(
        self, callable: Callable[..., Any], *args: object, **kwargs: object
    ) -> None:
        cb = lambda: callable(*args, **kwargs)
        self._thread_callbacks.append(cb)

    def callWhenRunning(self, callable: Callable[..., Any], *args: Any) -> None:
        """Run the callback immediately (we're always 'running')."""
        callable(*args)

    def addSystemEventTrigger(
        self, phase: str, event_type: str, callable: Callable[..., Any], *args: Any
    ) -> Any:
        key = (phase, event_type)
        self.triggers[key] = (callable, args)
        return key

    def removeSystemEventTrigger(self, trigger_id: Any) -> None:
        """Remove a previously registered system event trigger."""
        self.triggers.pop(trigger_id, None)

    def getThreadPool(self) -> Any:
        return self.threadpool

    def callInThread(
        self, callable: Callable[..., Any], *args: object, **kwargs: object
    ) -> None:
        callable(*args, **kwargs)

    def suggestThreadPoolSize(self, size: int) -> None:
        pass

    def pump(self, timings: list[float] | None = None) -> None:
        """Pump the reactor — advance time for each timing value."""
        if timings:
            for t in timings:
                self.advance(t)
        else:
            self.advance(0)


class _FakeThreadPool:
    """Minimal threadpool stub for tests."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def callInThreadWithCallback(
        self,
        onResult: Callable,
        func: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        # Run synchronously — no real threading in tests
        try:
            result = func(*args, **kwargs)
            onResult(True, result)
        except Exception as e:
            onResult(False, e)


def cleanup_test_reactor_system_event_triggers(
    reactor: ThreadedMemoryReactorClock,
) -> None:
    """Cleanup any registered system event triggers."""
    reactor.triggers.clear()


def validate_connector(connector: Any, expected_ip: str) -> None:
    """Stub — TCP connector validation is a Twisted concept.
    IPv4Address, with the hostname as the literal host that was given (which
    could be an IPv6-only host or an IPv6 literal).

    But when called from reactor.connectTCP *through* e.g. an Endpoint, the
    connector's destination will contain the specific IP address with the
    correct network stack class.

    Note that testing code paths that use connectTCP directly should not be
    affected by this check, unless they specifically add a test with a
    matching reactor.lookups[HOSTNAME] = "IPv6Literal", where reactor is of
    type ThreadedMemoryReactorClock.
    For an example of implementing such tests, see test/handlers/send_email.py.
    """
    destination = connector.getDestination()

    # We use address.IPv{4,6}Address to check what the reactor thinks it is
    # is sending but check for validity with ipaddress.IPv{4,6}Address
    # because they fail with IPs on the wrong network stack.
    cls_mapping = {
        address.IPv4Address: ipaddress.IPv4Address,
        address.IPv6Address: ipaddress.IPv6Address,
    }

    cls = cls_mapping.get(destination.__class__)

    if cls is not None:
        try:
            cls(expected_ip)
        except Exception as exc:
            raise ValueError(
                "Invalid IP type and resolution for %s. Expected %s to be %s"
                % (destination, expected_ip, cls.__name__)
            ) from exc
    else:
        raise ValueError(
            "Unknown address type %s for %s"
            % (destination.__class__.__name__, destination)
        )


def make_fake_db_pool(
    reactor: ISynapseReactor,
    db_config: DatabaseConnectionConfig,
    engine: BaseDatabaseEngine,
    server_name: str,
) -> adbapi.ConnectionPool:
    """Wrapper for `make_pool` which builds a pool which runs db queries synchronously.

    For more deterministic testing, we don't use a regular db connection pool: instead
    we run all db queries synchronously on the test reactor's main thread. This function
    is a drop-in replacement for the normal `make_pool` which builds such a connection
    pool.
    """
    pool = make_pool(
        reactor=reactor, db_config=db_config, engine=engine, server_name=server_name
    )

    def runWithConnection(
        func: Callable[..., R], *args: Any, **kwargs: Any
    ) -> Awaitable[R]:
        return threads.deferToThreadPool(
            pool._reactor,
            pool.threadpool,
            pool._runWithConnection,
            func,
            *args,
            **kwargs,
        )

    def runInteraction(
        desc: str, func: Callable[..., R], *args: Any, **kwargs: Any
    ) -> Awaitable[R]:
        return threads.deferToThreadPool(
            pool._reactor,
            pool.threadpool,
            pool._runInteraction,
            desc,
            func,
            *args,
            **kwargs,
        )

    pool.runWithConnection = runWithConnection  # type: ignore[method-assign]
    pool.runInteraction = runInteraction  # type: ignore[assignment]
    # Replace the thread pool with a threadless 'thread' pool
    pool.threadpool = ThreadPool(reactor)
    pool.running = True
    return pool


class ThreadPool:
    """
    Threadless thread pool.

    See twisted.python.threadpool.ThreadPool
    """

    def __init__(self, reactor: IReactorTime):
        self._reactor = reactor

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def callInThreadWithCallback(
        self,
        onResult: Callable[[bool, Failure | R], None],
        function: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> "Deferred[None]":
        def _(res: Any) -> None:
            if isinstance(res, Failure):
                onResult(False, res)
            else:
                onResult(True, res)

        d: "Deferred[None]" = Deferred()
        d.addCallback(lambda x: function(*args, **kwargs))
        d.addBoth(_)
        # mypy ignored here because:
        #   - this is part of the test infrastructure (outside of Synapse) so tracking
        #     these calls for for homeserver shutdown doesn't make sense.
        self._reactor.callLater(0, d.callback, True)  # type: ignore[call-later-not-tracked]
        return d


def get_clock() -> tuple[ThreadedMemoryReactorClock, Clock]:
    """Create a linked fake-reactor and clock for tests.

    The reactor and clock share the same fake time source:
    calling ``reactor.advance(N)`` or ``clock.advance(N)`` both advance
    the same underlying fake time.  The clock starts in fake-time mode
    so that ``clock.time()`` returns deterministic values from the start.
    """
    from synapse.util.clock import NativeClock

    reactor = ThreadedMemoryReactorClock()
    hs_clock = NativeClock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

    # Link the reactor to the clock so reactor.advance() delegates properly.
    reactor.set_clock(hs_clock)

    # Put the clock into fake-time mode from the start.
    # Tests that need deterministic time get it automatically;
    # advance(0) enables fake time without changing the initial value.
    hs_clock.advance(0)

    return reactor, hs_clock


@implementer(ITCPTransport)
@attr.s(cmp=False, auto_attribs=True)
class FakeTransport:
    """
    A twisted.internet.interfaces.ITransport implementation which sends all its data
    straight into an IProtocol object: it exists to connect two IProtocols together.

    To use it, instantiate it with the receiving IProtocol, and then pass it to the
    sending IProtocol's makeConnection method:

        server = HTTPChannel()
        client.makeConnection(FakeTransport(server, self.reactor))

    If you want bidirectional communication, you'll need two instances.
    """

    other: IProtocol
    """The Protocol object which will receive any data written to this transport.
    """

    _reactor: IReactorTime
    """Test reactor
    """

    _protocol: Optional[IProtocol] = None
    """The Protocol which is producing data for this transport. Optional, but if set
    will get called back for connectionLost() notifications etc.
    """

    _peer_address: IPv4Address | IPv6Address = attr.Factory(
        lambda: address.IPv4Address("TCP", "127.0.0.1", 5678)
    )
    """The value to be returned by getPeer"""

    _host_address: IPv4Address | IPv6Address = attr.Factory(
        lambda: address.IPv4Address("TCP", "127.0.0.1", 1234)
    )
    """The value to be returned by getHost"""

    disconnecting = False
    disconnected = False
    connected = True
    buffer: bytes = b""
    producer: Optional[IPushProducer] = None
    autoflush: bool = True

    def getPeer(self) -> IPv4Address | IPv6Address:
        return self._peer_address

    def getHost(self) -> IPv4Address | IPv6Address:
        return self._host_address

    def loseConnection(self) -> None:
        if not self.disconnecting:
            logger.info("FakeTransport: loseConnection()")
            self.disconnecting = True
            if self._protocol:
                self._protocol.connectionLost(
                    Failure(RuntimeError("FakeTransport.loseConnection()"))
                )

            # if we still have data to write, delay until that is done
            if self.buffer:
                logger.info(
                    "FakeTransport: Delaying disconnect until buffer is flushed"
                )
            else:
                self.connected = False
                self.disconnected = True

    def abortConnection(self) -> None:
        logger.info("FakeTransport: abortConnection()")

        if not self.disconnecting:
            self.disconnecting = True
            if self._protocol:
                self._protocol.connectionLost(None)  # type: ignore[arg-type]

        self.disconnected = True

    def pauseProducing(self) -> None:
        if not self.producer:
            return

        self.producer.pauseProducing()

    def resumeProducing(self) -> None:
        if not self.producer:
            return
        self.producer.resumeProducing()

    def unregisterProducer(self) -> None:
        if not self.producer:
            return

        self.producer = None

    def registerProducer(self, producer: IPushProducer, streaming: bool) -> None:
        self.producer = producer
        self.producerStreaming = streaming

        def _produce() -> None:
            if not self.producer:
                # we've been unregistered
                return
            # some implementations of IProducer (for example, FileSender)
            # don't return a deferred.
            d = maybeDeferred(self.producer.resumeProducing)
            # mypy ignored here because:
            #   - this is part of the test infrastructure (outside of Synapse) so tracking
            #     these calls for for homeserver shutdown doesn't make sense.
            d.addCallback(lambda x: self._reactor.callLater(0.1, _produce))  # type: ignore[call-later-not-tracked,call-overload]

        if not streaming:
            # mypy ignored here because:
            #   - this is part of the test infrastructure (outside of Synapse) so tracking
            #     these calls for for homeserver shutdown doesn't make sense.
            self._reactor.callLater(0.0, _produce)  # type: ignore[call-later-not-tracked]

    def write(self, byt: bytes) -> None:
        if self.disconnecting:
            raise Exception("Writing to disconnecting FakeTransport")

        self.buffer = self.buffer + byt

        # always actually do the write asynchronously. Some protocols (notably the
        # TLSMemoryBIOProtocol) get very confused if a read comes back while they are
        # still doing a write. Doing a callLater here breaks the cycle.
        if self.autoflush:
            # mypy ignored here because:
            #   - this is part of the test infrastructure (outside of Synapse) so tracking
            #     these calls for for homeserver shutdown doesn't make sense.
            self._reactor.callLater(0.0, self.flush)  # type: ignore[call-later-not-tracked]

    def writeSequence(self, seq: Iterable[bytes]) -> None:
        for x in seq:
            self.write(x)

    def flush(self, maxbytes: int | None = None) -> None:
        if not self.buffer:
            # nothing to do. Don't write empty buffers: it upsets the
            # TLSMemoryBIOProtocol
            return

        if self.disconnected:
            return

        if maxbytes is not None:
            to_write = self.buffer[:maxbytes]
        else:
            to_write = self.buffer

        logger.info("%s->%s: %s", self._protocol, self.other, to_write)

        try:
            self.other.dataReceived(to_write)
        except Exception as e:
            logger.exception("Exception writing to protocol: %s", e)
            return

        self.buffer = self.buffer[len(to_write) :]
        if self.buffer and self.autoflush:
            # mypy ignored here because:
            #   - this is part of the test infrastructure (outside of Synapse) so tracking
            #     these calls for for homeserver shutdown doesn't make sense.
            self._reactor.callLater(0.0, self.flush)  # type: ignore[call-later-not-tracked]

        if not self.buffer and self.disconnecting:
            logger.info("FakeTransport: Buffer now empty, completing disconnect")
            self.disconnected = True

    ## ITCPTransport methods. ##

    def loseWriteConnection(self) -> None:
        """
        Half-close the write side of a TCP connection.

        If the protocol instance this is attached to provides
        IHalfCloseableProtocol, it will get notified when the operation is
        done. When closing write connection, as with loseConnection this will
        only happen when buffer has emptied and there is no registered
        producer.
        """
        raise NotImplementedError()

    def getTcpNoDelay(self) -> bool:
        """
        Return if C{TCP_NODELAY} is enabled.
        """
        return False

    def setTcpNoDelay(self, enabled: bool) -> None:
        """
        Enable/disable C{TCP_NODELAY}.

        Enabling C{TCP_NODELAY} turns off Nagle's algorithm. Small packets are
        sent sooner, possibly at the expense of overall throughput.
        """
        # Ignore setting this.

    def getTcpKeepAlive(self) -> bool:
        """
        Return if C{SO_KEEPALIVE} is enabled.
        """
        return False

    def setTcpKeepAlive(self, enabled: bool) -> None:
        """
        Enable/disable C{SO_KEEPALIVE}.

        Enabling C{SO_KEEPALIVE} sends packets periodically when the connection
        is otherwise idle, usually once every two hours. They are intended
        to allow detection of lost peers in a non-infinite amount of time.
        """
        # Ignore setting this.


def connect_client(
    reactor: ThreadedMemoryReactorClock, client_id: int
) -> tuple[IProtocol, AccumulatingProtocol]:
    """
    Connect a client to a fake TCP transport.

    Args:
        reactor
        factory: The connecting factory to build.
    """
    factory = reactor.tcpClients.pop(client_id)[2]
    client = factory.buildProtocol(None)
    server = AccumulatingProtocol()
    server.makeConnection(FakeTransport(client, reactor))
    client.makeConnection(FakeTransport(server, reactor))

    return client, server


class TestHomeServer(HomeServer):
    DATASTORE_CLASS = DataStore


def setup_test_homeserver(
    *,
    cleanup_func: Callable[[Callable[[], Optional["Deferred[None]"]]], None],
    server_name: str = "test",
    config: HomeServerConfig | None = None,
    reactor: Optional[ISynapseReactor] = None,
    homeserver_to_use: type[HomeServer] = TestHomeServer,
    db_txn_limit: int | None = None,
    **extra_homeserver_attributes: Any,
) -> HomeServer:
    """
    Setup a homeserver suitable for running tests against.  Keyword arguments
    are passed to the Homeserver constructor.

    If no datastore is supplied, one is created and given to the homeserver.

    Args:
        cleanup_func: The function used to register a cleanup routine for
            after the test. If the function returns a Deferred, the
            test case will wait until the Deferred has fired before
            proceeding to the next cleanup function.
        server_name: Homeserver name
        config: Homeserver config
        reactor: Twisted reactor
        homeserver_to_use: Homeserver class to instantiate.
        db_txn_limit: Gives the maximum number of database transactions to run per
            connection before reconnecting. 0 means no limit. If unset, defaults to None
            here which will default upstream to `0`.
        **extra_homeserver_attributes: Additional keyword arguments to install as
            `@cache_in_self` attributes on the homeserver. For example, `clock` will be
            installed as `hs._clock`.

    Calling this method directly is deprecated: you should instead derive from
    HomeserverTestCase.
    """
    if reactor is None:
        reactor = ThreadedMemoryReactorClock()

    if config is None:
        config = default_config(server_name, parse=True)

    server_name = config.server.server_name
    if not isinstance(server_name, str):
        raise ConfigError("Must be a string", ("server_name",))

    if "clock" not in extra_homeserver_attributes:
        # Ignore `multiple-internal-clocks` linter error here since we are creating a `Clock`
        # for testing purposes (i.e. outside of Synapse).
        extra_homeserver_attributes["clock"] = Clock(reactor, server_name=server_name)  # type: ignore[multiple-internal-clocks]

    config.caches.resize_all_caches()

    if USE_POSTGRES_FOR_TESTS:
        test_db = "synapse_test_%s" % uuid.uuid4().hex

        database_config: JsonDict = {
            "name": "psycopg2",
            "args": {
                "dbname": test_db,
                "host": POSTGRES_HOST,
                "password": POSTGRES_PASSWORD,
                "user": POSTGRES_USER,
                "port": POSTGRES_PORT,
                "cp_min": 1,
                "cp_max": 5,
            },
        }
    else:
        if SQLITE_PERSIST_DB:
            # The current working directory is in _trial_temp, so this gets created within that directory.
            test_db_location = os.path.abspath("test.db")
            logger.debug("Will persist db to %s", test_db_location)
            # Ensure each test gets a clean database.
            try:
                os.remove(test_db_location)
            except FileNotFoundError:
                pass
            else:
                logger.debug("Removed existing DB at %s", test_db_location)
        else:
            test_db_location = ":memory:"

        database_config = {
            "name": "sqlite3",
            "args": {"database": test_db_location, "cp_min": 1, "cp_max": 1},
        }

        # Check if we have set up a DB that we can use as a template.
        global PREPPED_SQLITE_DB_CONN
        if PREPPED_SQLITE_DB_CONN is None:
            temp_engine = create_engine(database_config)
            PREPPED_SQLITE_DB_CONN = LoggingDatabaseConnection(
                conn=sqlite3.connect(":memory:", check_same_thread=False),
                engine=temp_engine,
                default_txn_name="PREPPED_CONN",
                server_name=server_name,
            )

            database = DatabaseConnectionConfig("master", database_config)
            config.database.databases = [database]
            prepare_database(
                PREPPED_SQLITE_DB_CONN, create_engine(database_config), config
            )

        database_config["_TEST_PREPPED_CONN"] = PREPPED_SQLITE_DB_CONN

    if db_txn_limit is not None:
        database_config["txn_limit"] = db_txn_limit

    database = DatabaseConnectionConfig("master", database_config)
    config.database.databases = [database]

    db_engine = create_engine(database.config)

    # Create the database before we actually try and connect to it, based off
    # the template database we generate in setupdb()
    if USE_POSTGRES_FOR_TESTS:
        db_conn = db_engine.module.connect(
            dbname=POSTGRES_BASE_DB,
            user=POSTGRES_USER,
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            password=POSTGRES_PASSWORD,
        )
        db_engine.attempt_to_set_autocommit(db_conn, True)
        cur = db_conn.cursor()
        cur.execute("DROP DATABASE IF EXISTS %s;" % (test_db,))
        cur.execute(
            "CREATE DATABASE %s WITH TEMPLATE %s;" % (test_db, POSTGRES_BASE_DB)
        )
        cur.close()
        db_conn.close()

        def cleanup() -> None:
            import psycopg2

            dropped = False

            # Drop the test database
            db_conn = db_engine.module.connect(
                dbname=POSTGRES_BASE_DB,
                user=POSTGRES_USER,
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                password=POSTGRES_PASSWORD,
            )
            db_engine.attempt_to_set_autocommit(db_conn, True)
            cur = db_conn.cursor()

            # Try a few times to drop the DB. Some things may hold on to the
            # database for a few more seconds due to flakiness, preventing
            # us from dropping it when the test is over. If we can't drop
            # it, warn and move on.
            for _ in range(5):
                try:
                    cur.execute("DROP DATABASE IF EXISTS %s;" % (test_db,))
                    db_conn.commit()
                    dropped = True
                except psycopg2.OperationalError as e:
                    warnings.warn(
                        "Couldn't drop old db: " + str(e),
                        category=UserWarning,
                        stacklevel=2,
                    )
                    time.sleep(0.5)

            cur.close()
            db_conn.close()

            if not dropped:
                warnings.warn(
                    "Failed to drop old DB.",
                    category=UserWarning,
                    stacklevel=2,
                )

        if not LEAVE_DB:
            # Register the cleanup hook
            cleanup_func(cleanup)

    hs = homeserver_to_use(
        server_name,
        config=config,
        reactor=reactor,
    )

    # Capture the `hs` as a `weakref` here to ensure there is no scenario where uncalled
    # cleanup functions result in holding the `hs` in memory.
    cleanup_hs_ref = weakref.ref(hs)

    async def shutdown_hs_on_cleanup() -> None:
        cleanup_hs = cleanup_hs_ref()
        if cleanup_hs is not None:
            await cleanup_hs.shutdown()

    # Register the cleanup hook for the homeserver.
    # A full `hs.shutdown()` is necessary otherwise CI tests will fail while exhibiting
    # strange behaviours.
    cleanup_func(shutdown_hs_on_cleanup)

    # Install @cache_in_self attributes
    for key, val in extra_homeserver_attributes.items():
        setattr(hs, "_" + key, val)

    # Mock TLS
    hs.tls_server_context_factory = Mock()

    # Patch `make_pool` before initialising the database, to make database transactions
    # synchronous for testing.
    with patch("synapse.storage.database.make_pool", side_effect=make_fake_db_pool):
        hs.setup()

    # Ideally, setup/start would be separated but since this is historically used
    # throughout tests, we keep the existing behavior for now. We probably just need to
    # rename this function.
    start_test_homeserver(hs=hs, cleanup_func=cleanup_func, reactor=reactor)

    return hs


def start_test_homeserver(
    *,
    hs: HomeServer,
    cleanup_func: Callable[[Callable[[], Optional["Deferred[None]"]]], None],
    reactor: ISynapseReactor,
) -> None:
    """
    Start a homeserver for testing.

    Args:
        hs: The homeserver to start.
        cleanup_func: The function used to register a cleanup routine for
            after the test. If the function returns a Deferred, the
            test case will wait until the Deferred has fired before
            proceeding to the next cleanup function.
        reactor: Twisted reactor
    """

    # Register background tasks required by this server. This must be done
    # somewhat manually due to the background tasks not being registered
    # unless handlers are instantiated.
    #
    # Since, we don't have to worry about `daemonize` (forking the process) in tests, we
    # can just start the background tasks straight away after `hs.setup`. (compare this
    # with where we call `hs.start_background_tasks()` outside of the test environment).
    if hs.config.worker.run_background_tasks:
        hs.start_background_tasks()

    # Since we've changed the databases to run DB transactions on the same
    # thread, we need to stop the event fetcher hogging that one thread.
    hs.get_datastores().main.USE_DEDICATED_DB_THREADS_FOR_EVENT_FETCHING = False

    if USE_POSTGRES_FOR_TESTS:
        # Capture the `database_pool` as a `weakref` here to ensure there is no scenario where uncalled
        # cleanup functions result in holding the `hs` in memory.
        database_pool = weakref.ref(hs.get_datastores().databases[0])

        # We need to do cleanup on PostgreSQL
        def cleanup() -> None:
            # Close all the db pools
            db_pool = database_pool()
            if db_pool is not None:
                db_pool._db_pool.close()

        if not LEAVE_DB:
            # Register the cleanup hook
            cleanup_func(cleanup)

    # bcrypt is far too slow to be doing in unit tests
    # Need to let the HS build an auth handler and then mess with it
    # because AuthHandler's constructor requires the HS, so we can't make one
    # beforehand and pass it in to the HS's constructor (chicken / egg)
    async def hash(p: str) -> str:
        return hashlib.md5(p.encode("utf8")).hexdigest()

    hs.get_auth_handler().hash = hash  # type: ignore[assignment]

    async def validate_hash(p: str, h: str) -> bool:
        return hashlib.md5(p.encode("utf8")).hexdigest() == h

    hs.get_auth_handler().validate_hash = validate_hash  # type: ignore[assignment]

    # We need to replace the media threadpool with the fake test threadpool.
    def thread_pool() -> threadpool.ThreadPool:
        return reactor.getThreadPool()

    hs.get_media_sender_thread_pool = thread_pool  # type: ignore[method-assign]

    # Load any configured modules into the homeserver
    module_api = hs.get_module_api()
    for module, module_config in hs.config.modules.loaded_modules:
        module(config=module_config, api=module_api)

    if hs.config.auto_accept_invites.enabled:
        # Start the local auto_accept_invites module.
        m = InviteAutoAccepter(hs.config.auto_accept_invites, module_api)
        logger.info("Loaded local module %s", m)

    load_legacy_spam_checkers(hs)
    load_legacy_third_party_event_rules(hs)
    load_legacy_presence_router(hs)
    load_legacy_password_auth_providers(hs)
