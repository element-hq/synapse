#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019,2020 The Matrix.org Foundation C.I.C.
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
import random
from threading import Lock
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Iterable,
    Mapping,
    cast,
)

from synapse.api import errors
from synapse.api.constants import EduTypes, EventTypes, Membership
from synapse.api.errors import (
    Codes,
    FederationDeniedError,
    HttpResponseException,
    InvalidAPICallError,
    RequestSendFailed,
    SynapseError,
)
from synapse.logging.opentracing import log_kv, set_tag, trace
from synapse.metrics.background_process_metrics import (
    wrap_as_background_process,
)
from synapse.replication.http.devices import (
    ReplicationDeviceHandleRoomUnPartialStated,
    ReplicationHandleNewDeviceUpdateRestServlet,
    ReplicationMultiUserDevicesResyncRestServlet,
    ReplicationNotifyDeviceUpdateRestServlet,
    ReplicationNotifyUserSignatureUpdateRestServlet,
)
from synapse.storage.databases.main.client_ips import DeviceLastConnectionInfo
from synapse.storage.databases.main.roommember import EventIdMembership
from synapse.storage.databases.main.state_deltas import StateDelta
from synapse.types import (
    DeviceListUpdates,
    JsonDict,
    JsonMapping,
    ScheduledTask,
    StrCollection,
    StreamKeyType,
    StreamToken,
    TaskStatus,
    UserID,
    get_domain_from_id,
    get_verify_key_from_cross_signing_key,
)
from synapse.util import stringutils
from synapse.util.async_helpers import Linearizer
from synapse.util.caches.expiringcache import ExpiringCache
from synapse.util.cancellation import cancellable
from synapse.util.metrics import measure_func
from synapse.util.retryutils import (
    NotRetryingDestination,
    filter_destinations_by_retry_limiter,
)

if TYPE_CHECKING:
    from synapse.app.generic_worker import GenericWorkerStore
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

DELETE_DEVICE_MSGS_TASK_NAME = "delete_device_messages"
MAX_DEVICE_DISPLAY_NAME_LEN = 100
DELETE_STALE_DEVICES_INTERVAL_MS = 24 * 60 * 60 * 1000


def _check_device_name_length(name: str | None) -> None:
    """
    Checks whether a device name is longer than the maximum allowed length.

    Args:
        name: The name of the device.

    Raises:
        SynapseError: if the device name is too long.
    """
    if name and len(name) > MAX_DEVICE_DISPLAY_NAME_LEN:
        raise SynapseError(
            400,
            "Device display name is too long (max %i)" % (MAX_DEVICE_DISPLAY_NAME_LEN,),
            errcode=Codes.TOO_LARGE,
        )


class DeviceHandler:
    """
    Handles most things related to devices. This doesn't do any writing to the
    device list stream on its own, and will call to device list writers through
    replication when necessary (see DeviceWriterHandler).
    """

    device_list_updater: "DeviceListWorkerUpdater"
    store: "GenericWorkerStore"

    def __init__(self, hs: "HomeServer"):
        self.server_name = hs.hostname  # nb must be called this for @measure_func
        self.clock = hs.get_clock()  # nb must be called this for @measure_func
        self.hs = hs  # nb must be called this for @wrap_as_background_process
        self.store = cast("GenericWorkerStore", hs.get_datastores().main)
        self.notifier = hs.get_notifier()
        self.state = hs.get_state_handler()
        self._appservice_handler = hs.get_application_service_handler()
        self._state_storage = hs.get_storage_controllers().state
        self._auth_handler = hs.get_auth_handler()
        self._account_data_handler = hs.get_account_data_handler()
        self._event_sources = hs.get_event_sources()
        self._msc3852_enabled = hs.config.experimental.msc3852_enabled
        self._query_appservices_for_keys = (
            hs.config.experimental.msc3984_appservice_key_query
        )
        self._task_scheduler = hs.get_task_scheduler()

        self._dont_notify_new_devices_for = (
            hs.config.registration.dont_notify_new_devices_for
        )

        self.device_list_updater = DeviceListWorkerUpdater(hs)

        self._task_scheduler.register_action(
            self._delete_device_messages, DELETE_DEVICE_MSGS_TASK_NAME
        )

        self._device_list_writers = hs.config.worker.writers.device_lists

        # Ensure a few operations are only running on the first device list writer
        #
        # This is needed because of a few linearizers in the DeviceListUpdater,
        # and avoid using cross-worker locks.
        #
        # The main logic update is that the DeviceListUpdater is now only
        # instantiated on the first device list writer, and a few methods that
        # were safe to move to any worker were moved to the DeviceListWorkerUpdater
        # This must be kept in sync with DeviceListWorkerUpdater
        self._main_device_list_writer = hs.config.worker.writers.device_lists[0]

        self._notify_device_update_client = (
            ReplicationNotifyDeviceUpdateRestServlet.make_client(hs)
        )
        self._notify_user_signature_update_client = (
            ReplicationNotifyUserSignatureUpdateRestServlet.make_client(hs)
        )
        self._handle_new_device_update_client = (
            ReplicationHandleNewDeviceUpdateRestServlet.make_client(hs)
        )
        self._handle_room_un_partial_stated_client = (
            ReplicationDeviceHandleRoomUnPartialStated.make_client(hs)
        )

        # The EDUs are handled on a single writer, as it needs to acquire a
        # per-user lock, for which it is cheaper to use in-memory linearizers
        # than cross-worker locks.
        hs.get_federation_registry().register_instances_for_edu(
            EduTypes.DEVICE_LIST_UPDATE,
            [self._main_device_list_writer],
        )

        self._delete_stale_devices_after = hs.config.server.delete_stale_devices_after

        if (
            hs.config.worker.run_background_tasks
            and self._delete_stale_devices_after is not None
        ):
            self.clock.looping_call(
                self.hs.run_as_background_process,
                DELETE_STALE_DEVICES_INTERVAL_MS,
                desc="delete_stale_devices",
                func=self._delete_stale_devices,
            )

    async def _delete_stale_devices(self) -> None:
        """Background task that deletes devices which haven't been accessed for more than
        a configured time period.
        """
        # We should only be running this job if the config option is defined.
        assert self._delete_stale_devices_after is not None
        now_ms = self.clock.time_msec()
        since_ms = now_ms - self._delete_stale_devices_after
        devices = await self.store.get_local_devices_not_accessed_since(since_ms)

        for user_id, user_devices in devices.items():
            await self.delete_devices(user_id, user_devices)

    async def check_device_registered(
        self,
        user_id: str,
        device_id: str | None,
        initial_device_display_name: str | None = None,
        auth_provider_id: str | None = None,
        auth_provider_session_id: str | None = None,
    ) -> str:
        """
        If the given device has not been registered, register it with the
        supplied display name.

        If no device_id is supplied, we make one up.

        Args:
            user_id:  @user:id
            device_id: device id supplied by client
            initial_device_display_name: device display name from client
            auth_provider_id: The SSO IdP the user used, if any.
            auth_provider_session_id: The session ID (sid) got from the SSO IdP.
        Returns:
            device id (generated if none was supplied)
        """

        _check_device_name_length(initial_device_display_name)

        # Check if we should send out device lists updates for this new device.
        notify = user_id not in self._dont_notify_new_devices_for

        if device_id is not None:
            new_device = await self.store.store_device(
                user_id=user_id,
                device_id=device_id,
                initial_device_display_name=initial_device_display_name,
                auth_provider_id=auth_provider_id,
                auth_provider_session_id=auth_provider_session_id,
            )
            if new_device:
                if notify:
                    await self.notify_device_update(user_id, [device_id])
            return device_id

        # if the device id is not specified, we'll autogen one, but loop a few
        # times in case of a clash.
        attempts = 0
        while attempts < 5:
            new_device_id = stringutils.random_string(10).upper()
            new_device = await self.store.store_device(
                user_id=user_id,
                device_id=new_device_id,
                initial_device_display_name=initial_device_display_name,
                auth_provider_id=auth_provider_id,
                auth_provider_session_id=auth_provider_session_id,
            )
            if new_device:
                if notify:
                    await self.notify_device_update(user_id, [new_device_id])
                return new_device_id
            attempts += 1

        raise errors.StoreError(500, "Couldn't generate a device ID.")

    @trace
    async def delete_all_devices_for_user(
        self, user_id: str, except_device_id: str | None = None
    ) -> None:
        """Delete all of the user's devices

        Args:
            user_id: The user to remove all devices from
            except_device_id: optional device id which should not be deleted
        """
        device_map = await self.store.get_devices_by_user(user_id)
        if except_device_id is not None:
            device_map.pop(except_device_id, None)
        user_device_ids = device_map.keys()
        await self.delete_devices(user_id, user_device_ids)

    async def delete_devices(self, user_id: str, device_ids: StrCollection) -> None:
        """Delete several devices

        Args:
            user_id: The user to delete devices from.
            device_ids: The list of device IDs to delete
        """
        to_device_stream_id = self._event_sources.get_current_token().to_device_key

        try:
            await self.store.delete_devices(user_id, device_ids)
        except errors.StoreError as e:
            if e.code == 404:
                # no match
                set_tag("error", True)
                set_tag("reason", "User doesn't have that device id.")
            else:
                raise

        # Delete data specific to each device. Not optimised as its an
        # experimental MSC.
        if self.hs.config.experimental.msc3890_enabled:
            for device_id in device_ids:
                # Remove any local notification settings for this device in accordance
                # with MSC3890.
                await self._account_data_handler.remove_account_data_for_user(
                    user_id,
                    f"org.matrix.msc3890.local_notification_settings.{device_id}",
                )

        # If we're deleting a lot of devices, a bunch of them may not have any
        # to-device messages queued up. We filter those out to avoid scheduling
        # unnecessary tasks.
        devices_with_messages = await self.store.get_devices_with_messages(
            user_id, device_ids
        )
        for device_id in devices_with_messages:
            # Delete device messages asynchronously and in batches using the task scheduler
            # We specify an upper stream id to avoid deleting non delivered messages
            # if an user re-uses a device ID.
            await self._task_scheduler.schedule_task(
                DELETE_DEVICE_MSGS_TASK_NAME,
                resource_id=device_id,
                params={
                    "user_id": user_id,
                    "device_id": device_id,
                    "up_to_stream_id": to_device_stream_id,
                },
            )

        await self._auth_handler.delete_access_tokens_for_devices(
            user_id, device_ids=device_ids
        )

        # Pushers are deleted after `delete_access_tokens_for_user` is called so that
        # modules using `on_logged_out` hook can use them if needed.
        await self.hs.get_pusherpool().remove_pushers_by_devices(user_id, device_ids)

        await self.notify_device_update(user_id, device_ids)

    async def upsert_device(
        self, user_id: str, device_id: str, display_name: str | None = None
    ) -> bool:
        """Create or update a device

        Args:
            user_id: The user to update devices of.
            device_id: The device to update.
            display_name: The new display name for this device.

        Returns:
            True if the device was created, False if it was updated.

        """

        # Reject a new displayname which is too long.
        _check_device_name_length(display_name)

        created = await self.store.store_device(
            user_id,
            device_id,
            initial_device_display_name=display_name,
        )

        if not created:
            await self.store.update_device(
                user_id,
                device_id,
                new_display_name=display_name,
            )

        await self.notify_device_update(user_id, [device_id])
        return created

    async def update_device(self, user_id: str, device_id: str, content: dict) -> None:
        """Update the given device

        Args:
            user_id: The user to update devices of.
            device_id: The device to update.
            content: body of update request
        """

        # Reject a new displayname which is too long.
        new_display_name = content.get("display_name")

        _check_device_name_length(new_display_name)

        try:
            await self.store.update_device(
                user_id, device_id, new_display_name=new_display_name
            )
            await self.notify_device_update(user_id, [device_id])
        except errors.StoreError as e:
            if e.code == 404:
                raise errors.NotFoundError()
            else:
                raise

    @trace
    async def get_devices_by_user(self, user_id: str) -> list[JsonDict]:
        """
        Retrieve the given user's devices

        Args:
            user_id: The user ID to query for devices.
        Returns:
            info on each device
        """

        set_tag("user_id", user_id)
        device_map = await self.store.get_devices_by_user(user_id)

        ips = await self.store.get_last_client_ip_by_device(user_id, device_id=None)

        devices = list(device_map.values())
        for device in devices:
            _update_device_from_client_ips(device, ips)

        log_kv(device_map)
        return devices

    async def get_dehydrated_device(self, user_id: str) -> tuple[str, JsonDict] | None:
        """Retrieve the information for a dehydrated device.

        Args:
            user_id: the user whose dehydrated device we are looking for
        Returns:
            a tuple whose first item is the device ID, and the second item is
            the dehydrated device information
        """
        return await self.store.get_dehydrated_device(user_id)

    async def store_dehydrated_device(
        self,
        user_id: str,
        device_id: str | None,
        device_data: JsonDict,
        initial_device_display_name: str | None = None,
        keys_for_device: JsonDict | None = None,
    ) -> str:
        """Store a dehydrated device for a user, optionally storing the keys associated with
        it as well.  If the user had a previous dehydrated device, it is removed.

        Args:
            user_id: the user that we are storing the device for
            device_id: device id supplied by client
            device_data: the dehydrated device information
            initial_device_display_name: The display name to use for the device
            keys_for_device: keys for the dehydrated device
        Returns:
            device id of the dehydrated device
        """
        device_id = await self.check_device_registered(
            user_id,
            device_id,
            initial_device_display_name,
        )

        time_now = self.clock.time_msec()

        old_device_id = await self.store.store_dehydrated_device(
            user_id, device_id, device_data, time_now, keys_for_device
        )

        if old_device_id is not None:
            await self.delete_devices(user_id, [old_device_id])

        return device_id

    async def rehydrate_device(
        self, user_id: str, access_token: str, device_id: str
    ) -> dict:
        """Process a rehydration request from the user.

        Args:
            user_id: the user who is rehydrating the device
            access_token: the access token used for the request
            device_id: the ID of the device that will be rehydrated
        Returns:
            a dict containing {"success": True}
        """
        success = await self.store.remove_dehydrated_device(user_id, device_id)

        if not success:
            raise errors.NotFoundError()

        # If the dehydrated device was successfully deleted (the device ID
        # matched the stored dehydrated device), then modify the access
        # token and refresh token to use the dehydrated device's ID and
        # copy the old device display name to the dehydrated device,
        # and destroy the old device ID
        old_device_id = await self.store.set_device_for_access_token(
            access_token, device_id
        )
        await self.store.set_device_for_refresh_token(user_id, old_device_id, device_id)
        old_device = await self.store.get_device(user_id, old_device_id)
        if old_device is None:
            raise errors.NotFoundError()
        await self.store.update_device(user_id, device_id, old_device["display_name"])
        # can't call self.delete_device because that will clobber the
        # access token so call the storage layer directly
        await self.store.delete_devices(user_id, [old_device_id])

        # tell everyone that the old device is gone and that the dehydrated
        # device has a new display name
        await self.notify_device_update(user_id, [old_device_id, device_id])

        return {"success": True}

    async def delete_dehydrated_device(self, user_id: str, device_id: str) -> None:
        """
        Delete a stored dehydrated device.

        Args:
            user_id: the user_id to delete the device from
            device_id: id of the dehydrated device to delete
        """
        success = await self.store.remove_dehydrated_device(user_id, device_id)

        if not success:
            raise errors.NotFoundError()

        await self.delete_devices(user_id, [device_id])

    @trace
    async def get_device(self, user_id: str, device_id: str) -> JsonDict:
        """Retrieve the given device

        Args:
            user_id: The user to get the device from
            device_id: The device to fetch.

        Returns:
            info on the device
        Raises:
            errors.NotFoundError: if the device was not found
        """
        device = await self.store.get_device(user_id, device_id)
        if device is None:
            raise errors.NotFoundError()

        ips = await self.store.get_last_client_ip_by_device(user_id, device_id)

        device = dict(device)
        _update_device_from_client_ips(device, ips)

        set_tag("device", str(device))
        set_tag("ips", str(ips))

        return device

    @cancellable
    async def get_device_changes_in_shared_rooms(
        self,
        user_id: str,
        room_ids: StrCollection,
        from_token: StreamToken,
        now_token: StreamToken | None = None,
    ) -> set[str]:
        """Get the set of users whose devices have changed who share a room with
        the given user.
        """
        now_device_lists_key = self.store.get_device_stream_token()
        if now_token:
            now_device_lists_key = now_token.device_list_key

        changed_users = await self.store.get_device_list_changes_in_rooms(
            room_ids,
            from_token.device_list_key,
            now_device_lists_key,
        )

        if changed_users is not None:
            # We also check if the given user has changed their device. If
            # they're in no rooms then the above query won't include them.
            changed = await self.store.get_users_whose_devices_changed(
                from_token.device_list_key,
                [user_id],
                to_key=now_device_lists_key,
            )
            changed_users.update(changed)
            return changed_users

        # If the DB returned None then the `from_token` is too old, so we fall
        # back on looking for device updates for all users.

        users_who_share_room = await self.store.get_users_who_share_room_with_user(
            user_id
        )

        tracked_users = set(users_who_share_room)

        # Always tell the user about their own devices
        tracked_users.add(user_id)

        changed = await self.store.get_users_whose_devices_changed(
            from_token.device_list_key,
            tracked_users,
            to_key=now_device_lists_key,
        )

        return changed

    @trace
    @cancellable
    async def get_user_ids_changed(
        self, user_id: str, from_token: StreamToken
    ) -> DeviceListUpdates:
        """Get list of users that have had the devices updated, or have newly
        joined a room, that `user_id` may be interested in.
        """

        set_tag("user_id", user_id)
        set_tag("from_token", str(from_token))

        now_token = self._event_sources.get_current_token()

        # We need to work out all the different membership changes for the user
        # and user they share a room with, to pass to
        # `generate_sync_entry_for_device_list`. See its docstring for details
        # on the data required.

        joined_room_ids = await self.store.get_rooms_for_user(user_id)

        # Get the set of rooms that the user has joined/left
        membership_changes = (
            await self.store.get_current_state_delta_membership_changes_for_user(
                user_id, from_key=from_token.room_key, to_key=now_token.room_key
            )
        )

        # Check for newly joined or left rooms. We need to make sure that we add
        # to newly joined in the case membership goes from join -> leave -> join
        # again.
        newly_joined_rooms: set[str] = set()
        newly_left_rooms: set[str] = set()
        for change in membership_changes:
            # We check for changes in "joinedness", i.e. if the membership has
            # changed to or from JOIN.
            if change.membership == Membership.JOIN:
                if change.prev_membership != Membership.JOIN:
                    newly_joined_rooms.add(change.room_id)
                    newly_left_rooms.discard(change.room_id)
            elif change.prev_membership == Membership.JOIN:
                newly_joined_rooms.discard(change.room_id)
                newly_left_rooms.add(change.room_id)

        # We now work out if any other users have since joined or left the rooms
        # the user is currently in.

        # List of membership changes per room
        room_to_deltas: dict[str, list[StateDelta]] = {}
        # The set of event IDs of membership events (so we can fetch their
        # associated membership).
        memberships_to_fetch: set[str] = set()

        # TODO: Only pull out membership events?
        state_changes = await self.store.get_current_state_deltas_for_rooms(
            joined_room_ids, from_token=from_token.room_key, to_token=now_token.room_key
        )
        for delta in state_changes:
            if delta.event_type != EventTypes.Member:
                continue

            room_to_deltas.setdefault(delta.room_id, []).append(delta)
            if delta.event_id:
                memberships_to_fetch.add(delta.event_id)
            if delta.prev_event_id:
                memberships_to_fetch.add(delta.prev_event_id)

        # Fetch all the memberships for the membership events
        event_id_to_memberships: Mapping[str, EventIdMembership | None] = {}
        if memberships_to_fetch:
            event_id_to_memberships = await self.store.get_membership_from_event_ids(
                memberships_to_fetch
            )

        joined_invited_knocked = (
            Membership.JOIN,
            Membership.INVITE,
            Membership.KNOCK,
        )

        # We now want to find any user that have newly joined/invited/knocked,
        # or newly left, similarly to above.
        newly_joined_or_invited_or_knocked_users: set[str] = set()
        newly_left_users: set[str] = set()
        for _, deltas in room_to_deltas.items():
            for delta in deltas:
                # Get the prev/new memberships for the delta
                new_membership = None
                prev_membership = None
                if delta.event_id:
                    m = event_id_to_memberships.get(delta.event_id)
                    if m is not None:
                        new_membership = m.membership
                if delta.prev_event_id:
                    m = event_id_to_memberships.get(delta.prev_event_id)
                    if m is not None:
                        prev_membership = m.membership

                # Check if a user has newly joined/invited/knocked, or left.
                if new_membership in joined_invited_knocked:
                    if prev_membership not in joined_invited_knocked:
                        newly_joined_or_invited_or_knocked_users.add(delta.state_key)
                        newly_left_users.discard(delta.state_key)
                elif prev_membership in joined_invited_knocked:
                    newly_joined_or_invited_or_knocked_users.discard(delta.state_key)
                    newly_left_users.add(delta.state_key)

        # Now we actually calculate the device list entry with the information
        # calculated above.
        device_list_updates = await self.generate_sync_entry_for_device_list(
            user_id=user_id,
            since_token=from_token,
            now_token=now_token,
            joined_room_ids=joined_room_ids,
            newly_joined_rooms=newly_joined_rooms,
            newly_joined_or_invited_or_knocked_users=newly_joined_or_invited_or_knocked_users,
            newly_left_rooms=newly_left_rooms,
            newly_left_users=newly_left_users,
        )

        log_kv(
            {
                "changed": device_list_updates.changed,
                "left": device_list_updates.left,
            }
        )

        return device_list_updates

    async def generate_sync_entry_for_device_list(
        self,
        user_id: str,
        since_token: StreamToken,
        now_token: StreamToken,
        joined_room_ids: AbstractSet[str],
        newly_joined_rooms: AbstractSet[str],
        newly_joined_or_invited_or_knocked_users: AbstractSet[str],
        newly_left_rooms: AbstractSet[str],
        newly_left_users: AbstractSet[str],
    ) -> DeviceListUpdates:
        """Generate the DeviceListUpdates section of sync

        Args:
            sync_result_builder
            newly_joined_rooms: Set of rooms user has joined since previous sync
            newly_joined_or_invited_or_knocked_users: Set of users that have joined,
                been invited to a room or are knocking on a room since
                previous sync.
            newly_left_rooms: Set of rooms user has left since previous sync
            newly_left_users: Set of users that have left a room we're in since
                previous sync
        """
        # Take a copy since these fields will be mutated later.
        newly_joined_or_invited_or_knocked_users = set(
            newly_joined_or_invited_or_knocked_users
        )
        newly_left_users = set(newly_left_users)

        # We want to figure out what user IDs the client should refetch
        # device keys for, and which users we aren't going to track changes
        # for anymore.
        #
        # For the first step we check:
        #   a. if any users we share a room with have updated their devices,
        #      and
        #   b. we also check if we've joined any new rooms, or if a user has
        #      joined a room we're in.
        #
        # For the second step we just find any users we no longer share a
        # room with by looking at all users that have left a room plus users
        # that were in a room we've left.

        users_that_have_changed = set()

        # Step 1a, check for changes in devices of users we share a room
        # with
        users_that_have_changed = await self.get_device_changes_in_shared_rooms(
            user_id,
            joined_room_ids,
            from_token=since_token,
            now_token=now_token,
        )

        # Step 1b, check for newly joined rooms
        for room_id in newly_joined_rooms:
            joined_users = await self.store.get_users_in_room(room_id)
            newly_joined_or_invited_or_knocked_users.update(joined_users)

        # TODO: Check that these users are actually new, i.e. either they
        # weren't in the previous sync *or* they left and rejoined.
        users_that_have_changed.update(newly_joined_or_invited_or_knocked_users)

        user_signatures_changed = await self.store.get_users_whose_signatures_changed(
            user_id, since_token.device_list_key
        )
        users_that_have_changed.update(user_signatures_changed)

        # Now find users that we no longer track
        for room_id in newly_left_rooms:
            left_users = await self.store.get_users_in_room(room_id)
            newly_left_users.update(left_users)

        # Remove any users that we still share a room with.
        left_users_rooms = await self.store.get_rooms_for_users(newly_left_users)
        for user_id, entries in left_users_rooms.items():
            if any(rid in joined_room_ids for rid in entries):
                newly_left_users.discard(user_id)

        return DeviceListUpdates(changed=users_that_have_changed, left=newly_left_users)

    async def on_federation_query_user_devices(self, user_id: str) -> JsonDict:
        if not self.hs.is_mine(UserID.from_string(user_id)):
            raise SynapseError(400, "User is not hosted on this homeserver")

        stream_id, devices = await self.store.get_e2e_device_keys_for_federation_query(
            user_id
        )
        master_key = await self.store.get_e2e_cross_signing_key(user_id, "master")
        self_signing_key = await self.store.get_e2e_cross_signing_key(
            user_id, "self_signing"
        )

        # Check if the application services have any results.
        if self._query_appservices_for_keys:
            # Query the appservice for all devices for this user.
            query: dict[str, list[str] | None] = {user_id: None}

            # Query the appservices for any keys.
            appservice_results = await self._appservice_handler.query_keys(query)

            # Merge results, overriding anything from the database.
            appservice_devices = appservice_results.get("device_keys", {}).get(
                user_id, {}
            )

            # Filter the database results to only those devices that the appservice has
            # *not* responded with.
            devices = [d for d in devices if d["device_id"] not in appservice_devices]
            # Append the appservice response by wrapping each result in another dictionary.
            devices.extend(
                {"device_id": device_id, "keys": device}
                for device_id, device in appservice_devices.items()
            )

            # TODO Handle cross-signing keys.

        return {
            "user_id": user_id,
            "stream_id": stream_id,
            "devices": devices,
            "master_key": master_key,
            "self_signing_key": self_signing_key,
        }

    async def handle_room_un_partial_stated(self, room_id: str) -> None:
        """Handles sending appropriate device list updates in a room that has
        gone from partial to full state.
        """

        await self._handle_room_un_partial_stated_client(
            instance_name=random.choice(self._device_list_writers),
            room_id=room_id,
        )

    @trace
    @measure_func("notify_device_update")
    async def notify_device_update(
        self, user_id: str, device_ids: StrCollection
    ) -> None:
        """Notify that a user's device(s) has changed. Pokes the notifier, and
        remote servers if the user is local.

        Args:
            user_id: The Matrix ID of the user who's device list has been updated.
            device_ids: The device IDs that have changed.
        """
        await self._notify_device_update_client(
            instance_name=random.choice(self._device_list_writers),
            user_id=user_id,
            device_ids=list(device_ids),
        )

    async def notify_user_signature_update(
        self,
        from_user_id: str,
        user_ids: list[str],
    ) -> None:
        """Notify a device writer that a user have made new signatures of other users.

        Args:
            from_user_id: The Matrix ID of the user who's signatures have been updated.
            user_ids: The Matrix IDs of the users that have changed.
        """
        await self._notify_user_signature_update_client(
            instance_name=random.choice(self._device_list_writers),
            from_user_id=from_user_id,
            user_ids=user_ids,
        )

    async def handle_new_device_update(self) -> None:
        """Wake up a device writer to send local device list changes as federation outbound pokes."""
        # This is only sent to the first device writer to avoid cross-worker
        # locks in _handle_new_device_update_async, as it makes assumptions
        # about being the only instance running.
        await self._handle_new_device_update_client(
            instance_name=self._device_list_writers[0],
        )

    DEVICE_MSGS_DELETE_BATCH_LIMIT = 1000
    DEVICE_MSGS_DELETE_SLEEP_MS = 100

    async def _delete_device_messages(
        self,
        task: ScheduledTask,
    ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
        """Scheduler task to delete device messages in batch of `DEVICE_MSGS_DELETE_BATCH_LIMIT`."""
        assert task.params is not None
        user_id = task.params["user_id"]
        device_id = task.params["device_id"]
        up_to_stream_id = task.params["up_to_stream_id"]

        # Delete the messages in batches to avoid too much DB load.
        from_stream_id = None
        while True:
            from_stream_id, _ = await self.store.delete_messages_for_device_between(
                user_id=user_id,
                device_id=device_id,
                from_stream_id=from_stream_id,
                to_stream_id=up_to_stream_id,
                limit=DeviceWriterHandler.DEVICE_MSGS_DELETE_BATCH_LIMIT,
            )

            if from_stream_id is None:
                return TaskStatus.COMPLETE, None, None

            await self.clock.sleep(
                DeviceWriterHandler.DEVICE_MSGS_DELETE_SLEEP_MS / 1000.0
            )


class DeviceWriterHandler(DeviceHandler):
    """
    Superclass of the DeviceHandler which gets instantiated on workers that can
    write to the device list stream.
    """

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.server_name = hs.hostname  # nb must be called this for @measure_func
        self.hs = hs  # nb must be called this for @wrap_as_background_process

        # We only need to poke the federation sender explicitly if its on the
        # same instance. Other federation sender instances will get notified by
        # `synapse.app.generic_worker.FederationSenderHandler` when it sees it
        # in the device lists stream.
        self.federation_sender = None
        if hs.should_send_federation():
            self.federation_sender = hs.get_federation_sender()

        self._storage_controllers = hs.get_storage_controllers()

        # There are a few things that are only handled on the main device list
        # writer to avoid cross-worker locks
        #
        # This mainly concerns the `DeviceListUpdater` class, which is only
        # instantiated on the first device list writer.
        self._is_main_device_list_writer = (
            hs.get_instance_name() == self._main_device_list_writer
        )

        # Whether `_handle_new_device_update_async` is currently processing.
        self._handle_new_device_update_is_processing = False

        # If a new device update may have happened while the loop was
        # processing.
        self._handle_new_device_update_new_data = False

        # Only the main device list writer handles device list EDUs and converts
        # device list updates to outbound federation pokes. This allows us to
        # use in-memory per-user locks instead of cross-worker locks, and
        # simplifies the logic for converting outbound pokes. This makes the
        # device_list writers a little bit unbalanced in terms of load, but
        # still unlocks local device changes (and therefore login/logouts) when
        # rolling-restarting Synapse.
        if self._is_main_device_list_writer:
            # On start up check if there are any updates pending.
            hs.get_clock().call_when_running(self._handle_new_device_update_async)
            self.device_list_updater = DeviceListUpdater(hs, self)
            hs.get_federation_registry().register_edu_handler(
                EduTypes.DEVICE_LIST_UPDATE,
                self.device_list_updater.incoming_device_list_update,
            )

    @trace
    @measure_func("notify_device_update")
    async def notify_device_update(
        self, user_id: str, device_ids: StrCollection
    ) -> None:
        """Notify that a user's device(s) has changed. Pokes the notifier, and
        remote servers if the user is local.

        Args:
            user_id: The Matrix ID of the user who's device list has been updated.
            device_ids: The device IDs that have changed.
        """
        if not device_ids:
            # No changes to notify about, so this is a no-op.
            return

        room_ids = await self.store.get_rooms_for_user(user_id)

        position = await self.store.add_device_change_to_streams(
            user_id,
            device_ids,
            room_ids=room_ids,
        )

        if not position:
            # This should only happen if there are no updates, so we bail.
            return

        if logger.isEnabledFor(logging.DEBUG):
            for device_id in device_ids:
                logger.debug(
                    "Notifying about update %r/%r, ID: %r", user_id, device_id, position
                )

        # specify the user ID too since the user should always get their own device list
        # updates, even if they aren't in any rooms.
        self.notifier.on_new_event(
            StreamKeyType.DEVICE_LIST, position, users={user_id}, rooms=room_ids
        )

        # We may need to do some processing asynchronously for local user IDs.
        if self.hs.is_mine_id(user_id):
            await self.handle_new_device_update()

    async def notify_user_signature_update(
        self, from_user_id: str, user_ids: list[str]
    ) -> None:
        """Notify a user that they have made new signatures of other users.

        Args:
            from_user_id: the user who made the signature
            user_ids: the users IDs that have new signatures
        """

        position = await self.store.add_user_signature_change_to_streams(
            from_user_id, user_ids
        )

        self.notifier.on_new_event(
            StreamKeyType.DEVICE_LIST, position, users=[from_user_id]
        )

    async def handle_new_device_update(self) -> None:
        # _handle_new_device_update_async is only called on the first device
        # writer, as it makes assumptions about only having one instance running
        # at a time. If this is not the first device writer, we defer to the
        # superclass, which will make the call go through replication.
        if not self._is_main_device_list_writer:
            return await super().handle_new_device_update()

        self._handle_new_device_update_async()
        return

    @wrap_as_background_process("_handle_new_device_update_async")
    async def _handle_new_device_update_async(self) -> None:
        """Called when we have a new local device list update that we need to
        send out over federation.

        This happens in the background so as not to block the original request
        that generated the device update.
        """
        # This should only ever be called on the main device list writer, as it
        # expects to only have a single instance of this loop running at a time.
        # See `handle_new_device_update`.
        assert self._is_main_device_list_writer

        if self._handle_new_device_update_is_processing:
            self._handle_new_device_update_new_data = True
            return

        self._handle_new_device_update_is_processing = True

        # Note that this logic only deals with the minimum stream ID, and not
        # the full stream token. This means that oubound pokes are only sent
        # once every writer on the device_lists stream has caught up. This is
        # fine, it may only introduces a bit of lag on the outbound pokes.
        # To fix this, 'device_lists_changes_converted_stream_position' would
        # need to include the full stream token instead of just a stream ID.
        # We could also consider have each writer converting their own device
        # list updates, but that can quickly become complex to handle changes in
        # the list of device writers.

        # The stream ID we processed previous iteration (if any), and the set of
        # hosts we've already poked about for this update. This is so that we
        # don't poke the same remote server about the same update repeatedly.
        current_stream_id = None
        hosts_already_sent_to: set[str] = set()

        try:
            stream_id, room_id = await self.store.get_device_change_last_converted_pos()

            while True:
                self._handle_new_device_update_new_data = False
                max_stream_id = self.store.get_device_stream_token().stream
                rows = await self.store.get_uncoverted_outbound_room_pokes(
                    stream_id, room_id
                )
                if not rows:
                    # If the DB returned nothing then there is nothing left to
                    # do, *unless* a new device list update happened during the
                    # DB query.

                    # Advance `(stream_id, room_id)`.
                    # `max_stream_id` comes from *before* the query for unconverted
                    # rows, which means that any unconverted rows must have a larger
                    # stream ID.
                    if max_stream_id > stream_id:
                        stream_id, room_id = max_stream_id, ""
                        await self.store.set_device_change_last_converted_pos(
                            stream_id, room_id
                        )
                    else:
                        assert max_stream_id == stream_id
                        # Avoid moving `room_id` backwards.

                    if self._handle_new_device_update_new_data:
                        continue
                    else:
                        return

                for user_id, device_id, room_id, stream_id, opentracing_context in rows:
                    hosts = set()

                    # Ignore any users that aren't ours
                    if self.hs.is_mine_id(user_id):
                        hosts = set(
                            await self._storage_controllers.state.get_current_hosts_in_room_or_partial_state_approximation(
                                room_id
                            )
                        )
                        hosts.discard(self.server_name)
                        # For rooms with partial state, `hosts` is merely an
                        # approximation. When we transition to a full state room, we
                        # will have to send out device list updates to any servers we
                        # missed.

                    # Check if we've already sent this update to some hosts
                    if current_stream_id == stream_id:
                        hosts -= hosts_already_sent_to

                    await self.store.add_device_list_outbound_pokes(
                        user_id=user_id,
                        device_id=device_id,
                        room_id=room_id,
                        hosts=hosts,
                        context=opentracing_context,
                    )

                    await self.store.mark_redundant_device_lists_pokes(
                        user_id=user_id,
                        device_id=device_id,
                        room_id=room_id,
                        converted_upto_stream_id=stream_id,
                    )

                    # Notify replication that we've updated the device list stream.
                    self.notifier.notify_replication()

                    if hosts and self.federation_sender:
                        logger.info(
                            "Sending device list update notif for %r to: %r",
                            user_id,
                            hosts,
                        )
                        await self.federation_sender.send_device_messages(
                            hosts, immediate=False
                        )
                        # TODO: when called, this isn't in a logging context.
                        # This leads to log spam, sentry event spam, and massive
                        # memory usage.
                        # See https://github.com/matrix-org/synapse/issues/12552.
                        # log_kv(
                        #     {"message": "sent device update to host", "host": host}
                        # )

                    if current_stream_id != stream_id:
                        # Clear the set of hosts we've already sent to as we're
                        # processing a new update.
                        hosts_already_sent_to.clear()

                    hosts_already_sent_to.update(hosts)
                    current_stream_id = stream_id

                # Advance `(stream_id, room_id)`.
                _, _, room_id, stream_id, _ = rows[-1]
                await self.store.set_device_change_last_converted_pos(
                    stream_id, room_id
                )

        finally:
            self._handle_new_device_update_is_processing = False

    async def handle_room_un_partial_stated(self, room_id: str) -> None:
        """Handles sending appropriate device list updates in a room that has
        gone from partial to full state.
        """

        # We defer to the device list updater to handle pending remote device
        # list updates.
        await self.device_list_updater.handle_room_un_partial_stated(room_id)

        # Replay local updates.
        (
            join_event_id,
            device_lists_stream_id,
        ) = await self.store.get_join_event_id_and_device_lists_stream_id_for_partial_state(
            room_id
        )

        # Get the local device list changes that have happened in the room since
        # we started joining. If there are no updates there's nothing left to do.
        changes = await self.store.get_device_list_changes_in_room(
            room_id, device_lists_stream_id
        )
        local_changes = {(u, d) for u, d in changes if self.hs.is_mine_id(u)}
        if not local_changes:
            return

        # Note: We have persisted the full state at this point, we just haven't
        # cleared the `partial_room` flag.
        join_state_ids = await self._state_storage.get_state_ids_for_event(
            join_event_id, await_full_state=False
        )
        current_state_ids = await self.store.get_partial_current_state_ids(room_id)

        # Now we need to work out all servers that might have been in the room
        # at any point during our join.

        # First we look for any membership states that have changed between the
        # initial join and now...
        all_keys = set(join_state_ids)
        all_keys.update(current_state_ids)

        potentially_changed_hosts = set()
        for etype, state_key in all_keys:
            if etype != EventTypes.Member:
                continue

            prev = join_state_ids.get((etype, state_key))
            current = current_state_ids.get((etype, state_key))

            if prev != current:
                potentially_changed_hosts.add(get_domain_from_id(state_key))

        # ... then we add all the hosts that are currently joined to the room...
        current_hosts_in_room = await self.store.get_current_hosts_in_room(room_id)
        potentially_changed_hosts.update(current_hosts_in_room)

        # ... and finally we remove any hosts that we were told about, as we
        # will have sent device list updates to those hosts when they happened.
        known_hosts_at_join = await self.store.get_partial_state_servers_at_join(
            room_id
        )
        assert known_hosts_at_join is not None
        potentially_changed_hosts.difference_update(known_hosts_at_join)

        potentially_changed_hosts.discard(self.server_name)

        if not potentially_changed_hosts:
            # Nothing to do.
            return

        logger.info(
            "Found %d changed hosts to send device list updates to",
            len(potentially_changed_hosts),
        )

        for user_id, device_id in local_changes:
            await self.store.add_device_list_outbound_pokes(
                user_id=user_id,
                device_id=device_id,
                room_id=room_id,
                hosts=potentially_changed_hosts,
                context=None,
            )

        # Notify things that device lists need to be sent out.
        self.notifier.notify_replication()
        if self.federation_sender:
            await self.federation_sender.send_device_messages(
                potentially_changed_hosts, immediate=False
            )


def _update_device_from_client_ips(
    device: JsonDict, client_ips: Mapping[tuple[str, str], DeviceLastConnectionInfo]
) -> None:
    ip = client_ips.get((device["user_id"], device["device_id"]))
    device.update(
        {
            "last_seen_user_agent": ip.user_agent if ip else None,
            "last_seen_ts": ip.last_seen if ip else None,
            "last_seen_ip": ip.ip if ip else None,
        }
    )


class DeviceListWorkerUpdater:
    "Handles incoming device list updates from federation and contacts the main device list writer over replication"

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self._notifier = hs.get_notifier()
        # On which instance the DeviceListUpdater is running
        # Must be kept in sync with DeviceHandler
        self._main_device_list_writer = hs.config.worker.writers.device_lists[0]
        self._multi_user_device_resync_client = (
            ReplicationMultiUserDevicesResyncRestServlet.make_client(hs)
        )

    async def multi_user_device_resync(
        self,
        user_ids: list[str],
    ) -> dict[str, JsonMapping | None]:
        """
        Like `user_device_resync` but operates on multiple users **from the same origin**
        at once.

        Returns:
            Dict from User ID to the same Dict as `user_device_resync`.
        """

        if not user_ids:
            # Shortcut empty requests
            return {}

        # This uses a per-user-id lock; to avoid using cross-worker locks, we
        # forward the request to the main device list writer.
        # See DeviceListUpdater
        return await self._multi_user_device_resync_client(
            instance_name=self._main_device_list_writer,
            user_ids=user_ids,
        )

    async def process_cross_signing_key_update(
        self,
        user_id: str,
        master_key: JsonDict | None,
        self_signing_key: JsonDict | None,
    ) -> list[str]:
        """Process the given new master and self-signing key for the given remote user.

        Args:
            user_id: The ID of the user these keys are for.
            master_key: The dict of the cross-signing master key as returned by the
                remote server.
            self_signing_key: The dict of the cross-signing self-signing key as returned
                by the remote server.

        Return:
            The device IDs for the given keys.
        """
        device_ids = []

        current_keys_map = await self.store.get_e2e_cross_signing_keys_bulk([user_id])
        current_keys = current_keys_map.get(user_id) or {}

        if master_key and master_key != current_keys.get("master"):
            await self.store.set_e2e_cross_signing_key(user_id, "master", master_key)
            _, verify_key = get_verify_key_from_cross_signing_key(master_key)
            # verify_key is a VerifyKey from signedjson, which uses
            # .version to denote the portion of the key ID after the
            # algorithm and colon, which is the device ID
            device_ids.append(verify_key.version)
        if self_signing_key and self_signing_key != current_keys.get("self_signing"):
            await self.store.set_e2e_cross_signing_key(
                user_id, "self_signing", self_signing_key
            )
            _, verify_key = get_verify_key_from_cross_signing_key(self_signing_key)
            device_ids.append(verify_key.version)

        return device_ids

    async def handle_room_un_partial_stated(self, room_id: str) -> None:
        """Handles sending appropriate device list updates in a room that has
        gone from partial to full state.
        """

        pending_updates = (
            await self.store.get_pending_remote_device_list_updates_for_room(room_id)
        )

        for user_id, device_id in pending_updates:
            logger.info(
                "Got pending device list update in room %s: %s / %s",
                room_id,
                user_id,
                device_id,
            )
            position = await self.store.add_device_change_to_streams(
                user_id,
                [device_id],
                room_ids=[room_id],
            )

            if not position:
                # This should only happen if there are no updates, which
                # shouldn't happen when we've passed in a non-empty set of
                # device IDs.
                continue

            self._notifier.on_new_event(
                StreamKeyType.DEVICE_LIST, position, rooms=[room_id]
            )


class DeviceListUpdater(DeviceListWorkerUpdater):
    """Handles incoming device list updates from federation and updates the DB.

    This is only instanciated on the first device list writer, as it uses
    in-process linearizers for some operations."""

    def __init__(self, hs: "HomeServer", device_handler: DeviceWriterHandler):
        super().__init__(hs)

        self.hs = hs
        self.federation = hs.get_federation_client()
        self.server_name = hs.hostname  # nb must be called this for @measure_func
        self.clock = hs.get_clock()  # nb must be called this for @measure_func
        self.device_handler = device_handler

        self._remote_edu_linearizer = Linearizer(
            name="remote_device_list", clock=self.clock
        )
        self._resync_linearizer = Linearizer(
            name="remote_device_resync", clock=self.clock
        )

        # user_id -> list of updates waiting to be handled.
        self._pending_updates: dict[
            str, list[tuple[str, str, Iterable[str], JsonDict]]
        ] = {}

        # Recently seen stream ids. We don't bother keeping these in the DB,
        # but they're useful to have them about to reduce the number of spurious
        # resyncs.
        self._seen_updates: ExpiringCache[str, set[str]] = ExpiringCache(
            cache_name="device_update_edu",
            server_name=self.server_name,
            hs=self.hs,
            clock=self.clock,
            max_len=10000,
            expiry_ms=30 * 60 * 1000,
            iterable=True,
        )

        # Attempt to resync out of sync device lists every 30s.
        self._resync_retry_lock = Lock()
        self.clock.looping_call(
            self.hs.run_as_background_process,
            30 * 1000,
            func=self._maybe_retry_device_resync,
            desc="_maybe_retry_device_resync",
        )

    @trace
    async def incoming_device_list_update(
        self, origin: str, edu_content: JsonDict
    ) -> None:
        """Called on incoming device list update from federation. Responsible
        for parsing the EDU and adding to pending updates list.
        """

        set_tag("origin", origin)
        set_tag("edu_content", str(edu_content))
        user_id = edu_content.pop("user_id")
        device_id = edu_content.pop("device_id")
        stream_id = str(edu_content.pop("stream_id"))  # They may come as ints
        prev_ids = edu_content.pop("prev_id", [])
        if not isinstance(prev_ids, list):
            raise SynapseError(
                400, "Device list update had an invalid 'prev_ids' field"
            )
        prev_ids = [str(p) for p in prev_ids]  # They may come as ints

        if get_domain_from_id(user_id) != origin:
            # TODO: Raise?
            logger.warning(
                "Got device list update edu for %r/%r from %r",
                user_id,
                device_id,
                origin,
            )

            set_tag("error", True)
            log_kv(
                {
                    "message": "Got a device list update edu from a user and "
                    "device which does not match the origin of the request.",
                    "user_id": user_id,
                    "device_id": device_id,
                }
            )
            return

        # Check if we are partially joining any rooms. If so we need to store
        # all device list updates so that we can handle them correctly once we
        # know who is in the room.
        # TODO(faster_joins): this fetches and processes a bunch of data that we don't
        # use. Could be replaced by a tighter query e.g.
        #   SELECT EXISTS(SELECT 1 FROM partial_state_rooms)
        partial_rooms = await self.store.get_partial_state_room_resync_info()
        if partial_rooms:
            await self.store.add_remote_device_list_to_pending(
                user_id,
                device_id,
            )
            self._notifier.notify_replication()

        room_ids = await self.store.get_rooms_for_user(user_id)
        if not room_ids:
            # We don't share any rooms with this user. Ignore update, as we
            # probably won't get any further updates.
            set_tag("error", True)
            log_kv(
                {
                    "message": "Got an update from a user for which "
                    "we don't share any rooms",
                    "other user_id": user_id,
                }
            )
            logger.warning(
                "Got device list update edu for %r/%r, but don't share a room",
                user_id,
                device_id,
            )
            return

        logger.debug("Received device list update for %r/%r", user_id, device_id)

        self._pending_updates.setdefault(user_id, []).append(
            (device_id, stream_id, prev_ids, edu_content)
        )

        await self._handle_device_updates(user_id)

    @measure_func("_incoming_device_list_update")
    async def _handle_device_updates(self, user_id: str) -> None:
        "Actually handle pending updates."

        async with self._remote_edu_linearizer.queue(user_id):
            pending_updates = self._pending_updates.pop(user_id, [])
            if not pending_updates:
                # This can happen since we batch updates
                return

            for device_id, stream_id, prev_ids, _ in pending_updates:
                logger.debug(
                    "Handling update %r/%r, ID: %r, prev: %r ",
                    user_id,
                    device_id,
                    stream_id,
                    prev_ids,
                )

            # Given a list of updates we check if we need to resync. This
            # happens if we've missed updates.
            resync = await self._need_to_do_resync(user_id, pending_updates)

            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "Received device list update for %s, requiring resync: %s. Devices: %s",
                    user_id,
                    resync,
                    ", ".join(u[0] for u in pending_updates),
                )

            if resync:
                # We mark as stale up front in case we get restarted.
                await self.store.mark_remote_users_device_caches_as_stale([user_id])
                self.hs.run_as_background_process(
                    "_maybe_retry_device_resync",
                    self.multi_user_device_resync,
                    [user_id],
                    False,
                )
            else:
                # Simply update the single device, since we know that is the only
                # change (because of the single prev_id matching the current cache)
                for device_id, stream_id, _, content in pending_updates:
                    await self.store.update_remote_device_list_cache_entry(
                        user_id, device_id, content, stream_id
                    )

                await self.device_handler.notify_device_update(
                    user_id, [device_id for device_id, _, _, _ in pending_updates]
                )

                self._seen_updates.setdefault(user_id, set()).update(
                    stream_id for _, stream_id, _, _ in pending_updates
                )

    async def _need_to_do_resync(
        self, user_id: str, updates: Iterable[tuple[str, str, Iterable[str], JsonDict]]
    ) -> bool:
        """Given a list of updates for a user figure out if we need to do a full
        resync, or whether we have enough data that we can just apply the delta.
        """
        seen_updates: set[str] = self._seen_updates.get(user_id, set())

        extremity = await self.store.get_device_list_last_stream_id_for_remote(user_id)

        logger.debug("Current extremity for %r: %r", user_id, extremity)

        stream_id_in_updates = set()  # stream_ids in updates list
        for _, stream_id, prev_ids, _ in updates:
            if not prev_ids:
                # We always do a resync if there are no previous IDs
                return True

            for prev_id in prev_ids:
                if prev_id == extremity:
                    continue
                elif prev_id in seen_updates:
                    continue
                elif prev_id in stream_id_in_updates:
                    continue
                else:
                    return True

            stream_id_in_updates.add(stream_id)

        return False

    @trace
    async def _maybe_retry_device_resync(self) -> None:
        """Retry to resync device lists that are out of sync, except if another retry is
        in progress.
        """
        # If the lock can not be acquired we want to always return immediately instead of blocking here
        if not self._resync_retry_lock.acquire(blocking=False):
            return
        try:
            # Get all of the users that need resyncing.
            need_resync = await self.store.get_user_ids_requiring_device_list_resync()

            # Filter out users whose host is marked as "down" up front.
            hosts = await filter_destinations_by_retry_limiter(
                {get_domain_from_id(u) for u in need_resync}, self.clock, self.store
            )
            hosts = set(hosts)

            # Iterate over the set of user IDs.
            for user_id in need_resync:
                if get_domain_from_id(user_id) not in hosts:
                    continue

                try:
                    # Try to resync the current user's devices list.
                    result = (await self.multi_user_device_resync([user_id], False))[
                        user_id
                    ]

                    # user_device_resync only returns a result if it managed to
                    # successfully resync and update the database. Updating the table
                    # of users requiring resync isn't necessary here as
                    # user_device_resync already does it (through
                    # self.store.update_remote_device_list_cache).
                    if result:
                        logger.debug(
                            "Successfully resynced the device list for %s",
                            user_id,
                        )
                except Exception as e:
                    # If there was an issue resyncing this user, e.g. if the remote
                    # server sent a malformed result, just log the error instead of
                    # aborting all the subsequent resyncs.
                    logger.debug(
                        "Could not resync the device list for %s: %s",
                        user_id,
                        e,
                    )
        finally:
            self._resync_retry_lock.release()

    async def multi_user_device_resync(
        self, user_ids: list[str], mark_failed_as_stale: bool = True
    ) -> dict[str, JsonMapping | None]:
        """
        Like `user_device_resync` but operates on multiple users **from the same origin**
        at once.

        Returns:
            Dict from User ID to the same Dict as `user_device_resync`.
        """
        if not user_ids:
            return {}

        origins = {UserID.from_string(user_id).domain for user_id in user_ids}

        if len(origins) != 1:
            raise InvalidAPICallError(f"Only one origin permitted, got {origins!r}")

        result = {}
        failed = set()
        # TODO(Perf): Actually batch these up
        for user_id in user_ids:
            async with self._resync_linearizer.queue(user_id):
                (
                    user_result,
                    user_failed,
                ) = await self._user_device_resync_returning_failed(user_id)
            result[user_id] = user_result
            if user_failed:
                failed.add(user_id)

        if mark_failed_as_stale:
            await self.store.mark_remote_users_device_caches_as_stale(failed)

        return result

    async def _user_device_resync_returning_failed(
        self, user_id: str
    ) -> tuple[JsonMapping | None, bool]:
        """Fetches all devices for a user and updates the device cache with them.

        Args:
            user_id: The user's id whose device_list will be updated.
        Returns:
            - A dict with device info as under the "devices" in the result of this
              request:
              https://matrix.org/docs/spec/server_server/r0.1.2#get-matrix-federation-v1-user-devices-userid
              None when we weren't able to fetch the device info for some reason,
              e.g. due to a connection problem.
            - True iff the resync failed and the device list should be marked as stale.
        """
        # Check that we haven't gone and fetched the devices since we last
        # checked if we needed to resync these device lists.
        if await self.store.get_users_whose_devices_are_cached([user_id]):
            cached = await self.store.get_cached_devices_for_user(user_id)
            return cached, False

        logger.debug("Attempting to resync the device list for %s", user_id)
        log_kv({"message": "Doing resync to update device list."})
        # Fetch all devices for the user.
        origin = get_domain_from_id(user_id)
        try:
            result = await self.federation.query_user_devices(origin, user_id)
        except NotRetryingDestination:
            return None, True
        except (RequestSendFailed, HttpResponseException) as e:
            logger.warning(
                "Failed to handle device list update for %s: %s",
                user_id,
                e,
            )

            # We abort on exceptions rather than accepting the update
            # as otherwise synapse will 'forget' that its device list
            # is out of date. If we bail then we will retry the resync
            # next time we get a device list update for this user_id.
            # This makes it more likely that the device lists will
            # eventually become consistent.
            return None, True
        except FederationDeniedError as e:
            set_tag("error", True)
            log_kv({"reason": "FederationDeniedError"})
            logger.info(e)
            return None, False
        except Exception as e:
            set_tag("error", True)
            log_kv(
                {"message": "Exception raised by federation request", "exception": e}
            )
            logger.exception("Failed to handle device list update for %s", user_id)

            return None, True
        log_kv({"result": result})
        stream_id = result["stream_id"]
        devices = result["devices"]

        # Get the master key and the self-signing key for this user if provided in the
        # response (None if not in the response).
        # The response will not contain the user signing key, as this key is only used by
        # its owner, thus it doesn't make sense to send it over federation.
        master_key = result.get("master_key")
        self_signing_key = result.get("self_signing_key")

        ignore_devices = False
        # If the remote server has more than ~1000 devices for this user
        # we assume that something is going horribly wrong (e.g. a bot
        # that logs in and creates a new device every time it tries to
        # send a message).  Maintaining lots of devices per user in the
        # cache can cause serious performance issues as if this request
        # takes more than 60s to complete, internal replication from the
        # inbound federation worker to the synapse master may time out
        # causing the inbound federation to fail and causing the remote
        # server to retry, causing a DoS.  So in this scenario we give
        # up on storing the total list of devices and only handle the
        # delta instead.
        if len(devices) > 1000:
            logger.warning(
                "Ignoring device list snapshot for %s as it has >1K devs (%d)",
                user_id,
                len(devices),
            )
            devices = []
            ignore_devices = True
        else:
            prev_stream_id = await self.store.get_device_list_last_stream_id_for_remote(
                user_id
            )
            cached_devices = await self.store.get_cached_devices_for_user(user_id)

            # To ensure that a user with no devices is cached, we skip the resync only
            # if we have a stream_id from previously writing a cache entry.
            if prev_stream_id is not None and cached_devices == {
                d["device_id"]: d for d in devices
            }:
                logger.info(
                    "Skipping device list resync for %s, as our cache matches already",
                    user_id,
                )
                devices = []
                ignore_devices = True

        for device in devices:
            logger.debug(
                "Handling resync update %r/%r, ID: %r",
                user_id,
                device["device_id"],
                stream_id,
            )

        if not ignore_devices:
            await self.store.update_remote_device_list_cache(
                user_id, devices, stream_id
            )
        # mark the cache as valid, whether or not we actually processed any device
        # list updates.
        await self.store.mark_remote_user_device_cache_as_valid(user_id)
        device_ids = [device["device_id"] for device in devices]

        # Handle cross-signing keys.
        cross_signing_device_ids = await self.process_cross_signing_key_update(
            user_id,
            master_key,
            self_signing_key,
        )
        device_ids = device_ids + cross_signing_device_ids

        if device_ids:
            await self.device_handler.notify_device_update(user_id, device_ids)

        # We clobber the seen updates since we've re-synced from a given
        # point.
        self._seen_updates[user_id] = {stream_id}

        return result, False
