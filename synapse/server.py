#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023-2024 New Vector, Ltd
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


# This file provides some classes for setting up (partially-populated)
# homeservers; either as a full homeserver as a real application, or a small
# partial one for unit test mocking.


import abc
import functools
import logging
from threading import Thread
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    TypeVar,
    cast,
)
from wsgiref.simple_server import WSGIServer

from attr import dataclass
from typing_extensions import TypeAlias

from twisted.internet import defer
from twisted.internet.base import _SystemEventID
from twisted.internet.interfaces import IOpenSSLContextFactory
from twisted.internet.tcp import Port
from twisted.python.threadpool import ThreadPool
from twisted.web.iweb import IPolicyForHTTPS
from twisted.web.resource import Resource

from synapse.api.auth import Auth
from synapse.api.auth.internal import InternalAuth
from synapse.api.auth.mas import MasDelegatedAuth
from synapse.api.auth_blocking import AuthBlocking
from synapse.api.filtering import Filtering
from synapse.api.ratelimiting import Ratelimiter, RequestRatelimiter
from synapse.app._base import unregister_sighups
from synapse.app.phone_stats_home import start_phone_stats_home
from synapse.appservice.api import ApplicationServiceApi
from synapse.appservice.scheduler import ApplicationServiceScheduler
from synapse.config.homeserver import HomeServerConfig
from synapse.crypto import context_factory
from synapse.crypto.context_factory import RegularPolicyForHTTPS
from synapse.crypto.keyring import Keyring
from synapse.events.builder import EventBuilderFactory
from synapse.events.presence_router import PresenceRouter
from synapse.events.utils import EventClientSerializer
from synapse.federation.federation_client import FederationClient
from synapse.federation.federation_server import (
    FederationHandlerRegistry,
    FederationServer,
)
from synapse.federation.send_queue import FederationRemoteSendQueue
from synapse.federation.sender import AbstractFederationSender, FederationSender
from synapse.federation.transport.client import TransportLayerClient
from synapse.handlers.account import AccountHandler
from synapse.handlers.account_data import AccountDataHandler
from synapse.handlers.account_validity import AccountValidityHandler
from synapse.handlers.admin import AdminHandler
from synapse.handlers.appservice import ApplicationServicesHandler
from synapse.handlers.auth import AuthHandler, PasswordAuthProvider
from synapse.handlers.cas import CasHandler
from synapse.handlers.deactivate_account import DeactivateAccountHandler
from synapse.handlers.delayed_events import DelayedEventsHandler
from synapse.handlers.device import DeviceHandler, DeviceWriterHandler
from synapse.handlers.devicemessage import DeviceMessageHandler
from synapse.handlers.directory import DirectoryHandler
from synapse.handlers.e2e_keys import E2eKeysHandler
from synapse.handlers.e2e_room_keys import E2eRoomKeysHandler
from synapse.handlers.event_auth import EventAuthHandler
from synapse.handlers.events import EventHandler, EventStreamHandler
from synapse.handlers.federation import FederationHandler
from synapse.handlers.federation_event import FederationEventHandler
from synapse.handlers.identity import IdentityHandler
from synapse.handlers.initial_sync import InitialSyncHandler
from synapse.handlers.message import EventCreationHandler, MessageHandler
from synapse.handlers.pagination import PaginationHandler
from synapse.handlers.password_policy import PasswordPolicyHandler
from synapse.handlers.presence import (
    BasePresenceHandler,
    PresenceHandler,
    WorkerPresenceHandler,
)
from synapse.handlers.profile import ProfileHandler
from synapse.handlers.push_rules import PushRulesHandler
from synapse.handlers.read_marker import ReadMarkerHandler
from synapse.handlers.receipts import ReceiptsHandler
from synapse.handlers.register import RegistrationHandler
from synapse.handlers.relations import RelationsHandler
from synapse.handlers.reports import ReportsHandler
from synapse.handlers.room import (
    RoomContextHandler,
    RoomCreationHandler,
    RoomShutdownHandler,
    TimestampLookupHandler,
)
from synapse.handlers.room_list import RoomListHandler
from synapse.handlers.room_member import (
    RoomForgetterHandler,
    RoomMemberHandler,
    RoomMemberMasterHandler,
)
from synapse.handlers.room_member_worker import RoomMemberWorkerHandler
from synapse.handlers.room_policy import RoomPolicyHandler
from synapse.handlers.room_summary import RoomSummaryHandler
from synapse.handlers.search import SearchHandler
from synapse.handlers.send_email import SendEmailHandler
from synapse.handlers.set_password import SetPasswordHandler
from synapse.handlers.sliding_sync import SlidingSyncHandler
from synapse.handlers.sso import SsoHandler
from synapse.handlers.stats import StatsHandler
from synapse.handlers.sync import SyncHandler
from synapse.handlers.thread_subscriptions import ThreadSubscriptionsHandler
from synapse.handlers.typing import FollowerTypingHandler, TypingWriterHandler
from synapse.handlers.user_directory import UserDirectoryHandler
from synapse.handlers.worker_lock import WorkerLocksHandler
from synapse.http.client import (
    InsecureInterceptableContextFactory,
    ReplicationClient,
    SimpleHttpClient,
)
from synapse.http.matrixfederationclient import MatrixFederationHttpClient
from synapse.logging.context import PreserveLoggingContext
from synapse.media.media_repository import MediaRepository
from synapse.metrics import (
    all_later_gauges_to_clean_up_on_shutdown,
    register_threadpool,
)
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.metrics.common_usage_metrics import CommonUsageMetricsManager
from synapse.module_api import ModuleApi
from synapse.module_api.callbacks import ModuleApiCallbacks
from synapse.notifier import Notifier, ReplicationNotifier
from synapse.push.bulk_push_rule_evaluator import BulkPushRuleEvaluator
from synapse.push.pusherpool import PusherPool
from synapse.replication.tcp.client import ReplicationDataHandler
from synapse.replication.tcp.external_cache import ExternalCache
from synapse.replication.tcp.handler import ReplicationCommandHandler
from synapse.replication.tcp.resource import ReplicationStreamer
from synapse.replication.tcp.streams import STREAMS_MAP, Stream
from synapse.rest.media.media_repository_resource import MediaRepositoryResource
from synapse.server_notices.server_notices_manager import ServerNoticesManager
from synapse.server_notices.server_notices_sender import ServerNoticesSender
from synapse.server_notices.worker_server_notices_sender import (
    WorkerServerNoticesSender,
)
from synapse.state import StateHandler, StateResolutionHandler
from synapse.storage import Databases
from synapse.storage.controllers import StorageControllers
from synapse.streams.events import EventSources
from synapse.synapse_rust.rendezvous import RendezvousHandler
from synapse.types import DomainSpecificString, ISynapseReactor
from synapse.util import SYNAPSE_VERSION
from synapse.util.caches import CACHE_METRIC_REGISTRY
from synapse.util.clock import Clock
from synapse.util.distributor import Distributor
from synapse.util.macaroons import MacaroonGenerator
from synapse.util.ratelimitutils import FederationRateLimiter
from synapse.util.stringutils import random_string
from synapse.util.task_scheduler import TaskScheduler

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Old Python versions don't have `LiteralString`
    from txredisapi import ConnectionHandler
    from typing_extensions import LiteralString

    from synapse.handlers.jwt import JwtHandler
    from synapse.handlers.oidc import OidcHandler
    from synapse.handlers.saml import SamlHandler
    from synapse.storage._base import SQLBaseStore


# The annotation for `cache_in_self` used to be
#     def (builder: Callable[["HomeServer"],T]) -> Callable[["HomeServer"],T]
# which mypy was happy with.
#
# But PyCharm was confused by this. If `foo` was decorated by `@cache_in_self`, then
# an expression like `hs.foo()`
#
# - would erroneously warn that we hadn't provided a `hs` argument to foo (PyCharm
#   confused about boundmethods and unbound methods?), and
# - would be considered to have type `Any`, making for a poor autocomplete and
#   cross-referencing experience.
#
# Instead, use a typevar `F` to express that `@cache_in_self` returns exactly the
# same type it receives. This isn't strictly true [*], but it's more than good
# enough to keep PyCharm and mypy happy.
#
# [*]: (e.g. `builder` could be an object with a __call__ attribute rather than a
#      types.FunctionType instance, whereas the return value is always a
#      types.FunctionType instance.)

T: TypeAlias = object
F = TypeVar("F", bound=Callable[["HomeServer"], T])
R = TypeVar("R")


def cache_in_self(builder: F) -> F:
    """Wraps a function called e.g. `get_foo`, checking if `self.foo` exists and
    returning if so. If not, calls the given function and sets `self.foo` to it.

    Also ensures that dependency cycles throw an exception correctly, rather
    than overflowing the stack.
    """

    if not builder.__name__.startswith("get_"):
        raise Exception(
            "@cache_in_self can only be used on functions starting with `get_`"
        )

    # get_attr -> _attr
    depname = builder.__name__[len("get") :]

    building = [False]

    @functools.wraps(builder)
    def _get(self: "HomeServer") -> T:
        try:
            dep = getattr(self, depname)
            return dep
        except AttributeError:
            pass

        # Prevent cyclic dependencies from deadlocking
        if building[0]:
            raise ValueError("Cyclic dependency while building %s" % (depname,))

        building[0] = True
        try:
            dep = builder(self)
            setattr(self, depname, dep)
        finally:
            building[0] = False

        return dep

    return cast(F, _get)


@dataclass
class ShutdownInfo:
    """Information for callable functions called at time of shutdown.

    Attributes:
        func: the object to call before shutdown.
        trigger_id: an ID returned when registering this event trigger.
        args: the arguments to call the function with.
        kwargs: the keyword arguments to call the function with.
    """

    func: Callable[..., Any]
    trigger_id: _SystemEventID
    kwargs: dict[str, object]


class HomeServer(metaclass=abc.ABCMeta):
    """A basic homeserver object without lazy component builders.

    This will need all of the components it requires to either be passed as
    constructor arguments, or the relevant methods overriding to create them.
    Typically this would only be used for unit tests.

    Dependencies should be added by creating a `def get_<depname>(self)`
    function, wrapping it in `@cache_in_self`.

    Attributes:
        config (synapse.config.homeserver.HomeserverConfig):
        _listening_services (list[Port]): TCP ports that
            we are listening on to provide HTTP services.
    """

    REQUIRED_ON_BACKGROUND_TASK_STARTUP = [
        "admin",
        "account_validity",
        "auth",
        "deactivate_account",
        "delayed_events",
        "e2e_keys",  # for the `delete_old_otks` scheduled-task handler
        "message",
        "pagination",
        "profile",
        "room_forgetter",
        "stats",
    ]

    @property
    @abc.abstractmethod
    def DATASTORE_CLASS(self) -> type["SQLBaseStore"]:
        # This is overridden in derived application classes
        # (such as synapse.app.homeserver.SynapseHomeServer) and gives the class to be
        # instantiated during setup() for future return by get_datastores()
        pass

    def __init__(
        self,
        hostname: str,
        config: HomeServerConfig,
        reactor: ISynapseReactor | None = None,
    ):
        """
        Args:
            hostname : The hostname for the server.
            config: The full config for the homeserver.
        """

        if not reactor:
            from twisted.internet import reactor as _reactor

            reactor = cast(ISynapseReactor, _reactor)

        self._reactor = reactor
        self.hostname = hostname
        # the key we use to sign events and requests
        self.signing_key = config.key.signing_key[0]
        self.config = config
        self._listening_services: list[Port] = []
        self._metrics_listeners: list[tuple[WSGIServer, Thread]] = []
        self.start_time: int | None = None

        self._instance_id = random_string(5)
        self._instance_name = config.worker.instance_name

        self.version_string = f"Synapse/{SYNAPSE_VERSION}"

        self.datastores: Databases | None = None

        self._module_web_resources: dict[str, Resource] = {}
        self._module_web_resources_consumed = False

        # This attribute is set by the free function `refresh_certificate`.
        self.tls_server_context_factory: IOpenSSLContextFactory | None = None

        self._is_shutdown = False
        self._async_shutdown_handlers: list[ShutdownInfo] = []
        self._sync_shutdown_handlers: list[ShutdownInfo] = []
        self._background_processes: set[defer.Deferred[Any | None]] = set()

    def run_as_background_process(
        self,
        desc: "LiteralString",
        func: Callable[..., Awaitable[R | None]],
        *args: Any,
        **kwargs: Any,
    ) -> "defer.Deferred[R | None]":
        """Run the given function in its own logcontext, with resource metrics

        This should be used to wrap processes which are fired off to run in the
        background, instead of being associated with a particular request.

        It returns a Deferred which completes when the function completes, but it doesn't
        follow the synapse logcontext rules, which makes it appropriate for passing to
        clock.looping_call and friends (or for firing-and-forgetting in the middle of a
        normal synapse async function).

        Because the returned Deferred does not follow the synapse logcontext rules, awaiting
        the result of this function will result in the log context being cleared (bad). In
        order to properly await the result of this function and maintain the current log
        context, use `make_deferred_yieldable`.

        Args:
            desc: a description for this background process type
            server_name: The homeserver name that this background process is being run for
                (this should be `hs.hostname`).
            func: a function, which may return a Deferred or a coroutine
            bg_start_span: Whether to start an opentracing span. Defaults to True.
                Should only be disabled for processes that will not log to or tag
                a span.
            args: positional args for func
            kwargs: keyword args for func

        Returns:
            Deferred which returns the result of func, or `None` if func raises.
            Note that the returned Deferred does not follow the synapse logcontext
            rules.
        """
        if self._is_shutdown:
            raise Exception(
                f"Cannot start background process. HomeServer has been shutdown {len(self._background_processes)} {len(self.get_clock()._looping_calls)} {len(self.get_clock()._call_id_to_delayed_call)}"
            )

        # Ignore linter error as this is the one location this should be called.
        deferred = run_as_background_process(desc, self.hostname, func, *args, **kwargs)  # type: ignore[untracked-background-process]
        self._background_processes.add(deferred)

        def on_done(res: R) -> R:
            try:
                self._background_processes.remove(deferred)
            except KeyError:
                # If the background process isn't being tracked anymore we can just move on.
                pass
            return res

        deferred.addBoth(on_done)
        return deferred

    async def shutdown(self) -> None:
        """
        Cleanly stops all aspects of the HomeServer and removes any references that
        have been handed out in order to allow the HomeServer object to be garbage
        collected.

        You must ensure the HomeServer object to not be frozen in the garbage collector
        in order for it to be cleaned up. By default, Synapse freezes the HomeServer
        object in the garbage collector.
        """

        self._is_shutdown = True

        logger.info(
            "Received shutdown request for %s (%s).",
            self.hostname,
            self.get_instance_id(),
        )

        # Unregister sighups first. If a shutdown was requested we shouldn't be responding
        # to things like config changes. So it would be best to stop listening to these first.
        unregister_sighups(self._instance_id)

        # TODO: It would be desireable to be able to report an error if the HomeServer
        # object is frozen in the garbage collector as that would prevent it from being
        # collected after being shutdown.
        # In theory the following should work, but it doesn't seem to make a difference
        # when I test it locally.
        #
        # if gc.is_tracked(self):
        #    logger.error("HomeServer object is tracked by garbage collection so cannot be fully cleaned up")

        for listener in self._listening_services:
            # During unit tests, an incomplete `twisted.pair.testing._FakePort` is used
            # for listeners so check listener type here to ensure shutdown procedure is
            # only applied to actual `Port` instances.
            if type(listener) is Port:
                port_shutdown = listener.stopListening()
                if port_shutdown is not None:
                    await port_shutdown
        self._listening_services.clear()

        for server, thread in self._metrics_listeners:
            server.shutdown()
            thread.join()
        self._metrics_listeners.clear()

        # TODO: Cleanup replication pieces

        self.get_keyring().shutdown()

        # Cleanup metrics associated with the homeserver
        for later_gauge in all_later_gauges_to_clean_up_on_shutdown.values():
            later_gauge.unregister_hooks_for_homeserver_instance_id(
                self.get_instance_id()
            )

        CACHE_METRIC_REGISTRY.unregister_hooks_for_homeserver(
            self.config.server.server_name
        )

        for db in self.get_datastores().databases:
            db.stop_background_updates()

        if self.should_send_federation():
            try:
                self.get_federation_sender().shutdown()
            except Exception:
                pass

        for shutdown_handler in self._async_shutdown_handlers:
            try:
                self.get_reactor().removeSystemEventTrigger(shutdown_handler.trigger_id)
                defer.ensureDeferred(shutdown_handler.func(**shutdown_handler.kwargs))
            except Exception as e:
                logger.error("Error calling shutdown async handler: %s", e)
        self._async_shutdown_handlers.clear()

        for shutdown_handler in self._sync_shutdown_handlers:
            try:
                self.get_reactor().removeSystemEventTrigger(shutdown_handler.trigger_id)
                shutdown_handler.func(**shutdown_handler.kwargs)
            except Exception as e:
                logger.error("Error calling shutdown sync handler: %s", e)
        self._sync_shutdown_handlers.clear()

        self.get_clock().shutdown()

        for background_process in list(self._background_processes):
            try:
                with PreserveLoggingContext():
                    background_process.cancel()
            except Exception:
                pass
        self._background_processes.clear()

        for db in self.get_datastores().databases:
            db._db_pool.close()

    def register_async_shutdown_handler(
        self,
        *,
        phase: str,
        eventType: str,
        shutdown_func: Callable[..., Any],
        **kwargs: object,
    ) -> None:
        """
        Register a system event trigger with the HomeServer so it can be cleanly
        removed when the HomeServer is shutdown.
        """
        id = self.get_clock().add_system_event_trigger(
            phase,
            eventType,
            shutdown_func,
            **kwargs,
        )
        self._async_shutdown_handlers.append(
            ShutdownInfo(func=shutdown_func, trigger_id=id, kwargs=kwargs)
        )

    def register_sync_shutdown_handler(
        self,
        *,
        phase: str,
        eventType: str,
        shutdown_func: Callable[..., Any],
        **kwargs: object,
    ) -> None:
        """
        Register a system event trigger with the HomeServer so it can be cleanly
        removed when the HomeServer is shutdown.
        """
        id = self.get_clock().add_system_event_trigger(
            phase,
            eventType,
            shutdown_func,
            **kwargs,
        )
        self._sync_shutdown_handlers.append(
            ShutdownInfo(func=shutdown_func, trigger_id=id, kwargs=kwargs)
        )

    def register_module_web_resource(self, path: str, resource: Resource) -> None:
        """Allows a module to register a web resource to be served at the given path.

        If multiple modules register a resource for the same path, the module that
        appears the highest in the configuration file takes priority.

        Args:
            path: The path to register the resource for.
            resource: The resource to attach to this path.

        Raises:
            SynapseError(500): A module tried to register a web resource after the HTTP
                listeners have been started.
        """
        if self._module_web_resources_consumed:
            raise RuntimeError(
                "Tried to register a web resource from a module after startup",
            )

        # Don't register a resource that's already been registered.
        if path not in self._module_web_resources.keys():
            self._module_web_resources[path] = resource
        else:
            logger.warning(
                "Module tried to register a web resource for path %s but another module"
                " has already registered a resource for this path.",
                path,
            )

    def get_instance_id(self) -> str:
        """A unique ID for this synapse process instance.

        This is used to distinguish running instances in worker-based
        deployments.
        """
        return self._instance_id

    def get_instance_name(self) -> str:
        """A unique name for this synapse process.

        Used to identify the process over replication and in config. Does not
        change over restarts.
        """
        return self._instance_name

    def setup(self) -> None:
        logger.info("Setting up.")
        self.start_time = int(self.get_clock().time())
        self.datastores = Databases(self.DATASTORE_CLASS, self)
        logger.info("Finished setting up.")

    # def __del__(self) -> None:
    #    """
    #    Called when an the homeserver is garbage collected.
    #
    #    Make sure we actually do some clean-up, rather than leak data.
    #    """
    #
    #    # NOTE: This is a chicken and egg problem.
    #    # __del__ will never be called since the HomeServer cannot be garbage collected
    #    # until the shutdown function has been called. So it makes no sense to call
    #    # shutdown inside of __del__, even though that is a logical place to assume it
    #    # should be called.
    #    self.shutdown()

    def start_listening(self) -> None:  # noqa: B027 (no-op by design)
        """Start the HTTP, manhole, metrics, etc listeners

        Does nothing in this base class; overridden in derived classes to start the
        appropriate listeners.
        """

    def start_background_tasks(self) -> None:
        """
        Some handlers have side effects on instantiation (like registering
        background updates). This function causes them to be fetched, and
        therefore instantiated, to run those side effects.
        """
        for i in self.REQUIRED_ON_BACKGROUND_TASK_STARTUP:
            getattr(self, "get_" + i + "_handler")()
        self.get_task_scheduler()
        self.get_common_usage_metrics_manager().setup()
        start_phone_stats_home(self)

    def get_reactor(self) -> ISynapseReactor:
        """
        Fetch the Twisted reactor in use by this HomeServer.
        """
        return self._reactor

    def is_mine(self, domain_specific_string: DomainSpecificString) -> bool:
        return domain_specific_string.domain == self.hostname

    def is_mine_id(self, user_id: str) -> bool:
        """Determines whether a user ID or room alias originates from this homeserver.

        Returns:
            `True` if the hostname part of the user ID or room alias matches this
            homeserver.
            `False` otherwise, or if the user ID or room alias is malformed.
        """
        localpart_hostname = user_id.split(":", 1)
        if len(localpart_hostname) < 2:
            return False
        return localpart_hostname[1] == self.hostname

    def is_mine_server_name(self, server_name: str) -> bool:
        """Determines whether a server name refers to this homeserver."""
        return server_name == self.hostname

    @cache_in_self
    def get_clock(self) -> Clock:
        # Ignore the linter error since this is the one place the `Clock` should be created.
        return Clock(self._reactor, server_name=self.hostname)  # type: ignore[multiple-internal-clocks]

    def get_datastores(self) -> Databases:
        if not self.datastores:
            raise Exception("HomeServer.setup must be called before getting datastores")

        return self.datastores

    @cache_in_self
    def get_distributor(self) -> Distributor:
        return Distributor(hs=self)

    @cache_in_self
    def get_registration_ratelimiter(self) -> Ratelimiter:
        return Ratelimiter(
            store=self.get_datastores().main,
            clock=self.get_clock(),
            cfg=self.config.ratelimiting.rc_registration,
        )

    @cache_in_self
    def get_federation_client(self) -> FederationClient:
        return FederationClient(self)

    @cache_in_self
    def get_federation_server(self) -> FederationServer:
        return FederationServer(self)

    @cache_in_self
    def get_notifier(self) -> Notifier:
        return Notifier(self)

    @cache_in_self
    def get_replication_notifier(self) -> ReplicationNotifier:
        return ReplicationNotifier()

    @cache_in_self
    def get_auth(self) -> Auth:
        if self.config.mas.enabled:
            return MasDelegatedAuth(self)
        if self.config.experimental.msc3861.enabled:
            from synapse.api.auth.msc3861_delegated import MSC3861DelegatedAuth

            return MSC3861DelegatedAuth(self)
        return InternalAuth(self)

    @cache_in_self
    def get_auth_blocking(self) -> AuthBlocking:
        return AuthBlocking(self)

    @cache_in_self
    def get_http_client_context_factory(self) -> IPolicyForHTTPS:
        if self.config.tls.use_insecure_ssl_client_just_for_testing_do_not_use:
            return InsecureInterceptableContextFactory()
        return RegularPolicyForHTTPS()

    @cache_in_self
    def get_simple_http_client(self) -> SimpleHttpClient:
        """
        An HTTP client with no special configuration.
        """
        return SimpleHttpClient(self)

    @cache_in_self
    def get_proxied_http_client(self) -> SimpleHttpClient:
        """
        An HTTP client that uses configured HTTP(S) proxies.
        """
        return SimpleHttpClient(self, use_proxy=True)

    @cache_in_self
    def get_proxied_blocklisted_http_client(self) -> SimpleHttpClient:
        """
        An HTTP client that uses configured HTTP(S) proxies and blocks IPs
        based on the configured IP ranges.
        """
        return SimpleHttpClient(
            self,
            ip_allowlist=self.config.server.ip_range_allowlist,
            ip_blocklist=self.config.server.ip_range_blocklist,
            use_proxy=True,
        )

    @cache_in_self
    def get_federation_http_client(self) -> MatrixFederationHttpClient:
        """
        An HTTP client for federation.
        """
        tls_client_options_factory = context_factory.FederationPolicyForHTTPS(
            self.config
        )
        return MatrixFederationHttpClient(self, tls_client_options_factory)

    @cache_in_self
    def get_replication_client(self) -> ReplicationClient:
        """
        An HTTP client for HTTP replication.
        """
        return ReplicationClient(self)

    @cache_in_self
    def get_room_creation_handler(self) -> RoomCreationHandler:
        return RoomCreationHandler(self)

    @cache_in_self
    def get_room_shutdown_handler(self) -> RoomShutdownHandler:
        return RoomShutdownHandler(self)

    @cache_in_self
    def get_state_handler(self) -> StateHandler:
        return StateHandler(self)

    @cache_in_self
    def get_state_resolution_handler(self) -> StateResolutionHandler:
        return StateResolutionHandler(self)

    @cache_in_self
    def get_presence_handler(self) -> BasePresenceHandler:
        if self.get_instance_name() in self.config.worker.writers.presence:
            return PresenceHandler(self)
        else:
            return WorkerPresenceHandler(self)

    @cache_in_self
    def get_typing_writer_handler(self) -> TypingWriterHandler:
        if self.get_instance_name() in self.config.worker.writers.typing:
            return TypingWriterHandler(self)
        else:
            raise Exception("Workers cannot write typing")

    @cache_in_self
    def get_presence_router(self) -> PresenceRouter:
        return PresenceRouter(self)

    @cache_in_self
    def get_typing_handler(self) -> FollowerTypingHandler:
        if self.get_instance_name() in self.config.worker.writers.typing:
            # Use get_typing_writer_handler to ensure that we use the same
            # cached version.
            return self.get_typing_writer_handler()
        else:
            return FollowerTypingHandler(self)

    @cache_in_self
    def get_sso_handler(self) -> SsoHandler:
        return SsoHandler(self)

    @cache_in_self
    def get_jwt_handler(self) -> "JwtHandler":
        from synapse.handlers.jwt import JwtHandler

        return JwtHandler(self)

    @cache_in_self
    def get_sync_handler(self) -> SyncHandler:
        return SyncHandler(self)

    @cache_in_self
    def get_sliding_sync_handler(self) -> SlidingSyncHandler:
        return SlidingSyncHandler(self)

    @cache_in_self
    def get_room_list_handler(self) -> RoomListHandler:
        return RoomListHandler(self)

    @cache_in_self
    def get_auth_handler(self) -> AuthHandler:
        return AuthHandler(self)

    @cache_in_self
    def get_macaroon_generator(self) -> MacaroonGenerator:
        return MacaroonGenerator(
            self.get_clock(), self.hostname, self.config.key.macaroon_secret_key
        )

    @cache_in_self
    def get_device_handler(self) -> DeviceHandler:
        if self.get_instance_name() in self.config.worker.writers.device_lists:
            return DeviceWriterHandler(self)

        return DeviceHandler(self)

    @cache_in_self
    def get_device_message_handler(self) -> DeviceMessageHandler:
        return DeviceMessageHandler(self)

    @cache_in_self
    def get_directory_handler(self) -> DirectoryHandler:
        return DirectoryHandler(self)

    @cache_in_self
    def get_e2e_keys_handler(self) -> E2eKeysHandler:
        return E2eKeysHandler(self)

    @cache_in_self
    def get_e2e_room_keys_handler(self) -> E2eRoomKeysHandler:
        return E2eRoomKeysHandler(self)

    @cache_in_self
    def get_admin_handler(self) -> AdminHandler:
        return AdminHandler(self)

    @cache_in_self
    def get_application_service_api(self) -> ApplicationServiceApi:
        return ApplicationServiceApi(self)

    @cache_in_self
    def get_application_service_scheduler(self) -> ApplicationServiceScheduler:
        return ApplicationServiceScheduler(self)

    @cache_in_self
    def get_application_service_handler(self) -> ApplicationServicesHandler:
        return ApplicationServicesHandler(self)

    @cache_in_self
    def get_event_handler(self) -> EventHandler:
        return EventHandler(self)

    @cache_in_self
    def get_event_stream_handler(self) -> EventStreamHandler:
        return EventStreamHandler(self)

    @cache_in_self
    def get_federation_handler(self) -> FederationHandler:
        return FederationHandler(self)

    @cache_in_self
    def get_federation_event_handler(self) -> FederationEventHandler:
        return FederationEventHandler(self)

    @cache_in_self
    def get_identity_handler(self) -> IdentityHandler:
        return IdentityHandler(self)

    @cache_in_self
    def get_initial_sync_handler(self) -> InitialSyncHandler:
        return InitialSyncHandler(self)

    @cache_in_self
    def get_profile_handler(self) -> ProfileHandler:
        return ProfileHandler(self)

    @cache_in_self
    def get_event_creation_handler(self) -> EventCreationHandler:
        return EventCreationHandler(self)

    @cache_in_self
    def get_deactivate_account_handler(self) -> DeactivateAccountHandler:
        return DeactivateAccountHandler(self)

    @cache_in_self
    def get_search_handler(self) -> SearchHandler:
        return SearchHandler(self)

    @cache_in_self
    def get_send_email_handler(self) -> SendEmailHandler:
        return SendEmailHandler(self)

    @cache_in_self
    def get_set_password_handler(self) -> SetPasswordHandler:
        return SetPasswordHandler(self)

    @cache_in_self
    def get_event_sources(self) -> EventSources:
        return EventSources(self)

    @cache_in_self
    def get_keyring(self) -> Keyring:
        return Keyring(self)

    @cache_in_self
    def get_event_builder_factory(self) -> EventBuilderFactory:
        return EventBuilderFactory(self)

    @cache_in_self
    def get_filtering(self) -> Filtering:
        return Filtering(self)

    @cache_in_self
    def get_pusherpool(self) -> PusherPool:
        return PusherPool(self)

    @cache_in_self
    def get_media_repository_resource(self) -> MediaRepositoryResource:
        # build the media repo resource. This indirects through the HomeServer
        # to ensure that we only have a single instance of
        return MediaRepositoryResource(self)

    @cache_in_self
    def get_media_repository(self) -> MediaRepository:
        return MediaRepository(self)

    @cache_in_self
    def get_federation_transport_client(self) -> TransportLayerClient:
        return TransportLayerClient(self)

    @cache_in_self
    def get_federation_sender(self) -> AbstractFederationSender:
        if self.should_send_federation():
            return FederationSender(self)
        elif not self.config.worker.worker_app:
            return FederationRemoteSendQueue(self)
        else:
            raise Exception("Workers cannot send federation traffic")

    @cache_in_self
    def get_receipts_handler(self) -> ReceiptsHandler:
        return ReceiptsHandler(self)

    @cache_in_self
    def get_reports_handler(self) -> ReportsHandler:
        return ReportsHandler(self)

    @cache_in_self
    def get_read_marker_handler(self) -> ReadMarkerHandler:
        return ReadMarkerHandler(self)

    @cache_in_self
    def get_replication_command_handler(self) -> ReplicationCommandHandler:
        return ReplicationCommandHandler(self)

    @cache_in_self
    def get_bulk_push_rule_evaluator(self) -> BulkPushRuleEvaluator:
        return BulkPushRuleEvaluator(self)

    @cache_in_self
    def get_user_directory_handler(self) -> UserDirectoryHandler:
        return UserDirectoryHandler(self)

    @cache_in_self
    def get_stats_handler(self) -> StatsHandler:
        return StatsHandler(self)

    @cache_in_self
    def get_password_auth_provider(self) -> PasswordAuthProvider:
        return PasswordAuthProvider()

    @cache_in_self
    def get_room_member_handler(self) -> RoomMemberHandler:
        if self.config.worker.worker_app:
            return RoomMemberWorkerHandler(self)
        return RoomMemberMasterHandler(self)

    @cache_in_self
    def get_federation_registry(self) -> FederationHandlerRegistry:
        return FederationHandlerRegistry(self)

    @cache_in_self
    def get_server_notices_manager(self) -> ServerNoticesManager:
        if self.config.worker.worker_app:
            raise Exception("Workers cannot send server notices")
        return ServerNoticesManager(self)

    @cache_in_self
    def get_server_notices_sender(self) -> WorkerServerNoticesSender:
        if self.config.worker.worker_app:
            return WorkerServerNoticesSender(self)
        return ServerNoticesSender(self)

    @cache_in_self
    def get_message_handler(self) -> MessageHandler:
        return MessageHandler(self)

    @cache_in_self
    def get_pagination_handler(self) -> PaginationHandler:
        return PaginationHandler(self)

    @cache_in_self
    def get_relations_handler(self) -> RelationsHandler:
        return RelationsHandler(self)

    @cache_in_self
    def get_room_context_handler(self) -> RoomContextHandler:
        return RoomContextHandler(self)

    @cache_in_self
    def get_timestamp_lookup_handler(self) -> TimestampLookupHandler:
        return TimestampLookupHandler(self)

    @cache_in_self
    def get_thread_subscriptions_handler(self) -> ThreadSubscriptionsHandler:
        return ThreadSubscriptionsHandler(self)

    @cache_in_self
    def get_registration_handler(self) -> RegistrationHandler:
        return RegistrationHandler(self)

    @cache_in_self
    def get_account_validity_handler(self) -> AccountValidityHandler:
        return AccountValidityHandler(self)

    @cache_in_self
    def get_cas_handler(self) -> CasHandler:
        return CasHandler(self)

    @cache_in_self
    def get_saml_handler(self) -> "SamlHandler":
        from synapse.handlers.saml import SamlHandler

        return SamlHandler(self)

    @cache_in_self
    def get_oidc_handler(self) -> "OidcHandler":
        from synapse.handlers.oidc import OidcHandler

        return OidcHandler(self)

    @cache_in_self
    def get_room_policy_handler(self) -> RoomPolicyHandler:
        return RoomPolicyHandler(self)

    @cache_in_self
    def get_event_client_serializer(self) -> EventClientSerializer:
        return EventClientSerializer(self)

    @cache_in_self
    def get_password_policy_handler(self) -> PasswordPolicyHandler:
        return PasswordPolicyHandler(self)

    @cache_in_self
    def get_storage_controllers(self) -> StorageControllers:
        return StorageControllers(self, self.get_datastores())

    @cache_in_self
    def get_replication_streamer(self) -> ReplicationStreamer:
        return ReplicationStreamer(self)

    @cache_in_self
    def get_replication_data_handler(self) -> ReplicationDataHandler:
        return ReplicationDataHandler(self)

    @cache_in_self
    def get_replication_streams(self) -> dict[str, Stream]:
        return {stream.NAME: stream(self) for stream in STREAMS_MAP.values()}

    @cache_in_self
    def get_federation_ratelimiter(self) -> FederationRateLimiter:
        return FederationRateLimiter(
            our_server_name=self.hostname,
            clock=self.get_clock(),
            config=self.config.ratelimiting.rc_federation,
            metrics_name="federation_servlets",
        )

    @cache_in_self
    def get_module_api(self) -> ModuleApi:
        return ModuleApi(self, self.get_auth_handler())

    @cache_in_self
    def get_module_api_callbacks(self) -> ModuleApiCallbacks:
        return ModuleApiCallbacks(self)

    @cache_in_self
    def get_account_data_handler(self) -> AccountDataHandler:
        return AccountDataHandler(self)

    @cache_in_self
    def get_room_summary_handler(self) -> RoomSummaryHandler:
        return RoomSummaryHandler(self)

    @cache_in_self
    def get_event_auth_handler(self) -> EventAuthHandler:
        return EventAuthHandler(self)

    @cache_in_self
    def get_external_cache(self) -> ExternalCache:
        return ExternalCache(self)

    @cache_in_self
    def get_account_handler(self) -> AccountHandler:
        return AccountHandler(self)

    @cache_in_self
    def get_push_rules_handler(self) -> PushRulesHandler:
        return PushRulesHandler(self)

    @cache_in_self
    def get_room_forgetter_handler(self) -> RoomForgetterHandler:
        return RoomForgetterHandler(self)

    @cache_in_self
    def get_rendezvous_handler(self) -> RendezvousHandler:
        return RendezvousHandler(self)

    @cache_in_self
    def get_outbound_redis_connection(self) -> "ConnectionHandler":
        """
        The Redis connection used for replication.

        Raises:
            AssertionError: if Redis is not enabled in the homeserver config.
        """
        assert self.config.redis.redis_enabled

        # We only want to import redis module if we're using it, as we have
        # `txredisapi` as an optional dependency.
        from synapse.replication.tcp.redis import lazyConnection, lazyUnixConnection

        if self.config.redis.redis_path is None:
            logger.info(
                "Connecting to redis (host=%r port=%r) for external cache",
                self.config.redis.redis_host,
                self.config.redis.redis_port,
            )

            return lazyConnection(
                hs=self,
                host=self.config.redis.redis_host,
                port=self.config.redis.redis_port,
                dbid=self.config.redis.redis_dbid,
                password=self.config.redis.redis_password,
                reconnect=True,
            )
        else:
            logger.info(
                "Connecting to redis (path=%r) for external cache",
                self.config.redis.redis_path,
            )

            return lazyUnixConnection(
                hs=self,
                path=self.config.redis.redis_path,
                dbid=self.config.redis.redis_dbid,
                password=self.config.redis.redis_password,
                reconnect=True,
            )

    def should_send_federation(self) -> bool:
        "Should this server be sending federation traffic directly?"
        return self.config.worker.send_federation

    @cache_in_self
    def get_request_ratelimiter(self) -> RequestRatelimiter:
        return RequestRatelimiter(
            self.get_datastores().main,
            self.get_clock(),
            self.config.ratelimiting.rc_message,
            self.config.ratelimiting.rc_admin_redaction,
        )

    @cache_in_self
    def get_common_usage_metrics_manager(self) -> CommonUsageMetricsManager:
        """Usage metrics shared between phone home stats and the prometheus exporter."""
        return CommonUsageMetricsManager(self)

    @cache_in_self
    def get_worker_locks_handler(self) -> WorkerLocksHandler:
        return WorkerLocksHandler(self)

    @cache_in_self
    def get_task_scheduler(self) -> TaskScheduler:
        return TaskScheduler(self)

    @cache_in_self
    def get_media_sender_thread_pool(self) -> ThreadPool:
        """Fetch the threadpool used to read files when responding to media
        download requests."""

        # We can choose a large threadpool size as these threads predominately
        # do IO rather than CPU work.
        media_threadpool = ThreadPool(
            name="media_threadpool", minthreads=1, maxthreads=50
        )

        media_threadpool.start()
        self.register_sync_shutdown_handler(
            phase="during",
            eventType="shutdown",
            shutdown_func=media_threadpool.stop,
        )

        # Register the threadpool with our metrics.
        server_name = self.hostname
        register_threadpool(
            name="media", server_name=server_name, threadpool=media_threadpool
        )

        return media_threadpool

    @cache_in_self
    def get_delayed_events_handler(self) -> DelayedEventsHandler:
        return DelayedEventsHandler(self)
