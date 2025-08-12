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

from unittest.mock import Mock, patch

from synapse.util.distributor import Distributor

from . import unittest


class DistributorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dist = Distributor()

    def test_signal_dispatch(self) -> None:
        self.dist.declare("alert")

        observer = Mock()
        self.dist.observe("alert", observer)

        self.dist.fire("alert", 1, 2, 3)
        observer.assert_called_with(1, 2, 3)

    def test_signal_catch(self) -> None:
        self.dist.declare("alarm")

        observers = [Mock() for i in (1, 2)]
        for o in observers:
            self.dist.observe("alarm", o)

        observers[0].side_effect = Exception("Awoogah!")

        with patch("synapse.util.distributor.logger", spec=["warning"]) as mock_logger:
            self.dist.fire("alarm", "Go")

            observers[0].assert_called_once_with("Go")
            observers[1].assert_called_once_with("Go")

            self.assertEqual(mock_logger.warning.call_count, 1)
            self.assertIsInstance(mock_logger.warning.call_args[0][0], str)

    def test_signal_prereg(self) -> None:
        observer = Mock()
        self.dist.observe("flare", observer)

        self.dist.declare("flare")
        self.dist.fire("flare", 4, 5)

        observer.assert_called_with(4, 5)

    def test_signal_undeclared(self) -> None:
        def code() -> None:
            self.dist.fire("notification")

        self.assertRaises(KeyError, code)
