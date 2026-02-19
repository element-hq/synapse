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
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    ChainMap,
    Mapping,
    MutableMapping,
    Sequence,
    cast,
)

from typing_extensions import TypeAlias, assert_never

from synapse.api.constants import AccountDataTypes, EduTypes
from synapse.handlers.receipts import ReceiptEventSource
from synapse.logging.opentracing import trace
from synapse.storage.databases.main.receipts import ReceiptInRoom
from synapse.types import (
    DeviceListUpdates,
    JsonMapping,
    MultiWriterStreamToken,
    SlidingSyncStreamToken,
    StrCollection,
    StreamToken,
    ThreadSubscriptionsToken,
)
from synapse.types.handlers.sliding_sync import (
    HaveSentRoomFlag,
    MutablePerConnectionState,
    OperationType,
    PerConnectionState,
    SlidingSyncConfig,
    SlidingSyncResult,
)
from synapse.util.async_helpers import (
    concurrently_execute,
    gather_optional_coroutines,
)

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
        self._enable_thread_subscriptions = hs.config.experimental.msc4306_enabled

    @trace
    async def get_extensions_response(
        self,
        sync_config: SlidingSyncConfig,
        previous_connection_state: "PerConnectionState",
        new_connection_state: "MutablePerConnectionState",
        actual_lists: Mapping[str, SlidingSyncResult.SlidingWindowList],
        actual_room_ids: set[str],
        actual_room_response_map: Mapping[str, SlidingSyncResult.RoomResult],
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None,
    ) -> SlidingSyncResult.Extensions:
        """Handle extension requests.

        Args:
            sync_config: Sync configuration
            new_connection_state: Snapshot of the current per-connection state
            new_per_connection_state: A mutable copy of the per-connection
                state, used to record updates to the state during this request.
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

        (
            to_device_response,
            e2ee_response,
            account_data_response,
            receipts_response,
            typing_response,
            thread_subs_response,
        ) = await gather_optional_coroutines(
            to_device_coro,
            e2ee_coro,
            account_data_coro,
            receipts_coro,
            typing_coro,
            thread_subs_coro,
        )

        return SlidingSyncResult.Extensions(
            to_device=to_device_response,
            e2ee=e2ee_response,
            account_data=account_data_response,
            receipts=receipts_response,
            typing=typing_response,
            thread_subscriptions=thread_subs_response,
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
                    (room_id, event.event_id)
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
