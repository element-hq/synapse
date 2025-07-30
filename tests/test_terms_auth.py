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

from unittest.mock import Mock

from twisted.internet.interfaces import IReactorTime
from twisted.internet.testing import MemoryReactor, MemoryReactorClock

from synapse.rest.client.register import register_servlets
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest


class TermsTestCase(unittest.HomeserverTestCase):
    servlets = [register_servlets]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.update(
            {
                "public_baseurl": "https://example.org/",
                "user_consent": {
                    "version": "1.0",
                    "policy_name": "My Cool Privacy Policy",
                    "template_dir": "/",
                    "require_at_registration": True,
                },
            }
        )
        return config

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # type-ignore: mypy-zope doesn't seem to recognise that MemoryReactorClock
        # implements IReactorTime, via inheritance from twisted.internet.testing.Clock
        self.clock: IReactorTime = MemoryReactorClock()  # type: ignore[assignment]
        self.hs_clock = Clock(self.clock)
        self.url = "/_matrix/client/r0/register"
        self.registration_handler = Mock()
        self.auth_handler = Mock()
        self.device_handler = Mock()

    def test_ui_auth(self) -> None:
        # Do a UI auth request
        request_data: JsonDict = {"username": "kermit", "password": "monkey"}
        channel = self.make_request(b"POST", self.url, request_data)

        self.assertEqual(channel.code, 401, channel.result)

        self.assertTrue(channel.json_body is not None)
        self.assertIsInstance(channel.json_body["session"], str)

        self.assertIsInstance(channel.json_body["flows"], list)
        for flow in channel.json_body["flows"]:
            self.assertIsInstance(flow["stages"], list)
            self.assertTrue(len(flow["stages"]) > 0)
            self.assertTrue("m.login.terms" in flow["stages"])

        expected_params = {
            "m.login.terms": {
                "policies": {
                    "privacy_policy": {
                        "en": {
                            "name": "My Cool Privacy Policy",
                            "url": "https://example.org/_matrix/consent?v=1.0",
                        },
                        "version": "1.0",
                    }
                }
            }
        }
        self.assertIsInstance(channel.json_body["params"], dict)
        self.assertLessEqual(
            channel.json_body["params"].items(), expected_params.items()
        )

        # We have to complete the dummy auth stage before completing the terms stage
        request_data = {
            "username": "kermit",
            "password": "monkey",
            "auth": {
                "session": channel.json_body["session"],
                "type": "m.login.dummy",
            },
        }

        self.registration_handler.check_username = Mock(return_value=True)

        channel = self.make_request(b"POST", self.url, request_data)

        # We don't bother checking that the response is correct - we'll leave that to
        # other tests. We just want to make sure we're on the right path.
        self.assertEqual(channel.code, 401, channel.result)

        # Finish the UI auth for terms
        request_data = {
            "username": "kermit",
            "password": "monkey",
            "auth": {
                "session": channel.json_body["session"],
                "type": "m.login.terms",
            },
        }
        channel = self.make_request(b"POST", self.url, request_data)

        # We're interested in getting a response that looks like a successful
        # registration, not so much that the details are exactly what we want.

        self.assertEqual(channel.code, 200, channel.result)

        self.assertTrue(channel.json_body is not None)
        self.assertIsInstance(channel.json_body["user_id"], str)
        self.assertIsInstance(channel.json_body["access_token"], str)
        self.assertIsInstance(channel.json_body["device_id"], str)
