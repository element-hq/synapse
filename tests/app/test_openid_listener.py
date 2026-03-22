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
from unittest.mock import Mock, patch

from parameterized import parameterized

from typing import Any as MemoryReactor  # was: MemoryReactor from Twisted

from synapse.app.generic_worker import GenericWorkerServer
from synapse.app.homeserver import SynapseHomeServer
from synapse.config.server import parse_listener_def
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.server import make_request
from tests.unittest import HomeserverTestCase


class FederationReaderOpenIDListenerTests(HomeserverTestCase):
    """Test that the openid listener is correctly configured on workers.

    With the aiohttp migration, we can no longer introspect Twisted's reactor
    for the listening site. Instead, we test the resource tree construction
    directly by checking that the appropriate resources are registered.
    """

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver(homeserver_to_use=GenericWorkerServer)
        return hs

    def default_config(self) -> JsonDict:
        conf = super().default_config()
        # we're using GenericWorkerServer, which uses a GenericWorkerStore, so we
        # have to tell the FederationHandler not to try to access stuff that is only
        # in the primary store.
        conf["worker_app"] = "yes"
        conf["instance_map"] = {"main": {"host": "127.0.0.1", "port": 0}}

        return conf

    @parameterized.expand(
        [
            (["federation"], True),
            ([], False),
            (["openid", "federation"], True),
            (["openid"], True),
        ]
    )
    def test_openid_listener(self, names: list[str], expect_federation: bool) -> None:
        """
        Test that the federation resource (which includes openid) is created
        when the appropriate listener names are configured.
        """
        from synapse.http.server import JsonResource, OptionsResource
        from synapse.util.httpresourcetree import create_resource_tree
        from synapse.api.urls import FEDERATION_PREFIX

        config = {
            "port": 8080,
            "type": "http",
            "bind_addresses": ["0.0.0.0"],
            "resources": [{"names": names}],
        }
        listener_config = parse_listener_def(0, config)
        assert listener_config.http_options is not None

        # Build the resource dict the same way GenericWorkerServer._listen_http does
        hs = self.hs
        assert isinstance(hs, GenericWorkerServer)

        from synapse.rest.health import HealthResource
        from synapse.federation.transport.server import TransportLayerServer

        resources: dict[str, Any] = {
            "/health": HealthResource(),
            "/_synapse/admin": JsonResource(hs, canonical_json=False),
        }

        for res in listener_config.http_options.resources:
            for name in res.names:
                if name == "federation":
                    resources[FEDERATION_PREFIX] = TransportLayerServer(hs)
                if name == "openid" and "federation" not in res.names:
                    resources[FEDERATION_PREFIX] = TransportLayerServer(
                        hs, servlet_groups=["openid"]
                    )

        root_resource = create_resource_tree(resources, OptionsResource())

        if expect_federation:
            # Check the federation resource exists in the tree
            self.assertIn(b"_matrix", root_resource.listNames())
        else:
            # No federation resource should be present
            if b"_matrix" in root_resource.listNames():
                matrix_child = root_resource.getStaticEntity(b"_matrix")
                self.assertNotIn(b"federation", matrix_child.listNames())


@patch("synapse.app.homeserver.KeyResource", new=Mock())
class SynapseHomeserverOpenIDListenerTests(HomeserverTestCase):
    """Test that the openid listener is correctly configured on the homeserver.

    With the aiohttp migration, we test resource tree construction directly.
    """

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver(homeserver_to_use=SynapseHomeServer)
        return hs

    @parameterized.expand(
        [
            (["federation"], True),
            ([], False),
            (["openid", "federation"], True),
            (["openid"], True),
        ]
    )
    def test_openid_listener(self, names: list[str], expect_federation: bool) -> None:
        """
        Test that the federation resource (which includes openid) is created
        when the appropriate listener names are configured.
        """
        from synapse.http.server import OptionsResource
        from synapse.util.httpresourcetree import create_resource_tree
        from synapse.rest.health import HealthResource

        config = {
            "port": 8080,
            "type": "http",
            "bind_addresses": ["0.0.0.0"],
            "resources": [{"names": names}],
        }

        hs = self.hs
        assert isinstance(hs, SynapseHomeServer)
        listener_config = parse_listener_def(0, config)
        assert listener_config.http_options is not None

        # Build resources the same way _listener_http does
        resources: dict[str, Any] = {"/health": HealthResource()}

        for res in listener_config.http_options.resources:
            for name in res.names:
                if name == "openid" and "federation" in res.names:
                    continue
                if name == "health":
                    continue
                resources.update(hs._configure_named_resource(name, res.compress))

        root_resource = create_resource_tree(
            resources, OptionsResource()
        )

        if expect_federation:
            self.assertIn(b"_matrix", root_resource.listNames())
        else:
            if b"_matrix" in root_resource.listNames():
                matrix_child = root_resource.getStaticEntity(b"_matrix")
                self.assertNotIn(b"federation", matrix_child.listNames())
