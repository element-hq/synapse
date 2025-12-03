#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from typing import Any, Mapping, Sequence
from unittest.mock import Mock

from twisted.internet.testing import MemoryReactor

from synapse.appservice import ApplicationService
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import override_config

PROTOCOL = "myproto"
TOKEN = "myastoken"
URL = "http://mytestservice"


class ApplicationServiceApiTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.api = hs.get_application_service_api()
        self.service = ApplicationService(
            id="unique_identifier",
            sender=UserID.from_string("@as:test"),
            url=URL,
            token="unused",
            hs_token=TOKEN,
        )

    def test_query_3pe_authenticates_token_via_header(self) -> None:
        """
        Tests that 3pe queries to the appservice are authenticated
        with the appservice's token.
        """

        SUCCESS_RESULT_USER = [
            {
                "protocol": PROTOCOL,
                "userid": "@a:user",
                "fields": {
                    "more": "fields",
                },
            }
        ]
        SUCCESS_RESULT_LOCATION = [
            {
                "protocol": PROTOCOL,
                "alias": "#a:room",
                "fields": {
                    "more": "fields",
                },
            }
        ]

        URL_USER = f"{URL}/_matrix/app/v1/thirdparty/user/{PROTOCOL}"
        URL_LOCATION = f"{URL}/_matrix/app/v1/thirdparty/location/{PROTOCOL}"

        self.request_url = None

        async def get_json(
            url: str,
            args: Mapping[Any, Any],
            headers: Mapping[str | bytes, Sequence[str | bytes]],
        ) -> list[JsonDict]:
            # Ensure the access token is passed as a header.
            if not headers or not headers.get(b"Authorization"):
                raise RuntimeError("Access token not provided")
            # ... and not as a query param
            if b"access_token" in args:
                raise RuntimeError(
                    "Access token should not be passed as a query param."
                )

            self.assertEqual(
                headers.get(b"Authorization"), [f"Bearer {TOKEN}".encode()]
            )
            self.request_url = url
            if url == URL_USER:
                return SUCCESS_RESULT_USER
            elif url == URL_LOCATION:
                return SUCCESS_RESULT_LOCATION
            else:
                raise RuntimeError(
                    "URL provided was invalid. This should never be seen."
                )

        # We assign to a method, which mypy doesn't like.
        self.api.get_json = Mock(side_effect=get_json)  # type: ignore[method-assign]

        result = self.get_success(
            self.api.query_3pe(self.service, "user", PROTOCOL, {b"some": [b"field"]})
        )
        self.assertEqual(self.request_url, URL_USER)
        self.assertEqual(result, SUCCESS_RESULT_USER)
        result = self.get_success(
            self.api.query_3pe(
                self.service, "location", PROTOCOL, {b"some": [b"field"]}
            )
        )
        self.assertEqual(self.request_url, URL_LOCATION)
        self.assertEqual(result, SUCCESS_RESULT_LOCATION)

    @override_config({"use_appservice_legacy_authorization": True})
    def test_query_3pe_authenticates_token_via_param(self) -> None:
        """
        Tests that 3pe queries to the appservice are authenticated
        with the appservice's token.
        """

        SUCCESS_RESULT_USER = [
            {
                "protocol": PROTOCOL,
                "userid": "@a:user",
                "fields": {
                    "more": "fields",
                },
            }
        ]
        SUCCESS_RESULT_LOCATION = [
            {
                "protocol": PROTOCOL,
                "alias": "#a:room",
                "fields": {
                    "more": "fields",
                },
            }
        ]

        URL_USER = f"{URL}/_matrix/app/v1/thirdparty/user/{PROTOCOL}"
        URL_LOCATION = f"{URL}/_matrix/app/v1/thirdparty/location/{PROTOCOL}"

        self.request_url = None

        async def get_json(
            url: str,
            args: Mapping[Any, Any],
            headers: Mapping[str | bytes, Sequence[str | bytes]] | None = None,
        ) -> list[JsonDict]:
            # Ensure the access token is passed as a both a query param and in the headers.
            if not args.get(b"access_token"):
                raise RuntimeError("Access token should be provided in query params.")
            if not headers or not headers.get(b"Authorization"):
                raise RuntimeError("Access token should be provided in auth headers.")

            self.assertEqual(args.get(b"access_token"), TOKEN)
            self.assertEqual(
                headers.get(b"Authorization"), [f"Bearer {TOKEN}".encode()]
            )
            self.request_url = url
            if url == URL_USER:
                return SUCCESS_RESULT_USER
            elif url == URL_LOCATION:
                return SUCCESS_RESULT_LOCATION
            else:
                raise RuntimeError(
                    "URL provided was invalid. This should never be seen."
                )

        # We assign to a method, which mypy doesn't like.
        self.api.get_json = Mock(side_effect=get_json)  # type: ignore[method-assign]

        result = self.get_success(
            self.api.query_3pe(self.service, "user", PROTOCOL, {b"some": [b"field"]})
        )
        self.assertEqual(self.request_url, URL_USER)
        self.assertEqual(result, SUCCESS_RESULT_USER)
        result = self.get_success(
            self.api.query_3pe(
                self.service, "location", PROTOCOL, {b"some": [b"field"]}
            )
        )
        self.assertEqual(self.request_url, URL_LOCATION)
        self.assertEqual(result, SUCCESS_RESULT_LOCATION)

    def test_claim_keys(self) -> None:
        """
        Tests that the /keys/claim response is properly parsed for missing
        keys.
        """

        RESPONSE: JsonDict = {
            "@alice:example.org": {
                "DEVICE_1": {
                    "signed_curve25519:AAAAHg": {
                        # We don't really care about the content of the keys,
                        # they get passed back transparently.
                    },
                    "signed_curve25519:BBBBHg": {},
                },
                "DEVICE_2": {"signed_curve25519:CCCCHg": {}},
            },
        }

        async def post_json_get_json(
            uri: str,
            post_json: Any,
            headers: Mapping[str | bytes, Sequence[str | bytes]],
        ) -> JsonDict:
            # Ensure the access token is passed as both a header and query arg.
            if not headers.get(b"Authorization"):
                raise RuntimeError("Access token not provided")

            self.assertEqual(
                headers.get(b"Authorization"), [f"Bearer {TOKEN}".encode()]
            )
            return RESPONSE

        # We assign to a method, which mypy doesn't like.
        self.api.post_json_get_json = Mock(side_effect=post_json_get_json)  # type: ignore[method-assign]

        MISSING_KEYS = [
            # Known user, known device, missing algorithm.
            ("@alice:example.org", "DEVICE_2", "xyz", 1),
            # Known user, missing device.
            ("@alice:example.org", "DEVICE_3", "signed_curve25519", 1),
            # Unknown user.
            ("@bob:example.org", "DEVICE_4", "signed_curve25519", 1),
        ]

        claimed_keys, missing = self.get_success(
            self.api.claim_client_keys(
                self.service,
                [
                    # Found devices
                    ("@alice:example.org", "DEVICE_1", "signed_curve25519", 1),
                    ("@alice:example.org", "DEVICE_2", "signed_curve25519", 1),
                ]
                + MISSING_KEYS,
            )
        )

        self.assertEqual(claimed_keys, RESPONSE)
        self.assertEqual(missing, MISSING_KEYS)
