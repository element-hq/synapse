#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

import logging
from typing import TypeVar

from canonicaljson import encode_canonical_json

from synapse.util import split_dict_to_fit_to_size

from tests.unittest import TestCase

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SplitDictTestCase(TestCase):
    def test_empty(self) -> None:
        "Test that an empty dict yields no payloads"

        self.assertEqual(
            list(
                split_dict_to_fit_to_size({}, soft_max_size=10, wrapping_object_size=0)
            ),
            [],
        )

    def test_no_splitting(self) -> None:
        "Test that a dict that fits within the size limit is yielded as a single payload"

        original_dict = {"a": {"key": "value"}, "b": {"key": "value"}}

        # Set the soft max size to be the size of the original dict, so it
        # should fit
        soft_max_size = len(encode_canonical_json(original_dict))

        self.assertEqual(
            list(
                split_dict_to_fit_to_size(
                    original_dict,
                    soft_max_size=soft_max_size,
                )
            ),
            [(original_dict, soft_max_size)],
        )

    def test_no_splitting_with_wrapping_size(self) -> None:
        "Test that the wrapping size is taken into account when deciding whether to split"

        wrapping = {"key": "value", "payload": {}}
        original_dict = {"a": {"key": "value"}, "b": {"key": "value"}}
        wrapping_object_size = len(encode_canonical_json(wrapping))

        # Set the soft max size to the size of the expected final output.
        soft_max_size = len(
            encode_canonical_json({"key": "value", "payload": original_dict})
        )

        self.assertEqual(
            list(
                split_dict_to_fit_to_size(
                    original_dict,
                    soft_max_size=soft_max_size,
                    wrapping_object_size=wrapping_object_size,
                )
            ),
            [(original_dict, soft_max_size)],
        )

    def test_splitting(self) -> None:
        "Test that a dict that exceeds the size limit is split into multiple payloads"

        original_dict = {
            "a": {"key": "value"},
            "b": {"key": "value"},
            "c": {"key": "value"},
        }

        # Set the soft max size to be the size of a single key-value pair, so
        # it should split into three payloads.
        soft_max_size = len(encode_canonical_json({"a": {"key": "value"}}))

        self.assertEqual(
            list(
                split_dict_to_fit_to_size(
                    original_dict,
                    soft_max_size=soft_max_size,
                )
            ),
            [
                ({"a": {"key": "value"}}, soft_max_size),
                ({"b": {"key": "value"}}, soft_max_size),
                ({"c": {"key": "value"}}, soft_max_size),
            ],
        )

    def test_splitting_with_wrapping_size(self) -> None:
        "Test that the wrapping size is taken into account when splitting"

        wrapping = {"key": "value", "payload": {}}
        original_dict = {
            "a": {"key": "value"},
            "b": {"key": "value"},
            "c": {"key": "value"},
        }
        wrapping_object_size = len(encode_canonical_json(wrapping))
        # Set the soft max size to be the size of a single key-value pair plus
        # the wrapping size, so it should split into three payloads.
        soft_max_size = (
            len(encode_canonical_json({"a": {"key": "value"}}))
            + wrapping_object_size
            - 2
        )

        self.assertEqual(
            list(
                split_dict_to_fit_to_size(
                    original_dict,
                    soft_max_size=soft_max_size,
                    wrapping_object_size=wrapping_object_size,
                )
            ),
            [
                ({"a": {"key": "value"}}, soft_max_size),
                ({"b": {"key": "value"}}, soft_max_size),
                ({"c": {"key": "value"}}, soft_max_size),
            ],
        )

    def test_oversized_entry(self) -> None:
        """Test that if a single entry exceeds the size limit, it is still
        yielded as a single payload"""

        original_dict = {
            "a": {"key": "value"},
            "b": {"key": "value"},
            "c": {"key": "value"},
        }

        # Set the soft max size to be smaller than the size of a single
        # key-value pair, so each entry exceeds the limit.
        soft_max_size = len(encode_canonical_json({"a": {"key": "value"}})) - 1

        self.assertEqual(
            list(
                split_dict_to_fit_to_size(
                    original_dict,
                    soft_max_size=soft_max_size,
                )
            ),
            [
                (
                    {"a": {"key": "value"}},
                    len(encode_canonical_json({"a": {"key": "value"}})),
                ),
                (
                    {"b": {"key": "value"}},
                    len(encode_canonical_json({"b": {"key": "value"}})),
                ),
                (
                    {"c": {"key": "value"}},
                    len(encode_canonical_json({"c": {"key": "value"}})),
                ),
            ],
        )

    def test_different_sized_entries(self) -> None:
        """Test that entries of different sizes are split correctly"""

        original_dict = {
            "a": "X" * 5,  # size 13
            "b": "X" * 10,  # size 18
            "c": "X" * 5,  # size 13
        }

        soft_max_size = 30

        self.assertEqual(
            list(
                split_dict_to_fit_to_size(
                    original_dict,
                    soft_max_size=soft_max_size,
                )
            ),
            [
                (
                    {"a": "X" * 5, "b": "X" * 10},
                    len(encode_canonical_json({"a": "X" * 5, "b": "X" * 10})),
                ),
                (
                    {"c": "X" * 5},
                    len(encode_canonical_json({"c": "X" * 5})),
                ),
            ],
        )
