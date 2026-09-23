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
from unittest import TestCase
from unittest.mock import Mock, patch

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor
from twisted.web.server import Site

from synapse.app.generic_worker import GenericWorkerServer, GenericWorkerStore
from synapse.app.homeserver import SynapseHomeServer
from synapse.config.server import parse_listener_def
from synapse.server import HomeServer
from synapse.storage.databases.main.openid import OpenIdStore
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.server import make_request
from tests.unittest import HomeserverTestCase


class GenericWorkerStoreOpenIdMixinTests(TestCase):
    def test_generic_worker_store_includes_openid_store(self) -> None:
        """Workers must inherit OpenIdStore so userinfo lookups do not 500."""
        self.assertTrue(issubclass(GenericWorkerStore, OpenIdStore))
        self.assertTrue(hasattr(GenericWorkerStore, "get_user_id_for_open_id_token"))
        self.assertTrue(hasattr(GenericWorkerStore, "insert_open_id_token"))


class FederationReaderOpenIDListenerTests(HomeserverTestCase):
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
            (["federation"], "auth_fail"),
            ([], "no_resource"),
            (["openid", "federation"], "auth_fail"),
            (["openid"], "auth_fail"),
        ]
    )
    def test_openid_listener(self, names: list[str], expectation: str) -> None:
        """
        Test different openid listener configurations.

        401 is success here since it means we hit the handler and auth failed.
        """
        config = {
            "port": 8080,
            "type": "http",
            "bind_addresses": ["0.0.0.0"],
            "resources": [{"names": names}],
        }

        # Listen with the config
        hs = self.hs
        assert isinstance(hs, GenericWorkerServer)
        hs._listen_http(parse_listener_def(0, config))

        # Grab the resource from the site that was told to listen
        site = self.reactor.tcpServers[0][1]
        try:
            site.resource.children[b"_matrix"].children[b"federation"]
        except KeyError:
            if expectation == "no_resource":
                return
            raise

        channel = make_request(
            self.reactor, site, "GET", "/_matrix/federation/v1/openid/userinfo"
        )

        self.assertEqual(channel.code, 401)

    def _listen_openid(self) -> Site:
        config = {
            "port": 8080,
            "type": "http",
            "bind_addresses": ["0.0.0.0"],
            "resources": [{"names": ["openid"]}],
        }
        hs = self.hs
        assert isinstance(hs, GenericWorkerServer)
        hs._listen_http(parse_listener_def(0, config))
        site = self.reactor.tcpServers[0][1]
        assert isinstance(site, Site)
        return site

    def test_openid_userinfo_valid_token(self) -> None:
        """Workers can look up a valid OpenID token instead of crashing."""
        site = self._listen_openid()
        token = "valid_openid_token"
        user_id = "@alice:test"
        self.get_success(
            self.hs.get_datastores().main.insert_open_id_token(
                token, self.clock.time_msec() + 3600 * 1000, user_id
            )
        )

        channel = make_request(
            self.reactor,
            site,
            "GET",
            f"/_matrix/federation/v1/openid/userinfo?access_token={token}",
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, {"sub": user_id})

    def test_openid_userinfo_unknown_token(self) -> None:
        """Unknown tokens return 401 rather than raising AttributeError."""
        site = self._listen_openid()

        channel = make_request(
            self.reactor,
            site,
            "GET",
            "/_matrix/federation/v1/openid/userinfo?access_token=unknown",
        )

        self.assertEqual(channel.code, 401)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")

    def test_openid_userinfo_expired_token(self) -> None:
        """Expired tokens return 401 rather than raising AttributeError."""
        site = self._listen_openid()
        token = "expired_openid_token"
        self.get_success(
            self.hs.get_datastores().main.insert_open_id_token(
                token, self.clock.time_msec() - 1, "@alice:test"
            )
        )

        channel = make_request(
            self.reactor,
            site,
            "GET",
            f"/_matrix/federation/v1/openid/userinfo?access_token={token}",
        )

        self.assertEqual(channel.code, 401)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")


@patch("synapse.app.homeserver.KeyResource", new=Mock())
class SynapseHomeserverOpenIDListenerTests(HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver(homeserver_to_use=SynapseHomeServer)
        return hs

    @parameterized.expand(
        [
            (["federation"], "auth_fail"),
            ([], "no_resource"),
            (["openid", "federation"], "auth_fail"),
            (["openid"], "auth_fail"),
        ]
    )
    def test_openid_listener(self, names: list[str], expectation: str) -> None:
        """
        Test different openid listener configurations.

        401 is success here since it means we hit the handler and auth failed.
        """
        config = {
            "port": 8080,
            "type": "http",
            "bind_addresses": ["0.0.0.0"],
            "resources": [{"names": names}],
        }

        # Listen with the config
        hs = self.hs
        assert isinstance(hs, SynapseHomeServer)
        hs._listener_http(self.hs.config, parse_listener_def(0, config))

        # Grab the resource from the site that was told to listen
        site = self.reactor.tcpServers[0][1]
        try:
            site.resource.children[b"_matrix"].children[b"federation"]
        except KeyError:
            if expectation == "no_resource":
                return
            raise

        channel = make_request(
            self.reactor, site, "GET", "/_matrix/federation/v1/openid/userinfo"
        )

        self.assertEqual(channel.code, 401)
