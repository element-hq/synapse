#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from signedjson.key import get_verify_key
from signedjson.sign import verify_signed_json

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.synapse_rust.federation import build_auth_header_for_test
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest


class FederationSigningTestCase(unittest.HomeserverTestCase):
    """The Rust `X-Matrix` header is byte-identical to Python's."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.client = hs.get_federation_http_client()

    def _check(
        self,
        method: str,
        uri: str,
        destination: str | None,
        destination_is: str | None = None,
        content: JsonDict | None = None,
    ) -> None:
        (python_header,) = self.client.build_auth_headers(
            destination.encode() if destination is not None else None,
            method.encode(),
            uri.encode(),
            content,
            destination_is.encode() if destination_is is not None else None,
        )

        rust_header = build_auth_header_for_test(
            self.hs.signing_key,
            self.hs.hostname,
            method,
            uri,
            destination,
            destination_is,
            content,
        )

        self.assertEqual(rust_header, python_header.decode("ascii"))

    def test_no_content(self) -> None:
        self._check("GET", "/_matrix/federation/v1/version", "remote.example")

    def test_content(self) -> None:
        self._check(
            "PUT",
            "/_matrix/federation/v1/send/123?x=%2Fy",
            "remote.example:8448",
            content={"pdus": [{"z": 1, "a": "é", "n": None}], "edus": []},
        )

    def test_empty_content(self) -> None:
        # An empty body is still signed, unlike a missing one.
        self._check("POST", "/path", "remote.example", content={})

    def test_identity_server(self) -> None:
        self._check("POST", "/_matrix/identity/v2/x", None, destination_is="is.example")

    def test_signature_verifies(self) -> None:
        content = {"key": "value"}
        header = build_auth_header_for_test(
            self.hs.signing_key,
            self.hs.hostname,
            "PUT",
            "/p",
            "remote.example",
            None,
            content,
        )
        params = dict(
            part.split("=", 1) for part in header.removeprefix("X-Matrix ").split(",")
        )
        params = {k: v.strip('"') for k, v in params.items()}

        signed = {
            "method": "PUT",
            "uri": "/p",
            "origin": self.hs.hostname,
            "destination": "remote.example",
            "content": content,
            "signatures": {self.hs.hostname: {params["key"]: params["sig"]}},
        }
        verify_key = get_verify_key(self.hs.signing_key)
        verify_signed_json(signed, self.hs.hostname, verify_key)
