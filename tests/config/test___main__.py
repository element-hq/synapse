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
from synapse.config.__main__ import main

from tests.config.utils import ConfigFileTestCase


class ConfigMainFileTestCase(ConfigFileTestCase):
    def test_executes_without_an_action(self) -> None:
        self.generate_config()
        main(["", "-c", self.config_file])

    def test_read__error_if_key_not_found(self) -> None:
        self.generate_config()
        with self.assertRaises(SystemExit):
            main(["", "read", "foo.bar.hello", "-c", self.config_file])

    def test_read__passes_if_key_found(self) -> None:
        self.generate_config()
        main(["", "read", "server.server_name", "-c", self.config_file])
