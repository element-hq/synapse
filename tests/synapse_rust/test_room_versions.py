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

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS

from tests import unittest


class KnownRoomVersionsMappingTestCase(unittest.TestCase):
    """Tests for the Rust-backed KNOWN_ROOM_VERSIONS mapping."""

    def test_contains_none(self) -> None:
        """Test that `None in KNOWN_ROOM_VERSIONS` returns False
        rather than raising a TypeError, matching Python dict semantics."""
        self.assertFalse(None in KNOWN_ROOM_VERSIONS)

    def test_contains_non_string(self) -> None:
        """Test that non-string keys return False from __contains__."""
        self.assertFalse(42 in KNOWN_ROOM_VERSIONS)  # type: ignore[comparison-overlap]
        self.assertFalse(3.14 in KNOWN_ROOM_VERSIONS)  # type: ignore[comparison-overlap]
        self.assertFalse([] in KNOWN_ROOM_VERSIONS)  # type: ignore[comparison-overlap]

    def test_contains_known_version(self) -> None:
        self.assertTrue("1" in KNOWN_ROOM_VERSIONS)

    def test_contains_unknown_version(self) -> None:
        self.assertFalse("unknown" in KNOWN_ROOM_VERSIONS)

    def test_getitem_non_string_raises_key_error(self) -> None:
        """Test that KNOWN_ROOM_VERSIONS[non_string] raises KeyError,
        not TypeError, matching Python dict semantics."""
        with self.assertRaises(KeyError):
            KNOWN_ROOM_VERSIONS[42]  # type: ignore[index]
        with self.assertRaises(KeyError):
            KNOWN_ROOM_VERSIONS[None]  # type: ignore[index]
