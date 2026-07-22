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
