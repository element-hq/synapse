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

from parameterized import parameterized_class

from synapse.rest.client import auth_metadata

from tests.unittest import HomeserverTestCase


class AuthIssuerTestCase(HomeserverTestCase):
    servlets = [
        auth_metadata.register_servlets,
    ]

    def test_returns_404_when_mas_disabled(self) -> None:
        # Make an unauthenticated request for the discovery info.
        channel = self.make_request(
            "GET",
            "/_matrix/client/unstable/org.matrix.msc2965/auth_issuer",
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)


@parameterized_class(
    ("endpoint",),
    [
        ("/_matrix/client/unstable/org.matrix.msc2965/auth_metadata",),
        ("/_matrix/client/v1/auth_metadata",),
    ],
)
class AuthMetadataTestCase(HomeserverTestCase):
    endpoint: ClassVar[str]
    servlets = [
        auth_metadata.register_servlets,
    ]

    def test_returns_404_when_mas_disabled(self) -> None:
        # Make an unauthenticated request for the discovery info.
        channel = self.make_request("GET", self.endpoint)
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)
