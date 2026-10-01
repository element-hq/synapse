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

from synapse.synapse_rust.events import JsonObject
from synapse.util import MutableOverlayMapping

from tests import unittest


class JsonObjectMappingTestCase(unittest.TestCase):
    def test_new_and_basic_mapping_behavior(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        self.assertEqual(len(obj), 2)
        self.assertTrue("a" in obj)
        self.assertTrue("b" in obj)
        self.assertFalse("c" in obj)
        self.assertFalse(123 in obj)  # type: ignore[comparison-overlap]

    def test_getitem_and_key_errors(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        self.assertEqual(obj["a"], 1)

        with self.assertRaises(KeyError):
            _ = obj["missing"]

        with self.assertRaises(KeyError):
            _ = obj[10]  # type: ignore[index]

    def test_iter_keys_values_items(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        iterator = iter(obj)
        first = next(iterator)
        second = next(iterator)
        self.assertCountEqual((first, second), ("a", "b"))
        with self.assertRaises(StopIteration):
            next(iterator)

        self.assertCountEqual(list(obj.keys()), ["a", "b"])
        self.assertCountEqual(list(obj.values()), [1, 2])
        self.assertCountEqual(list(obj.items()), [("a", 1), ("b", 2)])

    def test_keys_set_like_behavior(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        # Test 'and' operator.
        self.assertEqual(obj.keys() & {"a"}, {"a"})
        self.assertEqual({"a"} & obj.keys(), {"a"})
        self.assertEqual(obj.keys() & {"c"}, set())
        self.assertEqual({"c"} & obj.keys(), set())

        # Test 'or' operator.
        self.assertEqual(obj.keys() | {"a"}, {"a", "b"})
        self.assertEqual({"a"} | obj.keys(), {"a", "b"})
        self.assertEqual(obj.keys() | {"c"}, {"a", "b", "c"})
        self.assertEqual({"c"} | obj.keys(), {"a", "b", "c"})

        # Test 'xor' operator.
        self.assertEqual(obj.keys() ^ {"a"}, {"b"})
        self.assertEqual({"a"} ^ obj.keys(), {"b"})
        self.assertEqual(obj.keys() ^ {"c"}, {"a", "b", "c"})
        self.assertEqual({"c"} ^ obj.keys(), {"a", "b", "c"})

        # Test 'sub' operator.
        self.assertEqual(obj.keys() - {"a"}, {"b"})
        self.assertEqual({"a"} - obj.keys(), set())
        self.assertEqual(obj.keys() - {"c"}, {"a", "b"})
        self.assertEqual({"c"} - obj.keys(), {"c"})

    def test_values_view(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        values = obj.values()

        self.assertEqual(len(values), 2)
        self.assertCountEqual(list(values), [1, 2])

        self.assertIn(1, values)
        self.assertIn(2, values)
        self.assertNotIn(3, values)
        self.assertNotIn("a", values)
        self.assertNotIn(object(), values)

        # Iterating twice should yield the same values.
        self.assertCountEqual(list(values), [1, 2])

    def test_items_view(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        items = obj.items()

        self.assertEqual(len(items), 2)
        self.assertCountEqual(list(items), [("a", 1), ("b", 2)])

        self.assertIn(("a", 1), items)
        self.assertIn(("b", 2), items)
        self.assertNotIn(("a", 2), items)
        self.assertNotIn(("c", 1), items)
        self.assertNotIn("a", items)
        self.assertNotIn(("a", 1, "extra"), items)

        # Iterating twice should yield the same items.
        self.assertCountEqual(list(items), [("a", 1), ("b", 2)])

    def test_get(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        self.assertEqual(obj.get("a"), 1)
        self.assertEqual(obj.get("missing", "fallback"), "fallback")
        self.assertEqual(obj.get(5, "fallback"), "fallback")  # type: ignore[call-overload]

    def test_eq(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        self.assertEqual(obj, {"a": 1, "b": 2})
        self.assertNotEqual(obj, {"a": 1})
        self.assertNotEqual(obj, ["a", "b"])

    def test_str_and_repr(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        self.assertEqual(str(obj), r'{"a":1,"b":2}')
        self.assertEqual(repr(obj), r'JsonObject({"a":1,"b":2})')

    def test_json_object_constructor(self) -> None:
        obj = JsonObject({"a": 1, "b": 2})

        # Passing in an existing JsonObject should work.
        obj2 = JsonObject(obj)
        self.assertEqual(obj2, {"a": 1, "b": 2})

        # Other mapping types should also work.
        obj3 = JsonObject(MutableOverlayMapping({"a": 1, "b": 2}))
        self.assertEqual(obj3, {"a": 1, "b": 2})

        # Test that passing a non-mapping raises a TypeError.
        with self.assertRaises(TypeError):
            JsonObject(123)  # type: ignore[arg-type]
