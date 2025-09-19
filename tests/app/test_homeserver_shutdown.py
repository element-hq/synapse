#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
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
import weakref
from unittest import TestResult

from synapse.app.homeserver import SynapseHomeServer

from tests.server import get_clock, setup_test_homeserver
from tests.unittest import HomeserverTestCase


class HomeserverCleanShutdownTestCase(HomeserverTestCase):
    def setUp(self) -> None:
        pass

    # NOTE: ideally we'd have another test to ensure we properly shutdown with
    # real in-flight HTTP requests since those result in additional resources being
    # setup that hold strong references to the homeserver.
    # Mainly, the HTTP channel created by a real TCP connection from client to server
    # is held open between requests and care needs to be taken in Twisted to ensure it is properly
    # closed in a timely manner during shutdown. Simulating this behaviour in a unit test
    # won't be as good as a proper integration test in complement.

    def test_clean_homeserver_shutdown(self) -> None:
        """Ensure the `SynapseHomeServer` can be fully shutdown and garbage collected"""
        self.reactor, self.clock = get_clock()
        self.hs = setup_test_homeserver(
            self.addCleanup,
            reactor=self.reactor,
            homeserver_to_use=SynapseHomeServer,
            clock=self.clock,
        )
        self.wait_for_background_updates()

        hs_ref = weakref.ref(self.hs)

        # Cleanup the homeserver.
        # This works since we register `hs.shutdown()` as a cleanup function in
        # `setup_test_homeserver`.
        self._runCleanups(TestResult())
        self.reactor.shutdown()

        # Cleanup the internal reference in our test case
        del self.hs

        # Advance the reactor to allow for any outstanding calls to be run.
        # A value of 1 is not enough, so a value of 2 is used.
        self.reactor.advance(2)

        # Force garbage collection.
        gc.collect()

        # Ensure the `HomeServer` hs been garbage collected by attempting to use the
        # weakref to it.
        if hs_ref() is not None:
            self.fail("HomeServer reference should not be valid at this point")
