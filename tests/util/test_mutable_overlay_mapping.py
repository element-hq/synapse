#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

import random
import unittest

from synapse.util import MutableOverlayMapping


class TestMutableOverlayMapping(unittest.TestCase):
    """Tests for the MutableOverlayMapping class."""

    def test_init(self) -> None:
        """Test initialization with different input types."""
        # Test with empty dict
        empty_dict: dict[str, int] = {}
        mapping = MutableOverlayMapping(empty_dict)
        self.assertEqual(len(mapping), 0)

        # Test with populated dict
        populated_dict = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(populated_dict)
        self.assertEqual(len(mapping), 3)
        self.assertEqual(mapping["a"], 1)

    def test_get_item(self) -> None:
        """Test getting items from the mapping."""
        underlying = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(underlying)

        # Get from underlying map
        self.assertEqual(mapping["a"], 1)
        self.assertEqual(mapping["b"], 2)

        # Check KeyError for non-existent key
        with self.assertRaises(KeyError):
            mapping["d"]

    def test_set_item(self) -> None:
        """Test setting items in the mapping."""
        underlying = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(underlying)

        # Set new key
        mapping["d"] = 4
        self.assertEqual(mapping["d"], 4)

        # Override existing key
        mapping["a"] = 10
        self.assertEqual(mapping["a"], 10)

        # Original map should be unchanged
        self.assertEqual(underlying["a"], 1)
        self.assertNotIn("d", underlying)

    def test_del_item(self) -> None:
        """Test deleting items from the mapping."""
        underlying = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(underlying)

        # Delete a key
        del mapping["a"]
        with self.assertRaises(KeyError):
            mapping["a"]

        # Original map should be unchanged
        self.assertEqual(underlying["a"], 1)

        # Delete non-existent key
        with self.assertRaises(KeyError):
            del mapping["d"]

    def test_len(self) -> None:
        """Test the len() function."""
        underlying = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(underlying)

        self.assertEqual(len(mapping), 3)

        # Add a new key
        mapping["d"] = 4
        self.assertEqual(len(mapping), 4)

        # Override an existing key
        mapping["a"] = 10
        self.assertEqual(len(mapping), 4)

        # Delete a key
        del mapping["b"]
        self.assertEqual(len(mapping), 3)

        # Delete a key in mutable map
        del mapping["d"]
        self.assertEqual(len(mapping), 2)

    def test_iteration(self) -> None:
        """Test iteration over the mapping."""
        underlying = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(underlying)

        # Add a new key and override an existing one
        mapping["d"] = 4
        mapping["a"] = 10

        # Delete a key
        del mapping["c"]

        iterated_keys = set()
        for k in mapping:
            iterated_keys.add(k)

        # Expected keys: a, b, d (c is deleted)
        self.assertEqual(iterated_keys, {"a", "b", "d"})

        iterated_items = dict(mapping.items())
        self.assertDictEqual(iterated_items, {"a": 10, "b": 2, "d": 4})

    def test_clear(self) -> None:
        """Test the clear method."""
        underlying = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(underlying)

        # Add a new key and override an existing one
        mapping["d"] = 4
        mapping["a"] = 10

        # Clear the mapping
        mapping.clear()
        self.assertEqual(len(mapping), 0)

        # All keys should be gone
        with self.assertRaises(KeyError):
            mapping["a"]

        with self.assertRaises(KeyError):
            mapping["d"]

        # Adding a new key after clearing
        mapping["b"] = 2
        self.assertEqual(mapping["b"], 2)
        self.assertEqual(len(mapping), 1)

        # The underlying map should remain unchanged
        self.assertDictEqual(underlying, {"a": 1, "b": 2, "c": 3})

    def test_dict_methods(self) -> None:
        """Test standard dict methods."""
        underlying = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(underlying)

        # Test keys, values, and items
        self.assertEqual(set(mapping.keys()), {"a", "b", "c"})
        self.assertEqual(set(mapping.values()), {1, 2, 3})
        self.assertEqual(set(mapping.items()), {("a", 1), ("b", 2), ("c", 3)})

        # Modify, then test again
        mapping["d"] = 4
        mapping["a"] = 10
        del mapping["c"]

        self.assertEqual(set(mapping.keys()), {"a", "b", "d"})
        self.assertEqual(set(mapping.values()), {10, 2, 4})
        self.assertEqual(set(mapping.items()), {("a", 10), ("b", 2), ("d", 4)})

    def test_key_presence(self) -> None:
        """Test checking if keys exist in the mapping."""
        underlying = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(underlying)

        mapping["d"] = 4
        mapping["a"] = 10
        del mapping["c"]

        # Test key presence
        self.assertIn("a", mapping)
        self.assertIn("b", mapping)
        self.assertNotIn("c", mapping)
        self.assertIn("d", mapping)
        self.assertNotIn("e", mapping)

    def test_len_after_reset_and_redelete(self) -> None:
        """len() must follow keys that move between the underlying map, the
        overrides and the deletions."""
        underlying = {"a": 1, "b": 2, "c": 3}
        mapping = MutableOverlayMapping(underlying)

        # Delete an underlying key, then set it again.
        del mapping["a"]
        self.assertEqual(len(mapping), 2)
        mapping["a"] = 10
        self.assertEqual(len(mapping), 3)

        # Add a key only the overlay knows about, delete it, and add it back.
        mapping["d"] = 4
        self.assertEqual(len(mapping), 4)
        del mapping["d"]
        self.assertEqual(len(mapping), 3)
        mapping["d"] = 40
        self.assertEqual(len(mapping), 4)

        # Override then delete an underlying key.
        mapping["b"] = 20
        del mapping["b"]
        self.assertEqual(len(mapping), 3)

        self.assertEqual(len(mapping), len(dict(mapping)))

    def test_len_nested(self) -> None:
        """An overlay over an overlay reports the right length."""
        inner = MutableOverlayMapping({"a": 1, "b": 2})
        inner["c"] = 3
        del inner["a"]

        outer = MutableOverlayMapping(inner)
        self.assertEqual(len(outer), 2)

        outer["a"] = 10  # deleted in inner, so new to outer
        outer["b"] = 20  # present in inner's underlying map
        outer["c"] = 30  # present in inner's overrides
        outer["d"] = 40  # new
        self.assertEqual(len(outer), 4)

        del outer["b"]
        del outer["d"]
        self.assertEqual(len(outer), 2)
        self.assertEqual(len(outer), len(dict(outer)))

    def test_total_entries(self) -> None:
        """total_entries() counts the base map plus every override and
        deletion, unlike len()."""
        mapping = MutableOverlayMapping({"a": 1, "b": 2, "c": 3})
        self.assertEqual(mapping.total_entries(), 3)

        mapping["a"] = 10  # override: +1 entry, same length
        mapping["d"] = 4  # new key: +1 entry, +1 length
        del mapping["b"]  # deletion: +1 entry, -1 length
        self.assertEqual(len(mapping), 3)
        self.assertEqual(mapping.total_entries(), 6)

        # Deleting an override drops it from the overrides and records the
        # deletion, so the entry count is unchanged.
        del mapping["d"]
        self.assertEqual(len(mapping), 2)
        self.assertEqual(mapping.total_entries(), 6)

        # Nested overlays are counted all the way down.
        outer = MutableOverlayMapping(mapping)
        outer["e"] = 5
        self.assertEqual(outer.total_entries(), 7)

        mapping.clear()
        self.assertEqual(mapping.total_entries(), 0)
