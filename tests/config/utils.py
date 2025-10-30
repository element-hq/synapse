#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

from synapse.config.homeserver import HomeServerConfig


class ConfigFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.config_file = os.path.join(self.dir, "homeserver.yaml")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir)

    def generate_config(self) -> None:
        with redirect_stdout(StringIO()):
            HomeServerConfig.load_or_generate_config(
                "",
                [
                    "--generate-config",
                    "-c",
                    self.config_file,
                    "--report-stats=yes",
                    "-H",
                    "lemurs.win",
                ],
            )

    def generate_config_and_remove_lines_containing(self, needles: list[str]) -> None:
        self.generate_config()

        with open(self.config_file) as f:
            contents = f.readlines()
        for needle in needles:
            contents = [line for line in contents if needle not in line]
        with open(self.config_file, "w") as f:
            f.write("".join(contents))

    def add_lines_to_config(self, lines: list[str]) -> None:
        with open(self.config_file, "a") as f:
            for line in lines:
                f.write(line + "\n")
