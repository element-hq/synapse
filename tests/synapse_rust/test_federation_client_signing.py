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

import random
import string
from typing import Any

from canonicaljson import encode_canonical_json
from signedjson.key import SigningKey, generate_signing_key
from signedjson.sign import sign_json

from synapse.synapse_rust.federation_client import RequestSigner
from synapse.types import JsonDict

from tests import unittest

ORIGIN = "origin.example"

# Characters that JSON encoders disagree on, plus a spread of planes.
AWKWARD = '"\\/\b\f\n\r\t\x00\x1f\x7f\u00e9\u2028\u2029\ufeff\U0001f600'


def python_auth_header(
    signing_key: SigningKey,
    method: bytes,
    uri: bytes,
    destination: str | None,
    destination_is: str | None,
    content: JsonDict | None,
) -> bytes:
    """The header built with `signedjson`: the reference."""
    request: JsonDict = {
        "method": method.decode("ascii"),
        "uri": uri.decode("ascii"),
        "origin": ORIGIN,
    }
    if destination is not None:
        request["destination"] = destination
    if destination_is is not None:
        request["destination_is"] = destination_is
    if content is not None:
        request["content"] = content
    request = sign_json(request, ORIGIN, signing_key)
    ((key, sig),) = request["signatures"][ORIGIN].items()
    return (
        'X-Matrix origin="%s",key="%s",sig="%s",destination="%s"'
        % (ORIGIN, key, sig, request.get("destination") or request["destination_is"])
    ).encode("ascii")


def random_string(rng: random.Random) -> str:
    """Up to 11 characters, about a third from AWKWARD and the rest printable
    ASCII or from across the Basic Multilingual Plane."""
    return "".join(
        rng.choice(AWKWARD)
        if rng.random() < 0.3
        else chr(rng.choice([rng.randrange(0x20, 0x7F), rng.randrange(0xA0, 0xD800)]))
        for _ in range(rng.randrange(0, 12))
    )


def random_json(rng: random.Random, depth: int = 0) -> Any:
    """A JSON value of any type, nested up to three levels deep, with keys and
    strings from `random_string` and integers in the range canonical JSON
    allows."""
    kind = rng.randrange(8 if depth < 3 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randrange(-(2**53) + 1, 2**53)
    if kind in (3, 4):
        return random_string(rng)
    if kind == 5:
        return [random_json(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {
        random_string(rng): random_json(rng, depth + 1) for _ in range(rng.randrange(5))
    }


def random_uri(rng: random.Random) -> bytes:
    """A federation path and query, including characters that JSON escapes
    (`"` and `\\`) and that URIs reserve."""
    chars = string.ascii_letters + string.digits + "/%?&=._~-:@!$'()*+,;\"\\"
    return b"/_matrix/federation/v1/" + "".join(
        rng.choice(chars) for _ in range(rng.randrange(40))
    ).encode("ascii")


class RequestSignerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.signing_key = generate_signing_key("a_key")
        self.signer = RequestSigner(
            ORIGIN,
            f"{self.signing_key.alg}:{self.signing_key.version}",
            bytes(self.signing_key),
        )

    def assert_same_header(
        self,
        method: bytes,
        uri: bytes,
        destination: str | None,
        destination_is: str | None,
        content: JsonDict | None,
    ) -> None:
        expected = python_auth_header(
            self.signing_key, method, uri, destination, destination_is, content
        )
        content_bytes = None if content is None else encode_canonical_json(content)
        actual = self.signer.authorization(
            method, uri, destination, destination_is, content_bytes
        )
        self.assertEqual(actual, expected)

    def test_matches_python_on_random_requests(self) -> None:
        """Random bodies, paths and destination forms, so the comparison covers
        the encodings an author would not think to list by hand."""
        # Fixed, so that every run checks the same cases and a failure
        # reproduces.
        rng = random.Random(2026)
        destinations = [
            ("remote.example:8448", None),
            ("[::1]:8448", None),
            (None, "id.example"),
        ]
        for i in range(500):
            destination, destination_is = rng.choice(destinations)
            content = (
                None
                if rng.random() < 0.2
                else {random_string(rng): random_json(rng) for _ in range(4)}
            )
            with self.subTest(case=i):
                self.assert_same_header(
                    rng.choice([b"GET", b"PUT", b"POST", b"DELETE"]),
                    random_uri(rng),
                    destination,
                    destination_is,
                    content,
                )

    def test_not_exactly_one_destination(self) -> None:
        for destination, destination_is in [
            (None, None),
            ("", None),
            (None, ""),
            ("remote.example", "id.example"),
            ("", "id.example"),
        ]:
            with self.assertRaises(ValueError):
                self.signer.authorization(
                    b"GET", b"/x", destination, destination_is, None
                )

    def test_non_ascii_method_or_uri(self) -> None:
        with self.assertRaises(ValueError):
            self.signer.authorization(b"G\xc3\xa9T", b"/x", "remote", None, None)
        with self.assertRaises(ValueError):
            self.signer.authorization(b"GET", b"/\xc3\xa9", "remote", None, None)
