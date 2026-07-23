#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from unittest.mock import AsyncMock

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import login, thirdparty
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest

REMOTE_USER_RESULT = {
    "userid": "@_xmpp_someone:remote.example.com",
    "protocol": "xmpp",
    "fields": {"username": "someone", "domain": "xmpp.example.com"},
}

REMOTE_LOCATION_RESULT = {
    "alias": "#_xmpp_room_xmpp.example.com:remote.example.com",
    "protocol": "xmpp",
    "fields": {"muc": "room", "domain": "xmpp.example.com"},
}

LOCAL_USER_RESULT = {
    "userid": "@_local_bridge_user:test",
    "protocol": "xmpp",
    "fields": {"username": "someone", "domain": "local.example.com"},
}


class ThirdPartyFederatedLookupTests(unittest.HomeserverTestCase):
    """Tests for ?server= on the client /thirdparty endpoints."""

    servlets = [
        thirdparty.register_servlets,
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4517_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.appservice_handler = hs.get_application_service_handler()
        self.appservice_handler.query_3pe = AsyncMock(  # type: ignore[method-assign]
            return_value=[LOCAL_USER_RESULT]
        )
        self.appservice_handler.get_3pe_protocols = AsyncMock(  # type: ignore[method-assign]
            return_value={"xmpp": {"instances": [{"desc": "local xmpp"}]}}
        )

        self.transport = hs.get_federation_client().transport_layer
        self.transport.get_thirdparty_entities = AsyncMock(  # type: ignore[method-assign]
            return_value={"results": [REMOTE_USER_RESULT]}
        )
        self.transport.get_thirdparty_protocols = AsyncMock(  # type: ignore[method-assign]
            return_value={"xmpp": {"instances": [{"desc": "remote xmpp"}]}}
        )

        self.user = self.register_user("user", "pass")
        self.token = self.login(self.user, "pass")

    def test_local_lookup_unchanged(self) -> None:
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/thirdparty/user/xmpp?username=someone",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_list, [LOCAL_USER_RESULT])
        self.transport.get_thirdparty_entities.assert_not_called()

    def test_remote_user_lookup(self) -> None:
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/thirdparty/user/xmpp"
            "?username=someone&server=remote.example.com",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_list, [REMOTE_USER_RESULT])
        self.appservice_handler.query_3pe.assert_not_called()

        call = self.transport.get_thirdparty_entities.call_args
        self.assertEqual(call.args[0], "remote.example.com")
        self.assertEqual(call.args[1], "user")
        self.assertEqual(call.args[2], "xmpp")
        # The server routing param must not leak into the bridge query fields.
        self.assertEqual(call.args[3], {"username": ["someone"]})

    def test_remote_location_lookup(self) -> None:
        self.transport.get_thirdparty_entities.return_value = {
            "results": [REMOTE_LOCATION_RESULT]
        }
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/thirdparty/location/xmpp"
            "?muc=room&server=remote.example.com",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_list, [REMOTE_LOCATION_RESULT])
        self.assertEqual(
            self.transport.get_thirdparty_entities.call_args.args[1], "location"
        )

    def test_remote_protocols(self) -> None:
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/thirdparty/protocols?server=remote.example.com",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(
            channel.json_body, {"xmpp": {"instances": [{"desc": "remote xmpp"}]}}
        )
        self.appservice_handler.get_3pe_protocols.assert_not_called()

    def test_own_server_name_is_local(self) -> None:
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/thirdparty/user/xmpp?username=someone&server=test",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_list, [LOCAL_USER_RESULT])
        self.transport.get_thirdparty_entities.assert_not_called()
        # The stripped server param must not reach the local bridges either.
        fields = self.appservice_handler.query_3pe.call_args.args[2]
        self.assertNotIn(b"server", fields)

    def test_remote_failure_returns_empty(self) -> None:
        self.transport.get_thirdparty_entities.side_effect = RuntimeError("boom")
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/thirdparty/user/xmpp"
            "?username=someone&server=remote.example.com",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_list, [])


class ThirdPartyFederatedLookupDisabledTests(unittest.HomeserverTestCase):
    """With the flag off, ?server= is ignored and lookups stay local."""

    servlets = [
        thirdparty.register_servlets,
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.appservice_handler = hs.get_application_service_handler()
        self.appservice_handler.query_3pe = AsyncMock(  # type: ignore[method-assign]
            return_value=[LOCAL_USER_RESULT]
        )
        self.transport = hs.get_federation_client().transport_layer
        self.transport.get_thirdparty_entities = AsyncMock(  # type: ignore[method-assign]
            return_value={"results": [REMOTE_USER_RESULT]}
        )

        self.user = self.register_user("user", "pass")
        self.token = self.login(self.user, "pass")

    def test_server_param_ignored(self) -> None:
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/thirdparty/user/xmpp"
            "?username=someone&server=remote.example.com",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_list, [LOCAL_USER_RESULT])
        self.transport.get_thirdparty_entities.assert_not_called()
        # The server param is still stripped from the bridge query fields.
        fields = self.appservice_handler.query_3pe.call_args.args[2]
        self.assertNotIn(b"server", fields)
