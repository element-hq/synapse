#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
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

from collections import OrderedDict
from typing import Hashable, Optional, Tuple

from synapse.api.errors import LimitExceededError
from synapse.config.ratelimiting import RatelimitSettings
from synapse.storage.databases.main import DataStore
from synapse.types import Requester
from synapse.util import Clock


class Ratelimiter:
    """
    Ratelimit actions marked by arbitrary keys.

    (Note that the source code speaks of "actions" and "burst_count" rather than
    "tokens" and a "bucket_size".)

    This is a "leaky bucket as a meter". For each key to be tracked there is a bucket
    containing some number 0 <= T <= `burst_count` of tokens corresponding to previously
    permitted requests for that key. Each bucket starts empty, and gradually leaks
    tokens at a rate of `rate_hz`.

    Upon an incoming request, we must determine:
    - the key that this request falls under (which bucket to inspect), and
    - the cost C of this request in tokens.
    Then, if there is room in the bucket for C tokens (T + C <= `burst_count`),
    the request is permitted and `cost` tokens are added to the bucket.
    Otherwise, the request is denied, and the bucket continues to hold T tokens.

    This means that the limiter enforces an average request frequency of `rate_hz`,
    while accumulating a buffer of up to `burst_count` requests which can be consumed
    instantaneously.

    The tricky bit is the leaking. We do not want to have a periodic process which
    leaks every bucket! Instead, we track
    - the time point when the bucket was last completely empty, and
    - how many tokens have added to the bucket permitted since then.
    Then for each incoming request, we can calculate how many tokens have leaked
    since this time point, and use that to decide if we should accept or reject the
    request.

    Args:
        store: The datastore providing get_ratelimit_for_user.
        clock: A homeserver clock, for retrieving the current time
        cfg: The ratelimit configuration for this rate limiter including the
            allowed rate and burst count.
    """

    def __init__(
        self,
        store: DataStore,
        clock: Clock,
        cfg: RatelimitSettings,
    ):
        self.clock = clock
        self.rate_hz = cfg.per_second
        self.burst_count = cfg.burst_count
        self.store = store
        self._limiter_name = cfg.key

        # An ordered dictionary representing the token buckets tracked by this rate
        # limiter. Each entry maps a key of arbitrary type to a tuple representing:
        #   * The number of tokens currently in the bucket,
        #   * The time point when the bucket was last completely empty, and
        #   * The rate_hz (leak rate) of this particular bucket.
        self.actions: OrderedDict[Hashable, Tuple[float, float, float]] = OrderedDict()

    def _get_key(
        self, requester: Optional[Requester], key: Optional[Hashable]
    ) -> Hashable:
        """Use the requester's MXID as a fallback key if no key is provided."""
        if key is None:
            if not requester:
                raise ValueError("Must supply at least one of `requester` or `key`")

            key = requester.user.to_string()
        return key

    def _get_action_counts(
        self, key: Hashable, time_now_s: float
    ) -> Tuple[float, float, float]:
        """Retrieve the action counts, with a fallback representing an empty bucket."""
        return self.actions.get(key, (0.0, time_now_s, 0.0))

    async def can_do_action(
        self,
        requester: Optional[Requester],
        key: Optional[Hashable] = None,
        rate_hz: Optional[float] = None,
        burst_count: Optional[int] = None,
        update: bool = True,
        n_actions: int = 1,
        _time_now_s: Optional[float] = None,
    ) -> Tuple[bool, float]:
        """Can the entity (e.g. user or IP address) perform the action?

        Checks if the user has ratelimiting disabled in the database by looking
        for null/zero values in the `ratelimit_override` table. (Non-zero
        values aren't honoured, as they're specific to the event sending
        ratelimiter, rather than all ratelimiters)

        Args:
            requester: The requester that is doing the action, if any. Used to check
                if the user has ratelimits disabled in the database.
            key: An arbitrary key used to classify an action. Defaults to the
                requester's user ID.
            rate_hz: The long term number of actions that can be performed in a second.
                Overrides the value set during instantiation if set.
            burst_count: How many actions that can be performed before being limited.
                Overrides the value set during instantiation if set.
            update: Whether to count this check as performing the action. If the action
                cannot be performed, the user's action count is not incremented at all.
            n_actions: The number of times the user wants to do this action. If the user
                cannot do all of the actions, the user's action count is not incremented
                at all.
            _time_now_s: The current time. Optional, defaults to the current time according
                to self.clock. Only used by tests.

        Returns:
            A tuple containing:
                * A bool indicating if they can perform the action now
                * The reactor timestamp for when the action can be performed next.
                  -1 if rate_hz is less than or equal to zero
        """
        key = self._get_key(requester, key)

        if requester:
            # Disable rate limiting of users belonging to any AS that is configured
            # not to be rate limited in its registration file (rate_limited: true|false).
            if requester.app_service and not requester.app_service.is_rate_limited():
                return True, -1.0

            # Check if ratelimiting has been disabled for the user.
            #
            # Note that we don't use the returned rate/burst count, as the table
            # is specifically for the event sending ratelimiter. Instead, we
            # only use it to (somewhat cheekily) infer whether the user should
            # be subject to any rate limiting or not.
            override = await self.store.get_ratelimit_for_user(
                requester.authenticated_entity
            )
            if override and not override.messages_per_second:
                return True, -1.0

        # Override default values if set
        time_now_s = _time_now_s if _time_now_s is not None else self.clock.time()
        rate_hz = rate_hz if rate_hz is not None else self.rate_hz
        burst_count = burst_count if burst_count is not None else self.burst_count

        # Remove any expired entries
        self._prune_message_counts(time_now_s)

        # Check if there is an existing count entry for this key
        action_count, time_start, _ = self._get_action_counts(key, time_now_s)

        # Check whether performing another action is allowed
        time_delta = time_now_s - time_start
        performed_count = action_count - time_delta * rate_hz
        if performed_count < 0:
            performed_count = 0

            # Reset the start time and forgive all actions
            action_count = 0
            time_start = time_now_s

        # This check would be easier read as performed_count + n_actions > burst_count,
        # but performed_count might be a very precise float (with lots of numbers
        # following the point) in which case Python might round it up when adding it to
        # n_actions. Writing it this way ensures it doesn't happen.
        if performed_count > burst_count - n_actions:
            # Deny, we have exceeded our burst count
            allowed = False
        else:
            # We haven't reached our limit yet
            allowed = True
            action_count = action_count + n_actions

        if update:
            self.actions[key] = (action_count, time_start, rate_hz)

        if rate_hz > 0:
            # Find out when the count of existing actions expires
            time_allowed = time_start + (action_count - burst_count + 1) / rate_hz

            # Don't give back a time in the past
            if time_allowed < time_now_s:
                time_allowed = time_now_s

        else:
            # XXX: Why is this -1? This seems to only be used in
            # self.ratelimit. I guess so that clients get a time in the past and don't
            # feel afraid to try again immediately
            time_allowed = -1

        return allowed, time_allowed

    def record_action(
        self,
        requester: Optional[Requester],
        key: Optional[Hashable] = None,
        n_actions: int = 1,
        _time_now_s: Optional[float] = None,
    ) -> None:
        """Record that an action(s) took place, even if they violate the rate limit.

        This is useful for tracking the frequency of events that happen across
        federation which we still want to impose local rate limits on. For instance, if
        we are alice.com monitoring a particular room, we cannot prevent bob.com
        from joining users to that room. However, we can track the number of recent
        joins in the room and refuse to serve new joins ourselves if there have been too
        many in the room across both homeservers.

        Args:
            requester: The requester that is doing the action, if any.
            key: An arbitrary key used to classify an action. Defaults to the
                requester's user ID.
            n_actions: The number of times the user performed the action. May be negative
                to "refund" the rate limit.
            _time_now_s: The current time. Optional, defaults to the current time according
                to self.clock. Only used by tests.
        """
        key = self._get_key(requester, key)
        time_now_s = _time_now_s if _time_now_s is not None else self.clock.time()
        action_count, time_start, rate_hz = self._get_action_counts(key, time_now_s)
        self.actions[key] = (action_count + n_actions, time_start, rate_hz)

    def _prune_message_counts(self, time_now_s: float) -> None:
        """Remove message count entries that have not exceeded their defined
        rate_hz limit

        Args:
            time_now_s: The current time
        """
        # We create a copy of the key list here as the dictionary is modified during
        # the loop
        for key in list(self.actions.keys()):
            action_count, time_start, rate_hz = self.actions[key]

            # Rate limit = "seconds since we started limiting this action" * rate_hz
            # If this limit has not been exceeded, wipe our record of this action
            time_delta = time_now_s - time_start
            if action_count - time_delta * rate_hz > 0:
                continue
            else:
                del self.actions[key]

    async def ratelimit(
        self,
        requester: Optional[Requester],
        key: Optional[Hashable] = None,
        rate_hz: Optional[float] = None,
        burst_count: Optional[int] = None,
        update: bool = True,
        n_actions: int = 1,
        _time_now_s: Optional[float] = None,
    ) -> None:
        """Checks if an action can be performed. If not, raises a LimitExceededError

        Checks if the user has ratelimiting disabled in the database by looking
        for null/zero values in the `ratelimit_override` table. (Non-zero
        values aren't honoured, as they're specific to the event sending
        ratelimiter, rather than all ratelimiters)

        Args:
            requester: The requester that is doing the action, if any. Used to check for
                if the user has ratelimits disabled.
            key: An arbitrary key used to classify an action. Defaults to the
                requester's user ID.
            rate_hz: The long term number of actions that can be performed in a second.
                Overrides the value set during instantiation if set.
            burst_count: How many actions that can be performed before being limited.
                Overrides the value set during instantiation if set.
            update: Whether to count this check as performing the action
            n_actions: The number of times the user wants to do this action. If the user
                cannot do all of the actions, the user's action count is not incremented
                at all.
            _time_now_s: The current time. Optional, defaults to the current time according
                to self.clock. Only used by tests.

        Raises:
            LimitExceededError: If an action could not be performed, along with the time in
                milliseconds until the action can be performed again
        """
        time_now_s = _time_now_s if _time_now_s is not None else self.clock.time()

        allowed, time_allowed = await self.can_do_action(
            requester,
            key,
            rate_hz=rate_hz,
            burst_count=burst_count,
            update=update,
            n_actions=n_actions,
            _time_now_s=time_now_s,
        )

        if not allowed:
            # We pause for a bit here to stop clients from "tight-looping" on
            # retrying their request.
            await self.clock.sleep(0.5)

            raise LimitExceededError(
                limiter_name=self._limiter_name,
                retry_after_ms=int(1000 * (time_allowed - time_now_s)),
            )


class RequestRatelimiter:
    def __init__(
        self,
        store: DataStore,
        clock: Clock,
        rc_message: RatelimitSettings,
        rc_admin_redaction: Optional[RatelimitSettings],
    ):
        self.store = store
        self.clock = clock

        # The rate_hz and burst_count are overridden on a per-user basis
        self.request_ratelimiter = Ratelimiter(
            store=self.store,
            clock=self.clock,
            cfg=RatelimitSettings(key=rc_message.key, per_second=0, burst_count=0),
        )
        self._rc_message = rc_message

        # Check whether ratelimiting room admin message redaction is enabled
        # by the presence of rate limits in the config
        if rc_admin_redaction:
            self.admin_redaction_ratelimiter: Optional[Ratelimiter] = Ratelimiter(
                store=self.store,
                clock=self.clock,
                cfg=rc_admin_redaction,
            )
        else:
            self.admin_redaction_ratelimiter = None

    async def ratelimit(
        self,
        requester: Requester,
        update: bool = True,
        is_admin_redaction: bool = False,
        n_actions: int = 1,
    ) -> None:
        """Ratelimits requests.

        Args:
            requester
            update: Whether to record that a request is being processed.
                Set to False when doing multiple checks for one request (e.g.
                to check up front if we would reject the request), and set to
                True for the last call for a given request.
            is_admin_redaction: Whether this is a room admin/moderator
                redacting an event. If so then we may apply different
                ratelimits depending on config.
            n_actions: Multiplier for the number of actions to apply to the
                rate limiter at once.

        Raises:
            LimitExceededError if the request should be ratelimited
        """
        user_id = requester.user.to_string()

        # The AS user itself is never rate limited.
        app_service = self.store.get_app_service_by_user_id(user_id)
        if app_service is not None:
            return  # do not ratelimit app service senders

        messages_per_second = self._rc_message.per_second
        burst_count = self._rc_message.burst_count

        # Check if there is a per user override in the DB.
        override = await self.store.get_ratelimit_for_user(user_id)
        if override:
            # If overridden with a null Hz then ratelimiting has been entirely
            # disabled for the user
            if not override.messages_per_second:
                return

            messages_per_second = override.messages_per_second
            burst_count = override.burst_count

        if is_admin_redaction and self.admin_redaction_ratelimiter:
            # If we have separate config for admin redactions, use a separate
            # ratelimiter as to not have user_ids clash
            await self.admin_redaction_ratelimiter.ratelimit(
                requester, update=update, n_actions=n_actions
            )
        else:
            # Override rate and burst count per-user
            await self.request_ratelimiter.ratelimit(
                requester,
                rate_hz=messages_per_second,
                burst_count=burst_count,
                update=update,
                n_actions=n_actions,
            )
