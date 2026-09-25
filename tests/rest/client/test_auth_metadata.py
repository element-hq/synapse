#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C
# Copyright (C) 2023-2025 New Vector, Ltd
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
from http import HTTPStatus
from typing import ClassVar
from unittest.mock import Mock

from twisted.internet.testing import MemoryReactor

from synapse.api.auth.mas import MasDelegatedAuth
from synapse.rest.client import auth_metadata
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase


class AuthMetadataMasDisabledTestCase(HomeserverTestCase):
    endpoint: ClassVar[str]
    servlets = [
        auth_metadata.register_servlets,
    ]

    def test_returns_404_when_mas_disabled(self) -> None:
        # Make an unauthenticated request for the discovery info.
        channel = self.make_request("GET", "/_matrix/client/v1/auth_metadata")
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)


class AuthMetadataMasEnabledTestCase(HomeserverTestCase):
    servlets = [
        auth_metadata.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Registration must be disabled for the MAS integration to be enabled.
        config["enable_registration"] = False
        config["matrix_authentication_service"] = {
            "enabled": True,
            "endpoint": "https://auth.example.com/",
            "secret": "verysecret",
        }
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # Install a mock `MasDelegatedAuth` implementation.
        self.auth = Mock(spec=MasDelegatedAuth)
        return self.setup_test_homeserver(auth=self.auth)

    def test_returns_metadata_from_mas(self) -> None:
        test_auth_metadata = {
            "issuer": "https://auth.example.com/",
        }

        self.auth.auth_metadata.return_value = test_auth_metadata

        channel = self.make_request("GET", "/_matrix/client/v1/auth_metadata")

        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        self.assertEqual(channel.json_body, test_auth_metadata)
