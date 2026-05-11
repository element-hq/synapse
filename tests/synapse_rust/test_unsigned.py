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

from synapse.synapse_rust.events import Unsigned

from tests import unittest


class UnsignedTestCase(unittest.TestCase):
    def test_prev_content(self) -> None:
        """Test that the prev_content field is correctly exposed as a JsonObject."""
        unsigned = Unsigned({"prev_content": {"key1": "value1", "key2": 42}})

        self.assert_dict(unsigned["prev_content"], {"key1": "value1", "key2": 42})

        self.assert_dict(
            unsigned.for_event(), {"prev_content": {"key1": "value1", "key2": 42}}
        )

    def test_large_age_ts(self) -> None:
        """Test that we can handle integers larger than 2^128, which is larger
        than the maximum rust native integer size."""

        large_int = 2**200
        unsigned = Unsigned({"age_ts": large_int})

        self.assertEqual(unsigned["age_ts"], large_int)

        self.assert_dict(unsigned.for_event(), {"age_ts": large_int})

    def test_large_integer_in_prev_content(self) -> None:
        """Test that we can handle integers larger than 2^128 in the
        prev_content field, which is a JsonObject and thus can contain arbitrary
        JSON."""

        large_int = 2**200
        unsigned = Unsigned({"prev_content": {"some_field": large_int}})

        self.assertEqual(unsigned["prev_content"]["some_field"], large_int)
        self.assert_dict(
            unsigned.for_event(), {"prev_content": {"some_field": large_int}}
        )
