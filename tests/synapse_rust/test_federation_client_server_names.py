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

from synapse.http.federation.matrix_federation_agent import _is_ip_literal
from synapse.http.matrixfederationclient import _PORT
from synapse.synapse_rust.federation_client import _parse_server_name
from synapse.util.stringutils import parse_and_validate_server_name

from tests import unittest

SERVER_NAMES = [
    # https://spec.matrix.org/latest/appendices/#server-name
    "matrix.org",
    "matrix.org:8888",
    "1.2.3.4",
    "1.2.3.4:1234",
    "[1234:5678::abcd]",
    "[1234:5678::abcd]:5678",
    # Hostnames: case, hyphens, empty labels, trailing dots, IDNA.
    "EXAMPLE.com",
    "-x.com",
    "x-.com",
    "ex_ample.com",
    "a..b",
    ".a",
    "example.com.",
    "xn--bcher-kva.example",
    "b\u00fccher.example",
    "",
    # Ports.
    "h:",
    "h:0",
    "h:08448",
    "h:65535",
    "h:65536",
    "h:99999",
    "h:008448",
    "h: 8448",
    "h:+8448",
    "h:8_448",
    "h:-1",
    "h:8448\n",
    "h:8448\x0b",
    "h:\u0668\u0664\u0664\u0668",
    "h:1:2",
    # IPv4 lookalikes.
    "01.2.3.4",
    "1.2.3",
    "1.2.3.4.5",
    "256.1.1.1",
    "2130706433",
    "0x7f000001",
    "127.1",
    # IPv6.
    "[::]",
    "[::1]",
    "[::1]:8448",
    "[::ffff:1.2.3.4]",
    "[::ffff:01.2.3.4]",
    "[1:2:3:4:5:6:7:8]",
    "[1:2:3:4:5:6:7:8:9]",
    "[1::2::3]",
    "[ABCD::EF01]",
    "[fe80::1%eth0]",
    "[1.2.3.4]",
    "[]",
    "[::1",
    "::1",
    "[::1]x",
    "[[::1]]",
]


def python_parse(name: str) -> tuple[str, int | None, str] | None:
    """What the federation client accepts: Synapse's validation plus the port
    grammar `_send_request` enforces."""
    try:
        host, port = parse_and_validate_server_name(name)
    except ValueError:
        return None
    if port is not None and (
        not _PORT.fullmatch(name.rsplit(":", 1)[1]) or port > 65535
    ):
        return None
    if host.startswith("["):
        kind = "ipv6"
    elif _is_ip_literal(host.encode("ascii")):
        kind = "ipv4"
    else:
        kind = "dns"
    return host, port, kind


def rust_parse(name: str) -> tuple[str, int | None, str] | None:
    try:
        return _parse_server_name(name)
    except ValueError:
        return None


class ServerNameTestCase(unittest.TestCase):
    def test_corpus_matches_python(self) -> None:
        for name in SERVER_NAMES:
            with self.subTest(name=name):
                self.assertEqual(rust_parse(name), python_parse(name))

    def test_random_names_match_python(self) -> None:
        rng = random.Random(2026)
        alphabet = string.ascii_letters + string.digits + ".-:[]_+ %\u00e9\n"
        for _ in range(5000):
            name = "".join(rng.choice(alphabet) for _ in range(rng.randrange(12)))
            with self.subTest(name=name):
                self.assertEqual(rust_parse(name), python_parse(name))
