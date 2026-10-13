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

from synapse.http.appservice_proxy import has_dot_segments

from tests import unittest


class HasDotSegmentsTestCase(unittest.TestCase):
    def test_plain_path_has_no_dot_segments(self) -> None:
        self.assertFalse(has_dot_segments(b"/some/path"))
        self.assertFalse(has_dot_segments(b"/some/path.txt"))
        self.assertFalse(has_dot_segments(b"/some/...path"))

    def test_dot_segment_is_detected(self) -> None:
        self.assertTrue(has_dot_segments(b"/some/./path"))
        self.assertTrue(has_dot_segments(b"/./some/path"))
        self.assertTrue(has_dot_segments(b"/some/path/."))

    def test_dot_dot_segment_is_detected(self) -> None:
        self.assertTrue(has_dot_segments(b"/some/../path"))
        self.assertTrue(has_dot_segments(b"/../some/path"))
        self.assertTrue(has_dot_segments(b"/some/path/.."))

    def test_percent_encoded_dot_segments_are_detected(self) -> None:
        self.assertTrue(has_dot_segments(b"/some/%2e%2e/path"))
        self.assertTrue(has_dot_segments(b"/some/%2e/path"))
        self.assertTrue(has_dot_segments(b"/some/%2E%2E/path"))

    def test_percent_encoded_separator_is_detected(self) -> None:
        self.assertTrue(has_dot_segments(b"/some%2f../path"))

    def test_double_encoded_dot_segments_are_not_detected(self) -> None:
        # Only a single decode is performed, matching the single decode that route
        # arguments get elsewhere, so a double-encoded segment is left alone.
        self.assertFalse(has_dot_segments(b"/some/%252e%252e/path"))
