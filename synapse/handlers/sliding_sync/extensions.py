#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import itertools
import logging
from collections import ChainMap
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Mapping,
    MutableMapping,
    Sequence,
    cast,
)

from typing_extensions import TypeAlias, assert_never

from synapse.api.constants import (
    AccountDataTypes,
    EduTypes,
    EventTypes,
    ProfileFields,
    ProfileUpdateAction,
    StickyEvent,
)
from synapse.events.utils import FilteredEvent
from synapse.handlers.receipts import ReceiptEventSource
from synapse.logging.opentracing import trace
from synapse.storage.databases.main.receipts import ReceiptInRoom
from synapse.types import (
    Absent,
    AbsentType,
    DeviceListUpdates,
    JsonDict,
    JsonMapping,
    JsonValue,
    MultiWriterStreamToken,
    SlidingSyncStreamToken,
    StrCollection,
    StreamToken,
    ThreadSubscriptionsToken,
    UserID,
)
from synapse.types.handlers.sliding_sync import (
    HaveSentRoomFlag,
    MutablePerConnectionState,
    OperationType,
    PerConnectionState,
    SlidingSyncConfig,
    SlidingSyncResult,
    StateValues,
)
from synapse.types.rest.client import SlidingSyncStickyEventsToken
from synapse.util.async_helpers import (
    concurrently_execute,
    gather_optional_coroutines,
)
from synapse.visibility import filter_and_transform_events_for_client

_ThreadSubscription: TypeAlias = (
    SlidingSyncResult.Extensions.ThreadSubscriptionsExtension.ThreadSubscription
)
_ThreadUnsubscription: TypeAlias = (
    SlidingSyncResult.Extensions.ThreadSubscriptionsExtension.ThreadUnsubscription
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class SlidingSyncExtensionHandler:
    """Handles the extensions to sliding sync."""

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.device_handler = hs.get_device_handler()
        self.push_rules_handler = hs.get_push_rules_handler()
        self.clock = hs.get_clock()
        self._storage_controllers = hs.get_storage_controllers()
        self._enable_thread_subscriptions = hs.config.experimental.msc4306_enabled
        self._enable_sticky_events = hs.config.experimental.msc4354_enabled
        self._enable_profiles = hs.config.server.include_profile_updates_in_sync

    @trace
    async def get_extensions_response(
        self,
        sync_config: SlidingSyncConfig,
        previous_connection_state: "PerConnectionState",
        new_connection_state: "MutablePerConnectionState",
        all_interested_room_ids: set[str],
        actual_lists: Mapping[str, SlidingSyncResult.SlidingWindowList],
        actual_room_ids: set[str],
        actual_room_response_map: Mapping[str, SlidingSyncResult.RoomResult],
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None,
    ) -> SlidingSyncResult.Extensions:
        """Handle extension requests.

        Args:
            sync_config: Sync configuration
            previous_connection_state: Snapshot of the current per-connection state
            new_connection_state: A mutable copy of the per-connection
                state, used to record updates to the state during this request.
            all_interested_room_ids: The IDs of all rooms that the client is interested in,
                even if they don't appear in the current limited window.
                See `SlidingSyncInterestedRooms.all_rooms`.
            actual_lists: Sliding window API. A map of list key to list results in the
                Sliding Sync response.
            actual_room_ids: The actual room IDs in the the Sliding Sync response.
            actual_room_response_map: A map of room ID to room results in the the
                Sliding Sync response.
            to_token: The latest point in the stream to sync up to.
            from_token: The point in the stream to sync from.
        """

        if sync_config.extensions is None:
            return SlidingSyncResult.Extensions()

        to_device_coro = None
        if sync_config.extensions.to_device is not None:
            to_device_coro = self.get_to_device_extension_response(
                sync_config=sync_config,
                to_device_request=sync_config.extensions.to_device,
                to_token=to_token,
            )

        e2ee_coro = None
        if sync_config.extensions.e2ee is not None:
            e2ee_coro = self.get_e2ee_extension_response(
                sync_config=sync_config,
                e2ee_request=sync_config.extensions.e2ee,
                to_token=to_token,
                from_token=from_token,
            )

        account_data_coro = None
        if sync_config.extensions.account_data is not None:
            account_data_coro = self.get_account_data_extension_response(
                sync_config=sync_config,
                previous_connection_state=previous_connection_state,
                new_connection_state=new_connection_state,
                actual_lists=actual_lists,
                actual_room_ids=actual_room_ids,
                account_data_request=sync_config.extensions.account_data,
                to_token=to_token,
                from_token=from_token,
            )

        receipts_coro = None
        if sync_config.extensions.receipts is not None:
            receipts_coro = self.get_receipts_extension_response(
                sync_config=sync_config,
                previous_connection_state=previous_connection_state,
                new_connection_state=new_connection_state,
                actual_lists=actual_lists,
                actual_room_ids=actual_room_ids,
                actual_room_response_map=actual_room_response_map,
                receipts_request=sync_config.extensions.receipts,
                to_token=to_token,
                from_token=from_token,
            )

        typing_coro = None
        if sync_config.extensions.typing is not None:
            typing_coro = self.get_typing_extension_response(
                sync_config=sync_config,
                actual_lists=actual_lists,
                actual_room_ids=actual_room_ids,
                actual_room_response_map=actual_room_response_map,
                typing_request=sync_config.extensions.typing,
                to_token=to_token,
                from_token=from_token,
            )

        thread_subs_coro = None
        if (
            sync_config.extensions.thread_subscriptions is not None
            and self._enable_thread_subscriptions
        ):
            thread_subs_coro = self.get_thread_subscriptions_extension_response(
                sync_config=sync_config,
                thread_subscriptions_request=sync_config.extensions.thread_subscriptions,
                to_token=to_token,
                from_token=from_token,
            )

        sticky_events_coro = None
        if (
            sync_config.extensions.sticky_events is not Absent
            and self._enable_sticky_events
        ):
            sticky_events_coro = self.get_sticky_events_extension_response(
                sync_config=sync_config,
                sticky_events_request=sync_config.extensions.sticky_events,
                all_interested_room_ids=all_interested_room_ids,
                to_token=to_token,
                from_token=from_token,
            )

        profiles_coro = None
        if sync_config.extensions.profiles is not Absent and self._enable_profiles:
            profiles_coro = self.get_profiles_extension_response(
                sync_config=sync_config,
                profiles_request=sync_config.extensions.profiles,
                actual_room_ids=actual_room_ids,
                to_token=to_token,
                from_token=from_token,
                actual_room_response_map=actual_room_response_map,
                actual_lists=actual_lists,
            )

        (
            to_device_response,
            e2ee_response,
            account_data_response,
            receipts_response,
            typing_response,
            thread_subs_response,
            sticky_events_response,
            profiles_response,
        ) = await gather_optional_coroutines(
            to_device_coro,
            e2ee_coro,
            account_data_coro,
            receipts_coro,
            typing_coro,
            thread_subs_coro,
            sticky_events_coro,
            profiles_coro,
        )

        return SlidingSyncResult.Extensions(
            to_device=to_device_response,
            e2ee=e2ee_response,
            account_data=account_data_response,
            receipts=receipts_response,
            typing=typing_response,
            thread_subscriptions=thread_subs_response,
            sticky_events=sticky_events_response,
            profiles=profiles_response,
        )

    def find_relevant_room_ids_for_extension(
        self,
        requested_lists: StrCollection | None,
        requested_room_ids: StrCollection | None,
        actual_lists: Mapping[str, SlidingSyncResult.SlidingWindowList],
        actual_room_ids: AbstractSet[str],
    ) -> set[str]:
        """
        Handle the reserved `lists`/`rooms` keys for extensions. Extensions should only
        return results for rooms in the Sliding Sync response. This matches up the
        requested rooms/lists with the actual lists/rooms in the Sliding Sync response.

        {"lists": []}                    // Do not process any lists.
        {"lists": ["rooms", "dms"]}      // Process only a subset of lists.
        {"lists": ["*"]}                 // Process all lists defined in the Sliding Window API. (This is the default.)

        {"rooms": []}                    // Do not process any specific rooms.
        {"rooms": ["!a:b", "!c:d"]}      // Process only a subset of room subscriptions.
        {"rooms": ["*"]}                 // Process all room subscriptions defined in the Room Subscription API. (This is the default.)

        Args:
            requested_lists: The `lists` from the extension request.
            requested_room_ids: The `rooms` from the extension request.
            actual_lists: The actual lists from the Sliding Sync response.
            actual_room_ids: The actual room subscriptions from the Sliding Sync request.
        """

        # We only want to include account data for rooms that are already in the sliding
        # sync response AND that were requested in the account data request.
        relevant_room_ids: set[str] = set()

        # See what rooms from the room subscriptions we should get account data for
        if requested_room_ids is not None:
            for room_id in requested_room_ids:
                # A wildcard means we process all rooms from the room subscriptions
                if room_id == "*":
                    relevant_room_ids.update(actual_room_ids)
                    break

                if room_id in actual_room_ids:
                    relevant_room_ids.add(room_id)

        # See what rooms from the sliding window lists we should get account data for
        if requested_lists is not None:
            for list_key in requested_lists:
                # Just some typing because we share the variable name in multiple places
                actual_list: SlidingSyncResult.SlidingWindowList | None = None

                # A wildcard means we process rooms from all lists
                if list_key == "*":
                    for actual_list in actual_lists.values():
                        # We only expect a single SYNC operation for any list
                        assert len(actual_list.ops) == 1
                        sync_op = actual_list.ops[0]
                        assert sync_op.op == OperationType.SYNC

                        relevant_room_ids.update(sync_op.room_ids)

                    break

                actual_list = actual_lists.get(list_key)
                if actual_list is not None:
                    # We only expect a single SYNC operation for any list
                    assert len(actual_list.ops) == 1
                    sync_op = actual_list.ops[0]
                    assert sync_op.op == OperationType.SYNC

                    relevant_room_ids.update(sync_op.room_ids)

        return relevant_room_ids

    @trace
    async def get_to_device_extension_response(
        self,
        sync_config: SlidingSyncConfig,
        to_device_request: SlidingSyncConfig.Extensions.ToDeviceExtension,
        to_token: StreamToken,
    ) -> SlidingSyncResult.Extensions.ToDeviceExtension | None:
        """Handle to-device extension (MSC3885)

        Args:
            sync_config: Sync configuration
            to_device_request: The to-device extension from the request
            to_token: The point in the stream to sync up to.
        """
        user_id = sync_config.user.to_string()
        device_id = sync_config.requester.device_id

        # Skip if the extension is not enabled
        if not to_device_request.enabled:
            return None

        # Check that this request has a valid device ID (not all requests have
        # to belong to a device, and so device_id is None)
        if device_id is None:
            return SlidingSyncResult.Extensions.ToDeviceExtension(
                next_batch=f"{to_token.to_device_key}",
                events=[],
            )

        since_stream_id = 0
        if to_device_request.since is not None:
            # We've already validated this is an int.
            since_stream_id = int(to_device_request.since)

            if to_token.to_device_key < since_stream_id:
                # The since token is ahead of our current token, so we return an
                # empty response.
                logger.warning(
                    "Got to-device.since from the future. since token: %r is ahead of our current to_device stream position: %r",
                    since_stream_id,
                    to_token.to_device_key,
                )
                return SlidingSyncResult.Extensions.ToDeviceExtension(
                    next_batch=to_device_request.since,
                    events=[],
                )

            # Delete everything before the given since token, as we know the
            # device must have received them.
            deleted = await self.store.delete_messages_for_device(
                user_id=user_id,
                device_id=device_id,
                up_to_stream_id=since_stream_id,
            )

            logger.debug(
                "Deleted %d to-device messages up to %d for %s",
                deleted,
                since_stream_id,
                user_id,
            )

        messages, stream_id = await self.store.get_messages_for_device(
            user_id=user_id,
            device_id=device_id,
            from_stream_id=since_stream_id,
            to_stream_id=to_token.to_device_key,
            limit=min(to_device_request.limit, 100),  # Limit to at most 100 events
        )

        return SlidingSyncResult.Extensions.ToDeviceExtension(
            next_batch=f"{stream_id}",
            events=messages,
        )

    @trace
    async def get_e2ee_extension_response(
        self,
        sync_config: SlidingSyncConfig,
        e2ee_request: SlidingSyncConfig.Extensions.E2eeExtension,
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None,
    ) -> SlidingSyncResult.Extensions.E2eeExtension | None:
        """Handle E2EE device extension (MSC3884)

        Args:
            sync_config: Sync configuration
            e2ee_request: The e2ee extension from the request
            to_token: The point in the stream to sync up to.
            from_token: The point in the stream to sync from.
        """
        user_id = sync_config.user.to_string()
        device_id = sync_config.requester.device_id

        # Skip if the extension is not enabled
        if not e2ee_request.enabled:
            return None

        device_list_updates: DeviceListUpdates | None = None
        if from_token is not None:
            # TODO: This should take into account the `from_token` and `to_token`
            device_list_updates = await self.device_handler.get_user_ids_changed(
                user_id=user_id,
                from_token=from_token.stream_token,
            )

        device_one_time_keys_count: Mapping[str, int] = {}
        device_unused_fallback_key_types: Sequence[str] = []
        if device_id:
            # TODO: We should have a way to let clients differentiate between the states of:
            #   * no change in OTK count since the provided since token
            #   * the server has zero OTKs left for this device
            #  Spec issue: https://github.com/matrix-org/matrix-doc/issues/3298
            device_one_time_keys_count = await self.store.count_e2e_one_time_keys(
                user_id, device_id
            )
            device_unused_fallback_key_types = (
                await self.store.get_e2e_unused_fallback_key_types(user_id, device_id)
            )

        return SlidingSyncResult.Extensions.E2eeExtension(
            device_list_updates=device_list_updates,
            device_one_time_keys_count=device_one_time_keys_count,
            device_unused_fallback_key_types=device_unused_fallback_key_types,
        )

    @trace
    async def get_account_data_extension_response(
        self,
        sync_config: SlidingSyncConfig,
        previous_connection_state: "PerConnectionState",
        new_connection_state: "MutablePerConnectionState",
        actual_lists: Mapping[str, SlidingSyncResult.SlidingWindowList],
        actual_room_ids: set[str],
        account_data_request: SlidingSyncConfig.Extensions.AccountDataExtension,
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None,
    ) -> SlidingSyncResult.Extensions.AccountDataExtension | None:
        """Handle Account Data extension (MSC3959)

        Args:
            sync_config: Sync configuration
            actual_lists: Sliding window API. A map of list key to list results in the
                Sliding Sync response.
            actual_room_ids: The actual room IDs in the the Sliding Sync response.
            account_data_request: The account_data extension from the request
            to_token: The point in the stream to sync up to.
            from_token: The point in the stream to sync from.
        """
        user_id = sync_config.user.to_string()

        # Skip if the extension is not enabled
        if not account_data_request.enabled:
            return None

        global_account_data_map: Mapping[str, JsonMapping] = {}
        if from_token is not None:
            # TODO: This should take into account the `from_token` and `to_token`
            global_account_data_map = (
                await self.store.get_updated_global_account_data_for_user(
                    user_id, from_token.stream_token.account_data_key
                )
            )

            # TODO: This should take into account the `from_token` and `to_token`
            have_push_rules_changed = await self.store.have_push_rules_changed_for_user(
                user_id, from_token.stream_token.push_rules_key
            )
            if have_push_rules_changed:
                # TODO: This should take into account the `from_token` and `to_token`
                global_account_data_map[
                    AccountDataTypes.PUSH_RULES
                ] = await self.push_rules_handler.push_rules_for_user(sync_config.user)
        else:
            # TODO: This should take into account the `to_token`
            immutable_global_account_data_map = (
                await self.store.get_global_account_data_for_user(user_id)
            )

            # Use a `ChainMap` to avoid copying the immutable data from the cache
            global_account_data_map = ChainMap(
                {
                    # TODO: This should take into account the `to_token`
                    AccountDataTypes.PUSH_RULES: await self.push_rules_handler.push_rules_for_user(
                        sync_config.user
                    )
                },
                # Cast is safe because `ChainMap` only mutates the top-most map,
                # see https://github.com/python/typeshed/issues/8430
                cast(
                    MutableMapping[str, JsonMapping], immutable_global_account_data_map
                ),
            )

        # Fetch room account data
        #
        account_data_by_room_map: MutableMapping[str, Mapping[str, JsonMapping]] = {}
        relevant_room_ids = self.find_relevant_room_ids_for_extension(
            requested_lists=account_data_request.lists,
            requested_room_ids=account_data_request.rooms,
            actual_lists=actual_lists,
            actual_room_ids=actual_room_ids,
        )
        if len(relevant_room_ids) > 0:
            # We need to handle the different cases depending on if we have sent
            # down account data previously or not, so we split the relevant
            # rooms up into different collections based on status.
            live_rooms = set()
            previously_rooms: dict[str, int] = {}
            initial_rooms = set()

            for room_id in relevant_room_ids:
                if not from_token:
                    initial_rooms.add(room_id)
                    continue

                room_status = previous_connection_state.account_data.have_sent_room(
                    room_id
                )
                if room_status.status == HaveSentRoomFlag.LIVE:
                    live_rooms.add(room_id)
                elif room_status.status == HaveSentRoomFlag.PREVIOUSLY:
                    assert room_status.last_token is not None
                    previously_rooms[room_id] = room_status.last_token
                elif room_status.status == HaveSentRoomFlag.NEVER:
                    initial_rooms.add(room_id)
                else:
                    assert_never(room_status.status)

            # We fetch all room account data since the from_token. This is so
            # that we can record which rooms have updates that haven't been sent
            # down.
            #
            # Mapping from room_id to mapping of `type` to `content` of room account
            # data events.
            all_updates_since_the_from_token: Mapping[
                str, Mapping[str, JsonMapping]
            ] = {}
            if from_token is not None:
                # TODO: This should take into account the `from_token` and `to_token`
                all_updates_since_the_from_token = (
                    await self.store.get_updated_room_account_data_for_user(
                        user_id, from_token.stream_token.account_data_key
                    )
                )

                # Add room tags
                #
                # TODO: This should take into account the `from_token` and `to_token`
                tags_by_room = await self.store.get_updated_tags(
                    user_id, from_token.stream_token.account_data_key
                )
                for room_id, tags in tags_by_room.items():
                    all_updates_since_the_from_token.setdefault(room_id, {})[
                        AccountDataTypes.TAG
                    ] = {"tags": tags}

            # For live rooms we just get the updates from `all_updates_since_the_from_token`
            if live_rooms:
                for room_id in all_updates_since_the_from_token.keys() & live_rooms:
                    account_data_by_room_map[room_id] = (
                        all_updates_since_the_from_token[room_id]
                    )

            # For previously and initial rooms we query each room individually.
            if previously_rooms or initial_rooms:

                async def handle_previously(room_id: str) -> None:
                    # Either get updates or all account data in the room
                    # depending on if the room state is PREVIOUSLY or NEVER.
                    previous_token = previously_rooms.get(room_id)
                    if previous_token is not None:
                        room_account_data = await (
                            self.store.get_updated_room_account_data_for_user_for_room(
                                user_id=user_id,
                                room_id=room_id,
                                from_stream_id=previous_token,
                                to_stream_id=to_token.account_data_key,
                            )
                        )

                        # Add room tags
                        changed = await self.store.has_tags_changed_for_room(
                            user_id=user_id,
                            room_id=room_id,
                            from_stream_id=previous_token,
                            to_stream_id=to_token.account_data_key,
                        )
                        if changed:
                            # XXX: Ideally, this should take into account the `to_token`
                            # and return the set of tags at that time but we don't track
                            # changes to tags so we just have to return all tags for the
                            # room.
                            immutable_tag_map = await self.store.get_tags_for_room(
                                user_id, room_id
                            )
                            room_account_data[AccountDataTypes.TAG] = {
                                "tags": immutable_tag_map
                            }

                        # Only add an entry if there were any updates.
                        if room_account_data:
                            account_data_by_room_map[room_id] = room_account_data
                    else:
                        # TODO: This should take into account the `to_token`
                        immutable_room_account_data = (
                            await self.store.get_account_data_for_room(user_id, room_id)
                        )

                        # Add room tags
                        #
                        # XXX: Ideally, this should take into account the `to_token`
                        # and return the set of tags at that time but we don't track
                        # changes to tags so we just have to return all tags for the
                        # room.
                        immutable_tag_map = await self.store.get_tags_for_room(
                            user_id, room_id
                        )

                        account_data_by_room_map[room_id] = ChainMap(
                            {AccountDataTypes.TAG: {"tags": immutable_tag_map}}
                            if immutable_tag_map
                            else {},
                            # Cast is safe because `ChainMap` only mutates the top-most map,
                            # see https://github.com/python/typeshed/issues/8430
                            cast(
                                MutableMapping[str, JsonMapping],
                                immutable_room_account_data,
                            ),
                        )

                # We handle these rooms concurrently to speed it up.
                await concurrently_execute(
                    handle_previously,
                    previously_rooms.keys() | initial_rooms,
                    limit=20,
                )

            # Now record which rooms are now up to data, and which rooms have
            # pending updates to send.
            new_connection_state.account_data.record_sent_rooms(previously_rooms.keys())
            new_connection_state.account_data.record_sent_rooms(initial_rooms)
            missing_updates = (
                all_updates_since_the_from_token.keys() - relevant_room_ids
            )
            if missing_updates:
                # If we have missing updates then we must have had a from_token.
                assert from_token is not None

                new_connection_state.account_data.record_unsent_rooms(
                    missing_updates, from_token.stream_token.account_data_key
                )

        return SlidingSyncResult.Extensions.AccountDataExtension(
            global_account_data_map=global_account_data_map,
            account_data_by_room_map=account_data_by_room_map,
        )

    @trace
    async def get_receipts_extension_response(
        self,
        sync_config: SlidingSyncConfig,
        previous_connection_state: "PerConnectionState",
        new_connection_state: "MutablePerConnectionState",
        actual_lists: Mapping[str, SlidingSyncResult.SlidingWindowList],
        actual_room_ids: set[str],
        actual_room_response_map: Mapping[str, SlidingSyncResult.RoomResult],
        receipts_request: SlidingSyncConfig.Extensions.ReceiptsExtension,
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None,
    ) -> SlidingSyncResult.Extensions.ReceiptsExtension | None:
        """Handle Receipts extension (MSC3960)

        Args:
            sync_config: Sync configuration
            previous_connection_state: The current per-connection state
            new_connection_state: A mutable copy of the per-connection
                state, used to record updates to the state.
            actual_lists: Sliding window API. A map of list key to list results in the
                Sliding Sync response.
            actual_room_ids: The actual room IDs in the the Sliding Sync response.
            actual_room_response_map: A map of room ID to room results in the the
                Sliding Sync response.
            account_data_request: The account_data extension from the request
            to_token: The point in the stream to sync up to.
            from_token: The point in the stream to sync from.
        """
        # Skip if the extension is not enabled
        if not receipts_request.enabled:
            return None

        relevant_room_ids = self.find_relevant_room_ids_for_extension(
            requested_lists=receipts_request.lists,
            requested_room_ids=receipts_request.rooms,
            actual_lists=actual_lists,
            actual_room_ids=actual_room_ids,
        )

        room_id_to_receipt_map: dict[str, JsonMapping] = {}
        if len(relevant_room_ids) > 0:
            # We need to handle the different cases depending on if we have sent
            # down receipts previously or not, so we split the relevant rooms
            # up into different collections based on status.
            live_rooms = set()
            previously_rooms: dict[str, MultiWriterStreamToken] = {}
            initial_rooms = set()

            for room_id in relevant_room_ids:
                if not from_token:
                    initial_rooms.add(room_id)
                    continue

                # If we're sending down the room from scratch again for some
                # reason, we should always resend the receipts as well
                # (regardless of if we've sent them down before). This is to
                # mimic the behaviour of what happens on initial sync, where you
                # get a chunk of timeline with all of the corresponding receipts
                # for the events in the timeline.
                #
                # We also resend down receipts when we "expand" the timeline,
                # (see the "XXX: Odd behavior" in
                # `synapse.handlers.sliding_sync`).
                room_result = actual_room_response_map.get(room_id)
                if room_result is not None:
                    if room_result.initial or room_result.unstable_expanded_timeline:
                        initial_rooms.add(room_id)
                        continue

                room_status = previous_connection_state.receipts.have_sent_room(room_id)
                if room_status.status == HaveSentRoomFlag.LIVE:
                    live_rooms.add(room_id)
                elif room_status.status == HaveSentRoomFlag.PREVIOUSLY:
                    assert room_status.last_token is not None
                    previously_rooms[room_id] = room_status.last_token
                elif room_status.status == HaveSentRoomFlag.NEVER:
                    initial_rooms.add(room_id)
                else:
                    assert_never(room_status.status)

            # The set of receipts that we fetched. Private receipts need to be
            # filtered out before returning.
            fetched_receipts = []

            # For live rooms we just fetch all receipts in those rooms since the
            # `since` token.
            if live_rooms:
                assert from_token is not None
                receipts = await self.store.get_linearized_receipts_for_rooms(
                    room_ids=live_rooms,
                    from_key=from_token.stream_token.receipt_key,
                    to_key=to_token.receipt_key,
                )
                fetched_receipts.extend(receipts)

            # For rooms we've previously sent down, but aren't up to date, we
            # need to use the from token from the room status.
            if previously_rooms:
                # Fetch any missing rooms concurrently.

                async def handle_previously_room(room_id: str) -> None:
                    receipt_token = previously_rooms[room_id]
                    # TODO: Limit the number of receipts we're about to send down
                    # for the room, if its too many we should TODO
                    previously_receipts = (
                        await self.store.get_linearized_receipts_for_room(
                            room_id=room_id,
                            from_key=receipt_token,
                            to_key=to_token.receipt_key,
                        )
                    )
                    fetched_receipts.extend(previously_receipts)

                await concurrently_execute(
                    handle_previously_room, previously_rooms.keys(), 20
                )

            if initial_rooms:
                # We also always send down receipts for the current user.
                user_receipts = (
                    await self.store.get_linearized_receipts_for_user_in_rooms(
                        user_id=sync_config.user.to_string(),
                        room_ids=initial_rooms,
                        to_key=to_token.receipt_key,
                    )
                )

                # For rooms we haven't previously sent down, we could send all receipts
                # from that room but we only want to include receipts for events
                # in the timeline to avoid bloating and blowing up the sync response
                # as the number of users in the room increases. (this behavior is part of the spec)
                initial_rooms_and_event_ids = [
                    (room_id, event.event.event_id)
                    for room_id in initial_rooms
                    if room_id in actual_room_response_map
                    for event in actual_room_response_map[room_id].timeline_events
                ]
                initial_receipts = await self.store.get_linearized_receipts_for_events(
                    room_and_event_ids=initial_rooms_and_event_ids,
                )

                # Combine the receipts for a room and add them to
                # `fetched_receipts`
                for room_id in initial_receipts.keys() | user_receipts.keys():
                    receipt_content = ReceiptInRoom.merge_to_content(
                        list(
                            itertools.chain(
                                initial_receipts.get(room_id, []),
                                user_receipts.get(room_id, []),
                            )
                        )
                    )

                    fetched_receipts.append(
                        {
                            "room_id": room_id,
                            "type": EduTypes.RECEIPT,
                            "content": receipt_content,
                        }
                    )

            fetched_receipts = ReceiptEventSource.filter_out_private_receipts(
                fetched_receipts, sync_config.user.to_string()
            )

            for receipt in fetched_receipts:
                # These fields should exist for every receipt
                room_id = receipt["room_id"]
                type = receipt["type"]
                content = receipt["content"]

                room_id_to_receipt_map[room_id] = {"type": type, "content": content}

            # Update the per-connection state to track which rooms we have sent
            # all the receipts for.
            new_connection_state.receipts.record_sent_rooms(previously_rooms.keys())
            new_connection_state.receipts.record_sent_rooms(initial_rooms)

        if from_token:
            # Now find the set of rooms that may have receipts that we're not sending
            # down. We only need to check rooms that we have previously returned
            # receipts for (in `previous_connection_state`) because we only care about
            # updating `LIVE` rooms to `PREVIOUSLY`. The `PREVIOUSLY` rooms will just
            # stay pointing at their previous position so we don't need to waste time
            # checking those and since we default to `NEVER`, rooms that were `NEVER`
            # sent before don't need to be recorded as we'll handle them correctly when
            # they come into range for the first time.
            rooms_no_receipts = [
                room_id
                for room_id, room_status in previous_connection_state.receipts._statuses.items()
                if room_status.status == HaveSentRoomFlag.LIVE
                and room_id not in relevant_room_ids
            ]
            changed_rooms = await self.store.get_rooms_with_receipts_between(
                rooms_no_receipts,
                from_key=from_token.stream_token.receipt_key,
                to_key=to_token.receipt_key,
            )
            new_connection_state.receipts.record_unsent_rooms(
                changed_rooms, from_token.stream_token.receipt_key
            )

        return SlidingSyncResult.Extensions.ReceiptsExtension(
            room_id_to_receipt_map=room_id_to_receipt_map,
        )

    async def get_typing_extension_response(
        self,
        sync_config: SlidingSyncConfig,
        actual_lists: Mapping[str, SlidingSyncResult.SlidingWindowList],
        actual_room_ids: set[str],
        actual_room_response_map: Mapping[str, SlidingSyncResult.RoomResult],
        typing_request: SlidingSyncConfig.Extensions.TypingExtension,
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None,
    ) -> SlidingSyncResult.Extensions.TypingExtension | None:
        """Handle Typing Notification extension (MSC3961)

        Args:
            sync_config: Sync configuration
            actual_lists: Sliding window API. A map of list key to list results in the
                Sliding Sync response.
            actual_room_ids: The actual room IDs in the the Sliding Sync response.
            actual_room_response_map: A map of room ID to room results in the the
                Sliding Sync response.
            account_data_request: The account_data extension from the request
            to_token: The point in the stream to sync up to.
            from_token: The point in the stream to sync from.
        """
        # Skip if the extension is not enabled
        if not typing_request.enabled:
            return None

        relevant_room_ids = self.find_relevant_room_ids_for_extension(
            requested_lists=typing_request.lists,
            requested_room_ids=typing_request.rooms,
            actual_lists=actual_lists,
            actual_room_ids=actual_room_ids,
        )

        room_id_to_typing_map: dict[str, JsonMapping] = {}
        if len(relevant_room_ids) > 0:
            # Note: We don't need to take connection tracking into account for typing
            # notifications because they'll get anything still relevant and hasn't timed
            # out when the room comes into range. We consider the gap where the room
            # fell out of range, as long enough for any typing notifications to have
            # timed out (it's not worth the 30 seconds of data we may have missed).
            typing_source = self.event_sources.sources.typing
            typing_notifications, _ = await typing_source.get_new_events(
                user=sync_config.user,
                from_key=(from_token.stream_token.typing_key if from_token else 0),
                to_key=to_token.typing_key,
                # This is a dummy value and isn't used in the function
                limit=0,
                room_ids=relevant_room_ids,
                is_guest=False,
            )

            for typing_notification in typing_notifications:
                # These fields should exist for every typing notification
                room_id = typing_notification["room_id"]
                type = typing_notification["type"]
                content = typing_notification["content"]

                room_id_to_typing_map[room_id] = {"type": type, "content": content}

        return SlidingSyncResult.Extensions.TypingExtension(
            room_id_to_typing_map=room_id_to_typing_map,
        )

    async def get_thread_subscriptions_extension_response(
        self,
        sync_config: SlidingSyncConfig,
        thread_subscriptions_request: SlidingSyncConfig.Extensions.ThreadSubscriptionsExtension,
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None,
    ) -> SlidingSyncResult.Extensions.ThreadSubscriptionsExtension | None:
        """Handle Thread Subscriptions extension (MSC4308)

        Args:
            sync_config: Sync configuration
            thread_subscriptions_request: The thread_subscriptions extension from the request
            to_token: The point in the stream to sync up to.
            from_token: The point in the stream to sync from.

        Returns:
            the response (None if empty or thread subscriptions are disabled)
        """
        if not thread_subscriptions_request.enabled:
            return None

        limit = thread_subscriptions_request.limit

        if from_token:
            from_stream_id = from_token.stream_token.thread_subscriptions_key
        else:
            from_stream_id = StreamToken.START.thread_subscriptions_key

        to_stream_id = to_token.thread_subscriptions_key

        updates = await self.store.get_latest_updated_thread_subscriptions_for_user(
            user_id=sync_config.user.to_string(),
            from_id=from_stream_id,
            to_id=to_stream_id,
            limit=limit,
        )

        if len(updates) == 0:
            return None

        subscribed_threads: dict[str, dict[str, _ThreadSubscription]] = {}
        unsubscribed_threads: dict[str, dict[str, _ThreadUnsubscription]] = {}
        for stream_id, room_id, thread_root_id, subscribed, automatic in updates:
            if subscribed:
                subscribed_threads.setdefault(room_id, {})[thread_root_id] = (
                    _ThreadSubscription(
                        automatic=automatic,
                        bump_stamp=stream_id,
                    )
                )
            else:
                unsubscribed_threads.setdefault(room_id, {})[thread_root_id] = (
                    _ThreadUnsubscription(bump_stamp=stream_id)
                )

        prev_batch = None
        if len(updates) == limit:
            # Tell the client about a potential gap where there may be more
            # thread subscriptions for it to backpaginate.
            # We subtract one because the 'later in the stream' bound is inclusive,
            # and we already saw the element at index 0.
            prev_batch = ThreadSubscriptionsToken(updates[0][0] - 1)

        return SlidingSyncResult.Extensions.ThreadSubscriptionsExtension(
            subscribed=subscribed_threads,
            unsubscribed=unsubscribed_threads,
            prev_batch=prev_batch,
        )

    async def get_sticky_events_extension_response(
        self,
        sync_config: SlidingSyncConfig,
        sticky_events_request: SlidingSyncConfig.Extensions.StickyEventsExtension,
        all_interested_room_ids: set[str],
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None,
    ) -> SlidingSyncResult.Extensions.StickyEventsExtension | None:
        if not sticky_events_request.enabled:
            return None
        now = self.clock.time_msec()
        # If there is no `since` token specified, start from the beginning of the stream
        # to make sure the client receives all visible (unexpired) sticky events
        since_token = sticky_events_request.since or SlidingSyncStickyEventsToken.START
        (
            sticky_events_to_id,
            room_to_event_ids,
        ) = await self.store.get_sticky_events_in_rooms(
            all_interested_room_ids,
            from_id=since_token.sticky_events_stream_id,
            to_id=to_token.sticky_events_key,
            now=now,
            limit=min(sticky_events_request.limit, StickyEvent.MAX_EVENTS_IN_SYNC),
        )
        # No need to preserve sticky event order here because we will
        # reassemble it in the right order after.
        all_sticky_event_ids = {
            ev_id for evs in room_to_event_ids.values() for ev_id in evs
        }
        unfiltered_events = await self.store.get_events_as_list(all_sticky_event_ids)
        filtered_events = await filter_and_transform_events_for_client(
            self._storage_controllers,
            sync_config.user.to_string(),
            unfiltered_events,
            # As per MSC4354:
            # > History visibility checks MUST NOT be applied to sticky events.
            # > Any joined user is authorised to see sticky events for the duration they remain sticky.
            always_include_ids=frozenset(all_sticky_event_ids),
        )
        filtered_event_map = {ev.event.event_id: ev for ev in filtered_events}

        room_id_to_sticky_events: dict[str, list[FilteredEvent]] = {}
        for room_id, sticky_event_ids in room_to_event_ids.items():
            filtered_events_for_room = [
                filtered_event_map[event_id]
                # This reintroduces the correct order
                # (by the sticky events stream)
                for event_id in sticky_event_ids
                if event_id in filtered_event_map
            ]
            if len(filtered_events_for_room) == 0:
                continue

            room_id_to_sticky_events[room_id] = filtered_events_for_room

        return SlidingSyncResult.Extensions.StickyEventsExtension(
            room_id_to_sticky_events=room_id_to_sticky_events,
            next_batch=SlidingSyncStickyEventsToken(
                sticky_events_stream_id=sticky_events_to_id
            ),
        )

    async def _get_profile_ids_for_profiles_extension(
        self,
        user_id: str,
        actual_room_ids: set[str],
        sync_config: SlidingSyncConfig,
        actual_room_response_map: Mapping[str, SlidingSyncResult.RoomResult],
        actual_lists: Mapping[str, SlidingSyncResult.SlidingWindowList],
    ) -> tuple[set[str], set[str]]:
        """
        Calculate target user profiles as candiates to include in the profile
        extension sync response.

        This function looks at both the sync config and the already calculated
        rooms response, and pieces together the full set of user IDs to include
        profiles for, based on sync config rooms being lazy loading or not.

        For rooms with lazy loading, only profiles for those users who have sent events
        into the timeline will be included, unless they would be included otherwise.
        For other rooms, all members of the room will be included as candidates.

        Note, this does not collect user IDs from the profile updates stream.

        Args:
            user_id: The full user ID syncing.
            actual_room_ids: The actual room IDs in the the Sliding Sync response.
            sync_config: The Sliding Sync config object.
            actual_room_response_map: A calculated map of responses per room.
            actual_lists: Sliding window API. A map of list key to list results in the
                Sliding Sync response.

        Returns:
            Tuple containing two sets:
               - first including all found user IDs,
               - second containing user IDs calculated via lazy configured rooms.
        """
        lazy_profile_user_ids = set()
        non_lazy_profile_user_ids = set()

        # Separate rooms into lazy and non-lazy based on sync config.
        # Look at subscriptions first
        lazy_rooms = (
            {
                room_id
                for room_id, room_config in sync_config.room_subscriptions.items()
                if (EventTypes.Member, StateValues.LAZY) in room_config.required_state
            }
            if sync_config.room_subscriptions
            else set()
        )
        # Iterate lists to find lazy rooms
        if sync_config.lists:
            for list_name, list_data in sync_config.lists.items():
                if (EventTypes.Member, StateValues.LAZY) in list_data.required_state:
                    for op in actual_lists[list_name].ops:
                        lazy_rooms.update(op.room_ids)

        if lazy_rooms:
            # For rooms configured as lazy, include users based on room response.
            for room_id, room_data in actual_room_response_map.items():
                if room_id not in lazy_rooms:
                    continue
                # Include users from timeline events
                for timeline_event in room_data.timeline_events:
                    lazy_profile_user_ids.add(timeline_event.event.sender)
                # Include users from required state
                for state_event in room_data.required_state:
                    if state_event.type == EventTypes.Member:
                        lazy_profile_user_ids.add(state_event.state_key)
                # Include heroes
                if room_data.heroes:
                    for hero in room_data.heroes:
                        lazy_profile_user_ids.add(hero.user_id)

        non_lazy_rooms = actual_room_ids.difference(lazy_rooms)
        # If we still have non-lazy rooms, get their members.
        if non_lazy_rooms:
            non_lazy_profile_user_ids = (
                # TODO we should consider adding a limit to how many profiles
                # of room members we push down the line. However, this produces
                # a problem for clients in that they won't know which users
                # just don't have any profile information, and which users were limited
                # out. If we had an endpoint to fetch a list of profiles at once,
                # we could have a hard limit here and clients could fetch the missing
                # profiles separately for non-lazy initial sync cases.
                await self.store.get_local_users_who_share_room_with_user(
                    user_id,
                    limit_to_rooms=non_lazy_rooms,
                )
            )

        # Unify the two lists
        profile_user_ids = lazy_profile_user_ids.union(non_lazy_profile_user_ids)

        # Return a tuple containing the full list of user IDs and the lazy subset.
        return (
            profile_user_ids,
            lazy_profile_user_ids,
        )

    async def _get_profiles_extension_initial_sync_response(
        self,
        user_id: UserID,
        fields: set[str] | None,
        profile_user_ids: set[str],
    ) -> dict[str, JsonDict]:
        """
        Build an initial sync response for the profiles extension.

        Args:
            user_id: The syncing user UserID
            fields: A set of fields to include in the response.
                `None` means all fields.
            profile_user_ids: Set of user IDs whose profiles are related to this sync response.

        Returns:
            A dictionary (in API response format) mapping users to their
            profile updates in an `updated` dictionary.

            {
                "@user:example.org": {
                    "updated": {
                        "displayname": "Somebody",
                        "avatar_url": "mxc://example.org/123123123",
                        "org.example.field": "hiss",
                        ...
                    }
                },
                ...
            }
        """
        response: dict[str, JsonDict] = {}

        # This doesn't return entries for the users with no profile data,
        # which is good as we don't want to generate anything for users
        # with no profile data in initial sync.
        profile_data_by_user = await self.store.get_profile_data_for_users(
            # Force our own user to be in the set, as we should
            # always watch our own profile updates
            profile_user_ids | {user_id.to_string()}
        )

        # Serialise the profile updates into the sync response format.
        for profile_user_id, profile_data in profile_data_by_user.items():
            per_user_updates: dict[str, JsonValue | dict[str, JsonValue]]
            # Include the fields the client asked for, or all, if not specified
            if fields is not None:
                per_user_updates = {
                    k: v for k, v in profile_data.items() if k in fields
                }
            else:
                per_user_updates = profile_data

            if per_user_updates:
                response[profile_user_id] = {
                    "updated": per_user_updates,
                }

        return response

    async def get_profiles_extension_response(
        self,
        sync_config: SlidingSyncConfig,
        profiles_request: SlidingSyncConfig.Extensions.ProfilesExtension,
        actual_room_ids: set[str],
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None,
        actual_room_response_map: Mapping[str, SlidingSyncResult.RoomResult],
        actual_lists: Mapping[str, SlidingSyncResult.SlidingWindowList],
    ) -> SlidingSyncResult.Extensions.ProfilesExtension | None:
        """
        Generate a response for the profiles extension.

        Args:
            sync_config: The Sliding Sync config.
            profiles_request: The profiles extension request.
            actual_room_ids: The actual room IDs in the the Sliding Sync response.
            to_token: The stream token to generate a response until.
            from_token: The stream token to generate a response from.
            actual_room_response_map: A calculated map of responses per room.
            actual_lists: Sliding window API. A map of list key to list results in the
                Sliding Sync response.

        Returns:
            - A SlidingSyncResult.Extensions.ProfilesExtension object containing
            all the users who have profile updates.
            - None if the extension is disabled.
        """
        if not profiles_request.enabled:
            return None

        user_id = sync_config.user.to_string()
        fields = (
            set(profiles_request.fields)
            if profiles_request.fields is not Absent
            else None
        )

        response: dict[str, JsonDict | None] = {}

        (
            profile_user_ids,
            lazy_profile_user_ids,
        ) = await self._get_profile_ids_for_profiles_extension(
            user_id=user_id,
            actual_room_ids=actual_room_ids,
            sync_config=sync_config,
            actual_room_response_map=actual_room_response_map,
            actual_lists=actual_lists,
        )

        if from_token is None:
            # Initial sync
            return SlidingSyncResult.Extensions.ProfilesExtension(
                users=await self._get_profiles_extension_initial_sync_response(
                    user_id=sync_config.user,
                    fields=fields,
                    profile_user_ids=profile_user_ids,
                ),
            )

        # Incremental sync
        updates = await self.store.get_profile_updates_for_user_and_fields(
            from_id=from_token.stream_token.profile_updates_key,
            to_id=to_token.profile_updates_key,
            user_id=user_id,
            field_names=fields,
        )

        # Set of users that just joined their first room that we share with them
        joined_room_user_ids: set[str] = set()
        # Set of tracked users that have updated their profile
        updated_user_ids: set[str] = set()
        # Set of tracked users that just left their last room that we share with them
        left_room_user_ids: set[str] = set()

        # Process updates in stream order
        # We need to be careful of users that have multiple types of updates
        # within this sequence of stream rows.
        for update in updates:
            if update.action == ProfileUpdateAction.JOINED_ROOM:
                joined_room_user_ids.add(update.user_id)
                # If the user joins a shared room, that overrides
                # the fact that they previously left the last shared room
                left_room_user_ids.discard(update.user_id)
            elif update.action == ProfileUpdateAction.UPDATE:
                updated_user_ids.add(update.user_id)
            elif update.action == ProfileUpdateAction.LEFT_ROOM:
                left_room_user_ids.add(update.user_id)
                # If the user leaves their last shared room, that overrides
                # the fact that they previously joined a shared room
                # and perhaps updated their profile whilst they were in it
                joined_room_user_ids.discard(update.user_id)
                updated_user_ids.discard(update.user_id)

        # Add the users who joined a shared room or updated their profile to the set of
        # users we will serialise profiles for
        profile_user_ids.update(joined_room_user_ids)
        profile_user_ids.update(updated_user_ids)

        # Process left rooms
        for other_user_id in left_room_user_ids:
            # Return a null response to the client
            # This tells the client that it will no longer receive updates for the user
            response[other_user_id] = None

        updated_user_fields: dict[str, set[str]] = {}
        # Set fields from updates
        for update in updates:
            if (
                update.action != ProfileUpdateAction.UPDATE
                or not update.affected_fields
                or update.user_id in left_room_user_ids
                # Skip if not interested in this user
                or update.user_id not in profile_user_ids
            ):
                continue
            interesting_changed_fields: set[str]
            if fields is not None:
                interesting_changed_fields = set(update.affected_fields) & fields
            else:
                interesting_changed_fields = set(update.affected_fields)

            if not interesting_changed_fields:
                # Skip the update as the client is not interested in these fields
                continue

            updated_user_fields.setdefault(update.user_id, set()).update(
                interesting_changed_fields
            )

        profile_data_by_user = await self.store.get_profile_data_for_users(
            profile_user_ids,
        )

        # Serialise the profile updates into the sync response format.
        for profile_user_id in profile_user_ids:
            if profile_user_id in left_room_user_ids:
                continue
            profile_data = profile_data_by_user.get(profile_user_id)
            if profile_data is None:
                # We don't have profile data for this user
                # (This is different from having an empty profile)
                # Return a null in incremental sync, telling the client to
                # remove all profile information for this user.
                response[profile_user_id] = None
                continue

            # Calculate which fields had updates
            updated_fields: set[str] = updated_user_fields.get(profile_user_id, set())
            # Calculate the full available field list
            user_fields = set(profile_data.keys()).union(updated_fields)

            # If the user joined the room or is included via lazy loading events,
            # include all fields the client wants. This happens because when lazy
            # a room, clients will not necessarily have the profile for the user that
            # sent an event in the room, and thus we deliver all the fields. The same
            # is true if another user joins the room - we need to deliver an initial
            # state for clients to work on.
            # For non-lazy-loaded users, include only updated fields. We assume clients
            # with non-lazy loaded rooms have received the profiles for all the members
            # in the room, and thus only need updates.
            user_fields = (
                user_fields
                if profile_user_id in joined_room_user_ids
                or profile_user_id in lazy_profile_user_ids
                else updated_fields
            )
            # Filter down if the client only wants a subset
            if fields:
                user_fields = user_fields.intersection(fields)

            if not user_fields:
                continue

            per_user_updates: dict[str, JsonValue | dict[str, JsonValue]] = {}
            per_user_removals: set[str] = set()
            for field_name in user_fields:
                # For custom fields the lack of a field means it will be `Absent`,
                # for displayname/avatar_url it will be `None`, due to way we store
                # things differently.
                # FIXME: I intend to simplify this by pushing the special-case logic
                # for these 'original' profile fields into the storage layer instead.
                absent_type = (
                    Absent
                    if field_name
                    not in (ProfileFields.DISPLAYNAME, ProfileFields.AVATAR_URL)
                    else None
                )
                field_value: JsonValue | dict[str, JsonValue] | AbsentType = (
                    profile_data.get(field_name, absent_type)
                )
                if (
                    # If the field isn't found on the profile and it is present in
                    # `updated_fields`, that means an existing field has been removed.
                    # We need the check against `updated_fields` as some profile fields
                    # are `None` by default, for example each and every user created
                    # by Synapse will have `avatar_url: None`, and we don't want to
                    # constantly send that to the clients.
                    field_value is absent_type and field_name in updated_fields
                ):
                    per_user_removals.add(field_name)
                else:
                    per_user_updates[field_name] = cast(JsonValue, field_value)

            if per_user_updates or per_user_removals:
                entry: dict[str, JsonValue | JsonDict] = {}
                response[profile_user_id] = entry
                if per_user_updates:
                    entry["updated"] = per_user_updates
                if per_user_removals:
                    entry["removed"] = list(per_user_removals)

        return SlidingSyncResult.Extensions.ProfilesExtension(
            users=response,
        )
