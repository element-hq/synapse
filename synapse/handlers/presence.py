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

"""
This module is responsible for keeping track of presence status of local
and remote users.

The methods that define policy are:
    - PresenceHandler._update_states
    - PresenceHandler._handle_timeouts
    - should_notify

# Tracking local presence

For local users, presence is tracked on a per-device basis. When a user has multiple
devices the user presence state is derived by coalescing the presence from each
device:

    BUSY > ONLINE > UNAVAILABLE > OFFLINE

The time that each device was last active and last synced is tracked in order to
automatically downgrade a device's presence state:

    A device may move from ONLINE -> UNAVAILABLE, if it has not been active for
    a period of time.

    A device may go from any state -> OFFLINE, if it is not active and has not
    synced for a period of time.

The timeouts are handled using a wheel timer, which has coarse buckets. Timings
do not need to be exact.

Generally a device's presence state is updated whenever a user syncs (via the
set_presence parameter), when the presence API is called, or if "pro-active"
events occur, including:

* Sending an event, receipt, read marker.
* Updating typing status.

The busy state has special status that it cannot is not downgraded by a call to
sync with a lower priority state *and* it takes a long period of time to transition
to offline.

# Persisting (and restoring) presence

For all users, presence is persisted on a per-user basis. Data is kept in-memory
and persisted periodically. When Synapse starts each worker loads the current
presence state and then tracks the presence stream to keep itself up-to-date.

When restoring presence for local users a pseudo-device is created to match the
user state; this device follows the normal timeout logic (see above) and will
automatically be replaced with any information from currently available devices.

"""

import abc
import contextlib
import itertools
import logging
from bisect import bisect
from contextlib import contextmanager
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Any,
    Callable,
    Collection,
    ContextManager,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Type,
)

from prometheus_client import Counter

import synapse.metrics
from synapse.api.constants import EduTypes, EventTypes, Membership, PresenceState
from synapse.api.errors import SynapseError
from synapse.api.presence import UserDevicePresenceState, UserPresenceState
from synapse.appservice import ApplicationService
from synapse.events.presence_router import PresenceRouter
from synapse.logging.context import run_in_background
from synapse.metrics import LaterGauge
from synapse.metrics.background_process_metrics import (
    run_as_background_process,
    wrap_as_background_process,
)
from synapse.replication.http.presence import (
    ReplicationBumpPresenceActiveTime,
    ReplicationPresenceSetState,
)
from synapse.replication.http.streams import ReplicationGetStreamUpdates
from synapse.replication.tcp.commands import ClearUserSyncsCommand
from synapse.replication.tcp.streams import PresenceFederationStream, PresenceStream
from synapse.storage.databases.main import DataStore
from synapse.storage.databases.main.state_deltas import StateDelta
from synapse.streams import EventSource
from synapse.types import (
    JsonDict,
    StrCollection,
    StreamKeyType,
    UserID,
    get_domain_from_id,
)
from synapse.util.async_helpers import Linearizer
from synapse.util.metrics import Measure
from synapse.util.wheel_timer import WheelTimer

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


notified_presence_counter = Counter("synapse_handler_presence_notified_presence", "")
federation_presence_out_counter = Counter(
    "synapse_handler_presence_federation_presence_out", ""
)
presence_updates_counter = Counter("synapse_handler_presence_presence_updates", "")
timers_fired_counter = Counter("synapse_handler_presence_timers_fired", "")
federation_presence_counter = Counter(
    "synapse_handler_presence_federation_presence", ""
)
bump_active_time_counter = Counter("synapse_handler_presence_bump_active_time", "")

get_updates_counter = Counter("synapse_handler_presence_get_updates", "", ["type"])

notify_reason_counter = Counter(
    "synapse_handler_presence_notify_reason", "", ["locality", "reason"]
)
state_transition_counter = Counter(
    "synapse_handler_presence_state_transition", "", ["locality", "from", "to"]
)

# If a user was last active in the last LAST_ACTIVE_GRANULARITY, consider them
# "currently_active"
LAST_ACTIVE_GRANULARITY = 60 * 1000

# How long to wait until a new /events or /sync request before assuming
# the client has gone.
SYNC_ONLINE_TIMEOUT = 30 * 1000
# Busy status waits longer, but does eventually go offline.
BUSY_ONLINE_TIMEOUT = 60 * 60 * 1000

# How long to wait before marking the user as idle. Compared against last active
IDLE_TIMER = 5 * 60 * 1000

# How often we expect remote servers to resend us presence.
FEDERATION_TIMEOUT = 30 * 60 * 1000

# How often to resend presence to remote servers
FEDERATION_PING_INTERVAL = 25 * 60 * 1000

# How long we will wait before assuming that the syncs from an external process
# are dead.
EXTERNAL_PROCESS_EXPIRY = 5 * 60 * 1000

# Delay before a worker tells the presence handler that a user has stopped
# syncing.
UPDATE_SYNCING_USERS_MS = 10 * 1000

assert LAST_ACTIVE_GRANULARITY < IDLE_TIMER


class BasePresenceHandler(abc.ABC):
    """Parts of the PresenceHandler that are shared between workers and presence
    writer"""

    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.clock = hs.get_clock()
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self.presence_router = hs.get_presence_router()
        self.state = hs.get_state_handler()
        self.is_mine_id = hs.is_mine_id

        self._presence_enabled = hs.config.server.presence_enabled
        self._track_presence = hs.config.server.track_presence

        self._federation = None
        if hs.should_send_federation():
            self._federation = hs.get_federation_sender()

        self._federation_queue = PresenceFederationQueue(hs, self)

        self.VALID_PRESENCE: Tuple[str, ...] = (
            PresenceState.ONLINE,
            PresenceState.UNAVAILABLE,
            PresenceState.OFFLINE,
        )

        if hs.config.experimental.msc3026_enabled:
            self.VALID_PRESENCE += (PresenceState.BUSY,)

        active_presence = self.store.take_presence_startup_info()
        # The combined status across all user devices.
        self.user_to_current_state = {state.user_id: state for state in active_presence}

    @abc.abstractmethod
    async def user_syncing(
        self,
        user_id: str,
        device_id: Optional[str],
        affect_presence: bool,
        presence_state: str,
    ) -> ContextManager[None]:
        """Returns a context manager that should surround any stream requests
        from the user.

        This allows us to keep track of who is currently streaming and who isn't
        without having to have timers outside of this module to avoid flickering
        when users disconnect/reconnect.

        Args:
            user_id: the user that is starting a sync
            device_id: the user's device that is starting a sync
            affect_presence: If false this function will be a no-op.
                Useful for streams that are not associated with an actual
                client that is being used by a user.
            presence_state: The presence state indicated in the sync request
        """

    @abc.abstractmethod
    def get_currently_syncing_users_for_replication(
        self,
    ) -> Iterable[Tuple[str, Optional[str]]]:
        """Get an iterable of syncing users and devices on this worker, to send to the presence handler

        This is called when a replication connection is established. It should return
        a list of tuples of user ID & device ID, which are then sent as USER_SYNC commands
        to inform the process handling presence about those users/devices.

        Returns:
            An iterable of tuples of user ID and device ID.
        """

    async def get_state(self, target_user: UserID) -> UserPresenceState:
        results = await self.get_states([target_user.to_string()])
        return results[0]

    async def get_states(
        self, target_user_ids: Iterable[str]
    ) -> List[UserPresenceState]:
        """Get the presence state for users."""

        updates_d = await self.current_state_for_users(target_user_ids)
        updates = list(updates_d.values())

        for user_id in set(target_user_ids) - {u.user_id for u in updates}:
            updates.append(UserPresenceState.default(user_id))

        return updates

    async def current_state_for_users(
        self, user_ids: Iterable[str]
    ) -> Dict[str, UserPresenceState]:
        """Get the current presence state for multiple users.

        Returns:
            A mapping of `user_id` -> `UserPresenceState`
        """
        states = {}
        missing = []
        for user_id in user_ids:
            state = self.user_to_current_state.get(user_id, None)
            if state:
                states[user_id] = state
            else:
                missing.append(user_id)

        if missing:
            # There are things not in our in memory cache. Lets pull them out of
            # the database.
            res = await self.store.get_presence_for_users(missing)
            states.update(res)

            for user_id in missing:
                # if user has no state in database, create the state
                if not res.get(user_id, None):
                    new_state = UserPresenceState.default(user_id)
                    states[user_id] = new_state
                    self.user_to_current_state[user_id] = new_state

        return states

    async def current_state_for_user(self, user_id: str) -> UserPresenceState:
        """Get the current presence state for a user."""
        res = await self.current_state_for_users([user_id])
        return res[user_id]

    @abc.abstractmethod
    async def set_state(
        self,
        target_user: UserID,
        device_id: Optional[str],
        state: JsonDict,
        force_notify: bool = False,
        is_sync: bool = False,
    ) -> None:
        """Set the presence state of the user.

        Args:
            target_user: The ID of the user to set the presence state of.
            device_id: the device that the user is setting the presence state of.
            state: The presence state as a JSON dictionary.
            force_notify: Whether to force notification of the update to clients.
            is_sync: True if this update was from a sync, which results in
                *not* overriding a previously set BUSY status, updating the
                user's last_user_sync_ts, and ignoring the "status_msg" field of
                the `state` dict.
        """

    @abc.abstractmethod
    async def bump_presence_active_time(
        self, user: UserID, device_id: Optional[str]
    ) -> None:
        """We've seen the user do something that indicates they're interacting
        with the app.
        """

    async def update_external_syncs_row(  # noqa: B027 (no-op by design)
        self,
        process_id: str,
        user_id: str,
        device_id: Optional[str],
        is_syncing: bool,
        sync_time_msec: int,
    ) -> None:
        """Update the syncing users for an external process as a delta.

        This is a no-op when presence is handled by a different worker.

        Args:
            process_id: An identifier for the process the users are
                syncing against. This allows synapse to process updates
                as user start and stop syncing against a given process.
            user_id: The user who has started or stopped syncing
            device_id: The user's device that has started or stopped syncing
            is_syncing: Whether or not the user is now syncing
            sync_time_msec: Time in ms when the user was last syncing
        """

    async def update_external_syncs_clear(  # noqa: B027 (no-op by design)
        self, process_id: str
    ) -> None:
        """Marks all users that had been marked as syncing by a given process
        as offline.

        Used when the process has stopped/disappeared.

        This is a no-op when presence is handled by a different worker.
        """

    async def process_replication_rows(
        self, stream_name: str, instance_name: str, token: int, rows: list
    ) -> None:
        """Process streams received over replication."""
        await self._federation_queue.process_replication_rows(
            stream_name, instance_name, token, rows
        )

    def get_federation_queue(self) -> "PresenceFederationQueue":
        """Get the presence federation queue."""
        return self._federation_queue

    async def maybe_send_presence_to_interested_destinations(
        self, states: List[UserPresenceState]
    ) -> None:
        """If this instance is a federation sender, send the states to all
        destinations that are interested. Filters out any states for remote
        users.
        """

        if not self._federation:
            return

        states = [s for s in states if self.is_mine_id(s.user_id)]

        if not states:
            return

        hosts_to_states = await get_interested_remotes(
            self.store,
            self.presence_router,
            states,
        )

        for destinations, host_states in hosts_to_states:
            await self._federation.send_presence_to_destinations(
                host_states, destinations
            )

    async def send_full_presence_to_users(self, user_ids: StrCollection) -> None:
        """
        Adds to the list of users who should receive a full snapshot of presence
        upon their next sync. Note that this only works for local users.

        Then, grabs the current presence state for a given set of users and adds it
        to the top of the presence stream.

        Args:
            user_ids: The IDs of the local users to send full presence to.
        """
        # Retrieve one of the users from the given set
        if not user_ids:
            raise Exception(
                "send_full_presence_to_users must be called with at least one user"
            )
        user_id = next(iter(user_ids))

        # Mark all users as receiving full presence on their next sync
        await self.store.add_users_to_send_full_presence_to(user_ids)

        # Add a new entry to the presence stream. Since we use stream tokens to determine whether a
        # local user should receive a full snapshot of presence when they sync, we need to bump the
        # presence stream so that subsequent syncs with no presence activity in between won't result
        # in the client receiving multiple full snapshots of presence.
        #
        # If we bump the stream ID, then the user will get a higher stream token next sync, and thus
        # correctly won't receive a second snapshot.

        # Get the current presence state for one of the users (defaults to offline if not found)
        current_presence_state = await self.get_state(UserID.from_string(user_id))

        # Convert the UserPresenceState object into a serializable dict
        state = {
            "presence": current_presence_state.state,
            "status_message": current_presence_state.status_msg,
        }

        # Copy the presence state to the tip of the presence stream.

        # We set force_notify=True here so that this presence update is guaranteed to
        # increment the presence stream ID (which resending the current user's presence
        # otherwise would not do).
        await self.set_state(
            UserID.from_string(user_id), None, state, force_notify=True
        )

    async def is_visible(self, observed_user: UserID, observer_user: UserID) -> bool:
        raise NotImplementedError(
            "Attempting to check presence on a non-presence worker."
        )


class _NullContextManager(ContextManager[None]):
    """A context manager which does nothing."""

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        pass


class WorkerPresenceHandler(BasePresenceHandler):
    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self._presence_writer_instance = hs.config.worker.writers.presence[0]

        # Route presence EDUs to the right worker
        hs.get_federation_registry().register_instances_for_edu(
            EduTypes.PRESENCE,
            hs.config.worker.writers.presence,
        )

        # The number of ongoing syncs on this process, by (user ID, device ID).
        # Empty if _presence_enabled is false.
        self._user_device_to_num_current_syncs: Dict[
            Tuple[str, Optional[str]], int
        ] = {}

        self.notifier = hs.get_notifier()
        self.instance_id = hs.get_instance_id()

        # (user_id, device_id) -> last_sync_ms. Lists the devices that have stopped
        # syncing but we haven't notified the presence writer of that yet
        self._user_devices_going_offline: Dict[Tuple[str, Optional[str]], int] = {}

        self._bump_active_client = ReplicationBumpPresenceActiveTime.make_client(hs)
        self._set_state_client = ReplicationPresenceSetState.make_client(hs)

        self._send_stop_syncing_loop = self.clock.looping_call(
            self.send_stop_syncing, UPDATE_SYNCING_USERS_MS
        )

        hs.get_reactor().addSystemEventTrigger(
            "before",
            "shutdown",
            run_as_background_process,
            "generic_presence.on_shutdown",
            self._on_shutdown,
        )

    async def _on_shutdown(self) -> None:
        if self._track_presence:
            self.hs.get_replication_command_handler().send_command(
                ClearUserSyncsCommand(self.instance_id)
            )

    def send_user_sync(
        self,
        user_id: str,
        device_id: Optional[str],
        is_syncing: bool,
        last_sync_ms: int,
    ) -> None:
        if self._track_presence:
            self.hs.get_replication_command_handler().send_user_sync(
                self.instance_id, user_id, device_id, is_syncing, last_sync_ms
            )

    def mark_as_coming_online(self, user_id: str, device_id: Optional[str]) -> None:
        """A user has started syncing. Send a UserSync to the presence writer,
        unless they had recently stopped syncing.
        """
        going_offline = self._user_devices_going_offline.pop((user_id, device_id), None)
        if not going_offline:
            # Safe to skip because we haven't yet told the presence writer they
            # were offline
            self.send_user_sync(user_id, device_id, True, self.clock.time_msec())

    def mark_as_going_offline(self, user_id: str, device_id: Optional[str]) -> None:
        """A user has stopped syncing. We wait before notifying the presence
        writer as its likely they'll come back soon. This allows us to avoid
        sending a stopped syncing immediately followed by a started syncing
        notification to the presence writer
        """
        self._user_devices_going_offline[(user_id, device_id)] = self.clock.time_msec()

    def send_stop_syncing(self) -> None:
        """Check if there are any users who have stopped syncing a while ago and
        haven't come back yet. If there are poke the presence writer about them.
        """
        now = self.clock.time_msec()
        for (user_id, device_id), last_sync_ms in list(
            self._user_devices_going_offline.items()
        ):
            if now - last_sync_ms > UPDATE_SYNCING_USERS_MS:
                self._user_devices_going_offline.pop((user_id, device_id), None)
                self.send_user_sync(user_id, device_id, False, last_sync_ms)

    async def user_syncing(
        self,
        user_id: str,
        device_id: Optional[str],
        affect_presence: bool,
        presence_state: str,
    ) -> ContextManager[None]:
        """Record that a user is syncing.

        Called by the sync and events servlets to record that a user has connected to
        this worker and is waiting for some events.
        """
        if not affect_presence or not self._track_presence:
            return _NullContextManager()

        # Note that this causes last_active_ts to be incremented which is not
        # what the spec wants.
        await self.set_state(
            UserID.from_string(user_id),
            device_id,
            state={"presence": presence_state},
            is_sync=True,
        )

        curr_sync = self._user_device_to_num_current_syncs.get((user_id, device_id), 0)
        self._user_device_to_num_current_syncs[(user_id, device_id)] = curr_sync + 1

        # If this is the first in-flight sync, notify replication
        if self._user_device_to_num_current_syncs[(user_id, device_id)] == 1:
            self.mark_as_coming_online(user_id, device_id)

        def _end() -> None:
            # We check that the user_id is in user_to_num_current_syncs because
            # user_to_num_current_syncs may have been cleared if we are
            # shutting down.
            if (user_id, device_id) in self._user_device_to_num_current_syncs:
                self._user_device_to_num_current_syncs[(user_id, device_id)] -= 1

                # If there are no more in-flight syncs, notify replication
                if self._user_device_to_num_current_syncs[(user_id, device_id)] == 0:
                    self.mark_as_going_offline(user_id, device_id)

        @contextlib.contextmanager
        def _user_syncing() -> Generator[None, None, None]:
            try:
                yield
            finally:
                _end()

        return _user_syncing()

    async def notify_from_replication(
        self, states: List[UserPresenceState], stream_id: int
    ) -> None:
        parties = await get_interested_parties(self.store, self.presence_router, states)
        room_ids_to_states, users_to_states = parties

        self.notifier.on_new_event(
            StreamKeyType.PRESENCE,
            stream_id,
            rooms=room_ids_to_states.keys(),
            users=users_to_states.keys(),
        )

    async def process_replication_rows(
        self, stream_name: str, instance_name: str, token: int, rows: list
    ) -> None:
        await super().process_replication_rows(stream_name, instance_name, token, rows)

        if stream_name != PresenceStream.NAME:
            return

        states = [
            UserPresenceState(
                row.user_id,
                row.state,
                row.last_active_ts,
                row.last_federation_update_ts,
                row.last_user_sync_ts,
                row.status_msg,
                row.currently_active,
            )
            for row in rows
        ]

        # The list of states to notify sync streams and remote servers about.
        # This is calculated by comparing the old and new states for each user
        # using `should_notify(..)`.
        #
        # Note that this is necessary as the presence writer will periodically
        # flush presence state changes that should not be notified about to the
        # DB, and so will be sent over the replication stream.
        state_to_notify = []

        for new_state in states:
            old_state = self.user_to_current_state.get(new_state.user_id)
            self.user_to_current_state[new_state.user_id] = new_state
            is_mine = self.is_mine_id(new_state.user_id)
            if not old_state or should_notify(old_state, new_state, is_mine):
                state_to_notify.append(new_state)

        stream_id = token
        await self.notify_from_replication(state_to_notify, stream_id)

        # If this is a federation sender, notify about presence updates.
        await self.maybe_send_presence_to_interested_destinations(state_to_notify)

    def get_currently_syncing_users_for_replication(
        self,
    ) -> Iterable[Tuple[str, Optional[str]]]:
        return [
            user_id_device_id
            for user_id_device_id, count in self._user_device_to_num_current_syncs.items()
            if count > 0
        ]

    async def set_state(
        self,
        target_user: UserID,
        device_id: Optional[str],
        state: JsonDict,
        force_notify: bool = False,
        is_sync: bool = False,
    ) -> None:
        """Set the presence state of the user.

        Args:
            target_user: The ID of the user to set the presence state of.
            device_id: the device that the user is setting the presence state of.
            state: The presence state as a JSON dictionary.
            force_notify: Whether to force notification of the update to clients.
            is_sync: True if this update was from a sync, which results in
                *not* overriding a previously set BUSY status, updating the
                user's last_user_sync_ts, and ignoring the "status_msg" field of
                the `state` dict.
        """
        presence = state["presence"]

        if presence not in self.VALID_PRESENCE:
            raise SynapseError(400, "Invalid presence state")

        user_id = target_user.to_string()

        # If tracking of presence is disabled, no-op
        if not self._track_presence:
            return

        # Proxy request to instance that writes presence
        await self._set_state_client(
            instance_name=self._presence_writer_instance,
            user_id=user_id,
            device_id=device_id,
            state=state,
            force_notify=force_notify,
            is_sync=is_sync,
        )

    async def bump_presence_active_time(
        self, user: UserID, device_id: Optional[str]
    ) -> None:
        """We've seen the user do something that indicates they're interacting
        with the app.
        """
        # If presence is disabled, no-op
        if not self._track_presence:
            return

        # Proxy request to instance that writes presence
        user_id = user.to_string()
        await self._bump_active_client(
            instance_name=self._presence_writer_instance,
            user_id=user_id,
            device_id=device_id,
        )


class PresenceHandler(BasePresenceHandler):
    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.wheel_timer: WheelTimer[str] = WheelTimer()
        self.notifier = hs.get_notifier()

        federation_registry = hs.get_federation_registry()

        federation_registry.register_edu_handler(
            EduTypes.PRESENCE, self.incoming_presence
        )

        LaterGauge(
            "synapse_handlers_presence_user_to_current_state_size",
            "",
            [],
            lambda: len(self.user_to_current_state),
        )

        # The per-device presence state, maps user to devices to per-device presence state.
        self._user_to_device_to_current_state: Dict[
            str, Dict[Optional[str], UserDevicePresenceState]
        ] = {}

        now = self.clock.time_msec()
        if self._track_presence:
            for state in self.user_to_current_state.values():
                # Create a psuedo-device to properly handle time outs. This will
                # be overridden by any "real" devices within SYNC_ONLINE_TIMEOUT.
                pseudo_device_id = None
                self._user_to_device_to_current_state[state.user_id] = {
                    pseudo_device_id: UserDevicePresenceState(
                        user_id=state.user_id,
                        device_id=pseudo_device_id,
                        state=state.state,
                        last_active_ts=state.last_active_ts,
                        last_sync_ts=state.last_user_sync_ts,
                    )
                }

                self.wheel_timer.insert(
                    now=now, obj=state.user_id, then=state.last_active_ts + IDLE_TIMER
                )
                self.wheel_timer.insert(
                    now=now,
                    obj=state.user_id,
                    then=state.last_user_sync_ts + SYNC_ONLINE_TIMEOUT,
                )
                if self.is_mine_id(state.user_id):
                    self.wheel_timer.insert(
                        now=now,
                        obj=state.user_id,
                        then=state.last_federation_update_ts + FEDERATION_PING_INTERVAL,
                    )
                else:
                    self.wheel_timer.insert(
                        now=now,
                        obj=state.user_id,
                        then=state.last_federation_update_ts + FEDERATION_TIMEOUT,
                    )

        # Set of users who have presence in the `user_to_current_state` that
        # have not yet been persisted
        self.unpersisted_users_changes: Set[str] = set()

        hs.get_reactor().addSystemEventTrigger(
            "before",
            "shutdown",
            run_as_background_process,
            "presence.on_shutdown",
            self._on_shutdown,
        )

        # Keeps track of the number of *ongoing* syncs on this process. While
        # this is non zero a user will never go offline.
        self._user_device_to_num_current_syncs: Dict[
            Tuple[str, Optional[str]], int
        ] = {}

        # Keeps track of the number of *ongoing* syncs on other processes.
        #
        # While any sync is ongoing on another process the user's device will never
        # go offline.
        #
        # Each process has a unique identifier and an update frequency. If
        # no update is received from that process within the update period then
        # we assume that all the sync requests on that process have stopped.
        # Stored as a dict from process_id to set of (user_id, device_id), and
        # a dict of process_id to millisecond timestamp last updated.
        self.external_process_to_current_syncs: Dict[
            str, Set[Tuple[str, Optional[str]]]
        ] = {}
        self.external_process_last_updated_ms: Dict[str, int] = {}

        self.external_sync_linearizer = Linearizer(name="external_sync_linearizer")

        if self._track_presence:
            # Start a LoopingCall in 30s that fires every 5s.
            # The initial delay is to allow disconnected clients a chance to
            # reconnect before we treat them as offline.
            self.clock.call_later(
                30, self.clock.looping_call, self._handle_timeouts, 5000
            )

        # Presence information is persisted, whether or not it is being tracked
        # internally.
        if self._presence_enabled:
            self.clock.call_later(
                60,
                self.clock.looping_call,
                self._persist_unpersisted_changes,
                60 * 1000,
            )

        LaterGauge(
            "synapse_handlers_presence_wheel_timer_size",
            "",
            [],
            lambda: len(self.wheel_timer),
        )

        # Used to handle sending of presence to newly joined users/servers
        if self._track_presence:
            self.notifier.add_replication_callback(self.notify_new_event)

        # Presence is best effort and quickly heals itself, so lets just always
        # stream from the current state when we restart.
        self._event_pos = self.store.get_room_max_stream_ordering()
        self._event_processing = False

    async def _on_shutdown(self) -> None:
        """Gets called when shutting down. This lets us persist any updates that
        we haven't yet persisted, e.g. updates that only changes some internal
        timers. This allows changes to persist across startup without having to
        persist every single change.

        If this does not run it simply means that some of the timers will fire
        earlier than they should when synapse is restarted. This affect of this
        is some spurious presence changes that will self-correct.
        """
        # If the DB pool has already terminated, don't try updating
        if not self.store.db_pool.is_running():
            return

        logger.info(
            "Performing _on_shutdown. Persisting %d unpersisted changes",
            len(self.user_to_current_state),
        )

        if self.unpersisted_users_changes:
            await self.store.update_presence(
                [
                    self.user_to_current_state[user_id]
                    for user_id in self.unpersisted_users_changes
                ]
            )
        logger.info("Finished _on_shutdown")

    @wrap_as_background_process("persist_presence_changes")
    async def _persist_unpersisted_changes(self) -> None:
        """We periodically persist the unpersisted changes, as otherwise they
        may stack up and slow down shutdown times.
        """
        unpersisted = self.unpersisted_users_changes
        self.unpersisted_users_changes = set()

        if unpersisted:
            logger.info("Persisting %d unpersisted presence updates", len(unpersisted))
            await self.store.update_presence(
                [self.user_to_current_state[user_id] for user_id in unpersisted]
            )

    async def _update_states(
        self,
        new_states: Iterable[UserPresenceState],
        force_notify: bool = False,
    ) -> None:
        """Updates presence of users. Sets the appropriate timeouts. Pokes
        the notifier and federation if and only if the changed presence state
        should be sent to clients/servers.

        Args:
            new_states: The new user presence state updates to process.
            force_notify: Whether to force notifying clients of this presence state update,
                even if it doesn't change the state of a user's presence (e.g online -> online).
                This is currently used to bump the max presence stream ID without changing any
                user's presence (see PresenceHandler.add_users_to_send_full_presence_to).
        """
        if not self._presence_enabled:
            # We shouldn't get here if presence is disabled, but we check anyway
            # to ensure that we don't a) send out presence federation and b)
            # don't add things to the wheel timer that will never be handled.
            logger.warning("Tried to update presence states when presence is disabled")
            return

        now = self.clock.time_msec()

        with Measure(self.clock, "presence_update_states"):
            # NOTE: We purposefully don't await between now and when we've
            # calculated what we want to do with the new states, to avoid races.

            to_notify = {}  # Changes we want to notify everyone about
            to_federation_ping = {}  # These need sending keep-alives

            # Only bother handling the last presence change for each user
            new_states_dict = {}
            for new_state in new_states:
                new_states_dict[new_state.user_id] = new_state
            new_states = new_states_dict.values()

            for new_state in new_states:
                user_id = new_state.user_id

                # It's fine to not hit the database here, as the only thing not in
                # the current state cache are OFFLINE states, where the only field
                # of interest is last_active which is safe enough to assume is 0
                # here.
                prev_state = self.user_to_current_state.get(
                    user_id, UserPresenceState.default(user_id)
                )

                new_state, should_notify, should_ping = handle_update(
                    prev_state,
                    new_state,
                    is_mine=self.is_mine_id(user_id),
                    wheel_timer=self.wheel_timer,
                    now=now,
                    # When overriding disabled presence, don't kick off all the
                    # wheel timers.
                    persist=not self._track_presence,
                )

                if force_notify:
                    should_notify = True

                self.user_to_current_state[user_id] = new_state

                if should_notify:
                    to_notify[user_id] = new_state
                elif should_ping:
                    to_federation_ping[user_id] = new_state

            # TODO: We should probably ensure there are no races hereafter

            presence_updates_counter.inc(len(new_states))

            if to_notify:
                notified_presence_counter.inc(len(to_notify))
                await self._persist_and_notify(list(to_notify.values()))

            self.unpersisted_users_changes |= {s.user_id for s in new_states}
            self.unpersisted_users_changes -= set(to_notify.keys())

            # Check if we need to resend any presence states to remote hosts. We
            # only do this for states that haven't been updated in a while to
            # ensure that the remote host doesn't time the presence state out.
            #
            # Note that since these are states that have *not* been updated,
            # they won't get sent down the normal presence replication stream,
            # and so we have to explicitly send them via the federation stream.
            to_federation_ping = {
                user_id: state
                for user_id, state in to_federation_ping.items()
                if user_id not in to_notify
            }
            if to_federation_ping:
                federation_presence_out_counter.inc(len(to_federation_ping))

                hosts_to_states = await get_interested_remotes(
                    self.store,
                    self.presence_router,
                    list(to_federation_ping.values()),
                )

                for destinations, states in hosts_to_states:
                    await self._federation_queue.send_presence_to_destinations(
                        states, destinations
                    )

    @wrap_as_background_process("handle_presence_timeouts")
    async def _handle_timeouts(self) -> None:
        """Checks the presence of users that have timed out and updates as
        appropriate.
        """
        logger.debug("Handling presence timeouts")
        now = self.clock.time_msec()

        # Fetch the list of users that *may* have timed out. Things may have
        # changed since the timeout was set, so we won't necessarily have to
        # take any action.
        users_to_check = set(self.wheel_timer.fetch(now))

        # Check whether the lists of syncing processes from an external
        # process have expired.
        expired_process_ids = [
            process_id
            for process_id, last_update in self.external_process_last_updated_ms.items()
            if now - last_update > EXTERNAL_PROCESS_EXPIRY
        ]
        for process_id in expired_process_ids:
            # For each expired process drop tracking info and check the users
            # that were syncing on that process to see if they need to be timed
            # out.
            users_to_check.update(
                user_id
                for user_id, device_id in self.external_process_to_current_syncs.pop(
                    process_id, ()
                )
            )
            self.external_process_last_updated_ms.pop(process_id)

        states = [
            self.user_to_current_state.get(user_id, UserPresenceState.default(user_id))
            for user_id in users_to_check
        ]

        timers_fired_counter.inc(len(states))

        # Set of user ID & device IDs which are currently syncing.
        syncing_user_devices = {
            user_id_device_id
            for user_id_device_id, count in self._user_device_to_num_current_syncs.items()
            if count
        }
        syncing_user_devices.update(
            itertools.chain(*self.external_process_to_current_syncs.values())
        )

        changes = handle_timeouts(
            states,
            is_mine_fn=self.is_mine_id,
            syncing_user_devices=syncing_user_devices,
            user_to_devices=self._user_to_device_to_current_state,
            now=now,
        )

        return await self._update_states(changes)

    async def bump_presence_active_time(
        self, user: UserID, device_id: Optional[str]
    ) -> None:
        """We've seen the user do something that indicates they're interacting
        with the app.
        """
        # If presence is disabled, no-op
        if not self._track_presence:
            return

        user_id = user.to_string()

        bump_active_time_counter.inc()

        now = self.clock.time_msec()

        # Update the device information & mark the device as online if it was
        # unavailable.
        devices = self._user_to_device_to_current_state.setdefault(user_id, {})
        device_state = devices.setdefault(
            device_id,
            UserDevicePresenceState.default(user_id, device_id),
        )
        device_state.last_active_ts = now
        if device_state.state == PresenceState.UNAVAILABLE:
            device_state.state = PresenceState.ONLINE

        # Update the user state, this will always update last_active_ts and
        # might update the presence state.
        prev_state = await self.current_state_for_user(user_id)
        new_fields: Dict[str, Any] = {
            "last_active_ts": now,
            "state": _combine_device_states(devices.values()),
        }

        await self._update_states([prev_state.copy_and_replace(**new_fields)])

    async def user_syncing(
        self,
        user_id: str,
        device_id: Optional[str],
        affect_presence: bool = True,
        presence_state: str = PresenceState.ONLINE,
    ) -> ContextManager[None]:
        """Returns a context manager that should surround any stream requests
        from the user.

        This allows us to keep track of who is currently streaming and who isn't
        without having to have timers outside of this module to avoid flickering
        when users disconnect/reconnect.

        Args:
            user_id: the user that is starting a sync
            device_id: the user's device that is starting a sync
            affect_presence: If false this function will be a no-op.
                Useful for streams that are not associated with an actual
                client that is being used by a user.
            presence_state: The presence state indicated in the sync request
        """
        if not affect_presence or not self._track_presence:
            return _NullContextManager()

        curr_sync = self._user_device_to_num_current_syncs.get((user_id, device_id), 0)
        self._user_device_to_num_current_syncs[(user_id, device_id)] = curr_sync + 1

        # Note that this causes last_active_ts to be incremented which is not
        # what the spec wants.
        await self.set_state(
            UserID.from_string(user_id),
            device_id,
            state={"presence": presence_state},
            is_sync=True,
        )

        async def _end() -> None:
            try:
                self._user_device_to_num_current_syncs[(user_id, device_id)] -= 1

                prev_state = await self.current_state_for_user(user_id)
                await self._update_states(
                    [
                        prev_state.copy_and_replace(
                            last_user_sync_ts=self.clock.time_msec()
                        )
                    ]
                )
            except Exception:
                logger.exception("Error updating presence after sync")

        @contextmanager
        def _user_syncing() -> Generator[None, None, None]:
            try:
                yield
            finally:
                run_in_background(_end)

        return _user_syncing()

    def get_currently_syncing_users_for_replication(
        self,
    ) -> Iterable[Tuple[str, Optional[str]]]:
        # since we are the process handling presence, there is nothing to do here.
        return []

    async def update_external_syncs_row(
        self,
        process_id: str,
        user_id: str,
        device_id: Optional[str],
        is_syncing: bool,
        sync_time_msec: int,
    ) -> None:
        """Update the syncing users for an external process as a delta.

        Args:
            process_id: An identifier for the process the users are
                syncing against. This allows synapse to process updates
                as user start and stop syncing against a given process.
            user_id: The user who has started or stopped syncing
            device_id: The user's device that has started or stopped syncing
            is_syncing: Whether or not the user is now syncing
            sync_time_msec: Time in ms when the user was last syncing
        """
        async with self.external_sync_linearizer.queue(process_id):
            prev_state = await self.current_state_for_user(user_id)

            process_presence = self.external_process_to_current_syncs.setdefault(
                process_id, set()
            )

            # USER_SYNC is sent when a user's device starts or stops syncing on
            # a remote # process. (But only for the initial and last sync for that
            # device.)
            #
            # When a device *starts* syncing it also calls set_state(...) which
            # will update the state, last_active_ts, and last_user_sync_ts.
            # Simply ensure the user & device is tracked as syncing in this case.
            #
            # When a device *stops* syncing, update the last_user_sync_ts and mark
            # them as no longer syncing. Note this doesn't quite match the
            # monolith behaviour, which updates last_user_sync_ts at the end of
            # every sync, not just the last in-flight sync.
            if is_syncing and (user_id, device_id) not in process_presence:
                process_presence.add((user_id, device_id))
            elif not is_syncing and (user_id, device_id) in process_presence:
                devices = self._user_to_device_to_current_state.setdefault(user_id, {})
                device_state = devices.setdefault(
                    device_id, UserDevicePresenceState.default(user_id, device_id)
                )
                device_state.last_sync_ts = sync_time_msec

                new_state = prev_state.copy_and_replace(
                    last_user_sync_ts=sync_time_msec
                )
                await self._update_states([new_state])

                process_presence.discard((user_id, device_id))

            self.external_process_last_updated_ms[process_id] = self.clock.time_msec()

    async def update_external_syncs_clear(self, process_id: str) -> None:
        """Marks all users that had been marked as syncing by a given process
        as offline.

        Used when the process has stopped/disappeared.
        """
        async with self.external_sync_linearizer.queue(process_id):
            process_presence = self.external_process_to_current_syncs.pop(
                process_id, set()
            )

            time_now_ms = self.clock.time_msec()

            # Mark each device as having a last sync time.
            updated_users = set()
            for user_id, device_id in process_presence:
                device_state = self._user_to_device_to_current_state.setdefault(
                    user_id, {}
                ).setdefault(
                    device_id, UserDevicePresenceState.default(user_id, device_id)
                )

                device_state.last_sync_ts = time_now_ms
                updated_users.add(user_id)

            # Update each user (and insert into the appropriate timers to check if
            # they've gone offline).
            prev_states = await self.current_state_for_users(updated_users)
            await self._update_states(
                [
                    prev_state.copy_and_replace(last_user_sync_ts=time_now_ms)
                    for prev_state in prev_states.values()
                ]
            )
            self.external_process_last_updated_ms.pop(process_id, None)

    async def _persist_and_notify(self, states: List[UserPresenceState]) -> None:
        """Persist states in the database, poke the notifier and send to
        interested remote servers
        """
        stream_id, max_token = await self.store.update_presence(states)

        parties = await get_interested_parties(self.store, self.presence_router, states)
        room_ids_to_states, users_to_states = parties

        self.notifier.on_new_event(
            StreamKeyType.PRESENCE,
            stream_id,
            rooms=room_ids_to_states.keys(),
            users=[UserID.from_string(u) for u in users_to_states],
        )

        # We only want to poke the local federation sender, if any, as other
        # workers will receive the presence updates via the presence replication
        # stream (which is updated by `store.update_presence`).
        await self.maybe_send_presence_to_interested_destinations(states)

    async def incoming_presence(self, origin: str, content: JsonDict) -> None:
        """Called when we receive a `m.presence` EDU from a remote server."""
        if not self._track_presence:
            return

        now = self.clock.time_msec()
        updates = []
        for push in content.get("push", []):
            # A "push" contains a list of presence that we are probably interested
            # in.
            user_id = push.get("user_id", None)
            if not user_id:
                logger.info(
                    "Got presence update from %r with no 'user_id': %r", origin, push
                )
                continue

            if get_domain_from_id(user_id) != origin:
                logger.info(
                    "Got presence update from %r with bad 'user_id': %r",
                    origin,
                    user_id,
                )
                continue

            presence_state = push.get("presence", None)
            if not presence_state:
                logger.info(
                    "Got presence update from %r with no 'presence_state': %r",
                    origin,
                    push,
                )
                continue

            new_fields = {"state": presence_state, "last_federation_update_ts": now}

            last_active_ago = push.get("last_active_ago", None)
            if last_active_ago is not None:
                new_fields["last_active_ts"] = now - last_active_ago

            new_fields["status_msg"] = push.get("status_msg", None)
            new_fields["currently_active"] = push.get("currently_active", False)

            prev_state = await self.current_state_for_user(user_id)
            updates.append(prev_state.copy_and_replace(**new_fields))

        if updates:
            federation_presence_counter.inc(len(updates))
            await self._update_states(updates)

    async def set_state(
        self,
        target_user: UserID,
        device_id: Optional[str],
        state: JsonDict,
        force_notify: bool = False,
        is_sync: bool = False,
    ) -> None:
        """Set the presence state of the user.

        Args:
            target_user: The ID of the user to set the presence state of.
            device_id: the device that the user is setting the presence state of.
            state: The presence state as a JSON dictionary.
            force_notify: Whether to force notification of the update to clients.
            is_sync: True if this update was from a sync, which results in
                *not* overriding a previously set BUSY status, updating the
                user's last_user_sync_ts, and ignoring the "status_msg" field of
                the `state` dict.
        """
        status_msg = state.get("status_msg", None)
        presence = state["presence"]

        if presence not in self.VALID_PRESENCE:
            raise SynapseError(400, "Invalid presence state")

        # If presence is disabled, no-op
        if not self._track_presence:
            return

        user_id = target_user.to_string()
        now = self.clock.time_msec()

        prev_state = await self.current_state_for_user(user_id)

        # Syncs do not override a previous presence of busy.
        #
        # TODO: This is a hack for lack of multi-device support. Unfortunately
        # removing this requires coordination with clients.
        if prev_state.state == PresenceState.BUSY and is_sync:
            presence = PresenceState.BUSY

        # Update the device specific information.
        devices = self._user_to_device_to_current_state.setdefault(user_id, {})
        device_state = devices.setdefault(
            device_id,
            UserDevicePresenceState.default(user_id, device_id),
        )
        device_state.state = presence
        device_state.last_active_ts = now
        if is_sync:
            device_state.last_sync_ts = now

        # Based on the state of each user's device calculate the new presence state.
        presence = _combine_device_states(devices.values())

        new_fields = {"state": presence}

        if presence == PresenceState.ONLINE or presence == PresenceState.BUSY:
            new_fields["last_active_ts"] = now

        if is_sync:
            new_fields["last_user_sync_ts"] = now
        else:
            # Syncs do not override the status message.
            new_fields["status_msg"] = status_msg

        await self._update_states(
            [prev_state.copy_and_replace(**new_fields)], force_notify=force_notify
        )

    async def is_visible(self, observed_user: UserID, observer_user: UserID) -> bool:
        """Returns whether a user can see another user's presence."""
        observer_room_ids = await self.store.get_rooms_for_user(
            observer_user.to_string()
        )
        observed_room_ids = await self.store.get_rooms_for_user(
            observed_user.to_string()
        )

        if observer_room_ids & observed_room_ids:
            return True

        return False

    async def get_all_presence_updates(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> Tuple[List[Tuple[int, list]], int, bool]:
        """
        Gets a list of presence update rows from between the given stream ids.
        Each row has:
        - stream_id(str)
        - user_id(str)
        - state(str)
        - last_active_ts(int)
        - last_federation_update_ts(int)
        - last_user_sync_ts(int)
        - status_msg(int)
        - currently_active(int)

        Args:
            instance_name: The writer we want to fetch updates from. Unused
                here since there is only ever one writer.
            last_id: The token to fetch updates from. Exclusive.
            current_id: The token to fetch updates up to. Inclusive.
            limit: The requested limit for the number of rows to return. The
                function may return more or fewer rows.

        Returns:
            A tuple consisting of: the updates, a token to use to fetch
            subsequent updates, and whether we returned fewer rows than exists
            between the requested tokens due to the limit.

            The token returned can be used in a subsequent call to this
            function to get further updates.

            The updates are a list of 2-tuples of stream ID and the row data
        """

        # TODO(markjh): replicate the unpersisted changes.
        # This could use the in-memory stores for recent changes.
        rows = await self.store.get_all_presence_updates(
            instance_name, last_id, current_id, limit
        )
        return rows

    def notify_new_event(self) -> None:
        """Called when new events have happened. Handles users and servers
        joining rooms and require being sent presence.
        """

        if self._event_processing:
            return

        async def _process_presence() -> None:
            assert not self._event_processing

            self._event_processing = True
            try:
                await self._unsafe_process()
            finally:
                self._event_processing = False

        run_as_background_process("presence.notify_new_event", _process_presence)

    async def _unsafe_process(self) -> None:
        # Loop round handling deltas until we're up to date
        while True:
            with Measure(self.clock, "presence_delta"):
                room_max_stream_ordering = self.store.get_room_max_stream_ordering()
                if self._event_pos == room_max_stream_ordering:
                    return

                logger.debug(
                    "Processing presence stats %s->%s",
                    self._event_pos,
                    room_max_stream_ordering,
                )
                (
                    max_pos,
                    deltas,
                ) = await self._storage_controllers.state.get_current_state_deltas(
                    self._event_pos, room_max_stream_ordering
                )

                # We may get multiple deltas for different rooms, but we want to
                # handle them on a room by room basis, so we batch them up by
                # room.
                deltas_by_room: Dict[str, List[StateDelta]] = {}
                for delta in deltas:
                    deltas_by_room.setdefault(delta.room_id, []).append(delta)

                for room_id, deltas_for_room in deltas_by_room.items():
                    await self._handle_state_delta(room_id, deltas_for_room)

                self._event_pos = max_pos

                # Expose current event processing position to prometheus
                synapse.metrics.event_processing_positions.labels("presence").set(
                    max_pos
                )

    async def _handle_state_delta(self, room_id: str, deltas: List[StateDelta]) -> None:
        """Process current state deltas for the room to find new joins that need
        to be handled.
        """

        # Sets of newly joined users. Note that if the local server is
        # joining a remote room for the first time we'll see both the joining
        # user and all remote users as newly joined.
        newly_joined_users = set()

        for delta in deltas:
            assert room_id == delta.room_id

            logger.debug(
                "Handling: %r %r, %s", delta.event_type, delta.state_key, delta.event_id
            )

            # Drop any event that isn't a membership join
            if delta.event_type != EventTypes.Member:
                continue

            if delta.event_id is None:
                # state has been deleted, so this is not a join. We only care about
                # joins.
                continue

            event = await self.store.get_event(delta.event_id, allow_none=True)
            if not event or event.content.get("membership") != Membership.JOIN:
                # We only care about joins
                continue

            if delta.prev_event_id:
                prev_event = await self.store.get_event(
                    delta.prev_event_id, allow_none=True
                )
                if (
                    prev_event
                    and prev_event.content.get("membership") == Membership.JOIN
                ):
                    # Ignore changes to join events.
                    continue

            newly_joined_users.add(delta.state_key)

        if not newly_joined_users:
            # If nobody has joined then there's nothing to do.
            return

        # We want to send:
        #   1. presence states of all local users in the room to newly joined
        #      remote servers
        #   2. presence states of newly joined users to all remote servers in
        #      the room.
        #
        # TODO: Only send presence states to remote hosts that don't already
        # have them (because they already share rooms).

        # Get all the users who were already in the room, by fetching the
        # current users in the room and removing the newly joined users.
        users = await self.store.get_users_in_room(room_id)
        prev_users = set(users) - newly_joined_users

        # Construct sets for all the local users and remote hosts that were
        # already in the room
        prev_local_users = []
        prev_remote_hosts = set()
        for user_id in prev_users:
            if self.is_mine_id(user_id):
                prev_local_users.append(user_id)
            else:
                prev_remote_hosts.add(get_domain_from_id(user_id))

        # Similarly, construct sets for all the local users and remote hosts
        # that were *not* already in the room. Care needs to be taken with the
        # calculating the remote hosts, as a host may have already been in the
        # room even if there is a newly joined user from that host.
        newly_joined_local_users = []
        newly_joined_remote_hosts = set()
        for user_id in newly_joined_users:
            if self.is_mine_id(user_id):
                newly_joined_local_users.append(user_id)
            else:
                host = get_domain_from_id(user_id)
                if host not in prev_remote_hosts:
                    newly_joined_remote_hosts.add(host)

        # Send presence states of all local users in the room to newly joined
        # remote servers. (We actually only send states for local users already
        # in the room, as we'll send states for newly joined local users below.)
        if prev_local_users and newly_joined_remote_hosts:
            local_states = await self.current_state_for_users(prev_local_users)

            # Filter out old presence, i.e. offline presence states where
            # the user hasn't been active for a week. We can change this
            # depending on what we want the UX to be, but at the least we
            # should filter out offline presence where the state is just the
            # default state.
            now = self.clock.time_msec()
            states = [
                state
                for state in local_states.values()
                if state.state != PresenceState.OFFLINE
                or now - state.last_active_ts < 7 * 24 * 60 * 60 * 1000
                or state.status_msg is not None
            ]

            await self._federation_queue.send_presence_to_destinations(
                destinations=newly_joined_remote_hosts,
                states=states,
            )

        # Send presence states of newly joined users to all remote servers in
        # the room
        if newly_joined_local_users and (
            prev_remote_hosts or newly_joined_remote_hosts
        ):
            local_states = await self.current_state_for_users(newly_joined_local_users)
            await self._federation_queue.send_presence_to_destinations(
                destinations=prev_remote_hosts | newly_joined_remote_hosts,
                states=list(local_states.values()),
            )


def should_notify(
    old_state: UserPresenceState, new_state: UserPresenceState, is_mine: bool
) -> bool:
    """Decides if a presence state change should be sent to interested parties."""
    user_location = "remote"
    if is_mine:
        user_location = "local"

    if old_state == new_state:
        return False

    if old_state.status_msg != new_state.status_msg:
        notify_reason_counter.labels(user_location, "status_msg_change").inc()
        return True

    if old_state.state != new_state.state:
        notify_reason_counter.labels(user_location, "state_change").inc()
        state_transition_counter.labels(
            user_location, old_state.state, new_state.state
        ).inc()
        return True

    if old_state.state == PresenceState.ONLINE:
        if new_state.currently_active != old_state.currently_active:
            notify_reason_counter.labels(user_location, "current_active_change").inc()
            return True

        if (
            new_state.last_active_ts - old_state.last_active_ts
            > LAST_ACTIVE_GRANULARITY
        ):
            # Only notify about last active bumps if we're not currently active
            if not new_state.currently_active:
                notify_reason_counter.labels(
                    user_location, "last_active_change_online"
                ).inc()
                return True

    elif new_state.last_active_ts - old_state.last_active_ts > LAST_ACTIVE_GRANULARITY:
        # Always notify for a transition where last active gets bumped.
        notify_reason_counter.labels(
            user_location, "last_active_change_not_online"
        ).inc()
        return True

    return False


def format_user_presence_state(
    state: UserPresenceState, now: int, include_user_id: bool = True
) -> JsonDict:
    """Convert UserPresenceState to a JSON format that can be sent down to clients
    and to other servers.

    Args:
        state: The user presence state to format.
        now: The current timestamp since the epoch in ms.
        include_user_id: Whether to include `user_id` in the returned dictionary.
            As this function can be used both to format presence updates for client /sync
            responses and for federation /send requests, only the latter needs the include
            the `user_id` field.

    Returns:
        A JSON dictionary with the following keys:
            * presence: The presence state as a str.
            * user_id: Optional. Included if `include_user_id` is truthy. The canonical
                Matrix ID of the user.
            * last_active_ago: Optional. Included if `last_active_ts` is set on `state`.
                The timestamp that the user was last active.
            * status_msg: Optional. Included if `status_msg` is set on `state`. The user's
                status.
            * currently_active: Optional. Included only if `state.state` is "online".

        Example:

        {
            "presence": "online",
            "user_id": "@alice:example.com",
            "last_active_ago": 16783813918,
            "status_msg": "Hello world!",
            "currently_active": True
        }
    """
    content: JsonDict = {"presence": state.state}
    if include_user_id:
        content["user_id"] = state.user_id
    if state.last_active_ts:
        content["last_active_ago"] = now - state.last_active_ts
    if state.status_msg:
        content["status_msg"] = state.status_msg
    if state.state == PresenceState.ONLINE:
        content["currently_active"] = state.currently_active

    return content


class PresenceEventSource(EventSource[int, UserPresenceState]):
    def __init__(self, hs: "HomeServer"):
        # We can't call get_presence_handler here because there's a cycle:
        #
        #   Presence -> Notifier -> PresenceEventSource -> Presence
        #
        # Same with get_presence_router:
        #
        #   AuthHandler -> Notifier -> PresenceEventSource -> ModuleApi -> AuthHandler
        self.get_presence_handler = hs.get_presence_handler
        self.get_presence_router = hs.get_presence_router
        self.clock = hs.get_clock()
        self.store = hs.get_datastores().main

    async def get_new_events(
        self,
        user: UserID,
        from_key: Optional[int],
        # Having a default limit doesn't match the EventSource API, but some
        # callers do not provide it. It is unused in this class.
        limit: int = 0,
        room_ids: Optional[StrCollection] = None,
        is_guest: bool = False,
        explicit_room_id: Optional[str] = None,
        include_offline: bool = True,
        service: Optional[ApplicationService] = None,
    ) -> Tuple[List[UserPresenceState], int]:
        # The process for getting presence events are:
        #  1. Get the rooms the user is in.
        #  2. Get the list of user in the rooms.
        #  3. Get the list of users that are in the user's presence list.
        #  4. If there is a from_key set, cross reference the list of users
        #     with the `presence_stream_cache` to see which ones we actually
        #     need to check.
        #  5. Load current state for the users.
        #
        # We don't try and limit the presence updates by the current token, as
        # sending down the rare duplicate is not a concern.

        user_id = user.to_string()
        stream_change_cache = self.store.presence_stream_cache

        with Measure(self.clock, "presence.get_new_events"):
            if from_key is not None:
                from_key = int(from_key)

                # Check if this user should receive all current, online user presence. We only
                # bother to do this if from_key is set, as otherwise the user will receive all
                # user presence anyways.
                if await self.store.should_user_receive_full_presence_with_token(
                    user_id, from_key
                ):
                    # This user has been specified by a module to receive all current, online
                    # user presence. Removing from_key and setting include_offline to false
                    # will do effectively this.
                    from_key = None
                    include_offline = False

            max_token = self.store.get_current_presence_token()
            if from_key == max_token:
                # This is necessary as due to the way stream ID generators work
                # we may get updates that have a stream ID greater than the max
                # token (e.g. max_token is N but stream generator may return
                # results for N+2, due to N+1 not having finished being
                # persisted yet).
                #
                # This is usually fine, as it just means that we may send down
                # some presence updates multiple times. However, we need to be
                # careful that the sync stream either actually does make some
                # progress or doesn't return, otherwise clients will end up
                # tight looping calling /sync due to it immediately returning
                # the same token repeatedly.
                #
                # Hence this guard where we just return nothing so that the sync
                # doesn't return. C.f. https://github.com/matrix-org/synapse/issues/5503.
                return [], max_token

            # Figure out which other users this user should explicitly receive
            # updates for
            additional_users_interested_in = (
                await self.get_presence_router().get_interested_users(user.to_string())
            )

            # We have a set of users that we're interested in the presence of. We want to
            # cross-reference that with the users that have actually changed their presence.

            # Check whether this user should see all user updates

            if additional_users_interested_in == PresenceRouter.ALL_USERS:
                # Provide presence state for all users
                presence_updates = await self._filter_all_presence_updates_for_user(
                    user_id, include_offline, from_key
                )

                return presence_updates, max_token

            # Make mypy happy. users_interested_in should now be a set
            assert not isinstance(additional_users_interested_in, str)

            # We always care about our own presence.
            additional_users_interested_in.add(user_id)

            if explicit_room_id:
                user_ids = await self.store.get_users_in_room(explicit_room_id)
                additional_users_interested_in.update(user_ids)

            # The set of users that we're interested in and that have had a presence update.
            # We'll actually pull the presence updates for these users at the end.
            interested_and_updated_users: StrCollection

            if from_key is not None:
                # First get all users that have had a presence update
                result = stream_change_cache.get_all_entities_changed(from_key)

                # Cross-reference users we're interested in with those that have had updates.
                if result.hit:
                    updated_users = result.entities

                    # If we have the full list of changes for presence we can
                    # simply check which ones share a room with the user.
                    get_updates_counter.labels("stream").inc()

                    sharing_users = await self.store.do_users_share_a_room(
                        user_id, updated_users
                    )

                    interested_and_updated_users = (
                        sharing_users.union(additional_users_interested_in)
                    ).intersection(updated_users)

                else:
                    # Too many possible updates. Find all users we can see and check
                    # if any of them have changed.
                    get_updates_counter.labels("full").inc()

                    users_interested_in = (
                        await self.store.get_users_who_share_room_with_user(user_id)
                    )
                    users_interested_in.update(additional_users_interested_in)

                    interested_and_updated_users = (
                        stream_change_cache.get_entities_changed(
                            users_interested_in, from_key
                        )
                    )
            else:
                # No from_key has been specified. Return the presence for all users
                # this user is interested in
                interested_and_updated_users = (
                    await self.store.get_users_who_share_room_with_user(user_id)
                )
                interested_and_updated_users.update(additional_users_interested_in)

            # Retrieve the current presence state for each user
            users_to_state = await self.get_presence_handler().current_state_for_users(
                interested_and_updated_users
            )
            presence_updates = list(users_to_state.values())

        if not include_offline:
            # Filter out offline presence states
            presence_updates = self._filter_offline_presence_state(presence_updates)

        return presence_updates, max_token

    async def _filter_all_presence_updates_for_user(
        self,
        user_id: str,
        include_offline: bool,
        from_key: Optional[int] = None,
    ) -> List[UserPresenceState]:
        """
        Computes the presence updates a user should receive.

        First pulls presence updates from the database. Then consults PresenceRouter
        for whether any updates should be excluded by user ID.

        Args:
            user_id: The User ID of the user to compute presence updates for.
            include_offline: Whether to include offline presence states from the results.
            from_key: The minimum stream ID of updates to pull from the database
                before filtering.

        Returns:
            A list of presence states for the given user to receive.
        """
        updated_users = None
        if from_key:
            # Only return updates since the last sync
            result = self.store.presence_stream_cache.get_all_entities_changed(from_key)
            if result.hit:
                updated_users = result.entities

        if updated_users is not None:
            # Get the actual presence update for each change
            users_to_state = await self.get_presence_handler().current_state_for_users(
                updated_users
            )
            presence_updates = list(users_to_state.values())

            if not include_offline:
                # Filter out offline states
                presence_updates = self._filter_offline_presence_state(presence_updates)
        else:
            users_to_state = await self.store.get_presence_for_all_users(
                include_offline=include_offline
            )

            presence_updates = list(users_to_state.values())

        # TODO: This feels wildly inefficient, and it's unfortunate we need to ask the
        # module for information on a number of users when we then only take the info
        # for a single user

        # Filter through the presence router
        users_to_state_set = await self.get_presence_router().get_users_for_states(
            presence_updates
        )

        # We only want the mapping for the syncing user
        presence_updates = list(users_to_state_set[user_id])

        # Return presence information for all users
        return presence_updates

    def _filter_offline_presence_state(
        self, presence_updates: Iterable[UserPresenceState]
    ) -> List[UserPresenceState]:
        """Given an iterable containing user presence updates, return a list with any offline
        presence states removed.

        Args:
            presence_updates: Presence states to filter

        Returns:
            A new list with any offline presence states removed.
        """
        return [
            update
            for update in presence_updates
            if update.state != PresenceState.OFFLINE
        ]

    def get_current_key(self) -> int:
        return self.store.get_current_presence_token()


def handle_timeouts(
    user_states: List[UserPresenceState],
    is_mine_fn: Callable[[str], bool],
    syncing_user_devices: AbstractSet[Tuple[str, Optional[str]]],
    user_to_devices: Dict[str, Dict[Optional[str], UserDevicePresenceState]],
    now: int,
) -> List[UserPresenceState]:
    """Checks the presence of users that have timed out and updates as
    appropriate.

    Args:
        user_states: List of UserPresenceState's to check.
        is_mine_fn: Function that returns if a user_id is ours
        syncing_user_devices: A set of (user ID, device ID) tuples with active syncs..
        user_to_devices: A map of user ID to device ID to UserDevicePresenceState.
        now: Current time in ms.

    Returns:
        List of UserPresenceState updates
    """
    changes = {}  # Actual changes we need to notify people about

    for state in user_states:
        user_id = state.user_id
        is_mine = is_mine_fn(user_id)

        new_state = handle_timeout(
            state,
            is_mine,
            syncing_user_devices,
            user_to_devices.get(user_id, {}),
            now,
        )
        if new_state:
            changes[state.user_id] = new_state

    return list(changes.values())


def handle_timeout(
    state: UserPresenceState,
    is_mine: bool,
    syncing_device_ids: AbstractSet[Tuple[str, Optional[str]]],
    user_devices: Dict[Optional[str], UserDevicePresenceState],
    now: int,
) -> Optional[UserPresenceState]:
    """Checks the presence of the user to see if any of the timers have elapsed

    Args:
        state: UserPresenceState to check.
        is_mine: Whether the user is ours
        syncing_user_devices: A set of (user ID, device ID) tuples with active syncs..
        user_devices: A map of device ID to UserDevicePresenceState.
        now: Current time in ms.

    Returns:
        A UserPresenceState update or None if no update.
    """
    if state.state == PresenceState.OFFLINE:
        # No timeouts are associated with offline states.
        return None

    changed = False

    if is_mine:
        # Check per-device whether the device should be considered idle or offline
        # due to timeouts.
        device_changed = False
        offline_devices = []
        for device_id, device_state in user_devices.items():
            if device_state.state == PresenceState.ONLINE:
                if now - device_state.last_active_ts > IDLE_TIMER:
                    # Currently online, but last activity ages ago so auto
                    # idle
                    device_state.state = PresenceState.UNAVAILABLE
                    device_changed = True

            # If there are have been no sync for a while (and none ongoing),
            # set presence to offline.
            if (state.user_id, device_id) not in syncing_device_ids:
                # If the user has done something recently but hasn't synced,
                # don't set them as offline.
                sync_or_active = max(
                    device_state.last_sync_ts, device_state.last_active_ts
                )

                # Implementations aren't meant to timeout a device with a busy
                # state, but it needs to timeout *eventually* or else the user
                # will be stuck in that state.
                online_timeout = (
                    BUSY_ONLINE_TIMEOUT
                    if device_state.state == PresenceState.BUSY
                    else SYNC_ONLINE_TIMEOUT
                )
                if now - sync_or_active > online_timeout:
                    # Mark the device as going offline.
                    offline_devices.append(device_id)
                    device_changed = True

        # Offline devices are not needed and do not add information.
        for device_id in offline_devices:
            user_devices.pop(device_id)

        # If the presence state of the devices changed, then (maybe) update
        # the user's overall presence state.
        if device_changed:
            new_presence = _combine_device_states(user_devices.values())
            if new_presence != state.state:
                state = state.copy_and_replace(state=new_presence)
                changed = True

        if now - state.last_active_ts > LAST_ACTIVE_GRANULARITY:
            # So that we send down a notification that we've
            # stopped updating.
            changed = True

        if now - state.last_federation_update_ts > FEDERATION_PING_INTERVAL:
            # Need to send ping to other servers to ensure they don't
            # timeout and set us to offline
            changed = True
    else:
        # We expect to be poked occasionally by the other side.
        # This is to protect against forgetful/buggy servers, so that
        # no one gets stuck online forever.
        if now - state.last_federation_update_ts > FEDERATION_TIMEOUT:
            # The other side seems to have disappeared.
            state = state.copy_and_replace(state=PresenceState.OFFLINE)
            changed = True

    return state if changed else None


def handle_update(
    prev_state: UserPresenceState,
    new_state: UserPresenceState,
    is_mine: bool,
    wheel_timer: WheelTimer,
    now: int,
    persist: bool,
) -> Tuple[UserPresenceState, bool, bool]:
    """Given a presence update:
        1. Add any appropriate timers.
        2. Check if we should notify anyone.

    Args:
        prev_state
        new_state
        is_mine: Whether the user is ours
        wheel_timer
        now: Time now in ms
        persist: True if this state should persist until another update occurs.
            Skips insertion into wheel timers.

    Returns:
        3-tuple: `(new_state, persist_and_notify, federation_ping)` where:
            - new_state: is the state to actually persist
            - persist_and_notify: whether to persist and notify people
            - federation_ping: whether we should send a ping over federation
    """
    user_id = new_state.user_id

    persist_and_notify = False
    federation_ping = False

    # If the users are ours then we want to set up a bunch of timers
    # to time things out.
    if is_mine:
        if new_state.state == PresenceState.ONLINE:
            # Idle timer
            if not persist:
                wheel_timer.insert(
                    now=now, obj=user_id, then=new_state.last_active_ts + IDLE_TIMER
                )

            active = now - new_state.last_active_ts < LAST_ACTIVE_GRANULARITY
            new_state = new_state.copy_and_replace(currently_active=active)

            if active and not persist:
                wheel_timer.insert(
                    now=now,
                    obj=user_id,
                    then=new_state.last_active_ts + LAST_ACTIVE_GRANULARITY,
                )

        if new_state.state != PresenceState.OFFLINE:
            # User has stopped syncing
            if not persist:
                wheel_timer.insert(
                    now=now,
                    obj=user_id,
                    then=new_state.last_user_sync_ts + SYNC_ONLINE_TIMEOUT,
                )

            last_federate = new_state.last_federation_update_ts
            if now - last_federate > FEDERATION_PING_INTERVAL:
                # Been a while since we've poked remote servers
                new_state = new_state.copy_and_replace(last_federation_update_ts=now)
                federation_ping = True

        if new_state.state == PresenceState.BUSY and not persist:
            wheel_timer.insert(
                now=now,
                obj=user_id,
                then=new_state.last_user_sync_ts + BUSY_ONLINE_TIMEOUT,
            )

    else:
        # An update for a remote user was received.
        if not persist:
            wheel_timer.insert(
                now=now,
                obj=user_id,
                then=new_state.last_federation_update_ts + FEDERATION_TIMEOUT,
            )

    # Check whether the change was something worth notifying about
    if should_notify(prev_state, new_state, is_mine):
        new_state = new_state.copy_and_replace(last_federation_update_ts=now)
        persist_and_notify = True

    return new_state, persist_and_notify, federation_ping


PRESENCE_BY_PRIORITY = {
    PresenceState.BUSY: 4,
    PresenceState.ONLINE: 3,
    PresenceState.UNAVAILABLE: 2,
    PresenceState.OFFLINE: 1,
}


def _combine_device_states(
    device_states: Iterable[UserDevicePresenceState],
) -> str:
    """
    Find the device to use presence information from.

    Orders devices by priority, then last_active_ts.

    Args:
        device_states: An iterable of device presence states

    Return:
        The combined presence state.
    """

    # Based on (all) the user's devices calculate the new presence state.
    presence = PresenceState.OFFLINE
    last_active_ts = -1

    # Find the device to use the presence state of based on the presence priority,
    # but tie-break with how recently the device has been seen.
    for device_state in device_states:
        if (PRESENCE_BY_PRIORITY[device_state.state], device_state.last_active_ts) > (
            PRESENCE_BY_PRIORITY[presence],
            last_active_ts,
        ):
            presence = device_state.state
            last_active_ts = device_state.last_active_ts

    return presence


async def get_interested_parties(
    store: DataStore, presence_router: PresenceRouter, states: List[UserPresenceState]
) -> Tuple[Dict[str, List[UserPresenceState]], Dict[str, List[UserPresenceState]]]:
    """Given a list of states return which entities (rooms, users)
    are interested in the given states.

    Args:
        store: The homeserver's data store.
        presence_router: A module for augmenting the destinations for presence updates.
        states: A list of incoming user presence updates.

    Returns:
        A 2-tuple of `(room_ids_to_states, users_to_states)`,
        with each item being a dict of `entity_name` -> `[UserPresenceState]`
    """
    room_ids_to_states: Dict[str, List[UserPresenceState]] = {}
    users_to_states: Dict[str, List[UserPresenceState]] = {}
    for state in states:
        room_ids = await store.get_rooms_for_user(state.user_id)
        for room_id in room_ids:
            room_ids_to_states.setdefault(room_id, []).append(state)

        # Always notify self
        users_to_states.setdefault(state.user_id, []).append(state)

    # Ask a presence routing module for any additional parties if one
    # is loaded.
    router_users_to_states = await presence_router.get_users_for_states(states)

    # Update the dictionaries with additional destinations and state to send
    for user_id, user_states in router_users_to_states.items():
        users_to_states.setdefault(user_id, []).extend(user_states)

    return room_ids_to_states, users_to_states


async def get_interested_remotes(
    store: DataStore,
    presence_router: PresenceRouter,
    states: List[UserPresenceState],
) -> List[Tuple[StrCollection, Collection[UserPresenceState]]]:
    """Given a list of presence states figure out which remote servers
    should be sent which.

    All the presence states should be for local users only.

    Args:
        store: The homeserver's data store.
        presence_router: A module for augmenting the destinations for presence updates.
        states: A list of incoming user presence updates.

    Returns:
        A map from destinations to presence states to send to that destination.
    """
    hosts_and_states: List[Tuple[StrCollection, Collection[UserPresenceState]]] = []

    # First we look up the rooms each user is in (as well as any explicit
    # subscriptions), then for each distinct room we look up the remote
    # hosts in those rooms.
    for state in states:
        room_ids = await store.get_rooms_for_user(state.user_id)
        hosts: Set[str] = set()
        for room_id in room_ids:
            room_hosts = await store.get_current_hosts_in_room(room_id)
            hosts.update(room_hosts)
        hosts_and_states.append((hosts, [state]))

    # Ask a presence routing module for any additional parties if one
    # is loaded.
    router_users_to_states = await presence_router.get_users_for_states(states)

    for user_id, user_states in router_users_to_states.items():
        host = get_domain_from_id(user_id)
        hosts_and_states.append(([host], user_states))

    return hosts_and_states


class PresenceFederationQueue:
    """Handles sending ad hoc presence updates over federation, which are *not*
    due to state updates (that get handled via the presence stream), e.g.
    federation pings and sending existing present states to newly joined hosts.

    Only the last N minutes will be queued, so if a federation sender instance
    is down for longer then some updates will be dropped. This is OK as presence
    is ephemeral, and so it will self correct eventually.

    On workers the class tracks the last received position of the stream from
    replication, and handles querying for missed updates over HTTP replication,
    c.f. `get_current_token` and `get_replication_rows`.
    """

    # How long to keep entries in the queue for. Workers that are down for
    # longer than this duration will miss out on older updates.
    _KEEP_ITEMS_IN_QUEUE_FOR_MS = 5 * 60 * 1000

    # How often to check if we can expire entries from the queue.
    _CLEAR_ITEMS_EVERY_MS = 60 * 1000

    def __init__(self, hs: "HomeServer", presence_handler: BasePresenceHandler):
        self._clock = hs.get_clock()
        self._notifier = hs.get_notifier()
        self._instance_name = hs.get_instance_name()
        self._presence_handler = presence_handler
        self._repl_client = ReplicationGetStreamUpdates.make_client(hs)

        # Should we keep a queue of recent presence updates? We only bother if
        # another process may be handling federation sending.
        self._queue_presence_updates = True

        # Whether this instance is a presence writer.
        self._presence_writer = self._instance_name in hs.config.worker.writers.presence

        # The FederationSender instance, if this process sends federation traffic directly.
        self._federation = None

        if hs.should_send_federation():
            self._federation = hs.get_federation_sender()

            # We don't bother queuing up presence states if only this instance
            # is sending federation.
            if hs.config.worker.federation_shard_config.instances == [
                self._instance_name
            ]:
                self._queue_presence_updates = False

        # The queue of recently queued updates as tuples of: `(timestamp,
        # stream_id, destinations, user_ids)`. We don't store the full states
        # for efficiency, and remote workers will already have the full states
        # cached.
        self._queue: List[Tuple[int, int, StrCollection, Set[str]]] = []

        self._next_id = 1

        # Map from instance name to current token
        self._current_tokens: Dict[str, int] = {}

        if self._queue_presence_updates:
            self._clock.looping_call(self._clear_queue, self._CLEAR_ITEMS_EVERY_MS)

    def _clear_queue(self) -> None:
        """Clear out older entries from the queue."""
        clear_before = self._clock.time_msec() - self._KEEP_ITEMS_IN_QUEUE_FOR_MS

        # The queue is sorted by timestamp, so we can bisect to find the right
        # place to purge before. Note that we are searching using a 1-tuple with
        # the time, which does The Right Thing since the queue is a tuple where
        # the first item is a timestamp.
        index = bisect(self._queue, (clear_before,))
        self._queue = self._queue[index:]

    async def send_presence_to_destinations(
        self, states: Collection[UserPresenceState], destinations: StrCollection
    ) -> None:
        """Send the presence states to the given destinations.

        Will forward to the local federation sender (if there is one) and queue
        to send over replication (if there are other federation sender instances.).

        Must only be called on the presence writer process.
        """

        # This should only be called on a presence writer.
        assert self._presence_writer

        if not states or not destinations:
            # Ignore calls which either don't have any new states or don't need
            # to be sent anywhere.
            return

        if self._federation:
            await self._federation.send_presence_to_destinations(
                states=states,
                destinations=destinations,
            )

        if not self._queue_presence_updates:
            return

        now = self._clock.time_msec()

        stream_id = self._next_id
        self._next_id += 1

        self._queue.append((now, stream_id, destinations, {s.user_id for s in states}))

        self._notifier.notify_replication()

    def get_current_token(self, instance_name: str) -> int:
        """Get the current position of the stream.

        On workers this returns the last stream ID received from replication.
        """
        if instance_name == self._instance_name:
            return self._next_id - 1
        else:
            return self._current_tokens.get(instance_name, 0)

    async def get_replication_rows(
        self,
        instance_name: str,
        from_token: int,
        upto_token: int,
        target_row_count: int,
    ) -> Tuple[List[Tuple[int, Tuple[str, str]]], int, bool]:
        """Get all the updates between the two tokens.

        We return rows in the form of `(destination, user_id)` to keep the size
        of each row bounded (rather than returning the sets in a row).

        On workers this will query the presence writer process via HTTP replication.
        """
        if instance_name != self._instance_name:
            # If not local we query over http replication from the presence
            # writer
            result = await self._repl_client(
                instance_name=instance_name,
                stream_name=PresenceFederationStream.NAME,
                from_token=from_token,
                upto_token=upto_token,
            )
            return result["updates"], result["upto_token"], result["limited"]

        # If the from_token is the current token then there's nothing to return
        # and we can trivially no-op.
        if from_token == self._next_id - 1:
            return [], upto_token, False

        # We can find the correct position in the queue by noting that there is
        # exactly one entry per stream ID, and that the last entry has an ID of
        # `self._next_id - 1`, so we can count backwards from the end.
        #
        # Since we are returning all states in the range `from_token < stream_id
        # <= upto_token` we look for the index with a `stream_id` of `from_token
        # + 1`.
        #
        # Since the start of the queue is periodically truncated we need to
        # handle the case where `from_token` stream ID has already been dropped.
        start_idx = max(from_token + 1 - self._next_id, -len(self._queue))

        to_send: List[Tuple[int, Tuple[str, str]]] = []
        limited = False
        new_id = upto_token
        for _, stream_id, destinations, user_ids in self._queue[start_idx:]:
            if stream_id <= from_token:
                # Paranoia check that we are actually only sending states that
                # are have stream_id strictly greater than from_token. We should
                # never hit this.
                logger.warning(
                    "Tried returning presence federation stream ID: %d less than from_token: %d (next_id: %d, len: %d)",
                    stream_id,
                    from_token,
                    self._next_id,
                    len(self._queue),
                )
                continue

            if stream_id > upto_token:
                break

            new_id = stream_id

            to_send.extend(
                (stream_id, (destination, user_id))
                for destination in destinations
                for user_id in user_ids
            )

            if len(to_send) > target_row_count:
                limited = True
                break

        return to_send, new_id, limited

    async def process_replication_rows(
        self, stream_name: str, instance_name: str, token: int, rows: list
    ) -> None:
        if stream_name != PresenceFederationStream.NAME:
            return

        # We keep track of the current tokens (so that we can catch up with anything we missed after a disconnect)
        self._current_tokens[instance_name] = token

        # If we're a federation sender we pull out the presence states to send
        # and forward them on.
        if not self._federation:
            return

        hosts_to_users: Dict[str, Set[str]] = {}
        for row in rows:
            hosts_to_users.setdefault(row.destination, set()).add(row.user_id)

        for host, user_ids in hosts_to_users.items():
            states = await self._presence_handler.current_state_for_users(user_ids)
            await self._federation.send_presence_to_destinations(
                states=states.values(),
                destinations=[host],
            )
