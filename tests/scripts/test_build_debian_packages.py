#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
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

import runpy
from pathlib import Path

from tests.unittest import TestCase


class BuildDebianPackagesTestCase(TestCase):
    def test_default_dists_include_ubuntu_resolute(self) -> None:
        script = Path(__file__).parents[2] / "scripts-dev" / "build_debian_packages.py"

        dists = runpy.run_path(script)["DISTS"]

        self.assertIn("ubuntu:resolute", dists)
