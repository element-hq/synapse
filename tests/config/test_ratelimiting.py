#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
from synapse.config.homeserver import HomeServerConfig
from synapse.config.ratelimiting import RatelimitSettings

from tests.unittest import TestCase
from tests.utils import default_config


class ParseRatelimitSettingsTestcase(TestCase):
    def test_depth_1(self) -> None:
        cfg = {
            "a": {
                "per_second": 5,
                "burst_count": 10,
            }
        }
        parsed = RatelimitSettings.parse(cfg, "a")
        self.assertEqual(parsed, RatelimitSettings("a", 5, 10))

    def test_depth_2(self) -> None:
        cfg = {
            "a": {
                "b": {
                    "per_second": 5,
                    "burst_count": 10,
                },
            }
        }
        parsed = RatelimitSettings.parse(cfg, "a.b")
        self.assertEqual(parsed, RatelimitSettings("a.b", 5, 10))

    def test_missing(self) -> None:
        parsed = RatelimitSettings.parse(
            {}, "a", defaults={"per_second": 5, "burst_count": 10}
        )
        self.assertEqual(parsed, RatelimitSettings("a", 5, 10))


class RatelimitConfigTestCase(TestCase):
    def test_parse_rc_federation(self) -> None:
        config_dict = default_config("test")
        config_dict["rc_federation"] = {
            "window_size": 20000,
            "sleep_limit": 693,
            "sleep_delay": 252,
            "reject_limit": 198,
            "concurrent": 7,
        }

        config = HomeServerConfig()
        config.parse_config_dict(config_dict, "", "")
        config_obj = config.ratelimiting.rc_federation

        self.assertEqual(config_obj.window_size, 20000)
        self.assertEqual(config_obj.sleep_limit, 693)
        self.assertEqual(config_obj.sleep_delay, 252)
        self.assertEqual(config_obj.reject_limit, 198)
        self.assertEqual(config_obj.concurrent, 7)
