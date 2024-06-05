#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import Dict

from twisted.web.resource import Resource

from synapse.rest import admin
from synapse.rest.client import login
from synapse.rest.synapse.client import build_synapse_client_resource_tree

from tests import unittest


class FederationWhitelistTests(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
    ]

    def create_resource_dict(self) -> Dict[str, Resource]:
        base = super().create_resource_dict()
        base.update(build_synapse_client_resource_tree(self.hs))
        return base

    def test_default(self) -> None:
        "If the config option is not enabled, the endpoint should 404"
        channel = self.make_request(
            "GET", "/_synapse/client/v1/config/federation_whitelist", shorthand=False
        )

        self.assertEqual(channel.code, 404)

    @unittest.override_config({"federation_whitelist_endpoint_enabled": True})
    def test_no_auth(self) -> None:
        "Endpoint requires auth when enabled"

        channel = self.make_request(
            "GET", "/_synapse/client/v1/config/federation_whitelist", shorthand=False
        )

        self.assertEqual(channel.code, 401)

    @unittest.override_config({"federation_whitelist_endpoint_enabled": True})
    def test_no_whitelist(self) -> None:
        "Test when there is no whitelist configured"

        self.register_user("user", "password")
        tok = self.login("user", "password")

        channel = self.make_request(
            "GET",
            "/_synapse/client/v1/config/federation_whitelist",
            shorthand=False,
            access_token=tok,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"whitelist_enabled": False, "whitelist": []}
        )

    @unittest.override_config(
        {
            "federation_whitelist_endpoint_enabled": True,
            "federation_domain_whitelist": ["example.com"],
        }
    )
    def test_whitelist(self) -> None:
        "Test when there is a whitelist configured"

        self.register_user("user", "password")
        tok = self.login("user", "password")

        channel = self.make_request(
            "GET",
            "/_synapse/client/v1/config/federation_whitelist",
            shorthand=False,
            access_token=tok,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"whitelist_enabled": True, "whitelist": ["example.com"]}
        )

    @unittest.override_config(
        {
            "federation_whitelist_endpoint_enabled": True,
            "federation_domain_whitelist": ["example.com", "example.com"],
        }
    )
    def test_whitelist_no_duplicates(self) -> None:
        "Test when there is a whitelist configured with duplicates, no duplicates are returned"

        self.register_user("user", "password")
        tok = self.login("user", "password")

        channel = self.make_request(
            "GET",
            "/_synapse/client/v1/config/federation_whitelist",
            shorthand=False,
            access_token=tok,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"whitelist_enabled": True, "whitelist": ["example.com"]}
        )
