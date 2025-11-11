#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019-2021 The Matrix.org Foundation C.I.C
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
import atexit
import gc
import logging
import os
import signal
import socket
import sys
import traceback
import warnings
from textwrap import indent
from threading import Thread
from types import FrameType
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    NoReturn,
    cast,
)
from wsgiref.simple_server import WSGIServer

from cryptography.utils import CryptographyDeprecationWarning
from typing_extensions import ParamSpec

import twisted
from twisted.internet import defer, error, reactor as _reactor
from twisted.internet.interfaces import (
    IOpenSSLContextFactory,
    IReactorSSL,
    IReactorTCP,
    IReactorUNIX,
)
from twisted.internet.protocol import ServerFactory
from twisted.internet.tcp import Port
from twisted.logger import LoggingFile, LogLevel
from twisted.protocols.tls import TLSMemoryBIOFactory
from twisted.python.threadpool import ThreadPool
from twisted.web.resource import Resource

import synapse.util.caches
from synapse.api.constants import MAX_PDU_SIZE
from synapse.app import check_bind_error
from synapse.config import ConfigError
from synapse.config._base import format_config_error
from synapse.config.homeserver import HomeServerConfig
from synapse.config.server import ListenerConfig, ManholeConfig, TCPListenerConfig
from synapse.crypto import context_factory
from synapse.events.auto_accept_invites import InviteAutoAccepter
from synapse.events.presence_router import load_legacy_presence_router
from synapse.handlers.auth import load_legacy_password_auth_providers
from synapse.http.site import SynapseSite
from synapse.logging.context import LoggingContext, PreserveLoggingContext
from synapse.metrics import install_gc_manager, register_threadpool
from synapse.metrics.jemalloc import setup_jemalloc_stats
from synapse.module_api.callbacks.spamchecker_callbacks import load_legacy_spam_checkers
from synapse.module_api.callbacks.third_party_event_rules_callbacks import (
    load_legacy_third_party_event_rules,
)
from synapse.types import ISynapseReactor, StrCollection
from synapse.util import SYNAPSE_VERSION
from synapse.util.caches.lrucache import setup_expire_lru_cache_entries
from synapse.util.daemonize import daemonize_process
from synapse.util.gai_resolver import GAIResolver
from synapse.util.rlimit import change_resource_limit

if TYPE_CHECKING:
    from synapse.server import HomeServer

# Twisted injects the global reactor to make it easier to import, this confuses
# mypy which thinks it is a module. Tell it that it a more proper type.
reactor = cast(ISynapseReactor, _reactor)


logger = logging.getLogger(__name__)

_instance_id_to_sighup_callbacks_map: dict[
    str, list[tuple[Callable[..., None], tuple[object, ...], dict[str, object]]]
] = {}
"""
Map from homeserver instance_id to a list of callbacks.

We use `instance_id` instead of `server_name` because it's possible to have multiple
workers running in the same process with the same `server_name`.
"""
P = ParamSpec("P")


def register_sighup(
    hs: "HomeServer",
    func: Callable[P, None],
    *args: P.args,
    **kwargs: P.kwargs,
) -> None:
    """
    Register a function to be called when a SIGHUP occurs.

    Args:
        homeserver_instance_id: The unique ID for this Synapse process instance
            (`hs.get_instance_id()`) that this hook is associated with.
        func: Function to be called when sent a SIGHUP signal.
        *args, **kwargs: args and kwargs to be passed to the target function.
    """

    # Wrap the function so we can run it within a logcontext
    def _callback_wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        with LoggingContext(name="sighup", server_name=hs.hostname):
            func(*args, **kwargs)

    _instance_id_to_sighup_callbacks_map.setdefault(hs.get_instance_id(), []).append(
        (_callback_wrapper, args, kwargs)
    )


def unregister_sighups(homeserver_instance_id: str) -> None:
    """
    Unregister all sighup functions associated with this Synapse instance.

    Args:
        homeserver_instance_id: The unique ID for this Synapse process instance to
            unregister hooks for (`hs.get_instance_id()`).
    """
    _instance_id_to_sighup_callbacks_map.pop(homeserver_instance_id, [])


def start_worker_reactor(
    appname: str,
    config: HomeServerConfig,
    # Use a lambda to avoid binding to a given reactor at import time.
    # (needed when synapse.app.complement_fork_starter is being used)
    run_command: Callable[[], None] = lambda: reactor.run(),
) -> None:
    """Run the reactor in the main process

    Daemonizes if necessary, and then configures some resources, before starting
    the reactor. Pulls configuration from the 'worker' settings in 'config'.

    Args:
        appname: application name which will be sent to syslog
        config: config object
        run_command: callable that actually runs the reactor
    """

    logger = logging.getLogger(config.worker.worker_app)

    start_reactor(
        appname,
        soft_file_limit=config.server.soft_file_limit,
        gc_thresholds=config.server.gc_thresholds,
        pid_file=config.worker.worker_pid_file,
        daemonize=config.worker.worker_daemonize,
        print_pidfile=config.server.print_pidfile,
        logger=logger,
        run_command=run_command,
    )


def start_reactor(
    appname: str,
    soft_file_limit: int,
    gc_thresholds: tuple[int, int, int] | None,
    pid_file: str | None,
    daemonize: bool,
    print_pidfile: bool,
    logger: logging.Logger,
    # Use a lambda to avoid binding to a given reactor at import time.
    # (needed when synapse.app.complement_fork_starter is being used)
    run_command: Callable[[], None] = lambda: reactor.run(),
) -> None:
    """Run the reactor in the main process

    Daemonizes if necessary, and then configures some resources, before starting
    the reactor

    Args:
        appname: application name which will be sent to syslog
        soft_file_limit:
        gc_thresholds:
        pid_file: name of pid file to write to if daemonize is True
        daemonize: true to run the reactor in a background process
        print_pidfile: whether to print the pid file, if daemonize is True
        logger: logger instance to pass to Daemonize
        run_command: callable that actually runs the reactor
    """

    def run() -> None:
        logger.info("Running")
        setup_jemalloc_stats()
        change_resource_limit(soft_file_limit)
        if gc_thresholds:
            gc.set_threshold(*gc_thresholds)
        install_gc_manager()

        # Reset the logging context when we start the reactor (whenever we yield control
        # to the reactor, the `sentinel` logging context needs to be set so we don't
        # leak the current logging context and erroneously apply it to the next task the
        # reactor event loop picks up)
        with PreserveLoggingContext():
            run_command()

    if daemonize:
        assert pid_file is not None

        if print_pidfile:
            print(pid_file)

        daemonize_process(pid_file, logger)

    run()


def quit_with_error(error_string: str) -> NoReturn:
    message_lines = error_string.split("\n")
    line_length = min(max(len(line) for line in message_lines), 80) + 2
    sys.stderr.write("*" * line_length + "\n")
    for line in message_lines:
        sys.stderr.write(" %s\n" % (line.rstrip(),))
    sys.stderr.write("*" * line_length + "\n")
    sys.exit(1)


def handle_startup_exception(e: Exception) -> NoReturn:
    # Exceptions that occur between setting up the logging and forking or starting
    # the reactor are written to the logs, followed by a summary to stderr.
    logger.exception("Exception during startup")

    error_string = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    indented_error_string = indent(error_string, "    ")

    quit_with_error(
        f"Error during initialisation:\n{indented_error_string}\nThere may be more information in the logs."
    )


def redirect_stdio_to_logs() -> None:
    streams = [("stdout", LogLevel.info), ("stderr", LogLevel.error)]

    for stream, level in streams:
        oldStream = getattr(sys, stream)
        loggingFile = LoggingFile(
            logger=twisted.logger.Logger(namespace=stream),
            level=level,
            encoding=getattr(oldStream, "encoding", None),
        )
        setattr(sys, stream, loggingFile)

    print("Redirected stdout/stderr to logs")


def register_start(
    hs: "HomeServer", cb: Callable[P, Awaitable], *args: P.args, **kwargs: P.kwargs
) -> None:
    """Register a callback with the reactor, to be called once it is running

    This can be used to initialise parts of the system which require an asynchronous
    setup.

    Any exception raised by the callback will be printed and logged, and the process
    will exit.
    """

    async def wrapper() -> None:
        try:
            await cb(*args, **kwargs)
        except Exception:
            # previously, we used Failure().printTraceback() here, in the hope that
            # would give better tracebacks than traceback.print_exc(). However, that
            # doesn't handle chained exceptions (with a __cause__ or __context__) well,
            # and I *think* the need for Failure() is reduced now that we mostly use
            # async/await.

            # Write the exception to both the logs *and* the unredirected stderr,
            # because people tend to get confused if it only goes to one or the other.
            #
            # One problem with this is that if people are using a logging config that
            # logs to the console (as is common eg under docker), they will get two
            # copies of the exception. We could maybe try to detect that, but it's
            # probably a cost we can bear.
            logger.fatal("Error during startup", exc_info=True)
            print("Error during startup:", file=sys.__stderr__)
            traceback.print_exc(file=sys.__stderr__)

            # it's no use calling sys.exit here, since that just raises a SystemExit
            # exception which is then caught by the reactor, and everything carries
            # on as normal.
            os._exit(1)

    clock = hs.get_clock()
    clock.call_when_running(lambda: defer.ensureDeferred(wrapper()))


def listen_metrics(
    bind_addresses: StrCollection, port: int
) -> list[tuple[WSGIServer, Thread]]:
    """
    Start Prometheus metrics server.

    This method runs the metrics server on a different port, in a different thread to
    Synapse. This can make it more resilient to heavy load in Synapse causing metric
    requests to be slow or timeout.

    Even though `start_http_server_prometheus(...)` uses `threading.Thread` behind the
    scenes (where all threads share the GIL and only one thread can execute Python
    bytecode at a time), this still works because the metrics thread can preempt the
    Twisted reactor thread between bytecode boundaries and the metrics thread gets
    scheduled with roughly equal priority to the Twisted reactor thread.

    Returns:
        List of WSGIServer with the thread they are running on.
    """
    from prometheus_client import start_http_server as start_http_server_prometheus

    from synapse.metrics import RegistryProxy

    servers: list[tuple[WSGIServer, Thread]] = []
    for host in bind_addresses:
        logger.info("Starting metrics listener on %s:%d", host, port)
        server, thread = start_http_server_prometheus(
            port, addr=host, registry=RegistryProxy
        )
        servers.append((server, thread))
    return servers


def listen_manhole(
    bind_addresses: StrCollection,
    port: int,
    manhole_settings: ManholeConfig,
    manhole_globals: dict,
) -> list[Port]:
    # twisted.conch.manhole 21.1.0 uses "int_from_bytes", which produces a confusing
    # warning. It's fixed by https://github.com/twisted/twisted/pull/1522), so
    # suppress the warning for now.
    warnings.filterwarnings(
        action="ignore",
        category=CryptographyDeprecationWarning,
        message="int_from_bytes is deprecated",
    )

    from synapse.util.manhole import manhole

    return listen_tcp(
        bind_addresses,
        port,
        manhole(settings=manhole_settings, globals=manhole_globals),
    )


def listen_tcp(
    bind_addresses: StrCollection,
    port: int,
    factory: ServerFactory,
    reactor: IReactorTCP = reactor,
    backlog: int = 50,
) -> list[Port]:
    """
    Create a TCP socket for a port and several addresses

    Returns:
        list of twisted.internet.tcp.Port listening for TCP connections
    """
    r = []
    for address in bind_addresses:
        try:
            r.append(reactor.listenTCP(port, factory, backlog, address))
        except error.CannotListenError as e:
            check_bind_error(e, address, bind_addresses)

    # IReactorTCP returns an object implementing IListeningPort from listenTCP,
    # but we know it will be a Port instance.
    return r  # type: ignore[return-value]


def listen_unix(
    path: str,
    mode: int,
    factory: ServerFactory,
    reactor: IReactorUNIX = reactor,
    backlog: int = 50,
) -> list[Port]:
    """
    Create a UNIX socket for a given path and 'mode' permission

    Returns:
        list of twisted.internet.tcp.Port listening for TCP connections
    """
    wantPID = True

    return [
        # IReactorUNIX returns an object implementing IListeningPort from listenUNIX,
        # but we know it will be a Port instance.
        cast(Port, reactor.listenUNIX(path, factory, backlog, mode, wantPID))
    ]


def listen_http(
    hs: "HomeServer",
    listener_config: ListenerConfig,
    root_resource: Resource,
    version_string: str,
    max_request_body_size: int,
    context_factory: IOpenSSLContextFactory | None,
    reactor: ISynapseReactor = reactor,
) -> list[Port]:
    """
    Args:
        listener_config: TODO
        root_resource: TODO
        version_string: A string to present for the Server header
        max_request_body_size: TODO
        context_factory: TODO
        reactor: TODO
    """
    assert listener_config.http_options is not None

    site_tag = listener_config.get_site_tag()

    site = SynapseSite(
        logger_name="synapse.access.%s.%s"
        % ("https" if listener_config.is_tls() else "http", site_tag),
        site_tag=site_tag,
        config=listener_config,
        resource=root_resource,
        server_version_string=version_string,
        max_request_body_size=max_request_body_size,
        reactor=reactor,
        hs=hs,
    )

    if isinstance(listener_config, TCPListenerConfig):
        if listener_config.is_tls():
            # refresh_certificate should have been called before this.
            assert context_factory is not None
            ports = listen_ssl(
                listener_config.bind_addresses,
                listener_config.port,
                site,
                context_factory,
                reactor=reactor,
            )
            logger.info(
                "Synapse now listening on TCP port %d (TLS)", listener_config.port
            )
        else:
            ports = listen_tcp(
                listener_config.bind_addresses,
                listener_config.port,
                site,
                reactor=reactor,
            )
            logger.info("Synapse now listening on TCP port %d", listener_config.port)

    else:
        ports = listen_unix(
            listener_config.path, listener_config.mode, site, reactor=reactor
        )
        # getHost() returns a UNIXAddress which contains an instance variable of 'name'
        # encoded as a byte string. Decode as utf-8 so pretty.
        logger.info(
            "Synapse now listening on Unix Socket at: %s",
            ports[0].getHost().name.decode("utf-8"),
        )

    return ports


def listen_ssl(
    bind_addresses: StrCollection,
    port: int,
    factory: ServerFactory,
    context_factory: IOpenSSLContextFactory,
    reactor: IReactorSSL = reactor,
    backlog: int = 50,
) -> list[Port]:
    """
    Create an TLS-over-TCP socket for a port and several addresses

    Returns:
        list of twisted.internet.tcp.Port listening for TLS connections
    """
    r = []
    for address in bind_addresses:
        try:
            r.append(
                reactor.listenSSL(port, factory, context_factory, backlog, address)
            )
        except error.CannotListenError as e:
            check_bind_error(e, address, bind_addresses)

    # IReactorSSL incorrectly declares that an int is returned from listenSSL,
    # it actually returns an object implementing IListeningPort, but we know it
    # will be a Port instance.
    return r  # type: ignore[return-value]


def refresh_certificate(hs: "HomeServer") -> None:
    """
    Refresh the TLS certificates that Synapse is using by re-reading them from
    disk and updating the TLS context factories to use them.
    """
    if not hs.config.server.has_tls_listener():
        return

    hs.config.tls.read_certificate_from_disk()
    hs.tls_server_context_factory = context_factory.ServerContextFactory(hs.config)

    if hs._listening_services:
        logger.info("Updating context factories...")
        for i in hs._listening_services:
            # When you listenSSL, it doesn't make an SSL port but a TCP one with
            # a TLS wrapping factory around the factory you actually want to get
            # requests. This factory attribute is public but missing from
            # Twisted's documentation.
            if isinstance(i.factory, TLSMemoryBIOFactory):
                addr = i.getHost()
                logger.info(
                    "Replacing TLS context factory on [%s]:%i", addr.host, addr.port
                )
                # We want to replace TLS factories with a new one, with the new
                # TLS configuration. We do this by reaching in and pulling out
                # the wrappedFactory, and then re-wrapping it.
                i.factory = TLSMemoryBIOFactory(
                    hs.tls_server_context_factory, False, i.factory.wrappedFactory
                )
        logger.info("Context factories updated.")


_already_setup_sighup_handling = False
"""
Marks whether we've already successfully ran `setup_sighup_handling()`.
"""


def setup_sighup_handling() -> None:
    """
    Set up SIGHUP handling to call registered callbacks.

    This can be called multiple times safely.
    """
    global _already_setup_sighup_handling
    # We only need to set things up once per process.
    if _already_setup_sighup_handling:
        return

    previous_sighup_handler: Callable[[int, FrameType | None], Any] | int | None = None

    # Set up the SIGHUP machinery.
    if hasattr(signal, "SIGHUP"):

        def handle_sighup(*args: Any, **kwargs: Any) -> None:
            # Tell systemd our state, if we're using it. This will silently fail if
            # we're not using systemd.
            sdnotify(b"RELOADING=1")

            if callable(previous_sighup_handler):
                previous_sighup_handler(*args, **kwargs)

            for sighup_callbacks in _instance_id_to_sighup_callbacks_map.values():
                for func, args, kwargs in sighup_callbacks:
                    func(*args, **kwargs)

            sdnotify(b"READY=1")

        # We defer running the sighup handlers until next reactor tick. This
        # is so that we're in a sane state, e.g. flushing the logs may fail
        # if the sighup happens in the middle of writing a log entry.
        def run_sighup(*args: Any, **kwargs: Any) -> None:
            # `callFromThread` should be "signal safe" as well as thread
            # safe.
            reactor.callFromThread(handle_sighup, *args, **kwargs)

        # Register for the SIGHUP signal, chaining any existing handler as there can
        # only be one handler per signal and we don't want to clobber any existing
        # handlers (like the `multi_synapse` shard process in the context of Synapse Pro
        # for small hosts)
        previous_sighup_handler = signal.signal(signal.SIGHUP, run_sighup)

    _already_setup_sighup_handling = True


async def start(hs: "HomeServer", *, freeze: bool = True) -> None:
    """
    Start a Synapse server or worker.

    Should be called once the reactor is running.

    Will start the main HTTP listeners and do some other startup tasks, and then
    notify systemd.

    Args:
        hs: homeserver instance
        freeze: whether to freeze the homeserver base objects in the garbage collector.
            May improve garbage collection performance by marking objects with an effectively
            static lifetime as frozen so they don't need to be considered for cleanup.
            If you ever want to `shutdown` the homeserver, this needs to be
            False otherwise the homeserver cannot be garbage collected after `shutdown`.
    """
    server_name = hs.hostname
    reactor = hs.get_reactor()

    # We want to use a separate thread pool for the resolver so that large
    # numbers of DNS requests don't starve out other users of the threadpool.
    resolver_threadpool = ThreadPool(name="gai_resolver")
    resolver_threadpool.start()
    hs.get_clock().add_system_event_trigger(
        "during", "shutdown", resolver_threadpool.stop
    )
    reactor.installNameResolver(
        GAIResolver(reactor, getThreadPool=lambda: resolver_threadpool)
    )

    # Register the threadpools with our metrics.
    register_threadpool(
        name="default", server_name=server_name, threadpool=reactor.getThreadPool()
    )
    register_threadpool(
        name="gai_resolver", server_name=server_name, threadpool=resolver_threadpool
    )

    setup_sighup_handling()
    register_sighup(hs, refresh_certificate, hs)
    register_sighup(hs, reload_cache_config, hs.config)

    # Apply the cache config.
    hs.config.caches.resize_all_caches()

    # Load the OIDC provider metadatas, if OIDC is enabled.
    if hs.config.oidc.oidc_enabled:
        oidc = hs.get_oidc_handler()
        # Loading the provider metadata also ensures the provider config is valid.
        #
        # FIXME: It feels a bit strange to validate and block on startup as one of these
        # OIDC providers could be temporarily unavailable and cause Synapse to be unable
        # to start.
        await oidc.load_metadata()

    # Load the certificate from disk.
    refresh_certificate(hs)

    # Instantiate the modules so they can register their web resources to the module API
    # before we start the listeners.
    module_api = hs.get_module_api()
    for module, config in hs.config.modules.loaded_modules:
        m = module(config, module_api)
        logger.info("Loaded module %s", m)

    if hs.config.auto_accept_invites.enabled:
        # Start the local auto_accept_invites module.
        m = InviteAutoAccepter(hs.config.auto_accept_invites, module_api)
        logger.info("Loaded local module %s", m)

    load_legacy_spam_checkers(hs)
    load_legacy_third_party_event_rules(hs)
    load_legacy_presence_router(hs)
    load_legacy_password_auth_providers(hs)

    # If we've configured an expiry time for caches, start the background job now.
    setup_expire_lru_cache_entries(hs)

    # It is now safe to start your Synapse.
    hs.start_listening()
    hs.get_datastores().main.db_pool.start_profiling()
    hs.get_pusherpool().start()

    def log_shutdown() -> None:
        with LoggingContext(name="log_shutdown", server_name=server_name):
            logger.info("Shutting down...")

    # Log when we start the shut down process.
    hs.register_sync_shutdown_handler(
        phase="before",
        eventType="shutdown",
        shutdown_func=log_shutdown,
    )

    setup_sentry(hs)
    setup_sdnotify(hs)

    # Register background tasks required by this server. This must be done
    # somewhat manually due to the background tasks not being registered
    # unless handlers are instantiated.
    #
    # While we could "start" these before the reactor runs, nothing will happen until
    # the reactor is running, so we may as well do it here in `start`.
    #
    # Additionally, this means we also start them after we daemonize and fork the
    # process which means we can avoid any potential problems with cputime metrics
    # getting confused about the per-thread resource usage appearing to go backwards
    # because we're comparing the resource usage (`rusage`) from the original process to
    # the forked process.
    if hs.config.worker.run_background_tasks:
        hs.start_background_tasks()

    if freeze:
        # We now freeze all allocated objects in the hopes that (almost)
        # everything currently allocated are things that will be used for the
        # rest of time. Doing so means less work each GC (hopefully).
        #
        # Note that freezing the homeserver object means that it won't be able to be
        # garbage collected in the case of attempting an in-memory `shutdown`. This only
        # needs to be considered if such a case is desirable. Exiting the entire Python
        # process will function expectedly either way.
        #
        # PyPy does not (yet?) implement gc.freeze()
        if hasattr(gc, "freeze"):
            gc.collect()
            gc.freeze()

            # Speed up process exit by freezing all allocated objects. This moves everything
            # into the permanent generation and excludes them from the final GC.
            atexit.register(gc.freeze)


def reload_cache_config(config: HomeServerConfig) -> None:
    """Reload cache config from disk and immediately apply it.resize caches accordingly.

    If the config is invalid, a `ConfigError` is logged and no changes are made.

    Otherwise, this:
        - replaces the `caches` section on the given `config` object,
        - resizes all caches according to the new cache factors, and

    Note that the following cache config keys are read, but not applied:
        - event_cache_size: used to set a max_size and _original_max_size on
              EventsWorkerStore._get_event_cache when it is created. We'd have to update
              the _original_max_size (and maybe
        - sync_response_cache_duration: would have to update the timeout_sec attribute on
              HomeServer ->  SyncHandler -> ResponseCache.
        - track_memory_usage. This affects synapse.util.caches.TRACK_MEMORY_USAGE which
              influences Synapse's self-reported metrics.

    Also, the HTTPConnectionPool in SimpleHTTPClient sets its maxPersistentPerHost
    parameter based on the global_factor. This won't be applied on a config reload.
    """
    try:
        previous_cache_config = config.reload_config_section("caches")
    except ConfigError as e:
        logger.warning("Failed to reload cache config")
        for f in format_config_error(e):
            logger.warning(f)
    else:
        logger.debug(
            "New cache config. Was:\n %s\nNow:\n %s",
            previous_cache_config.__dict__,
            config.caches.__dict__,
        )
        synapse.util.caches.TRACK_MEMORY_USAGE = config.caches.track_memory_usage
        config.caches.resize_all_caches()


def setup_sentry(hs: "HomeServer") -> None:
    """Enable sentry integration, if enabled in configuration"""

    if not hs.config.metrics.sentry_enabled:
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=hs.config.metrics.sentry_dsn,
        release=SYNAPSE_VERSION,
        environment=hs.config.metrics.sentry_environment,
    )

    # We set some default tags that give some context to this instance
    global_scope = sentry_sdk.Scope.get_global_scope()
    global_scope.set_tag("matrix_server_name", hs.config.server.server_name)

    app = (
        hs.config.worker.worker_app
        if hs.config.worker.worker_app
        else "synapse.app.homeserver"
    )
    name = hs.get_instance_name()
    global_scope.set_tag("worker_app", app)
    global_scope.set_tag("worker_name", name)


def setup_sdnotify(hs: "HomeServer") -> None:
    """Adds process state hooks to tell systemd what we are up to."""

    # Tell systemd our state, if we're using it. This will silently fail if
    # we're not using systemd.
    sdnotify(b"READY=1\nMAINPID=%i" % (os.getpid(),))

    hs.get_clock().add_system_event_trigger(
        "before", "shutdown", sdnotify, b"STOPPING=1"
    )


sdnotify_sockaddr = os.getenv("NOTIFY_SOCKET")


def sdnotify(state: bytes) -> None:
    """
    Send a notification to systemd, if the NOTIFY_SOCKET env var is set.

    This function is based on the sdnotify python package, but since it's only a few
    lines of code, it's easier to duplicate it here than to add a dependency on a
    package which many OSes don't include as a matter of principle.

    Args:
        state: notification to send
    """
    if not isinstance(state, bytes):
        raise TypeError("sdnotify should be called with a bytes")
    if not sdnotify_sockaddr:
        return
    addr = sdnotify_sockaddr
    if addr[0] == "@":
        addr = "\0" + addr[1:]

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state)
    except Exception as e:
        # this is a bit surprising, since we don't expect to have a NOTIFY_SOCKET
        # unless systemd is expecting us to notify it.
        logger.warning("Unable to send notification to systemd: %s", e)


def max_request_body_size(config: HomeServerConfig) -> int:
    """Get a suitable maximum size for incoming HTTP requests"""

    # Other than media uploads, the biggest request we expect to see is a fully-loaded
    # /federation/v1/send request.
    #
    # The main thing in such a request is up to 50 PDUs, and up to 100 EDUs. PDUs are
    # limited to 65536 bytes (possibly slightly more if the sender didn't use canonical
    # json encoding); there is no specced limit to EDUs (see
    # https://github.com/matrix-org/matrix-doc/issues/3121).
    #
    # in short, we somewhat arbitrarily limit requests to 200 * 64K (about 12.5M)
    #
    max_request_size = 200 * MAX_PDU_SIZE

    # if we have a media repo enabled, we may need to allow larger uploads than that
    if config.media.can_load_media_repo:
        max_request_size = max(max_request_size, config.media.max_upload_size)

    return max_request_size
