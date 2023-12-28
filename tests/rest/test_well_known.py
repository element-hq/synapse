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
from twisted.web.resource import Resource

from synapse.rest.well_known import well_known_resource

from tests import unittest
from tests.utils import HAS_AUTHLIB


class WellKnownTests(unittest.HomeserverTestCase):
    def create_test_resource(self) -> Resource:
        # replace the JsonResource with a Resource wrapping the WellKnownResource
        res = Resource()
        res.putChild(b".well-known", well_known_resource(self.hs))
        return res

    @unittest.override_config(
        {
            "public_baseurl": "https://tesths",
            "default_identity_server": "https://testis",
        }
    )
    def test_client_well_known(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/client", shorthand=False
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {
                "m.homeserver": {"base_url": "https://tesths/"},
                "m.identity_server": {"base_url": "https://testis"},
            },
        )

    @unittest.override_config(
        {
            "public_baseurl": None,
        }
    )
    def test_client_well_known_no_public_baseurl(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/client", shorthand=False
        )

        self.assertEqual(channel.code, 404)

    @unittest.override_config(
        {
            "public_baseurl": "https://tesths",
            "default_identity_server": "https://testis",
            "extra_well_known_client_content": {"custom": False},
        }
    )
    def test_client_well_known_custom(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/client", shorthand=False
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {
                "m.homeserver": {"base_url": "https://tesths/"},
                "m.identity_server": {"base_url": "https://testis"},
                "custom": False,
            },
        )

    @unittest.override_config({"serve_server_wellknown": True})
    def test_server_well_known(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/server", shorthand=False
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {"m.server": "test:443"},
        )

    def test_server_well_known_disabled(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/server", shorthand=False
        )
        self.assertEqual(channel.code, 404)

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @unittest.override_config(
        {
            "public_baseurl": "https://homeserver",  # this is only required so that client well known is served
            "experimental_features": {
                "msc3861": {
                    "enabled": True,
                    "issuer": "https://issuer",
                    "account_management_url": "https://my-account.issuer",
                    "client_id": "id",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "secret",
                },
            },
            "disable_registration": True,
        }
    )
    def test_client_well_known_msc3861_oauth_delegation(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/client", shorthand=False
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {
                "m.homeserver": {"base_url": "https://homeserver/"},
                "org.matrix.msc2965.authentication": {
                    "issuer": "https://issuer",
                    "account": "https://my-account.issuer",
                },
            },
        )
