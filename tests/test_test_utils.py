#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

from tests import unittest
from tests.utils import MockClock


class MockClockTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MockClock()

    def test_advance_time(self) -> None:
        start_time = self.clock.time()

        self.clock.advance_time(20)

        self.assertEqual(20, self.clock.time() - start_time)

    def test_later(self) -> None:
        invoked = [0, 0]

        def _cb0() -> None:
            invoked[0] = 1

        self.clock.call_later(10, _cb0)

        def _cb1() -> None:
            invoked[1] = 1

        self.clock.call_later(20, _cb1)

        self.assertFalse(invoked[0])

        self.clock.advance_time(15)

        self.assertTrue(invoked[0])
        self.assertFalse(invoked[1])

        self.clock.advance_time(5)

        self.assertTrue(invoked[1])

    def test_cancel_later(self) -> None:
        invoked = [0, 0]

        def _cb0() -> None:
            invoked[0] = 1

        t0 = self.clock.call_later(10, _cb0)

        def _cb1() -> None:
            invoked[1] = 1

        self.clock.call_later(20, _cb1)

        self.clock.cancel_call_later(t0)

        self.clock.advance_time(30)

        self.assertFalse(invoked[0])
        self.assertTrue(invoked[1])
