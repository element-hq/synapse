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
import sys

from synapse.logging.formatter import LogFormatter

from tests import unittest


class TestException(Exception):
    pass


class LogFormatterTestCase(unittest.TestCase):
    def test_formatter(self) -> None:
        formatter = LogFormatter()

        try:
            raise TestException("testytest")
        except TestException:
            ei = sys.exc_info()

        output = formatter.formatException(ei)

        # check the output looks vaguely sane
        self.assertIn("testytest", output)
        self.assertIn("Capture point", output)
