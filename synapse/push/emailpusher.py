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

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

from twisted.internet.error import AlreadyCalled, AlreadyCancelled
from twisted.internet.interfaces import IDelayedCall

from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.push import Pusher, PusherConfig, PusherConfigException, ThrottleParams
from synapse.push.mailer import Mailer
from synapse.push.push_types import EmailReason
from synapse.storage.databases.main.event_push_actions import EmailPushAction
from synapse.util.threepids import validate_email

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# THROTTLE is the minimum time between mail notifications sent for a given room.
# Each room maintains its own throttle counter, but each new mail notification
# sends the pending notifications for all rooms.
THROTTLE_MAX_MS = 24 * 60 * 60 * 1000  # 24h
# THROTTLE_MULTIPLIER = 6              # 10 mins, 1 hour, 6 hours, 24 hours
THROTTLE_MULTIPLIER = 144  # 10 mins, 24 hours - i.e. jump straight to 1 day

# If no event triggers a notification for this long after the previous,
# the throttle is released.
# 12 hours - a gap of 12 hours in conversation is surely enough to merit a new
# notification when things get going again...
THROTTLE_RESET_AFTER_MS = 12 * 60 * 60 * 1000

# does each email include all unread notifs, or just the ones which have happened
# since the last mail?
# XXX: this is currently broken as it includes ones from parted rooms(!)
INCLUDE_ALL_UNREAD_NOTIFS = False


class EmailPusher(Pusher):
    """
    A pusher that sends email notifications about events (approximately)
    when they happen.
    This shares quite a bit of code with httpusher: it would be good to
    factor out the common parts
    """

    def __init__(self, hs: "HomeServer", pusher_config: PusherConfig, mailer: Mailer):
        super().__init__(hs, pusher_config)
        self.mailer = mailer

        self.server_name = hs.hostname
        self.store = self.hs.get_datastores().main
        self.email = pusher_config.pushkey
        self.timed_call: Optional[IDelayedCall] = None
        self.throttle_params: Dict[str, ThrottleParams] = {}
        self._inited = False

        self._is_processing = False

        # Make sure that the email is valid.
        try:
            validate_email(self.email)
        except ValueError:
            raise PusherConfigException("Invalid email")

        self._delay_before_mail_ms = self.hs.config.email.notif_delay_before_mail_ms

    def on_started(self, should_check_for_notifs: bool) -> None:
        """Called when this pusher has been started.

        Args:
            should_check_for_notifs: Whether we should immediately
                check for push to send. Set to False only if it's known there
                is nothing to send
        """
        if should_check_for_notifs and self.mailer is not None:
            self._start_processing()

    def on_stop(self) -> None:
        if self.timed_call:
            try:
                self.timed_call.cancel()
            except (AlreadyCalled, AlreadyCancelled):
                pass
            self.timed_call = None

    def on_new_receipts(self) -> None:
        # We could wake up and cancel the timer but there tend to be quite a
        # lot of read receipts so it's probably less work to just let the
        # timer fire
        pass

    def on_timer(self) -> None:
        self.timed_call = None
        self._start_processing()

    def _start_processing(self) -> None:
        if self._is_processing:
            return

        run_as_background_process("emailpush.process", self.server_name, self._process)

    def _pause_processing(self) -> None:
        """Used by tests to temporarily pause processing of events.

        Asserts that its not currently processing.
        """
        assert not self._is_processing
        self._is_processing = True

    def _resume_processing(self) -> None:
        """Used by tests to resume processing of events after pausing."""
        assert self._is_processing
        self._is_processing = False
        self._start_processing()

    async def _process(self) -> None:
        # we should never get here if we are already processing
        assert not self._is_processing

        try:
            self._is_processing = True

            if not self._inited:
                # this is our first loop: load up the throttle params
                assert self.pusher_id is not None
                self.throttle_params = await self.store.get_throttle_params_by_room(
                    self.pusher_id
                )
                self._inited = True

            # if the max ordering changes while we're running _unsafe_process,
            # call it again, and so on until we've caught up.
            while True:
                starting_max_ordering = self.max_stream_ordering
                try:
                    await self._unsafe_process()
                except Exception:
                    logger.exception("Exception processing notifs")
                if self.max_stream_ordering == starting_max_ordering:
                    break
        finally:
            self._is_processing = False

    async def _unsafe_process(self) -> None:
        """
        Main logic of the push loop without the wrapper function that sets
        up logging, measures and guards against multiple instances of it
        being run.
        """
        start = 0 if INCLUDE_ALL_UNREAD_NOTIFS else self.last_stream_ordering
        unprocessed = (
            await self.store.get_unread_push_actions_for_user_in_range_for_email(
                self.user_id, start, self.max_stream_ordering
            )
        )

        soonest_due_at: Optional[int] = None

        if not unprocessed:
            await self.save_last_stream_ordering_and_success(self.max_stream_ordering)
            return

        for push_action in unprocessed:
            received_at = push_action.received_ts
            if received_at is None:
                received_at = 0
            notif_ready_at = received_at + self._delay_before_mail_ms

            room_ready_at = self.room_ready_to_notify_at(push_action.room_id)

            should_notify_at = max(notif_ready_at, room_ready_at)

            if should_notify_at <= self.clock.time_msec():
                # one of our notifications is ready for sending, so we send
                # *one* email updating the user on their notifications,
                # we then consider all previously outstanding notifications
                # to be delivered.

                reason: EmailReason = {
                    "room_id": push_action.room_id,
                    "now": self.clock.time_msec(),
                    "received_at": received_at,
                    "delay_before_mail_ms": self._delay_before_mail_ms,
                    "last_sent_ts": self.get_room_last_sent_ts(push_action.room_id),
                    "throttle_ms": self.get_room_throttle_ms(push_action.room_id),
                }

                await self.send_notification(unprocessed, reason)

                await self.save_last_stream_ordering_and_success(
                    max(ea.stream_ordering for ea in unprocessed)
                )

                # we update the throttle on all the possible unprocessed push actions
                for ea in unprocessed:
                    await self.sent_notif_update_throttle(ea.room_id, ea)
                break
            else:
                if soonest_due_at is None or should_notify_at < soonest_due_at:
                    soonest_due_at = should_notify_at

                if self.timed_call is not None:
                    try:
                        self.timed_call.cancel()
                    except (AlreadyCalled, AlreadyCancelled):
                        pass
                    self.timed_call = None

        if soonest_due_at is not None:
            self.timed_call = self.hs.get_reactor().callLater(
                self.seconds_until(soonest_due_at), self.on_timer
            )

    async def save_last_stream_ordering_and_success(
        self, last_stream_ordering: int
    ) -> None:
        self.last_stream_ordering = last_stream_ordering
        pusher_still_exists = (
            await self.store.update_pusher_last_stream_ordering_and_success(
                self.app_id,
                self.email,
                self.user_id,
                last_stream_ordering,
                self.clock.time_msec(),
            )
        )
        if not pusher_still_exists:
            # The pusher has been deleted while we were processing, so
            # lets just stop and return.
            self.on_stop()

    def seconds_until(self, ts_msec: int) -> float:
        secs = (ts_msec - self.clock.time_msec()) / 1000
        return max(secs, 0)

    def get_room_throttle_ms(self, room_id: str) -> int:
        if room_id in self.throttle_params:
            return self.throttle_params[room_id].throttle_ms
        else:
            return 0

    def get_room_last_sent_ts(self, room_id: str) -> int:
        if room_id in self.throttle_params:
            return self.throttle_params[room_id].last_sent_ts
        else:
            return 0

    def room_ready_to_notify_at(self, room_id: str) -> int:
        """
        Determines whether throttling should prevent us from sending an email
        for the given room

        Returns:
            The timestamp when we are next allowed to send an email notif
            for this room
        """
        last_sent_ts = self.get_room_last_sent_ts(room_id)
        throttle_ms = self.get_room_throttle_ms(room_id)

        may_send_at = last_sent_ts + throttle_ms
        return may_send_at

    async def sent_notif_update_throttle(
        self, room_id: str, notified_push_action: EmailPushAction
    ) -> None:
        # We have sent a notification, so update the throttle accordingly.
        # If the event that triggered the notif happened more than
        # THROTTLE_RESET_AFTER_MS after the previous one that triggered a
        # notif, we release the throttle. Otherwise, the throttle is increased.
        time_of_previous_notifs = await self.store.get_time_of_last_push_action_before(
            notified_push_action.stream_ordering
        )

        time_of_this_notifs = notified_push_action.received_ts

        if time_of_previous_notifs is not None and time_of_this_notifs is not None:
            gap = time_of_this_notifs - time_of_previous_notifs
        else:
            # if we don't know the arrival time of one of the notifs (it was not
            # stored prior to email notification code) then assume a gap of
            # zero which will just not reset the throttle
            gap = 0

        current_throttle_ms = self.get_room_throttle_ms(room_id)

        if gap > THROTTLE_RESET_AFTER_MS:
            new_throttle_ms = self._delay_before_mail_ms
        else:
            if current_throttle_ms == 0:
                new_throttle_ms = self._delay_before_mail_ms
            else:
                new_throttle_ms = min(
                    current_throttle_ms * THROTTLE_MULTIPLIER, THROTTLE_MAX_MS
                )
        self.throttle_params[room_id] = ThrottleParams(
            self.clock.time_msec(),
            new_throttle_ms,
        )
        assert self.pusher_id is not None
        await self.store.set_throttle_params(
            self.pusher_id, room_id, self.throttle_params[room_id]
        )

    async def send_notification(
        self, push_actions: List[EmailPushAction], reason: EmailReason
    ) -> None:
        logger.info("Sending notif email for user %r", self.user_id)

        await self.mailer.send_notification_mail(
            self.app_id, self.user_id, self.email, push_actions, reason
        )
