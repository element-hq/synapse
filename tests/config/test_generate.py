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

import os.path
import re
import shutil
import tempfile
from contextlib import redirect_stdout
from io import StringIO

from synapse.config.homeserver import HomeServerConfig

from tests import unittest


class ConfigGenerationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.file = os.path.join(self.dir, "homeserver.yaml")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir)

    def test_generate_config_generates_files(self) -> None:
        with redirect_stdout(StringIO()):
            HomeServerConfig.load_or_generate_config(
                "",
                [
                    "--generate-config",
                    "-c",
                    self.file,
                    "--report-stats=yes",
                    "-H",
                    "lemurs.win",
                ],
            )

        self.assertSetEqual(
            {"homeserver.yaml", "lemurs.win.log.config", "lemurs.win.signing.key"},
            set(os.listdir(self.dir)),
        )

        self.assert_log_filename_is(
            os.path.join(self.dir, "lemurs.win.log.config"),
            os.path.join(os.getcwd(), "homeserver.log"),
        )

    def assert_log_filename_is(self, log_config_file: str, expected: str) -> None:
        with open(log_config_file) as f:
            config = f.read()
            # find the 'filename' line
            matches = re.findall(r"^\s*filename:\s*(.*)$", config, re.M)
            self.assertEqual(1, len(matches))
            self.assertEqual(matches[0], expected)
