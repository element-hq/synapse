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

import synapse.app.homeserver
from synapse.config._base import ConfigError

from tests.config.utils import ConfigFileTestCase


class HomeserverAppStartTestCase(ConfigFileTestCase):
    def test_wrong_start_caught(self) -> None:
        # Generate a config with a worker_app
        self.generate_config()
        # Add a blank line as otherwise the next addition ends up on a line with a comment
        self.add_lines_to_config(["  "])
        self.add_lines_to_config(["worker_app: test_worker_app"])
        self.add_lines_to_config(["worker_log_config: /data/logconfig.config"])
        self.add_lines_to_config(["instance_map:"])
        self.add_lines_to_config(["  main:", "    host: 127.0.0.1", "    port: 1234"])
        # Ensure that starting master process with worker config raises an exception
        with self.assertRaises(ConfigError):
            synapse.app.homeserver.setup(["-c", self.config_file])
