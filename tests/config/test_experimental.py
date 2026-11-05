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


from typing import Any

import yaml
from parameterized import parameterized

from synapse.config._base import ConfigError, RootConfig
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
            [
                "msc4140",
                {
                    "experimental_features": {
                        "msc4140_finalised_retention_period": "1d",
                        "msc4140_finalised_retention_limit_per_user": 10,
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

    def test_finalised_delayed_events_retention_period_enforces_non_negative(
        self,
    ) -> None:
        """
        Test that the configured retention period of finalised delayed events
        must be a non-negative duration if set
        """

        def generate_config(value: Any) -> JsonDict:
            return {
                "experimental_features": {"msc4140_finalised_retention_period": value}
            }

        for allowed_value in (0, 1, "7d"):
            _read_config(generate_config(allowed_value))

        for disallowed_value in (-1, "-1d", "1x", None):
            with self.assertRaises((ConfigError, TypeError, ValueError)):
                _read_config(generate_config(disallowed_value))

    def test_finalised_delayed_events_retention_limit_per_user_enforces_non_negative_int(
        self,
    ) -> None:
        """
        Test that the configured retention limit of finalised delayed events per user
        must be a non-negative integer if set
        """

        def generate_config(value: Any) -> JsonDict:
            return {
                "experimental_features": {
                    "msc4140_finalised_retention_limit_per_user": value
                }
            }

        for allowed_value in (0, 1):
            _read_config(generate_config(allowed_value))

        for disallowed_value in (-1, 0.5, "1"):
            with self.assertRaises(ConfigError):
                _read_config(generate_config(disallowed_value))


def _read_config(config_values: JsonDict) -> None:
    ExperimentalConfig(RootConfig()).read_config(
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
