#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
# Copyright 2016 OpenMarket Ltd
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
import sys
from typing import Dict, List

from twisted.web.resource import Resource

import synapse
import synapse.events
from synapse.api.urls import (
    CLIENT_API_PREFIX,
    FEDERATION_PREFIX,
    LEGACY_MEDIA_PREFIX,
    MEDIA_R0_PREFIX,
    MEDIA_V3_PREFIX,
    SERVER_KEY_PREFIX,
)
from synapse.app import _base
from synapse.app._base import (
    handle_startup_exception,
    max_request_body_size,
    redirect_stdio_to_logs,
    register_start,
)
from synapse.config._base import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.config.logger import setup_logging
from synapse.config.server import ListenerConfig, TCPListenerConfig
from synapse.federation.transport.server import TransportLayerServer
from synapse.http.server import JsonResource, OptionsResource
from synapse.logging.context import LoggingContext
from synapse.metrics import METRICS_PREFIX, MetricsResource, RegistryProxy
from synapse.replication.http import REPLICATION_PREFIX, ReplicationRestResource
from synapse.rest import ClientRestResource
from synapse.rest.admin import register_servlets_for_media_repo
from synapse.rest.health import HealthResource
from synapse.rest.key.v2 import KeyResource
from synapse.rest.synapse.client import build_synapse_client_resource_tree
from synapse.rest.well_known import well_known_resource
from synapse.server import HomeServer
from synapse.storage.databases.main.account_data import AccountDataWorkerStore
from synapse.storage.databases.main.appservice import (
    ApplicationServiceTransactionWorkerStore,
    ApplicationServiceWorkerStore,
)
from synapse.storage.databases.main.censor_events import CensorEventsStore
from synapse.storage.databases.main.client_ips import ClientIpWorkerStore
from synapse.storage.databases.main.deviceinbox import DeviceInboxWorkerStore
from synapse.storage.databases.main.devices import DeviceWorkerStore
from synapse.storage.databases.main.directory import DirectoryWorkerStore
from synapse.storage.databases.main.e2e_room_keys import EndToEndRoomKeyStore
from synapse.storage.databases.main.event_federation import EventFederationWorkerStore
from synapse.storage.databases.main.event_push_actions import (
    EventPushActionsWorkerStore,
)
from synapse.storage.databases.main.events_worker import EventsWorkerStore
from synapse.storage.databases.main.experimental_features import (
    ExperimentalFeaturesStore,
)
from synapse.storage.databases.main.filtering import FilteringWorkerStore
from synapse.storage.databases.main.keys import KeyStore
from synapse.storage.databases.main.lock import LockStore
from synapse.storage.databases.main.media_repository import MediaRepositoryStore
from synapse.storage.databases.main.metrics import ServerMetricsStore
from synapse.storage.databases.main.monthly_active_users import (
    MonthlyActiveUsersWorkerStore,
)
from synapse.storage.databases.main.presence import PresenceStore
from synapse.storage.databases.main.profile import ProfileWorkerStore
from synapse.storage.databases.main.purge_events import PurgeEventsStore
from synapse.storage.databases.main.push_rule import PushRulesWorkerStore
from synapse.storage.databases.main.pusher import PusherWorkerStore
from synapse.storage.databases.main.receipts import ReceiptsWorkerStore
from synapse.storage.databases.main.registration import RegistrationWorkerStore
from synapse.storage.databases.main.relations import RelationsWorkerStore
from synapse.storage.databases.main.room import RoomWorkerStore
from synapse.storage.databases.main.roommember import RoomMemberWorkerStore
from synapse.storage.databases.main.search import SearchStore
from synapse.storage.databases.main.session import SessionStore
from synapse.storage.databases.main.signatures import SignatureWorkerStore
from synapse.storage.databases.main.sliding_sync import SlidingSyncStore
from synapse.storage.databases.main.state import StateGroupWorkerStore
from synapse.storage.databases.main.stats import StatsStore
from synapse.storage.databases.main.stream import StreamWorkerStore
from synapse.storage.databases.main.tags import TagsWorkerStore
from synapse.storage.databases.main.task_scheduler import TaskSchedulerWorkerStore
from synapse.storage.databases.main.transactions import TransactionWorkerStore
from synapse.storage.databases.main.ui_auth import UIAuthWorkerStore
from synapse.storage.databases.main.user_directory import UserDirectoryStore
from synapse.storage.databases.main.user_erasure_store import UserErasureWorkerStore
from synapse.util import SYNAPSE_VERSION
from synapse.util.httpresourcetree import create_resource_tree

logger = logging.getLogger("synapse.app.generic_worker")


class GenericWorkerStore(
    # FIXME(https://github.com/matrix-org/synapse/issues/3714): We need to add
    # UserDirectoryStore as we write directly rather than going via the correct worker.
    UserDirectoryStore,
    StatsStore,
    UIAuthWorkerStore,
    EndToEndRoomKeyStore,
    PresenceStore,
    DeviceInboxWorkerStore,
    DeviceWorkerStore,
    TagsWorkerStore,
    AccountDataWorkerStore,
    CensorEventsStore,
    ClientIpWorkerStore,
    # KeyStore isn't really safe to use from a worker, but for now we do so and hope that
    # the races it creates aren't too bad.
    KeyStore,
    RoomWorkerStore,
    DirectoryWorkerStore,
    PushRulesWorkerStore,
    ApplicationServiceTransactionWorkerStore,
    ApplicationServiceWorkerStore,
    ProfileWorkerStore,
    FilteringWorkerStore,
    MonthlyActiveUsersWorkerStore,
    MediaRepositoryStore,
    ServerMetricsStore,
    PusherWorkerStore,
    RoomMemberWorkerStore,
    RelationsWorkerStore,
    EventFederationWorkerStore,
    EventPushActionsWorkerStore,
    PurgeEventsStore,
    StateGroupWorkerStore,
    SignatureWorkerStore,
    UserErasureWorkerStore,
    ReceiptsWorkerStore,
    StreamWorkerStore,
    EventsWorkerStore,
    RegistrationWorkerStore,
    SearchStore,
    TransactionWorkerStore,
    LockStore,
    SessionStore,
    TaskSchedulerWorkerStore,
    ExperimentalFeaturesStore,
    SlidingSyncStore,
):
    # Properties that multiple storage classes define. Tell mypy what the
    # expected type is.
    server_name: str
    config: HomeServerConfig


class GenericWorkerServer(HomeServer):
    DATASTORE_CLASS = GenericWorkerStore

    def _listen_http(self, listener_config: ListenerConfig) -> None:
        assert listener_config.http_options is not None

        # We always include a health resource.
        resources: Dict[str, Resource] = {"/health": HealthResource()}

        for res in listener_config.http_options.resources:
            for name in res.names:
                if name == "metrics":
                    resources[METRICS_PREFIX] = MetricsResource(RegistryProxy)
                elif name == "client":
                    resource: Resource = ClientRestResource(self)

                    resources[CLIENT_API_PREFIX] = resource

                    resources.update(build_synapse_client_resource_tree(self))
                    resources["/.well-known"] = well_known_resource(self)

                elif name == "federation":
                    resources[FEDERATION_PREFIX] = TransportLayerServer(self)
                elif name == "media":
                    if self.config.media.can_load_media_repo:
                        media_repo = self.get_media_repository_resource()

                        # We need to serve the admin servlets for media on the
                        # worker.
                        admin_resource = JsonResource(self, canonical_json=False)
                        register_servlets_for_media_repo(self, admin_resource)

                        resources.update(
                            {
                                MEDIA_R0_PREFIX: media_repo,
                                MEDIA_V3_PREFIX: media_repo,
                                LEGACY_MEDIA_PREFIX: media_repo,
                                "/_synapse/admin": admin_resource,
                            }
                        )

                        if "federation" not in res.names:
                            # Only load the federation media resource separately if federation
                            # resource is not specified since federation resource includes media
                            # resource.
                            resources[FEDERATION_PREFIX] = TransportLayerServer(
                                self, servlet_groups=["media"]
                            )
                        if "client" not in res.names:
                            # Only load the client media resource separately if client
                            # resource is not specified since client resource includes media
                            # resource.
                            resources[CLIENT_API_PREFIX] = ClientRestResource(
                                self, servlet_groups=["media"]
                            )
                    else:
                        logger.warning(
                            "A 'media' listener is configured but the media"
                            " repository is disabled. Ignoring."
                        )
                elif name == "health":
                    # Skip loading, health resource is always included
                    continue

                if name == "openid" and "federation" not in res.names:
                    # Only load the openid resource separately if federation resource
                    # is not specified since federation resource includes openid
                    # resource.
                    resources[FEDERATION_PREFIX] = TransportLayerServer(
                        self, servlet_groups=["openid"]
                    )

                if name in ["keys", "federation"]:
                    resources[SERVER_KEY_PREFIX] = KeyResource(self)

                if name == "replication":
                    resources[REPLICATION_PREFIX] = ReplicationRestResource(self)

        # Attach additional resources registered by modules.
        resources.update(self._module_web_resources)
        self._module_web_resources_consumed = True

        root_resource = create_resource_tree(resources, OptionsResource())

        _base.listen_http(
            self,
            listener_config,
            root_resource,
            self.version_string,
            max_request_body_size(self.config),
            self.tls_server_context_factory,
            reactor=self.get_reactor(),
        )

    def start_listening(self) -> None:
        for listener in self.config.worker.worker_listeners:
            if listener.type == "http":
                self._listen_http(listener)
            elif listener.type == "manhole":
                if isinstance(listener, TCPListenerConfig):
                    _base.listen_manhole(
                        listener.bind_addresses,
                        listener.port,
                        manhole_settings=self.config.server.manhole_settings,
                        manhole_globals={"hs": self},
                    )
                else:
                    raise ConfigError(
                        "Can not using a unix socket for manhole at this time."
                    )

            elif listener.type == "metrics":
                if not self.config.metrics.enable_metrics:
                    logger.warning(
                        "Metrics listener configured, but "
                        "enable_metrics is not True!"
                    )
                else:
                    if isinstance(listener, TCPListenerConfig):
                        _base.listen_metrics(
                            listener.bind_addresses,
                            listener.port,
                        )
                    else:
                        raise ConfigError(
                            "Can not use a unix socket for metrics at this time."
                        )

            else:
                logger.warning("Unsupported listener type: %s", listener.type)

        self.get_replication_command_handler().start_replication(self)


def start(config_options: List[str]) -> None:
    try:
        config = HomeServerConfig.load_config("Synapse worker", config_options)
    except ConfigError as e:
        sys.stderr.write("\n" + str(e) + "\n")
        sys.exit(1)

    # For backwards compatibility let any of the old app names.
    assert config.worker.worker_app in (
        "synapse.app.appservice",
        "synapse.app.client_reader",
        "synapse.app.event_creator",
        "synapse.app.federation_reader",
        "synapse.app.federation_sender",
        "synapse.app.frontend_proxy",
        "synapse.app.generic_worker",
        "synapse.app.media_repository",
        "synapse.app.pusher",
        "synapse.app.synchrotron",
        "synapse.app.user_dir",
    )

    synapse.events.USE_FROZEN_DICTS = config.server.use_frozen_dicts
    synapse.util.caches.TRACK_MEMORY_USAGE = config.caches.track_memory_usage

    if config.server.gc_seconds:
        synapse.metrics.MIN_TIME_BETWEEN_GCS = config.server.gc_seconds

    hs = GenericWorkerServer(
        config.server.server_name,
        config=config,
        version_string=f"Synapse/{SYNAPSE_VERSION}",
    )

    setup_logging(hs, config, use_worker_options=True)

    try:
        hs.setup()

        # Ensure the replication streamer is always started in case we write to any
        # streams. Will no-op if no streams can be written to by this worker.
        hs.get_replication_streamer()
    except Exception as e:
        handle_startup_exception(e)

    register_start(_base.start, hs)

    # redirect stdio to the logs, if configured.
    if not hs.config.logging.no_redirect_stdio:
        redirect_stdio_to_logs()

    _base.start_worker_reactor("synapse-generic-worker", config)


def main() -> None:
    with LoggingContext("main"):
        start(sys.argv[1:])


if __name__ == "__main__":
    main()
