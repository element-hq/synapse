#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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

from synapse.util.wheel_timer import WheelTimer

from .. import unittest


class WheelTimerTestCase(unittest.TestCase):
    def test_single_insert_fetch(self) -> None:
        wheel: WheelTimer[object] = WheelTimer(bucket_size=5)

        wheel.insert(100, "1", 150)

        self.assertListEqual(wheel.fetch(101), [])
        self.assertListEqual(wheel.fetch(110), [])
        self.assertListEqual(wheel.fetch(120), [])
        self.assertListEqual(wheel.fetch(130), [])
        self.assertListEqual(wheel.fetch(149), [])
        self.assertListEqual(wheel.fetch(156), ["1"])
        self.assertListEqual(wheel.fetch(170), [])

    def test_multi_insert(self) -> None:
        wheel: WheelTimer[object] = WheelTimer(bucket_size=5)

        wheel.insert(100, "1", 150)
        wheel.insert(105, "2", 130)
        wheel.insert(106, "3", 160)

        self.assertListEqual(wheel.fetch(110), [])
        self.assertListEqual(wheel.fetch(135), ["2"])
        self.assertListEqual(wheel.fetch(149), [])
        self.assertListEqual(wheel.fetch(158), ["1"])
        self.assertListEqual(wheel.fetch(160), [])
        self.assertListEqual(wheel.fetch(200), ["3"])
        self.assertListEqual(wheel.fetch(210), [])

    def test_insert_past(self) -> None:
        wheel: WheelTimer[object] = WheelTimer(bucket_size=5)

        wheel.insert(100, "1", 50)
        self.assertListEqual(wheel.fetch(120), ["1"])

    def test_insert_past_multi(self) -> None:
        wheel: WheelTimer[object] = WheelTimer(bucket_size=5)

        wheel.insert(100, "1", 150)
        wheel.insert(100, "2", 140)
        wheel.insert(100, "3", 50)
        self.assertListEqual(wheel.fetch(110), ["3"])
        self.assertListEqual(wheel.fetch(120), [])
        self.assertListEqual(wheel.fetch(147), ["2"])
        self.assertListEqual(wheel.fetch(200), ["1"])
        self.assertListEqual(wheel.fetch(240), [])

    def test_multi_insert_then_past(self) -> None:
        wheel: WheelTimer[object] = WheelTimer(bucket_size=5)

        wheel.insert(100, "1", 150)
        wheel.insert(100, "2", 160)
        wheel.insert(100, "3", 155)

        self.assertListEqual(wheel.fetch(110), [])
        self.assertListEqual(wheel.fetch(158), ["1"])
