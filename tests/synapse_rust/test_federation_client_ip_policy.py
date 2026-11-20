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

from netaddr import IPAddress

from synapse.config.server import DEFAULT_IP_RANGE_BLOCKLIST, generate_ip_set
from synapse.synapse_rust.federation_client import _ip_blocked

from tests import unittest


class IpBlocklistTestCase(unittest.TestCase):
    def test_matches_netaddr(self) -> None:
        """Synapse's default blocklist, as the config builds it, against
        netaddr's membership test."""
        blocklist = generate_ip_set(DEFAULT_IP_RANGE_BLOCKLIST, ["0.0.0.0", "::"])
        cidrs = [str(cidr) for cidr in blocklist.iter_cidrs()]
        rng = random.Random(2026)
        addresses = [
            "0.0.0.0",
            "::",
            "127.0.0.1",
            "::ffff:127.0.0.1",
            "::127.0.0.1",
            "2002:7f00::1",
            "8.8.8.8",
            "::ffff:8.8.8.8",
            "2001:4860::8888",
            "fe80::1",
            "100.64.0.1",
            "169.254.1.1",
        ]
        addresses += [str(IPAddress(rng.getrandbits(32), 4)) for _ in range(2000)]
        addresses += [
            str(IPAddress((0xFFFF << 32) | rng.getrandbits(32), 6)) for _ in range(500)
        ]
        addresses += [str(IPAddress(rng.getrandbits(128), 6)) for _ in range(500)]
        for ip in addresses:
            with self.subTest(ip=ip):
                self.assertEqual(_ip_blocked(cidrs, ip), IPAddress(ip) in blocklist)
