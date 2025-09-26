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

from synapse.app.homeserver import SynapseHomeServer
from synapse.storage.background_updates import UpdaterStatus
from synapse.util.clock import CALL_LATER_DELAY_TRACKING_THRESHOLD_S

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

        # Run the reactor so any `callWhenRunning` functions can be cleared out.
        self.reactor.run()

        # Cleanup the homeserver.
        self.get_success(self.hs.shutdown())

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

            # To help debug this test when it fails, it is useful to leverage the
            # `objgraph` module.
            # The following code serves as an example of what I have found to be useful
            # when tracking down references holding the `SynapseHomeServer` in memory:
            #
            # all_objects = gc.get_objects()
            # for obj in all_objects:
            #     try:
            #         # These are a subset of types that are typically involved with
            #         # holding the `HomeServer` in memory. You may want to inspect
            #         # other types as well.
            #         if isinstance(obj, DataStore):
            #             print(sys.getrefcount(obj), "refs to", obj)
            #             if not isinstance(obj, weakref.ProxyType):
            #                 db_obj = obj
            #         if isinstance(obj, SynapseHomeServer):
            #             print(sys.getrefcount(obj), "refs to", obj)
            #             if not isinstance(obj, weakref.ProxyType):
            #                 synapse_hs = obj
            #         if isinstance(obj, SynapseSite):
            #             print(sys.getrefcount(obj), "refs to", obj)
            #             if not isinstance(obj, weakref.ProxyType):
            #                 sysite = obj
            #         if isinstance(obj, DatabasePool):
            #             print(sys.getrefcount(obj), "refs to", obj)
            #             if not isinstance(obj, weakref.ProxyType):
            #                 dbpool = obj
            #     except Exception:
            #         pass
            #
            # print(sys.getrefcount(hs_ref()), "refs to", hs_ref())
            #
            # # The following values for `max_depth` and `too_many` have been found to
            # # render a useful amount of information without taking an overly long time
            # # to generate the result.
            # objgraph.show_backrefs(synapse_hs, max_depth=10, too_many=10)

    def test_clean_homeserver_shutdown_mid_background_updates(self) -> None:
        """Ensure the `SynapseHomeServer` can be fully shutdown and garbage collected
        before background updates have completed"""
        self.reactor, self.clock = get_clock()
        self.hs = setup_test_homeserver(
            self.addCleanup,
            reactor=self.reactor,
            homeserver_to_use=SynapseHomeServer,
            clock=self.clock,
        )

        # Pump the background updates by a single iteration, just to ensure any extra
        # resources it uses have been started.
        store = weakref.proxy(self.hs.get_datastores().main)
        self.get_success(store.db_pool.updates.do_next_background_update(False), by=0.1)

        hs_ref = weakref.ref(self.hs)

        # Run the reactor so any `callWhenRunning` functions can be cleared out.
        self.reactor.run()
        # Also advance the reactor by the delay tracking threshold to ensure all
        # cancellable delayed calls have been scheduled. Must be done prior to
        # `hs.shutdown()` otherwise they will be scheduled later during the test when we
        # advance the reactor to wait out any non-tracked delayed calls.
        self.reactor.advance(CALL_LATER_DELAY_TRACKING_THRESHOLD_S)

        # Ensure the background updates are not complete.
        self.assertNotEqual(store.db_pool.updates.get_status(), UpdaterStatus.COMPLETE)

        # Cleanup the homeserver.
        self.get_success(self.hs.shutdown())

        # Cleanup the internal reference in our test case
        del self.hs

        # Force garbage collection.
        gc.collect()

        # Ensure the `HomeServer` hs been garbage collected by attempting to use the
        # weakref to it.
        if hs_ref() is not None:
            self.fail("HomeServer reference should not be valid at this point")

            # To help debug this test when it fails, it is useful to leverage the
            # `objgraph` module.
            # The following code serves as an example of what I have found to be useful
            # when tracking down references holding the `SynapseHomeServer` in memory:
            #
            # all_objects = gc.get_objects()
            # for obj in all_objects:
            #     try:
            #         # These are a subset of types that are typically involved with
            #         # holding the `HomeServer` in memory. You may want to inspect
            #         # other types as well.
            #         if isinstance(obj, DataStore):
            #             print(sys.getrefcount(obj), "refs to", obj)
            #             if not isinstance(obj, weakref.ProxyType):
            #                 db_obj = obj
            #         if isinstance(obj, SynapseHomeServer):
            #             print(sys.getrefcount(obj), "refs to", obj)
            #             if not isinstance(obj, weakref.ProxyType):
            #                 synapse_hs = obj
            #         if isinstance(obj, SynapseSite):
            #             print(sys.getrefcount(obj), "refs to", obj)
            #             if not isinstance(obj, weakref.ProxyType):
            #                 sysite = obj
            #         if isinstance(obj, DatabasePool):
            #             print(sys.getrefcount(obj), "refs to", obj)
            #             if not isinstance(obj, weakref.ProxyType):
            #                 dbpool = obj
            #     except Exception:
            #         pass
            #
            # print(sys.getrefcount(hs_ref()), "refs to", hs_ref())
            #
            # # The following values for `max_depth` and `too_many` have been found to
            # # render a useful amount of information without taking an overly long time
            # # to generate the result.
            # objgraph.show_backrefs(synapse_hs, max_depth=10, too_many=10)
