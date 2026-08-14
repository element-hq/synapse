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

    @parameterized.expand(["msc4133_key_allowlist", "msc4133_key_denylist"])
    def test_msc4133_key_lists_parsing(self, option: str) -> None:
        """A list of strings is accepted, and defaults to None when absent."""
        _read_config({"experimental_features": {option: ["some_field"]}})
        _read_config({"experimental_features": {option: []}})

        config = ExperimentalConfig(RootConfig())
        config.read_config({}, allow_secrets_in_config=False)
        self.assertIsNone(getattr(config, option))

    @parameterized.expand(
        [
            (f"{option}_{test_description}", option, value)
            for option in ("msc4133_key_allowlist", "msc4133_key_denylist")
            for test_description, value in (
                ("not_a_list", "some_field"),
                ("non_string_entries", ["some_field", 1]),
                ("nested_list", [["some_field"]]),
            )
        ]
    )
    def test_msc4133_key_lists_reject_invalid_values(
        self, test_description: str, option: str, value: object
    ) -> None:
        """Values which are not a list of custom profile field names are rejected."""
        with self.assertRaises(ConfigError):
            _read_config({"experimental_features": {option: value}})


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
