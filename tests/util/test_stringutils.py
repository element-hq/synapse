#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from synapse.api.errors import SynapseError
from synapse.util.stringutils import assert_valid_client_secret, base62_encode

from .. import unittest


class StringUtilsTestCase(unittest.TestCase):
    def test_client_secret_regex(self) -> None:
        """Ensure that client_secret does not contain illegal characters"""
        good = [
            "abcde12345",
            "ABCabc123",
            "_--something==_",
            "...--==-18913",
            "8Dj2odd-e9asd.cd==_--ddas-secret-",
        ]

        bad = [
            "--+-/secret",
            "\\dx--dsa288",
            "",
            "AAS//",
            "asdj**",
            ">X><Z<!!-)))",
            "a@b.com",
        ]

        for client_secret in good:
            assert_valid_client_secret(client_secret)

        for client_secret in bad:
            with self.assertRaises(SynapseError):
                assert_valid_client_secret(client_secret)

    def test_base62_encode(self) -> None:
        self.assertEqual("0", base62_encode(0))
        self.assertEqual("10", base62_encode(62))
        self.assertEqual("1c", base62_encode(100))
        self.assertEqual("001c", base62_encode(100, minwidth=4))
