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

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersions
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

    def test_msc4430_implies_msc4428(self) -> None:
        """Clients learn the user behind a member key through MSC4428's
        `member_info`, so MSC4430 cannot be enabled without it."""
        config = _read_config({"experimental_features": {"msc4430_enabled": True}})
        self.assertTrue(config.msc4430_enabled)
        self.assertTrue(config.msc4428_enabled)

        config = _read_config({"experimental_features": {"msc4428_enabled": True}})
        self.assertFalse(config.msc4430_enabled)
        self.assertTrue(config.msc4428_enabled)

        # An explicit `false` for MSC4428 is overridden, not rejected.
        config = _read_config(
            {
                "experimental_features": {
                    "msc4428_enabled": False,
                    "msc4430_enabled": True,
                }
            }
        )
        self.assertTrue(config.msc4430_enabled)
        self.assertTrue(config.msc4428_enabled)

        config = _read_config({"experimental_features": {}})
        self.assertFalse(config.msc4430_enabled)
        self.assertFalse(config.msc4428_enabled)

    def test_msc4430_room_version_not_registered(self) -> None:
        """The member key room version is defined but cannot yet be used: enabling
        the flag must not add it to the known room versions."""
        _read_config({"experimental_features": {"msc4430_enabled": True}})
        self.assertNotIn(RoomVersions.MSC4430v12.identifier, KNOWN_ROOM_VERSIONS)
        self.assertNotIn("org.matrix.msc4430.12", KNOWN_ROOM_VERSIONS)


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
