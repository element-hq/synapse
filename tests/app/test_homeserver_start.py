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

import gc

import synapse.app.homeserver
from synapse.config._base import ConfigError
from synapse.server import HomeServer

from tests.config.utils import ConfigFileTestCase
from tests.server import get_clock
from tests.unittest import HomeserverTestCase


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


class HomeserverCleanShutdownTests(HomeserverTestCase):
    def setUp(self) -> None:
        # Override setup to manually create homeserver instance ourself
        pass

    def test_homeserver_can_be_garbage_collected(self) -> None:
        self.reactor, self.clock = get_clock()
        self._hs_args = {"clock": self.clock, "reactor": self.reactor}
        self.hs = self.make_homeserver(self.reactor, self.clock)

        # TODO: make legit homeserver so I'm testing actual shutdown, not some fake test
        # shutdown scenario

        if self.hs is None:
            raise Exception("No homeserver returned from make_homeserver.")

        if not isinstance(self.hs, HomeServer):
            raise Exception("A homeserver wasn't returned, but %r" % (self.hs,))

        self.hs.shutdown()
        # del self.helper
        # del self.site
        # del self.resource
        # del self.servlets
        del self.hs

        gc.collect()

        gc_objects = gc.get_objects()
        for obj in gc_objects:
            if isinstance(obj, HomeServer):
                # import objgraph
                # objgraph.show_backrefs(obj, max_depth=10, too_many=10, extra_info=lambda x: bla(x))
                raise Exception(
                    "Found HomeServer instance referenced by garbage collector"
                )


def bla(x):
    if isinstance(x, list):
        for k, v in globals().items():
            if v is x:
                print(f"Found in globals: {k}")
        # print("List:")
        # print(x[:3])
        # print(type(x), len(x))
        # for ref in gc.get_referrers(x):
        #     print(type(ref), repr(ref)[:300])
        # print(objgraph.at(id(x)))
    if isinstance(x, dict):
        for k, v in globals().items():
            if v is x:
                print(f"Found in globals: {k}")
        # print("Dict:")
        # print(x.__repr__)
        # print(type(x), len(x))
        # for ref in gc.get_referrers(x):
        #     print(type(ref), repr(ref)[:300])
    # print(hex(id(x)))
    return hex(id(x))
