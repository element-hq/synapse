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

        obj = object()
        wheel.insert(100, obj, 150)

        self.assertListEqual(wheel.fetch(101), [])
        self.assertListEqual(wheel.fetch(110), [])
        self.assertListEqual(wheel.fetch(120), [])
        self.assertListEqual(wheel.fetch(130), [])
        self.assertListEqual(wheel.fetch(149), [])
        self.assertListEqual(wheel.fetch(156), [obj])
        self.assertListEqual(wheel.fetch(170), [])

    def test_multi_insert(self) -> None:
        wheel: WheelTimer[object] = WheelTimer(bucket_size=5)

        obj1 = object()
        obj2 = object()
        obj3 = object()
        wheel.insert(100, obj1, 150)
        wheel.insert(105, obj2, 130)
        wheel.insert(106, obj3, 160)

        self.assertListEqual(wheel.fetch(110), [])
        self.assertListEqual(wheel.fetch(135), [obj2])
        self.assertListEqual(wheel.fetch(149), [])
        self.assertListEqual(wheel.fetch(158), [obj1])
        self.assertListEqual(wheel.fetch(160), [])
        self.assertListEqual(wheel.fetch(200), [obj3])
        self.assertListEqual(wheel.fetch(210), [])

    def test_insert_past(self) -> None:
        wheel: WheelTimer[object] = WheelTimer(bucket_size=5)

        obj = object()
        wheel.insert(100, obj, 50)
        self.assertListEqual(wheel.fetch(120), [obj])

    def test_insert_past_multi(self) -> None:
        wheel: WheelTimer[object] = WheelTimer(bucket_size=5)

        obj1 = object()
        obj2 = object()
        obj3 = object()
        wheel.insert(100, obj1, 150)
        wheel.insert(100, obj2, 140)
        wheel.insert(100, obj3, 50)
        self.assertListEqual(wheel.fetch(110), [obj3])
        self.assertListEqual(wheel.fetch(120), [])
        self.assertListEqual(wheel.fetch(147), [obj2])
        self.assertListEqual(wheel.fetch(200), [obj1])
        self.assertListEqual(wheel.fetch(240), [])
