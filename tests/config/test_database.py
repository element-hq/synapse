#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

import yaml

from synapse.config._base import RootConfig
from synapse.config.database import DatabaseConfig

from tests import unittest


class DatabaseConfigTestCase(unittest.TestCase):
    def test_database_configured_correctly(self) -> None:
        conf = yaml.safe_load(
            DatabaseConfig(RootConfig()).generate_config_section(
                data_dir_path="/data_dir_path"
            )
        )

        expected_database_conf = {
            "name": "sqlite3",
            "args": {"database": "/data_dir_path/homeserver.db"},
        }

        self.assertEqual(conf["database"], expected_database_conf)
