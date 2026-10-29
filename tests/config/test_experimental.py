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


import yaml
from parameterized import parameterized

from synapse.config import ConfigError
from synapse.config._base import RootConfig
from synapse.config.experimental import ExperimentalConfig
from synapse.config.homeserver import HomeServerConfig
from synapse.types import JsonDict

from tests import unittest


class ExperimentalConfigTestCase(unittest.TestCase):
    @parameterized.expand(
        [
            [
                "single",
                {
                    "experimental_features": {
                        "msc3575_enabled": True,
                    }
                },
            ],
            [
                "multi",
                {
                    "experimental_features": {
                        "msc3575_enabled": True,
                        "msc3030_enabled": True,
                    }
                },
            ],
            # This has historically worked and this is being added as a regression test
            ["none", {"experimental_features": None}],
        ]
    )
    def test_experimental_features_parsing(
        self, test_description: str, config_values: JsonDict
    ) -> None:
        """
        Test the that `experimental_features` parses with these values
        """

        _read_config(config_values)

    @parameterized.expand(
        [
            ["default", None, 2 * 60 * 1000],
            ["minimum", "2m", 2 * 60 * 1000],
            ["maximum", "5m", 5 * 60 * 1000],
            ["milliseconds", 200000, 200000],
        ]
    )
    def test_msc4388_session_ttl(
        self, test_description: str, ttl: str | int | None, expected_ms: int
    ) -> None:
        """
        Test that the MSC4388 session TTL is parsed as a duration.
        """
        experimental_features = {} if ttl is None else {"msc4388_session_ttl": ttl}
        config = _read_config({"experimental_features": experimental_features})
        self.assertEqual(config.msc4388_session_ttl_ms, expected_ms)

    @parameterized.expand([["too_short", "119s"], ["too_long", "301s"]])
    def test_msc4388_session_ttl_out_of_range(
        self, test_description: str, ttl: str
    ) -> None:
        """
        Test that an MSC4388 session TTL outside of the range recommended by the
        MSC is rejected.
        """
        with self.assertRaises(ConfigError):
            _read_config({"experimental_features": {"msc4388_session_ttl": ttl}})

    @parameterized.expand([["zero", 0], ["negative", -1], ["string", "100"]])
    def test_msc4388_max_sessions_invalid(
        self, test_description: str, max_sessions: object
    ) -> None:
        """
        Test that an invalid maximum number of MSC4388 sessions is rejected.
        """
        with self.assertRaises(ConfigError):
            _read_config(
                {"experimental_features": {"msc4388_max_sessions": max_sessions}}
            )


def _read_config(config_values: JsonDict) -> ExperimentalConfig:
    config = ExperimentalConfig(RootConfig())
    config.read_config(
        yaml.safe_load(
            HomeServerConfig().generate_config(
                config_dir_path="CONFDIR",
                data_dir_path="/data_dir_path",
                server_name="che.org",
            )
        )
        | config_values,
        allow_secrets_in_config=False,
    )
    return config
