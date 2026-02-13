#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

from twisted.internet import defer
from twisted.internet.defer import Deferred

from synapse.config.homeserver import HomeServerConfig
from synapse.config.ratelimiting import FederationRatelimitSettings
from synapse.util.ratelimitutils import FederationRateLimiter

from tests.server import ThreadedMemoryReactorClock, get_clock
from tests.unittest import TestCase
from tests.utils import default_config


class FederationRateLimiterTestCase(TestCase):
    def test_ratelimit(self) -> None:
        """A simple test with the default values"""
        reactor, clock = get_clock()
        rc_config = build_rc_config()
        ratelimiter = FederationRateLimiter("test_server", clock, rc_config)

        with ratelimiter.ratelimit("testhost") as d1:
            # shouldn't block
            self.successResultOf(d1)

    def test_concurrent_limit(self) -> None:
        """Test what happens when we hit the concurrent limit"""
        reactor, clock = get_clock()
        rc_config = build_rc_config({"rc_federation": {"concurrent": 2}})
        ratelimiter = FederationRateLimiter("test_server", clock, rc_config)

        with ratelimiter.ratelimit("testhost") as d1:
            # shouldn't block
            self.successResultOf(d1)

            cm2 = ratelimiter.ratelimit("testhost")
            d2 = cm2.__enter__()
            # also shouldn't block
            self.successResultOf(d2)

            cm3 = ratelimiter.ratelimit("testhost")
            d3 = cm3.__enter__()
            # this one should block, though ...
            self.assertNoResult(d3)

            # ... until we complete an earlier request
            cm2.__exit__(None, None, None)
            reactor.advance(0.0)
            self.successResultOf(d3)

    def test_sleep_limit(self) -> None:
        """Test what happens when we hit the sleep limit"""
        reactor, clock = get_clock()
        rc_config = build_rc_config(
            {"rc_federation": {"sleep_limit": 2, "sleep_delay": 500}}
        )
        ratelimiter = FederationRateLimiter("test_server", clock, rc_config)

        with ratelimiter.ratelimit("testhost") as d1:
            # shouldn't block
            self.successResultOf(d1)

        with ratelimiter.ratelimit("testhost") as d2:
            # nor this
            self.successResultOf(d2)

        with ratelimiter.ratelimit("testhost") as d3:
            # this one should block, though ...
            self.assertNoResult(d3)
            sleep_time = _await_resolution(reactor, d3)
            self.assertAlmostEqual(sleep_time, 500, places=3)

    def test_lots_of_queued_things(self) -> None:
        """Tests lots of synchronous things queued up behind a slow thing.

        The stack should *not* explode when the slow thing completes.
        """
        reactor, clock = get_clock()
        rc_config = build_rc_config(
            {
                "rc_federation": {
                    "sleep_limit": 1000000000,  # never sleep
                    "reject_limit": 1000000000,  # never reject requests
                    "concurrent": 1,
                }
            }
        )
        ratelimiter = FederationRateLimiter("test_server", clock, rc_config)

        with ratelimiter.ratelimit("testhost") as d:
            # shouldn't block
            self.successResultOf(d)

            async def task() -> None:
                with ratelimiter.ratelimit("testhost") as d:
                    await d

            for _ in range(1, 100):
                defer.ensureDeferred(task())

            last_task = defer.ensureDeferred(task())

            # Upon exiting the context manager, all the synchronous things will resume.
            # If a stack overflow occurs, the final task will not complete.

        # Wait for all the things to complete.
        reactor.advance(0.0)
        self.successResultOf(last_task)


def _await_resolution(reactor: ThreadedMemoryReactorClock, d: Deferred) -> float:
    """advance the clock until the deferred completes.

    Returns the number of milliseconds it took to complete.
    """
    start_time = reactor.seconds()
    while not d.called:
        reactor.advance(0.01)
    return (reactor.seconds() - start_time) * 1000


def build_rc_config(settings: dict | None = None) -> FederationRatelimitSettings:
    config_dict = default_config("test")
    config_dict.update(settings or {})
    config = HomeServerConfig()
    config.parse_config_dict(config_dict, "", "")
    return config.ratelimiting.rc_federation
