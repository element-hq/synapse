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
from synapse.util.stringutils import parse_and_validate_server_name, parse_server_name

from tests import unittest


class ServerNameTestCase(unittest.TestCase):
    def test_parse_server_name(self) -> None:
        test_data = {
            "localhost": ("localhost", None),
            "my-example.com:1234": ("my-example.com", 1234),
            "1.2.3.4": ("1.2.3.4", None),
            "[0abc:1def::1234]": ("[0abc:1def::1234]", None),
            "1.2.3.4:1": ("1.2.3.4", 1),
            "[0abc:1def::1234]:8080": ("[0abc:1def::1234]", 8080),
            ":80": ("", 80),
            "": ("", None),
        }

        for i, o in test_data.items():
            self.assertEqual(parse_server_name(i), o)

    def test_validate_bad_server_names(self) -> None:
        test_data = [
            "",  # empty
            "localhost:http",  # non-numeric port
            "1234]",  # smells like ipv6 literal but isn't
            "[1234",
            "[1.2.3.4]",
            "underscore_.com",
            "percent%65.com",
            "newline.com\n",
            ".empty-label.com",
            "1234:5678:80",  # too many colons
            ":80",
        ]
        for i in test_data:
            try:
                parse_and_validate_server_name(i)
                self.fail(
                    "Expected parse_and_validate_server_name('%s') to throw" % (i,)
                )
            except ValueError:
                pass
