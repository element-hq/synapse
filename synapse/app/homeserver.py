#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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
import os
import sys
from typing import Iterable

from twisted.internet.tcp import Port
from twisted.web.resource import EncodingResourceWrapper, Resource
from twisted.web.server import GzipEncoderFactory

import synapse
from synapse import events
from synapse.api.urls import (
    CLIENT_API_PREFIX,
    FEDERATION_PREFIX,
    LEGACY_MEDIA_PREFIX,
    MEDIA_R0_PREFIX,
    MEDIA_V3_PREFIX,
    SERVER_KEY_PREFIX,
    STATIC_PREFIX,
)
from synapse.app import _base
from synapse.app._base import (
    handle_startup_exception,
    listen_http,
    max_request_body_size,
    redirect_stdio_to_logs,
    register_start,
)
from synapse.config._base import ConfigError, format_config_error
from synapse.config.homeserver import HomeServerConfig
from synapse.config.logger import setup_logging
from synapse.config.server import ListenerConfig, TCPListenerConfig
from synapse.federation.transport.server import TransportLayerServer
from synapse.http.additional_resource import AdditionalResource
from synapse.http.server import (
    JsonResource,
    OptionsResource,
    RootOptionsRedirectResource,
    StaticResource,
)
from synapse.logging.context import LoggingContext
from synapse.logging.opentracing import init_tracer
from synapse.metrics import METRICS_PREFIX, MetricsResource, RegistryProxy
from synapse.replication.http import REPLICATION_PREFIX, ReplicationRestResource
from synapse.rest import ClientRestResource, admin
from synapse.rest.health import HealthResource
from synapse.rest.key.v2 import KeyResource
from synapse.rest.synapse.client import build_synapse_client_resource_tree
from synapse.rest.well_known import well_known_resource
from synapse.server import HomeServer
from synapse.storage import DataStore
from synapse.types import ISynapseReactor
from synapse.util.httpresourcetree import create_resource_tree
from synapse.util.module_loader import load_module

logger = logging.getLogger("synapse.app.homeserver")


def gz_wrap(r: Resource) -> Resource:
    return EncodingResourceWrapper(r, [GzipEncoderFactory()])


class SynapseHomeServer(HomeServer):
    """
    Homeserver class for the main Synapse process.
    """

    DATASTORE_CLASS = DataStore

    def _listener_http(
        self,
        config: HomeServerConfig,
        listener_config: ListenerConfig,
    ) -> Iterable[Port]:
        # Must exist since this is an HTTP listener.
        assert listener_config.http_options is not None
        site_tag = listener_config.get_site_tag()

        # We always include a health resource.
        resources: dict[str, Resource] = {"/health": HealthResource()}

        for res in listener_config.http_options.resources:
            for name in res.names:
                if name == "openid" and "federation" in res.names:
                    # Skip loading openid resource if federation is defined
                    # since federation resource will include openid
                    continue
                if name == "media" and (
                    "federation" in res.names or "client" in res.names
                ):
                    # Skip loading media resource if federation or client are defined
                    # since federation & client resources will include media
                    continue
                if name == "health":
                    # Skip loading, health resource is always included
                    continue
                resources.update(self._configure_named_resource(name, res.compress))

        additional_resources = listener_config.http_options.additional_resources
        logger.debug("Configuring additional resources: %r", additional_resources)
        module_api = self.get_module_api()
        for path, resmodule in additional_resources.items():
            handler_cls, config = load_module(
                resmodule,
                ("listeners", site_tag, "additional_resources", "<%s>" % (path,)),
            )
            handler = handler_cls(config, module_api)
            if isinstance(handler, Resource):
                resource = handler
            elif hasattr(handler, "handle_request"):
                resource = AdditionalResource(self, handler.handle_request)
            else:
                raise ConfigError(
                    "additional_resource %s does not implement a known interface"
                    % (resmodule["module"],)
                )
            resources[path] = resource

        # Attach additional resources registered by modules.
        resources.update(self._module_web_resources)
        self._module_web_resources_consumed = True

        # Try to find something useful to serve at '/':
        #
        # 1. Redirect to the web client if it is an HTTP(S) URL.
        # 2. Redirect to the static "Synapse is running" page.
        # 3. Do not redirect and use a blank resource.
        if self.config.server.web_client_location:
            root_resource: Resource = RootOptionsRedirectResource(
                self.config.server.web_client_location
            )
        elif STATIC_PREFIX in resources:
            root_resource = RootOptionsRedirectResource(STATIC_PREFIX)
        else:
            root_resource = OptionsResource()

        ports = listen_http(
            self,
            listener_config,
            create_resource_tree(resources, root_resource),
            self.version_string,
            max_request_body_size(self.config),
            self.tls_server_context_factory,
            reactor=self.get_reactor(),
        )

        return ports

    def _configure_named_resource(
        self, name: str, compress: bool = False
    ) -> dict[str, Resource]:
        """Build a resource map for a named resource

        Args:
            name: named resource: one of "client", "federation", etc
            compress: whether to enable gzip compression for this resource

        Returns:
            map from path to HTTP resource
        """
        resources: dict[str, Resource] = {}
        if name == "client":
            client_resource: Resource = ClientRestResource(self)
            if compress:
                client_resource = gz_wrap(client_resource)

            admin_resource = JsonResource(self, canonical_json=False)
            admin.register_servlets(self, admin_resource)

            resources.update(
                {
                    CLIENT_API_PREFIX: client_resource,
                    "/.well-known": well_known_resource(self),
                    "/_synapse/admin": admin_resource,
                    **build_synapse_client_resource_tree(self),
                }
            )

            if self.config.email.can_verify_email:
                from synapse.rest.synapse.client.password_reset import (
                    PasswordResetSubmitTokenResource,
                )

                resources["/_synapse/client/password_reset/email/submit_token"] = (
                    PasswordResetSubmitTokenResource(self)
                )

        if name == "consent":
            from synapse.rest.consent.consent_resource import ConsentResource

            consent_resource: Resource = ConsentResource(self)
            if compress:
                consent_resource = gz_wrap(consent_resource)
            resources["/_matrix/consent"] = consent_resource

        if name == "federation":
            federation_resource: Resource = TransportLayerServer(self)
            if compress:
                federation_resource = gz_wrap(federation_resource)
            resources[FEDERATION_PREFIX] = federation_resource

        if name == "openid":
            resources[FEDERATION_PREFIX] = TransportLayerServer(
                self, servlet_groups=["openid"]
            )

        if name in ["static", "client"]:
            resources[STATIC_PREFIX] = StaticResource(
                os.path.join(os.path.dirname(synapse.__file__), "static")
            )

        if name in ["media", "federation", "client"]:
            if self.config.media.can_load_media_repo:
                media_repo = self.get_media_repository_resource()
                resources.update(
                    {
                        MEDIA_R0_PREFIX: media_repo,
                        MEDIA_V3_PREFIX: media_repo,
                        LEGACY_MEDIA_PREFIX: media_repo,
                    }
                )
            elif name == "media":
                raise ConfigError(
                    "'media' resource conflicts with enable_media_repo=False"
                )

        if name == "media":
            resources[FEDERATION_PREFIX] = TransportLayerServer(
                self, servlet_groups=["media"]
            )
            resources[CLIENT_API_PREFIX] = ClientRestResource(
                self, servlet_groups=["media"]
            )

        if name in ["keys", "federation"]:
            resources[SERVER_KEY_PREFIX] = KeyResource(self)

        if name == "metrics" and self.config.metrics.enable_metrics:
            metrics_resource: Resource = MetricsResource(RegistryProxy)
            if compress:
                metrics_resource = gz_wrap(metrics_resource)
            resources[METRICS_PREFIX] = metrics_resource

        if name == "replication":
            resources[REPLICATION_PREFIX] = ReplicationRestResource(self)

        return resources

    def start_listening(self) -> None:
        if self.config.redis.redis_enabled:
            # If redis is enabled we connect via the replication command handler
            # in the same way as the workers (since we're effectively a client
            # rather than a server).
            self.get_replication_command_handler().start_replication(self)

        for listener in self.config.server.listeners:
            if listener.type == "http":
                self._listening_services.extend(
                    self._listener_http(self.config, listener)
                )
            elif listener.type == "manhole":
                if isinstance(listener, TCPListenerConfig):
                    self._listening_services.extend(
                        _base.listen_manhole(
                            listener.bind_addresses,
                            listener.port,
                            manhole_settings=self.config.server.manhole_settings,
                            manhole_globals={"hs": self},
                        )
                    )
                else:
                    raise ConfigError(
                        "Can not use a unix socket for manhole at this time."
                    )
            elif listener.type == "metrics":
                if not self.config.metrics.enable_metrics:
                    logger.warning(
                        "Metrics listener configured, but enable_metrics is not True!"
                    )
                else:
                    if isinstance(listener, TCPListenerConfig):
                        self._metrics_listeners.extend(
                            _base.listen_metrics(
                                listener.bind_addresses,
                                listener.port,
                            )
                        )
                    else:
                        raise ConfigError(
                            "Can not use a unix socket for metrics at this time."
                        )

            else:
                # this shouldn't happen, as the listener type should have been checked
                # during parsing
                logger.warning("Unrecognized listener type: %s", listener.type)


def load_or_generate_config(argv_options: list[str]) -> HomeServerConfig:
    """
    Parse the commandline and config files

    Supports generation of config files, so is used for the main homeserver app.

    Args:
        argv_options: The options passed to Synapse. Usually `sys.argv[1:]`.

    Returns:
        A homeserver instance.
    """
    try:
        config = HomeServerConfig.load_or_generate_config(
            "Synapse Homeserver", argv_options
        )
    except ConfigError as e:
        sys.stderr.write("\n")
        for f in format_config_error(e):
            sys.stderr.write(f)
        sys.stderr.write("\n")
        sys.exit(1)

    if not config:
        # If a config isn't returned, and an exception isn't raised, we're just
        # generating config files and shouldn't try to continue.
        sys.exit(0)

    return config


def create_homeserver(
    config: HomeServerConfig,
    reactor: ISynapseReactor | None = None,
) -> SynapseHomeServer:
    """
    Create a homeserver instance for the Synapse main process.

    Our composable functions (`create_homeserver`, `setup`, `start`) should not exit the
    Python process (call `exit(...)`) and instead raise exceptions which can be handled
    by the caller as desired. This doesn't matter for the normal case of one Synapse
    instance running in the Python process (as we're only affecting ourselves), but is
    important when we have multiple Synapse homeserver tenants running in the same
    Python process (c.f. Synapse Pro for small hosts) as we don't want some problem from
    one tenant stopping the rest of the tenants.

    Args:
        config: The configuration for the homeserver.
        reactor: Optionally provide a reactor to use. Can be useful in different
            scenarios that you want control over the reactor, such as tests.

    Returns:
        A homeserver instance.
    """

    if config.worker.worker_app:
        raise ConfigError(
            "You have specified `worker_app` in the config but are attempting to setup a non-worker "
            "instance. Please use `python -m synapse.app.generic_worker` instead (or remove the option if this is the main process)."
        )

    events.USE_FROZEN_DICTS = config.server.use_frozen_dicts
    synapse.util.caches.TRACK_MEMORY_USAGE = config.caches.track_memory_usage

    if config.server.gc_seconds:
        synapse.metrics.MIN_TIME_BETWEEN_GCS = config.server.gc_seconds

    hs = SynapseHomeServer(
        hostname=config.server.server_name,
        config=config,
        reactor=reactor,
    )

    return hs


def setup(
    hs: SynapseHomeServer,
) -> None:
    """
    Setup a `SynapseHomeServer` (main) instance.

    Our composable functions (`create_homeserver`, `setup`, `start`) should not exit the
    Python process (call `exit(...)`) and instead raise exceptions which can be handled
    by the caller as desired. This doesn't matter for the normal case of one Synapse
    instance running in the Python process (as we're only affecting ourselves), but is
    important when we have multiple Synapse homeserver tenants running in the same
    Python process (c.f. Synapse Pro for small hosts) as we don't want some problem from
    one tenant stopping the rest of the tenants.

    Args:
        hs: The homeserver to setup.
    """

    setup_logging(hs, hs.config, use_worker_options=False)

    # Log after we've configured logging.
    logger.info("Setting up server")

    # Start the tracer
    init_tracer(hs)  # noqa

    hs.setup()


async def start(
    hs: SynapseHomeServer,
    *,
    freeze: bool = True,
) -> None:
    """
    Should be called once the reactor is running.

    Our composable functions (`create_homeserver`, `setup`, `start`) should not exit the
    Python process (call `exit(...)`) and instead raise exceptions which can be handled
    by the caller as desired. This doesn't matter for the normal case of one Synapse
    instance running in the Python process (as we're only affecting ourselves), but is
    important when we have multiple Synapse homeserver tenants running in the same
    Python process (c.f. Synapse Pro for small hosts) as we don't want some problem from
    one tenant stopping the rest of the tenants.

    Args:
        hs: The homeserver to setup.
        freeze: whether to freeze the homeserver base objects in the garbage collector.
            May improve garbage collection performance by marking objects with an effectively
            static lifetime as frozen so they don't need to be considered for cleanup.
            If you ever want to `shutdown` the homeserver, this needs to be
            False otherwise the homeserver cannot be garbage collected after `shutdown`.
    """

    await _base.start(hs, freeze=freeze)

    # TODO: Feels like this should be moved somewhere else.
    for db in hs.get_datastores().databases:
        db.updates.start_doing_background_updates()


def start_reactor(
    config: HomeServerConfig,
) -> None:
    """
    Start the reactor (Twisted event-loop).

    Args:
        config: The configuration for the homeserver.
    """
    _base.start_reactor(
        "synapse-homeserver",
        soft_file_limit=config.server.soft_file_limit,
        gc_thresholds=config.server.gc_thresholds,
        pid_file=config.server.pid_file,
        daemonize=config.server.daemonize,
        print_pidfile=config.server.print_pidfile,
        logger=logger,
    )


def main() -> None:
    homeserver_config = load_or_generate_config(sys.argv[1:])

    # Create a logging context as soon as possible so we can start associating
    # everything with this homeserver.
    with LoggingContext(name="main", server_name=homeserver_config.server.server_name):
        # Initialize and setup the homeserver
        hs = create_homeserver(homeserver_config)
        try:
            setup(hs)
        except Exception as e:
            handle_startup_exception(e)

        # For problems immediately apparent during initialization, we want to log to
        # stderr in the terminal so that they are obvious and visible to the operator.
        #
        # Now that we're past the initialization stage, we can redirect anything printed
        # to stdio to the logs, if configured.
        if not homeserver_config.logging.no_redirect_stdio:
            redirect_stdio_to_logs()

        # Register a callback to be invoked once the reactor is running
        register_start(hs, start, hs)

        start_reactor(homeserver_config)


if __name__ == "__main__":
    main()
