#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from synapse.config import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.types import JsonDict

from tests.unittest import TestCase
from tests.utils import default_config


class SentryConfigTestCase(TestCase):
    def _parse(self, sentry: JsonDict) -> HomeServerConfig:
        config_dict = default_config(server_name="test")
        config_dict["sentry"] = sentry

        config = HomeServerConfig()
        config.parse_config_dict(config_dict, "", "")
        return config

    def test_max_breadcrumbs_default(self) -> None:
        """`max_breadcrumbs` is optional."""
        config = self._parse({"dsn": "https://key@example.invalid/1"})

        self.assertEqual(config.metrics.sentry_max_breadcrumbs, 50)

    def test_max_breadcrumbs_must_be_a_non_negative_integer(self) -> None:
        """A bad `max_breadcrumbs` is rejected at config load (see the
        validation comment in `synapse/config/metrics.py` for why)."""
        for value in (-1, 1.5, True, "many"):
            with self.assertRaises(ConfigError):
                self._parse(
                    {"dsn": "https://key@example.invalid/1", "max_breadcrumbs": value}
                )
