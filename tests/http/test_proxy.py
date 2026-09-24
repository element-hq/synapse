#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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

from parameterized import parameterized

from synapse.http.proxy import (
    HOP_BY_HOP_HEADERS_LOWERCASE,
    parse_connection_header_value,
)

from tests.unittest import TestCase


def mix_case(s: str) -> str:
    """
    Mix up the case of each character in the string (upper or lower case)
    """
    return "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(s))


class ProxyTests(TestCase):
    @parameterized.expand(
        [
            [b"close, X-Foo, X-Bar", {"close", "x-foo", "x-bar"}],
            # No whitespace
            [b"close,X-Foo,X-Bar", {"close", "x-foo", "x-bar"}],
            # More whitespace
            [b"close,    X-Foo,      X-Bar", {"close", "x-foo", "x-bar"}],
            # "close" directive in not the first position
            [b"X-Foo, X-Bar, close", {"x-foo", "x-bar", "close"}],
            # Normalizes header capitalization
            [b"keep-alive, x-fOo, x-bAr", {"keep-alive", "x-foo", "x-bar"}],
            # Handles header names with whitespace
            [
                b"keep-alive, x  foo, x bar",
                {"keep-alive", "x  foo", "x bar"},
            ],
            # Make sure we handle all of the hop-by-hop headers
            [
                mix_case(", ".join(HOP_BY_HOP_HEADERS_LOWERCASE)).encode("ascii"),
                HOP_BY_HOP_HEADERS_LOWERCASE,
            ],
        ]
    )
    def test_parse_connection_header_value(
        self,
        connection_header_value: bytes,
        expected_extra_headers_to_remove: set[str],
    ) -> None:
        """
        Tests that the connection header value is parsed correctly
        """
        self.assertIncludes(
            expected_extra_headers_to_remove,
            parse_connection_header_value(connection_header_value),
            exact=True,
        )
