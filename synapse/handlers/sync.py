#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015-2021 The Matrix.org Foundation C.I.C.
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
import itertools
import logging
from enum import Enum
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Any,
    Dict,
    FrozenSet,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    overload,
)

import attr
from prometheus_client import Counter

from synapse.api.constants import (
    AccountDataTypes,
    Direction,
    EventContentFields,
    EventTypes,
    JoinRules,
    Membership,
)
from synapse.api.filtering import FilterCollection
from synapse.api.presence import UserPresenceState
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.events import EventBase
from synapse.handlers.relations import BundledAggregations
from synapse.logging import issue9533_logger
from synapse.logging.context import current_context
from synapse.logging.opentracing import (
    SynapseTags,
    log_kv,
    set_tag,
    start_active_span,
    trace,
)
from synapse.storage.databases.main.event_push_actions import RoomNotifCounts
from synapse.storage.databases.main.roommember import extract_heroes_from_room_summary
from synapse.storage.databases.main.stream import PaginateFunction
from synapse.storage.roommember import MemberSummary
from synapse.types import (
    DeviceListUpdates,
    JsonDict,
    JsonMapping,
    MultiWriterStreamToken,
    MutableStateMap,
    Requester,
    RoomStreamToken,
    StateMap,
    StrCollection,
    StreamKeyType,
    StreamToken,
    UserID,
)
from synapse.types.state import StateFilter
from synapse.util.async_helpers import concurrently_execute
from synapse.util.caches.expiringcache import ExpiringCache
from synapse.util.caches.lrucache import LruCache
from synapse.util.caches.response_cache import ResponseCache, ResponseCacheContext
from synapse.util.metrics import Measure
from synapse.visibility import filter_events_for_client

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# Counts the number of times we returned a non-empty sync. `type` is one of
# "initial_sync", "full_state_sync" or "incremental_sync", `lazy_loaded` is
# "true" or "false" depending on if the request asked for lazy loaded members or
# not.
non_empty_sync_counter = Counter(
    "synapse_handlers_sync_nonempty_total",
    "Count of non empty sync responses. type is initial_sync/full_state_sync"
    "/incremental_sync. lazy_loaded indicates if lazy loaded members were "
    "enabled for that request.",
    ["type", "lazy_loaded"],
)

# Store the cache that tracks which lazy-loaded members have been sent to a given
# client for no more than 30 minutes.
LAZY_LOADED_MEMBERS_CACHE_MAX_AGE = 30 * 60 * 1000

# Remember the last 100 members we sent to a client for the purposes of
# avoiding redundantly sending the same lazy-loaded members to the client
LAZY_LOADED_MEMBERS_CACHE_MAX_SIZE = 100


SyncRequestKey = Tuple[Any, ...]


class SyncVersion(Enum):
    """
    Enum for specifying the version of sync request. This is used to key which type of
    sync response that we are generating.

    This is different than the `sync_type` you might see used in other code below; which
    specifies the sub-type sync request (e.g. initial_sync, full_state_sync,
    incremental_sync) and is really only relevant for the `/sync` v2 endpoint.
    """

    # These string values are semantically significant because they are used in the the
    # metrics

    # Traditional `/sync` endpoint
    SYNC_V2 = "sync_v2"
    # Part of MSC3575 Sliding Sync
    E2EE_SYNC = "e2ee_sync"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class SyncConfig:
    user: UserID
    filter_collection: FilterCollection
    is_guest: bool
    device_id: Optional[str]
    use_state_after: bool


@attr.s(slots=True, frozen=True, auto_attribs=True)
class TimelineBatch:
    prev_batch: StreamToken
    events: Sequence[EventBase]
    limited: bool
    # A mapping of event ID to the bundled aggregations for the above events.
    # This is only calculated if limited is true.
    bundled_aggregations: Optional[Dict[str, BundledAggregations]] = None

    def __bool__(self) -> bool:
        """Make the result appear empty if there are no updates. This is used
        to tell if room needs to be part of the sync result.
        """
        return bool(self.events)


# We can't freeze this class, because we need to update it after it's instantiated to
# update its unread count. This is because we calculate the unread count for a room only
# if there are updates for it, which we check after the instance has been created.
# This should not be a big deal because we update the notification counts afterwards as
# well anyway.
@attr.s(slots=True, auto_attribs=True)
class JoinedSyncResult:
    room_id: str
    timeline: TimelineBatch
    state: StateMap[EventBase]
    ephemeral: List[JsonDict]
    account_data: List[JsonDict]
    unread_notifications: JsonDict
    unread_thread_notifications: JsonDict
    summary: Optional[JsonDict]
    unread_count: int

    def __bool__(self) -> bool:
        """Make the result appear empty if there are no updates. This is used
        to tell if room needs to be part of the sync result.
        """
        return bool(
            self.timeline or self.state or self.ephemeral or self.account_data
            # nb the notification count does not, er, count: if there's nothing
            # else in the result, we don't need to send it.
        )


@attr.s(slots=True, frozen=True, auto_attribs=True)
class ArchivedSyncResult:
    room_id: str
    timeline: TimelineBatch
    state: StateMap[EventBase]
    account_data: List[JsonDict]

    def __bool__(self) -> bool:
        """Make the result appear empty if there are no updates. This is used
        to tell if room needs to be part of the sync result.
        """
        return bool(self.timeline or self.state or self.account_data)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class InvitedSyncResult:
    room_id: str
    invite: EventBase

    def __bool__(self) -> bool:
        """Invited rooms should always be reported to the client"""
        return True


@attr.s(slots=True, frozen=True, auto_attribs=True)
class KnockedSyncResult:
    room_id: str
    knock: EventBase

    def __bool__(self) -> bool:
        """Knocked rooms should always be reported to the client"""
        return True


@attr.s(slots=True, auto_attribs=True)
class _RoomChanges:
    """The set of room entries to include in the sync, plus the set of joined
    and left room IDs since last sync.
    """

    room_entries: List["RoomSyncResultBuilder"]
    invited: List[InvitedSyncResult]
    knocked: List[KnockedSyncResult]
    newly_joined_rooms: List[str]
    newly_left_rooms: List[str]


@attr.s(slots=True, frozen=True, auto_attribs=True)
class SyncResult:
    """
    Attributes:
        next_batch: Token for the next sync
        presence: List of presence events for the user.
        account_data: List of account_data events for the user.
        joined: JoinedSyncResult for each joined room.
        invited: InvitedSyncResult for each invited room.
        knocked: KnockedSyncResult for each knocked on room.
        archived: ArchivedSyncResult for each archived room.
        to_device: List of direct messages for the device.
        device_lists: List of user_ids whose devices have changed
        device_one_time_keys_count: Dict of algorithm to count for one time keys
            for this device
        device_unused_fallback_key_types: List of key types that have an unused fallback
            key
    """

    next_batch: StreamToken
    presence: List[UserPresenceState]
    account_data: List[JsonDict]
    joined: List[JoinedSyncResult]
    invited: List[InvitedSyncResult]
    knocked: List[KnockedSyncResult]
    archived: List[ArchivedSyncResult]
    to_device: List[JsonDict]
    device_lists: DeviceListUpdates
    device_one_time_keys_count: JsonMapping
    device_unused_fallback_key_types: List[str]

    def __bool__(self) -> bool:
        """Make the result appear empty if there are no updates. This is used
        to tell if the notifier needs to wait for more events when polling for
        events.
        """
        return bool(
            self.presence
            or self.joined
            or self.invited
            or self.knocked
            or self.archived
            or self.account_data
            or self.to_device
            or self.device_lists
        )

    @staticmethod
    def empty(
        next_batch: StreamToken,
        device_one_time_keys_count: JsonMapping,
        device_unused_fallback_key_types: List[str],
    ) -> "SyncResult":
        "Return a new empty result"
        return SyncResult(
            next_batch=next_batch,
            presence=[],
            account_data=[],
            joined=[],
            invited=[],
            knocked=[],
            archived=[],
            to_device=[],
            device_lists=DeviceListUpdates(),
            device_one_time_keys_count=device_one_time_keys_count,
            device_unused_fallback_key_types=device_unused_fallback_key_types,
        )


@attr.s(slots=True, frozen=True, auto_attribs=True)
class E2eeSyncResult:
    """
    Attributes:
        next_batch: Token for the next sync
        to_device: List of direct messages for the device.
        device_lists: List of user_ids whose devices have changed
        device_one_time_keys_count: Dict of algorithm to count for one time keys
            for this device
        device_unused_fallback_key_types: List of key types that have an unused fallback
            key
    """

    next_batch: StreamToken
    to_device: List[JsonDict]
    device_lists: DeviceListUpdates
    device_one_time_keys_count: JsonMapping
    device_unused_fallback_key_types: List[str]


class SyncHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs_config = hs.config
        self.store = hs.get_datastores().main
        self.notifier = hs.get_notifier()
        self.presence_handler = hs.get_presence_handler()
        self._relations_handler = hs.get_relations_handler()
        self._push_rules_handler = hs.get_push_rules_handler()
        self.event_sources = hs.get_event_sources()
        self.clock = hs.get_clock()
        self.state = hs.get_state_handler()
        self.auth_blocking = hs.get_auth_blocking()
        self._storage_controllers = hs.get_storage_controllers()
        self._state_storage_controller = self._storage_controllers.state
        self._device_handler = hs.get_device_handler()
        self._task_scheduler = hs.get_task_scheduler()

        self.should_calculate_push_rules = hs.config.push.enable_push

        # TODO: flush cache entries on subsequent sync request.
        #    Once we get the next /sync request (ie, one with the same access token
        #    that sets 'since' to 'next_batch'), we know that device won't need a
        #    cached result any more, and we could flush the entry from the cache to save
        #    memory.
        self.response_cache: ResponseCache[SyncRequestKey] = ResponseCache(
            hs.get_clock(),
            "sync",
            timeout_ms=hs.config.caches.sync_response_cache_duration,
        )

        # ExpiringCache((User, Device)) -> LruCache(user_id => event_id)
        self.lazy_loaded_members_cache: ExpiringCache[
            Tuple[str, Optional[str]], LruCache[str, str]
        ] = ExpiringCache(
            "lazy_loaded_members_cache",
            self.clock,
            max_len=0,
            expiry_ms=LAZY_LOADED_MEMBERS_CACHE_MAX_AGE,
        )

        self.rooms_to_exclude_globally = hs.config.server.rooms_to_exclude_from_sync

    @overload
    async def wait_for_sync_for_user(
        self,
        requester: Requester,
        sync_config: SyncConfig,
        sync_version: Literal[SyncVersion.SYNC_V2],
        request_key: SyncRequestKey,
        since_token: Optional[StreamToken] = None,
        timeout: int = 0,
        full_state: bool = False,
    ) -> SyncResult: ...

    @overload
    async def wait_for_sync_for_user(
        self,
        requester: Requester,
        sync_config: SyncConfig,
        sync_version: Literal[SyncVersion.E2EE_SYNC],
        request_key: SyncRequestKey,
        since_token: Optional[StreamToken] = None,
        timeout: int = 0,
        full_state: bool = False,
    ) -> E2eeSyncResult: ...

    @overload
    async def wait_for_sync_for_user(
        self,
        requester: Requester,
        sync_config: SyncConfig,
        sync_version: SyncVersion,
        request_key: SyncRequestKey,
        since_token: Optional[StreamToken] = None,
        timeout: int = 0,
        full_state: bool = False,
    ) -> Union[SyncResult, E2eeSyncResult]: ...

    async def wait_for_sync_for_user(
        self,
        requester: Requester,
        sync_config: SyncConfig,
        sync_version: SyncVersion,
        request_key: SyncRequestKey,
        since_token: Optional[StreamToken] = None,
        timeout: int = 0,
        full_state: bool = False,
    ) -> Union[SyncResult, E2eeSyncResult]:
        """Get the sync for a client if we have new data for it now. Otherwise
        wait for new data to arrive on the server. If the timeout expires, then
        return an empty sync result.

        Args:
            requester: The user requesting the sync response.
            sync_config: Config/info necessary to process the sync request.
            sync_version: Determines what kind of sync response to generate.
            request_key: The key to use for caching the response.
            since_token: The point in the stream to sync from.
            timeout: How long to wait for new data to arrive before giving up.
            full_state: Whether to return the full state for each room.

        Returns:
            When `SyncVersion.SYNC_V2`, returns a full `SyncResult`.
            When `SyncVersion.E2EE_SYNC`, returns a `E2eeSyncResult`.
        """
        # If the user is not part of the mau group, then check that limits have
        # not been exceeded (if not part of the group by this point, almost certain
        # auth_blocking will occur)
        user_id = sync_config.user.to_string()
        await self.auth_blocking.check_auth_blocking(requester=requester)

        res = await self.response_cache.wrap(
            request_key,
            self._wait_for_sync_for_user,
            sync_config,
            sync_version,
            since_token,
            timeout,
            full_state,
            cache_context=True,
        )
        logger.debug("Returning sync response for %s", user_id)
        return res

    @overload
    async def _wait_for_sync_for_user(
        self,
        sync_config: SyncConfig,
        sync_version: Literal[SyncVersion.SYNC_V2],
        since_token: Optional[StreamToken],
        timeout: int,
        full_state: bool,
        cache_context: ResponseCacheContext[SyncRequestKey],
    ) -> SyncResult: ...

    @overload
    async def _wait_for_sync_for_user(
        self,
        sync_config: SyncConfig,
        sync_version: Literal[SyncVersion.E2EE_SYNC],
        since_token: Optional[StreamToken],
        timeout: int,
        full_state: bool,
        cache_context: ResponseCacheContext[SyncRequestKey],
    ) -> E2eeSyncResult: ...

    @overload
    async def _wait_for_sync_for_user(
        self,
        sync_config: SyncConfig,
        sync_version: SyncVersion,
        since_token: Optional[StreamToken],
        timeout: int,
        full_state: bool,
        cache_context: ResponseCacheContext[SyncRequestKey],
    ) -> Union[SyncResult, E2eeSyncResult]: ...

    async def _wait_for_sync_for_user(
        self,
        sync_config: SyncConfig,
        sync_version: SyncVersion,
        since_token: Optional[StreamToken],
        timeout: int,
        full_state: bool,
        cache_context: ResponseCacheContext[SyncRequestKey],
    ) -> Union[SyncResult, E2eeSyncResult]:
        """The start of the machinery that produces a /sync response.

        See https://spec.matrix.org/v1.1/client-server-api/#syncing for full details.

        This method does high-level bookkeeping:
        - tracking the kind of sync in the logging context
        - deleting any to_device messages whose delivery has been acknowledged.
        - deciding if we should dispatch an instant or delayed response
        - marking the sync as being lazily loaded, if appropriate

        Computing the body of the response begins in the next method,
        `current_sync_for_user`.
        """
        if since_token is None:
            sync_type = "initial_sync"
        elif full_state:
            sync_type = "full_state_sync"
        else:
            sync_type = "incremental_sync"

        sync_label = f"{sync_version}:{sync_type}"

        context = current_context()
        if context:
            context.tag = sync_label

        if since_token is not None:
            # We need to make sure this worker has caught up with the token. If
            # this returns false it means we timed out waiting, and we should
            # just return an empty response.
            start = self.clock.time_msec()
            if not await self.notifier.wait_for_stream_token(since_token):
                logger.warning(
                    "Timed out waiting for worker to catch up. Returning empty response"
                )
                device_id = sync_config.device_id
                one_time_keys_count: JsonMapping = {}
                unused_fallback_key_types: List[str] = []
                if device_id:
                    user_id = sync_config.user.to_string()
                    # TODO: We should have a way to let clients differentiate between the states of:
                    #   * no change in OTK count since the provided since token
                    #   * the server has zero OTKs left for this device
                    #  Spec issue: https://github.com/matrix-org/matrix-doc/issues/3298
                    one_time_keys_count = await self.store.count_e2e_one_time_keys(
                        user_id, device_id
                    )
                    unused_fallback_key_types = list(
                        await self.store.get_e2e_unused_fallback_key_types(
                            user_id, device_id
                        )
                    )

                cache_context.should_cache = False  # Don't cache empty responses
                return SyncResult.empty(
                    since_token, one_time_keys_count, unused_fallback_key_types
                )

            # If we've spent significant time waiting to catch up, take it off
            # the timeout.
            now = self.clock.time_msec()
            if now - start > 1_000:
                timeout -= now - start
                timeout = max(timeout, 0)

        # if we have a since token, delete any to-device messages before that token
        # (since we now know that the device has received them)
        if since_token is not None:
            since_stream_id = since_token.to_device_key
            deleted = await self.store.delete_messages_for_device(
                sync_config.user.to_string(),
                sync_config.device_id,
                since_stream_id,
            )
            logger.debug(
                "Deleted %d to-device messages up to %d", deleted, since_stream_id
            )

        if timeout == 0 or since_token is None or full_state:
            # we are going to return immediately, so don't bother calling
            # notifier.wait_for_events.
            result: Union[
                SyncResult, E2eeSyncResult
            ] = await self.current_sync_for_user(
                sync_config, sync_version, since_token, full_state=full_state
            )
        else:
            # Otherwise, we wait for something to happen and report it to the user.
            async def current_sync_callback(
                before_token: StreamToken, after_token: StreamToken
            ) -> Union[SyncResult, E2eeSyncResult]:
                return await self.current_sync_for_user(
                    sync_config, sync_version, since_token
                )

            result = await self.notifier.wait_for_events(
                sync_config.user.to_string(),
                timeout,
                current_sync_callback,
                from_token=since_token,
            )

        # if nothing has happened in any of the users' rooms since /sync was called,
        # the resultant next_batch will be the same as since_token (since the result
        # is generated when wait_for_events is first called, and not regenerated
        # when wait_for_events times out).
        #
        # If that happens, we mustn't cache it, so that when the client comes back
        # with the same cache token, we don't immediately return the same empty
        # result, causing a tightloop. (https://github.com/matrix-org/synapse/issues/8518)
        if result.next_batch == since_token:
            cache_context.should_cache = False

        if result:
            if sync_config.filter_collection.lazy_load_members():
                lazy_loaded = "true"
            else:
                lazy_loaded = "false"
            non_empty_sync_counter.labels(sync_label, lazy_loaded).inc()

        return result

    @overload
    async def current_sync_for_user(
        self,
        sync_config: SyncConfig,
        sync_version: Literal[SyncVersion.SYNC_V2],
        since_token: Optional[StreamToken] = None,
        full_state: bool = False,
    ) -> SyncResult: ...

    @overload
    async def current_sync_for_user(
        self,
        sync_config: SyncConfig,
        sync_version: Literal[SyncVersion.E2EE_SYNC],
        since_token: Optional[StreamToken] = None,
        full_state: bool = False,
    ) -> E2eeSyncResult: ...

    @overload
    async def current_sync_for_user(
        self,
        sync_config: SyncConfig,
        sync_version: SyncVersion,
        since_token: Optional[StreamToken] = None,
        full_state: bool = False,
    ) -> Union[SyncResult, E2eeSyncResult]: ...

    async def current_sync_for_user(
        self,
        sync_config: SyncConfig,
        sync_version: SyncVersion,
        since_token: Optional[StreamToken] = None,
        full_state: bool = False,
    ) -> Union[SyncResult, E2eeSyncResult]:
        """
        Generates the response body of a sync result, represented as a
        `SyncResult`/`E2eeSyncResult`.

        This is a wrapper around `generate_sync_result` which starts an open tracing
        span to track the sync. See `generate_sync_result` for the next part of your
        indoctrination.

        Args:
            sync_config: Config/info necessary to process the sync request.
            sync_version: Determines what kind of sync response to generate.
            since_token: The point in the stream to sync from.p.
            full_state: Whether to return the full state for each room.

        Returns:
            When `SyncVersion.SYNC_V2`, returns a full `SyncResult`.
            When `SyncVersion.E2EE_SYNC`, returns a `E2eeSyncResult`.
        """
        with start_active_span("sync.current_sync_for_user"):
            log_kv({"since_token": since_token})

            # Go through the `/sync` v2 path
            if sync_version == SyncVersion.SYNC_V2:
                sync_result: Union[
                    SyncResult, E2eeSyncResult
                ] = await self.generate_sync_result(
                    sync_config, since_token, full_state
                )
            # Go through the MSC3575 Sliding Sync `/sync/e2ee` path
            elif sync_version == SyncVersion.E2EE_SYNC:
                sync_result = await self.generate_e2ee_sync_result(
                    sync_config, since_token
                )
            else:
                raise Exception(
                    f"Unknown sync_version (this is a Synapse problem): {sync_version}"
                )

            set_tag(SynapseTags.SYNC_RESULT, bool(sync_result))
            return sync_result

    async def ephemeral_by_room(
        self,
        sync_result_builder: "SyncResultBuilder",
        now_token: StreamToken,
        since_token: Optional[StreamToken] = None,
    ) -> Tuple[StreamToken, Dict[str, List[JsonDict]]]:
        """Get the ephemeral events for each room the user is in
        Args:
            sync_result_builder
            now_token: Where the server is currently up to.
            since_token: Where the server was when the client
                last synced.
        Returns:
            A tuple of the now StreamToken, updated to reflect the which typing
            events are included, and a dict mapping from room_id to a list of
            typing events for that room.
        """

        sync_config = sync_result_builder.sync_config

        with Measure(self.clock, "ephemeral_by_room"):
            typing_key = since_token.typing_key if since_token else 0

            room_ids = sync_result_builder.joined_room_ids

            typing_source = self.event_sources.sources.typing
            typing, typing_key = await typing_source.get_new_events(
                user=sync_config.user,
                from_key=typing_key,
                limit=sync_config.filter_collection.ephemeral_limit(),
                room_ids=room_ids,
                is_guest=sync_config.is_guest,
            )
            now_token = now_token.copy_and_replace(StreamKeyType.TYPING, typing_key)

            ephemeral_by_room: JsonDict = {}

            for event in typing:
                # we want to exclude the room_id from the event, but modifying the
                # result returned by the event source is poor form (it might cache
                # the object)
                room_id = event["room_id"]
                event_copy = {k: v for (k, v) in event.items() if k != "room_id"}
                ephemeral_by_room.setdefault(room_id, []).append(event_copy)

            receipt_key = (
                since_token.receipt_key
                if since_token
                else MultiWriterStreamToken(stream=0)
            )

            receipt_source = self.event_sources.sources.receipt
            receipts, receipt_key = await receipt_source.get_new_events(
                user=sync_config.user,
                from_key=receipt_key,
                limit=sync_config.filter_collection.ephemeral_limit(),
                room_ids=room_ids,
                is_guest=sync_config.is_guest,
            )
            now_token = now_token.copy_and_replace(StreamKeyType.RECEIPT, receipt_key)

            for event in receipts:
                room_id = event["room_id"]
                # exclude room id, as above
                event_copy = {k: v for (k, v) in event.items() if k != "room_id"}
                ephemeral_by_room.setdefault(room_id, []).append(event_copy)

        return now_token, ephemeral_by_room

    async def _load_filtered_recents(
        self,
        room_id: str,
        sync_result_builder: "SyncResultBuilder",
        sync_config: SyncConfig,
        upto_token: StreamToken,
        since_token: Optional[StreamToken] = None,
        potential_recents: Optional[List[EventBase]] = None,
        newly_joined_room: bool = False,
    ) -> TimelineBatch:
        """Create a timeline batch for the room

        Args:
            room_id
            sync_result_builder
            sync_config
            upto_token: The token up to which we should fetch (more) events.
                If `potential_results` is non-empty then this is *start* of
                the the list.
            since_token
            potential_recents: If non-empty, the events between the since token
                and current token to send down to clients.
            newly_joined_room
        """
        with Measure(self.clock, "load_filtered_recents"):
            timeline_limit = sync_config.filter_collection.timeline_limit()
            block_all_timeline = (
                sync_config.filter_collection.blocks_all_room_timeline()
            )

            if (
                potential_recents is None
                or newly_joined_room
                or timeline_limit < len(potential_recents)
            ):
                limited = True
            else:
                limited = False

            # Check if there is a gap, if so we need to mark this as limited and
            # recalculate which events to send down.
            gap_token = await self.store.get_timeline_gaps(
                room_id,
                since_token.room_key if since_token else None,
                sync_result_builder.now_token.room_key,
            )
            if gap_token:
                # There's a gap, so we need to ignore the passed in
                # `potential_recents`, and reset `upto_token` to match.
                potential_recents = None
                upto_token = sync_result_builder.now_token
                limited = True

            log_kv({"limited": limited})

            if potential_recents:
                recents = await sync_config.filter_collection.filter_room_timeline(
                    potential_recents
                )
                log_kv({"recents_after_sync_filtering": len(recents)})

                # We check if there are any state events, if there are then we pass
                # all current state events to the filter_events function. This is to
                # ensure that we always include current state in the timeline
                current_state_ids: FrozenSet[str] = frozenset()
                if any(e.is_state() for e in recents):
                    # FIXME(faster_joins): We use the partial state here as
                    # we don't want to block `/sync` on finishing a lazy join.
                    # Which should be fine once
                    # https://github.com/matrix-org/synapse/issues/12989 is resolved,
                    # since we shouldn't reach here anymore?
                    # Note that we use the current state as a whitelist for filtering
                    # `recents`, so partial state is only a problem when a membership
                    # event turns up in `recents` but has not made it into the current
                    # state.
                    current_state_ids = (
                        await self.store.check_if_events_in_current_state(
                            {e.event_id for e in recents if e.is_state()}
                        )
                    )

                recents = await filter_events_for_client(
                    self._storage_controllers,
                    sync_config.user.to_string(),
                    recents,
                    always_include_ids=current_state_ids,
                )
                log_kv({"recents_after_visibility_filtering": len(recents)})
            else:
                recents = []

            if not limited or block_all_timeline:
                prev_batch_token = upto_token
                if recents:
                    assert recents[0].internal_metadata.stream_ordering
                    room_key = RoomStreamToken(
                        stream=recents[0].internal_metadata.stream_ordering - 1
                    )
                    prev_batch_token = upto_token.copy_and_replace(
                        StreamKeyType.ROOM, room_key
                    )

                return TimelineBatch(
                    events=recents, prev_batch=prev_batch_token, limited=False
                )

            filtering_factor = 2
            load_limit = max(timeline_limit * filtering_factor, 10)
            max_repeat = 5  # Only try a few times per room, otherwise
            room_key = upto_token.room_key
            end_key = room_key

            since_key = None
            if since_token and gap_token:
                # If there is a gap then we need to only include events after
                # it.
                since_key = gap_token
            elif since_token and not newly_joined_room:
                since_key = since_token.room_key

            while limited and len(recents) < timeline_limit and max_repeat:
                # For initial `/sync`, we want to view a historical section of the
                # timeline; to fetch events by `topological_ordering` (best
                # representation of the room DAG as others were seeing it at the time).
                # This also aligns with the order that `/messages` returns events in.
                #
                # For incremental `/sync`, we want to get all updates for rooms since
                # the last `/sync` (regardless if those updates arrived late or happened
                # a while ago in the past); to fetch events by `stream_ordering` (in the
                # order they were received by the server).
                #
                # Relevant spec issue: https://github.com/matrix-org/matrix-spec/issues/1917
                #
                # FIXME: Using workaround for mypy,
                # https://github.com/python/mypy/issues/10740#issuecomment-1997047277 and
                # https://github.com/python/mypy/issues/17479
                paginate_room_events_by_topological_ordering: PaginateFunction = (
                    self.store.paginate_room_events_by_topological_ordering
                )
                paginate_room_events_by_stream_ordering: PaginateFunction = (
                    self.store.paginate_room_events_by_stream_ordering
                )
                pagination_method: PaginateFunction = (
                    # Use `topographical_ordering` for historical events
                    paginate_room_events_by_topological_ordering
                    if since_key is None
                    # Use `stream_ordering` for updates
                    else paginate_room_events_by_stream_ordering
                )
                events, end_key, limited = await pagination_method(
                    room_id=room_id,
                    # The bounds are reversed so we can paginate backwards
                    # (from newer to older events) starting at to_bound.
                    # This ensures we fill the `limit` with the newest events first,
                    from_key=end_key,
                    to_key=since_key,
                    direction=Direction.BACKWARDS,
                    limit=load_limit,
                )
                # We want to return the events in ascending order (the last event is the
                # most recent).
                events.reverse()

                log_kv({"loaded_recents": len(events)})

                loaded_recents = (
                    await sync_config.filter_collection.filter_room_timeline(events)
                )

                log_kv({"loaded_recents_after_sync_filtering": len(loaded_recents)})

                # We check if there are any state events, if there are then we pass
                # all current state events to the filter_events function. This is to
                # ensure that we always include current state in the timeline
                current_state_ids = frozenset()
                if any(e.is_state() for e in loaded_recents):
                    # FIXME(faster_joins): We use the partial state here as
                    # we don't want to block `/sync` on finishing a lazy join.
                    # Which should be fine once
                    # https://github.com/matrix-org/synapse/issues/12989 is resolved,
                    # since we shouldn't reach here anymore?
                    # Note that we use the current state as a whitelist for filtering
                    # `loaded_recents`, so partial state is only a problem when a
                    # membership event turns up in `loaded_recents` but has not made it
                    # into the current state.
                    current_state_ids = (
                        await self.store.check_if_events_in_current_state(
                            {e.event_id for e in loaded_recents if e.is_state()}
                        )
                    )

                filtered_recents = await filter_events_for_client(
                    self._storage_controllers,
                    sync_config.user.to_string(),
                    loaded_recents,
                    always_include_ids=current_state_ids,
                )

                loaded_recents = []
                for event in filtered_recents:
                    if event.type == EventTypes.CallInvite:
                        room_info = await self.store.get_room_with_stats(event.room_id)
                        assert room_info is not None
                        if room_info.join_rules == JoinRules.PUBLIC:
                            continue
                    loaded_recents.append(event)

                log_kv({"loaded_recents_after_client_filtering": len(loaded_recents)})

                loaded_recents.extend(recents)
                recents = loaded_recents

                max_repeat -= 1

            if len(recents) > timeline_limit:
                limited = True
                recents = recents[-timeline_limit:]
                assert recents[0].internal_metadata.stream_ordering
                room_key = RoomStreamToken(
                    stream=recents[0].internal_metadata.stream_ordering - 1
                )

            prev_batch_token = upto_token.copy_and_replace(StreamKeyType.ROOM, room_key)

        # Don't bother to bundle aggregations if the timeline is unlimited,
        # as clients will have all the necessary information.
        bundled_aggregations = None
        if limited or newly_joined_room:
            bundled_aggregations = (
                await self._relations_handler.get_bundled_aggregations(
                    recents, sync_config.user.to_string()
                )
            )

        return TimelineBatch(
            events=recents,
            prev_batch=prev_batch_token,
            # Also mark as limited if this is a new room or there has been a gap
            # (to force client to paginate the gap).
            limited=limited or newly_joined_room or gap_token is not None,
            bundled_aggregations=bundled_aggregations,
        )

    async def compute_summary(
        self,
        room_id: str,
        sync_config: SyncConfig,
        batch: TimelineBatch,
        state: MutableStateMap[EventBase],
        now_token: StreamToken,
    ) -> Optional[JsonDict]:
        """Works out a room summary block for this room, summarising the number
        of joined members in the room, and providing the 'hero' members if the
        room has no name so clients can consistently name rooms.  Also adds
        state events to 'state' if needed to describe the heroes.

        Args
            room_id
            sync_config
            batch: The timeline batch for the room that will be sent to the user.
            state: State as returned by compute_state_delta
            now_token: Token of the end of the current batch.
        """

        # FIXME: we could/should get this from room_stats when matthew/stats lands

        # FIXME: this promulgates https://github.com/matrix-org/synapse/issues/3305
        last_events, _ = await self.store.get_recent_event_ids_for_room(
            room_id, end_token=now_token.room_key, limit=1
        )

        if not last_events:
            return None

        last_event = last_events[-1]
        state_ids = await self._state_storage_controller.get_state_ids_for_event(
            last_event.event_id,
            state_filter=StateFilter.from_types(
                [(EventTypes.Name, ""), (EventTypes.CanonicalAlias, "")]
            ),
        )

        # this is heavily cached, thus: fast.
        details = await self.store.get_room_summary(room_id)

        name_id = state_ids.get((EventTypes.Name, ""))
        canonical_alias_id = state_ids.get((EventTypes.CanonicalAlias, ""))

        summary: JsonDict = {}
        empty_ms = MemberSummary([], 0)

        # TODO: only send these when they change.
        summary["m.joined_member_count"] = details.get(Membership.JOIN, empty_ms).count
        summary["m.invited_member_count"] = details.get(
            Membership.INVITE, empty_ms
        ).count

        # if the room has a name or canonical_alias set, we can skip
        # calculating heroes. Empty strings are falsey, so we check
        # for the "name" value and default to an empty string.
        if name_id:
            name = await self.store.get_event(name_id, allow_none=True)
            if name and name.content.get("name"):
                return summary

        if canonical_alias_id:
            canonical_alias = await self.store.get_event(
                canonical_alias_id, allow_none=True
            )
            if canonical_alias and canonical_alias.content.get("alias"):
                return summary

        # FIXME: only build up a member_ids list for our heroes
        member_ids = {}
        for membership in (
            Membership.JOIN,
            Membership.INVITE,
            Membership.LEAVE,
            Membership.BAN,
        ):
            for user_id, event_id in details.get(membership, empty_ms).members:
                member_ids[user_id] = event_id

        me = sync_config.user.to_string()
        summary["m.heroes"] = extract_heroes_from_room_summary(details, me)

        if not sync_config.filter_collection.lazy_load_members():
            return summary

        # ensure we send membership events for heroes if needed
        cache_key = (sync_config.user.to_string(), sync_config.device_id)
        cache = self.get_lazy_loaded_members_cache(cache_key)

        # track which members the client should already know about via LL:
        # Ones which are already in state...
        existing_members = {
            user_id for (typ, user_id) in state.keys() if typ == EventTypes.Member
        }

        # ...or ones which are in the timeline...
        for ev in batch.events:
            if ev.type == EventTypes.Member:
                existing_members.add(ev.state_key)

        # ...and then ensure any missing ones get included in state.
        missing_hero_event_ids = [
            member_ids[hero_id]
            for hero_id in summary["m.heroes"]
            if (
                cache.get(hero_id) != member_ids[hero_id]
                and hero_id not in existing_members
            )
        ]

        missing_hero_state = await self.store.get_events(missing_hero_event_ids)

        for s in missing_hero_state.values():
            cache.set(s.state_key, s.event_id)
            state[(EventTypes.Member, s.state_key)] = s

        return summary

    def get_lazy_loaded_members_cache(
        self, cache_key: Tuple[str, Optional[str]]
    ) -> LruCache[str, str]:
        cache: Optional[LruCache[str, str]] = self.lazy_loaded_members_cache.get(
            cache_key
        )
        if cache is None:
            logger.debug("creating LruCache for %r", cache_key)
            cache = LruCache(LAZY_LOADED_MEMBERS_CACHE_MAX_SIZE)
            self.lazy_loaded_members_cache[cache_key] = cache
        else:
            logger.debug("found LruCache for %r", cache_key)
        return cache

    async def compute_state_delta(
        self,
        room_id: str,
        batch: TimelineBatch,
        sync_config: SyncConfig,
        since_token: Optional[StreamToken],
        end_token: StreamToken,
        full_state: bool,
        joined: bool,
    ) -> MutableStateMap[EventBase]:
        """Works out the difference in state between the end of the previous sync and
        the start of the timeline.

        Args:
            room_id:
            batch: The timeline batch for the room that will be sent to the user.
            sync_config:
            since_token: Token of the end of the previous batch. May be `None`.
            end_token: Token of the end of the current batch. Normally this will be
                the same as the global "now_token", but if the user has left the room,
                the point just after their leave event.
            full_state: Whether to force returning the full state.
                `lazy_load_members` still applies when `full_state` is `True`.
            joined: whether the user is currently joined to the room

        Returns:
            The state to return in the sync response for the room.

            Clients will overlay this onto the state at the end of the previous sync to
            arrive at the state at the start of the timeline.

            Clients will then overlay state events in the timeline to arrive at the
            state at the end of the timeline, in preparation for the next sync.
        """
        # TODO(mjark) Check if the state events were received by the server
        # after the previous sync, since we need to include those state
        # updates even if they occurred logically before the previous event.
        # TODO(mjark) Check for new redactions in the state events.

        with Measure(self.clock, "compute_state_delta"):
            # The memberships needed for events in the timeline.
            # Only calculated when `lazy_load_members` is on.
            members_to_fetch: Optional[Set[str]] = None

            # A dictionary mapping user IDs to the first event in the timeline sent by
            # them. Only calculated when `lazy_load_members` is on.
            first_event_by_sender_map: Optional[Dict[str, EventBase]] = None

            # The contribution to the room state from state events in the timeline.
            # Only contains the last event for any given state key.
            timeline_state: StateMap[str]

            lazy_load_members = sync_config.filter_collection.lazy_load_members()
            include_redundant_members = (
                sync_config.filter_collection.include_redundant_members()
            )

            if lazy_load_members:
                # We only request state for the members needed to display the
                # timeline:

                timeline_state = {}

                # Membership events to fetch that can be found in the room state, or in
                # the case of partial state rooms, the auth events of timeline events.
                members_to_fetch = set()
                first_event_by_sender_map = {}
                for event in batch.events:
                    # Build the map from user IDs to the first timeline event they sent.
                    if event.sender not in first_event_by_sender_map:
                        first_event_by_sender_map[event.sender] = event

                    # We need the event's sender, unless their membership was in a
                    # previous timeline event.
                    if (EventTypes.Member, event.sender) not in timeline_state:
                        members_to_fetch.add(event.sender)
                    # FIXME: we also care about invite targets etc.

                    if event.is_state():
                        timeline_state[(event.type, event.state_key)] = event.event_id

            else:
                timeline_state = {
                    (event.type, event.state_key): event.event_id
                    for event in batch.events
                    if event.is_state()
                }

            # Now calculate the state to return in the sync response for the room.
            # This is more or less the change in state between the end of the previous
            # sync's timeline and the start of the current sync's timeline.
            # See the docstring above for details.
            state_ids: StateMap[str]
            # We need to know whether the state we fetch may be partial, so check
            # whether the room is partial stated *before* fetching it.
            is_partial_state_room = await self.store.is_partial_state_room(room_id)
            if full_state:
                state_ids = await self._compute_state_delta_for_full_sync(
                    room_id,
                    sync_config,
                    batch,
                    end_token,
                    members_to_fetch,
                    timeline_state,
                    joined,
                )
            else:
                # If this is an initial sync then full_state should be set, and
                # that case is handled above. We assert here to ensure that this
                # is indeed the case.
                assert since_token is not None

                state_ids = await self._compute_state_delta_for_incremental_sync(
                    room_id,
                    sync_config,
                    batch,
                    since_token,
                    end_token,
                    members_to_fetch,
                    timeline_state,
                )

            # If we only have partial state for the room, `state_ids` may be missing the
            # memberships we wanted. We attempt to find some by digging through the auth
            # events of timeline events.
            if lazy_load_members and is_partial_state_room:
                assert members_to_fetch is not None
                assert first_event_by_sender_map is not None

                additional_state_ids = (
                    await self._find_missing_partial_state_memberships(
                        room_id, members_to_fetch, first_event_by_sender_map, state_ids
                    )
                )
                state_ids = {**state_ids, **additional_state_ids}

            # At this point, if `lazy_load_members` is enabled, `state_ids` includes
            # the memberships of all event senders in the timeline. This is because we
            # may not have sent the memberships in a previous sync.

            # When `include_redundant_members` is on, we send all the lazy-loaded
            # memberships of event senders. Otherwise we make an effort to limit the set
            # of memberships we send to those that we have not already sent to this client.
            if lazy_load_members and not include_redundant_members:
                cache_key = (sync_config.user.to_string(), sync_config.device_id)
                cache = self.get_lazy_loaded_members_cache(cache_key)

                # if it's a new sync sequence, then assume the client has had
                # amnesia and doesn't want any recent lazy-loaded members
                # de-duplicated.
                if since_token is None:
                    logger.debug("clearing LruCache for %r", cache_key)
                    cache.clear()
                else:
                    # only send members which aren't in our LruCache (either
                    # because they're new to this client or have been pushed out
                    # of the cache)
                    logger.debug("filtering state from %r...", state_ids)
                    state_ids = {
                        t: event_id
                        for t, event_id in state_ids.items()
                        if cache.get(t[1]) != event_id
                    }
                    logger.debug("...to %r", state_ids)

                # add any member IDs we are about to send into our LruCache
                for t, event_id in itertools.chain(
                    state_ids.items(), timeline_state.items()
                ):
                    if t[0] == EventTypes.Member:
                        cache.set(t[1], event_id)

        state: Dict[str, EventBase] = {}
        if state_ids:
            state = await self.store.get_events(list(state_ids.values()))

        return {
            (e.type, e.state_key): e
            for e in await sync_config.filter_collection.filter_room_state(
                list(state.values())
            )
            if e.type != EventTypes.Aliases  # until MSC2261 or alternative solution
        }

    async def _compute_state_delta_for_full_sync(
        self,
        room_id: str,
        sync_config: SyncConfig,
        batch: TimelineBatch,
        end_token: StreamToken,
        members_to_fetch: Optional[Set[str]],
        timeline_state: StateMap[str],
        joined: bool,
    ) -> StateMap[str]:
        """Calculate the state events to be included in a full sync response.

        As with `_compute_state_delta_for_incremental_sync`, the result will include
        the membership events for the senders of each event in `members_to_fetch`.

        Note that whether this returns the state at the start or the end of the
        batch depends on `sync_config.use_state_after` (c.f. MSC4222).

        Args:
            room_id: The room we are calculating for.
            sync_confg: The user that is calling `/sync`.
            batch: The timeline batch for the room that will be sent to the user.
            end_token: Token of the end of the current batch. Normally this will be
                the same as the global "now_token", but if the user has left the room,
                the point just after their leave event.
            members_to_fetch: If lazy-loading is enabled, the memberships needed for
                events in the timeline.
            timeline_state: The contribution to the room state from state events in
                `batch`. Only contains the last event for any given state key.
            joined: whether the user is currently joined to the room

        Returns:
            A map from (type, state_key) to event_id, for each event that we believe
            should be included in the `state` or `state_after` part of the sync response.
        """
        if members_to_fetch is not None:
            # Lazy-loading of membership events is enabled.
            #
            # Always make sure we load our own membership event so we know if
            # we're in the room, to fix https://github.com/vector-im/riot-web/issues/7209.
            #
            # We only need apply this on full state syncs given we disabled
            # LL for incr syncs in https://github.com/matrix-org/synapse/pull/3840.
            #
            # We don't insert ourselves into `members_to_fetch`, because in some
            # rare cases (an empty event batch with a now_token after the user's
            # leave in a partial state room which another local user has
            # joined), the room state will be missing our membership and there
            # is no guarantee that our membership will be in the auth events of
            # timeline events when the room is partial stated.
            state_filter = StateFilter.from_lazy_load_member_list(
                members_to_fetch.union((sync_config.user.to_string(),))
            )

            # We are happy to use partial state to compute the `/sync` response.
            # Since partial state may not include the lazy-loaded memberships we
            # require, we fix up the state response afterwards with memberships from
            # auth events.
            await_full_state = False
            lazy_load_members = True
        else:
            state_filter = StateFilter.all()
            await_full_state = True
            lazy_load_members = False

        # Check if we are wanting to return the state at the start or end of the
        # timeline. If at the end we can just use the current state.
        if sync_config.use_state_after:
            # If we're getting the state at the end of the timeline, we can just
            # use the current state of the room (and roll back any changes
            # between when we fetched the current state and `end_token`).
            #
            # For rooms we're not joined to, there might be a very large number
            # of deltas between `end_token` and "now", and so instead we fetch
            # the state at the end of the timeline.
            if joined:
                state_ids = await self._state_storage_controller.get_current_state_ids(
                    room_id,
                    state_filter=state_filter,
                    await_full_state=await_full_state,
                )

                # Now roll back the state by looking at the state deltas between
                # end_token and now.
                deltas = await self.store.get_current_state_deltas_for_room(
                    room_id,
                    from_token=end_token.room_key,
                    to_token=self.store.get_room_max_token(),
                )
                if deltas:
                    mutable_state_ids = dict(state_ids)

                    # We iterate over the deltas backwards so that if there are
                    # multiple changes of the same type/state_key we'll
                    # correctly pick the earliest delta.
                    for delta in reversed(deltas):
                        if delta.prev_event_id:
                            mutable_state_ids[(delta.event_type, delta.state_key)] = (
                                delta.prev_event_id
                            )
                        elif (delta.event_type, delta.state_key) in mutable_state_ids:
                            mutable_state_ids.pop((delta.event_type, delta.state_key))

                    state_ids = mutable_state_ids

                return state_ids

            else:
                # Just use state groups to get the state at the end of the
                # timeline, i.e. the state at the leave/etc event.
                state_at_timeline_end = (
                    await self._state_storage_controller.get_state_ids_at(
                        room_id,
                        stream_position=end_token,
                        state_filter=state_filter,
                        await_full_state=await_full_state,
                    )
                )
                return state_at_timeline_end

        state_at_timeline_end = await self._state_storage_controller.get_state_ids_at(
            room_id,
            stream_position=end_token,
            state_filter=state_filter,
            await_full_state=await_full_state,
        )

        if batch:
            # Strictly speaking, this returns the state *after* the first event in the
            # timeline, but that is good enough here.
            state_at_timeline_start = (
                await self._state_storage_controller.get_state_ids_for_event(
                    batch.events[0].event_id,
                    state_filter=state_filter,
                    await_full_state=await_full_state,
                )
            )
        else:
            state_at_timeline_start = state_at_timeline_end

        state_ids = _calculate_state(
            timeline_contains=timeline_state,
            timeline_start=state_at_timeline_start,
            timeline_end=state_at_timeline_end,
            previous_timeline_end={},
            lazy_load_members=lazy_load_members,
        )
        return state_ids

    async def _compute_state_delta_for_incremental_sync(
        self,
        room_id: str,
        sync_config: SyncConfig,
        batch: TimelineBatch,
        since_token: StreamToken,
        end_token: StreamToken,
        members_to_fetch: Optional[Set[str]],
        timeline_state: StateMap[str],
    ) -> StateMap[str]:
        """Calculate the state events to be included in an incremental sync response.

        If lazy-loading of membership events is enabled (as indicated by
        `members_to_fetch` being not-`None`), the result will include the membership
        events for each member in `members_to_fetch`. The caller
        (`compute_state_delta`) is responsible for keeping track of which membership
        events we have already sent to the client, and hence ripping them out.

        Note that whether this returns the state at the start or the end of the
        batch depends on `sync_config.use_state_after` (c.f. MSC4222).

        Args:
            room_id: The room we are calculating for.
            sync_config
            batch: The timeline batch for the room that will be sent to the user.
            since_token: Token of the end of the previous batch.
            end_token: Token of the end of the current batch. Normally this will be
                the same as the global "now_token", but if the user has left the room,
                the point just after their leave event.
            members_to_fetch: If lazy-loading is enabled, the memberships needed for
                events in the timeline. Otherwise, `None`.
            timeline_state: The contribution to the room state from state events in
                `batch`. Only contains the last event for any given state key.

        Returns:
            A map from (type, state_key) to event_id, for each event that we believe
            should be included in the `state` or `state_after` part of the sync response.
        """
        if members_to_fetch is not None:
            # Lazy-loading is enabled. Only return the state that is needed.
            state_filter = StateFilter.from_lazy_load_member_list(members_to_fetch)
            await_full_state = False
            lazy_load_members = True
        else:
            state_filter = StateFilter.all()
            await_full_state = True
            lazy_load_members = False

        # Check if we are wanting to return the state at the start or end of the
        # timeline. If at the end we can just use the current state delta stream.
        if sync_config.use_state_after:
            delta_state_ids: MutableStateMap[str] = {}

            if members_to_fetch:
                # We're lazy-loading, so the client might need some more member
                # events to understand the events in this timeline. So we always
                # fish out all the member events corresponding to the timeline
                # here. The caller will then dedupe any redundant ones.
                member_ids = await self._state_storage_controller.get_current_state_ids(
                    room_id=room_id,
                    state_filter=StateFilter.from_types(
                        (EventTypes.Member, member) for member in members_to_fetch
                    ),
                    await_full_state=await_full_state,
                )
                delta_state_ids.update(member_ids)

            # We don't do LL filtering for incremental syncs - see
            # https://github.com/vector-im/riot-web/issues/7211#issuecomment-419976346
            # N.B. this slows down incr syncs as we are now processing way more
            # state in the server than if we were LLing.
            #
            # i.e. we return all state deltas, including membership changes that
            # we'd normally exclude due to LL.
            deltas = await self.store.get_current_state_deltas_for_room(
                room_id=room_id,
                from_token=since_token.room_key,
                to_token=end_token.room_key,
            )
            for delta in deltas:
                if delta.event_id is None:
                    # There was a state reset and this state entry is no longer
                    # present, but we have no way of informing the client about
                    # this, so we just skip it for now.
                    continue

                # Note that deltas are in stream ordering, so if there are
                # multiple deltas for a given type/state_key we'll always pick
                # the latest one.
                delta_state_ids[(delta.event_type, delta.state_key)] = delta.event_id

            return delta_state_ids

        # For a non-gappy sync if the events in the timeline are simply a linear
        # chain (i.e. no merging/branching of the graph), then we know the state
        # delta between the end of the previous sync and start of the new one is
        # empty.
        #
        # c.f. #16941 for an example of why we can't do this for all non-gappy
        # syncs.
        is_linear_timeline = True
        if batch.events:
            # We need to make sure the first event in our batch points to the
            # last event in the previous batch.
            last_event_id_prev_batch = (
                await self.store.get_last_event_id_in_room_before_stream_ordering(
                    room_id,
                    end_token=since_token.room_key,
                )
            )

            prev_event_id = last_event_id_prev_batch
            for e in batch.events:
                if e.prev_event_ids() != [prev_event_id]:
                    is_linear_timeline = False
                    break
                prev_event_id = e.event_id

        if is_linear_timeline and not batch.limited:
            state_ids: StateMap[str] = {}
            if lazy_load_members:
                if members_to_fetch and batch.events:
                    # We're lazy-loading, so the client might need some more
                    # member events to understand the events in this timeline.
                    # So we fish out all the member events corresponding to the
                    # timeline here. The caller will then dedupe any redundant
                    # ones.

                    state_ids = (
                        await self._state_storage_controller.get_state_ids_for_event(
                            batch.events[0].event_id,
                            # we only want members!
                            state_filter=StateFilter.from_types(
                                (EventTypes.Member, member)
                                for member in members_to_fetch
                            ),
                            await_full_state=False,
                        )
                    )
            return state_ids

        if batch:
            state_at_timeline_start = (
                await self._state_storage_controller.get_state_ids_for_event(
                    batch.events[0].event_id,
                    state_filter=state_filter,
                    await_full_state=await_full_state,
                )
            )
        else:
            # We can get here if the user has ignored the senders of all
            # the recent events.
            state_at_timeline_start = (
                await self._state_storage_controller.get_state_ids_at(
                    room_id,
                    stream_position=end_token,
                    state_filter=state_filter,
                    await_full_state=await_full_state,
                )
            )

        if batch.limited:
            # for now, we disable LL for gappy syncs - see
            # https://github.com/vector-im/riot-web/issues/7211#issuecomment-419976346
            # N.B. this slows down incr syncs as we are now processing way
            # more state in the server than if we were LLing.
            #
            # We still have to filter timeline_start to LL entries (above) in order
            # for _calculate_state's LL logic to work, as we have to include LL
            # members for timeline senders in case they weren't loaded in the initial
            # sync.  We do this by (counterintuitively) by filtering timeline_start
            # members to just be ones which were timeline senders, which then ensures
            # all of the rest get included in the state block (if we need to know
            # about them).
            state_filter = StateFilter.all()

        state_at_previous_sync = await self._state_storage_controller.get_state_ids_at(
            room_id,
            stream_position=since_token,
            state_filter=state_filter,
            await_full_state=await_full_state,
        )

        state_at_timeline_end = await self._state_storage_controller.get_state_ids_at(
            room_id,
            stream_position=end_token,
            state_filter=state_filter,
            await_full_state=await_full_state,
        )

        state_ids = _calculate_state(
            timeline_contains=timeline_state,
            timeline_start=state_at_timeline_start,
            timeline_end=state_at_timeline_end,
            previous_timeline_end=state_at_previous_sync,
            lazy_load_members=lazy_load_members,
        )

        return state_ids

    async def _find_missing_partial_state_memberships(
        self,
        room_id: str,
        members_to_fetch: StrCollection,
        events_with_membership_auth: Mapping[str, EventBase],
        found_state_ids: StateMap[str],
    ) -> StateMap[str]:
        """Finds missing memberships from a set of auth events and returns them as a
        state map.

        Args:
            room_id: The partial state room to find the remaining memberships for.
            members_to_fetch: The memberships to find.
            events_with_membership_auth: A mapping from user IDs to events whose auth
                events would contain their prior membership, if one exists.
                Note that join events will not cite a prior membership if a user has
                never been in a room before.
            found_state_ids: A dict from (type, state_key) -> state_event_id, containing
                memberships that have been previously found. Entries in
                `members_to_fetch` that have a membership in `found_state_ids` are
                ignored.

        Returns:
            A dict from ("m.room.member", state_key) -> state_event_id, containing the
            memberships missing from `found_state_ids`.

            When `events_with_membership_auth` contains a join event for a given user
            which does not cite a prior membership, no membership is returned for that
            user.

        Raises:
            KeyError: if `events_with_membership_auth` does not have an entry for a
                missing membership. Memberships in `found_state_ids` do not need an
                entry in `events_with_membership_auth`.
        """
        additional_state_ids: MutableStateMap[str] = {}

        # Tracks the missing members for logging purposes.
        missing_members = set()

        # Identify memberships missing from `found_state_ids` and pick out the auth
        # events in which to look for them.
        auth_event_ids: Set[str] = set()
        for member in members_to_fetch:
            if (EventTypes.Member, member) in found_state_ids:
                continue

            event_with_membership_auth = events_with_membership_auth[member]
            is_create = (
                event_with_membership_auth.is_state()
                and event_with_membership_auth.type == EventTypes.Create
            )
            is_join = (
                event_with_membership_auth.is_state()
                and event_with_membership_auth.type == EventTypes.Member
                and event_with_membership_auth.state_key == member
                and event_with_membership_auth.content.get("membership")
                == Membership.JOIN
            )
            if not is_create and not is_join:
                # The event must include the desired membership as an auth event, unless
                # it's the `m.room.create` event for a room or the first join event for
                # a given user.
                missing_members.add(member)
            auth_event_ids.update(event_with_membership_auth.auth_event_ids())

        auth_events = await self.store.get_events(auth_event_ids)

        # Run through the missing memberships once more, picking out the memberships
        # from the pile of auth events we have just fetched.
        for member in members_to_fetch:
            if (EventTypes.Member, member) in found_state_ids:
                continue

            event_with_membership_auth = events_with_membership_auth[member]

            # Dig through the auth events to find the desired membership.
            for auth_event_id in event_with_membership_auth.auth_event_ids():
                # We only store events once we have all their auth events,
                # so the auth event must be in the pile we have just
                # fetched.
                auth_event = auth_events[auth_event_id]

                if (
                    auth_event.type == EventTypes.Member
                    and auth_event.state_key == member
                ):
                    missing_members.discard(member)
                    additional_state_ids[(EventTypes.Member, member)] = (
                        auth_event.event_id
                    )
                    break

        if missing_members:
            # There really shouldn't be any missing memberships now. Either:
            #  * we couldn't find an auth event, which shouldn't happen because we do
            #    not persist events with persisting their auth events first, or
            #  * the set of auth events did not contain a membership we wanted, which
            #    means our caller didn't compute the events in `members_to_fetch`
            #    correctly, or we somehow accepted an event whose auth events were
            #    dodgy.
            logger.error(
                "Failed to find memberships for %s in partial state room "
                "%s in the auth events of %s.",
                missing_members,
                room_id,
                [
                    events_with_membership_auth[member].event_id
                    for member in missing_members
                ],
            )

        return additional_state_ids

    async def unread_notifs_for_room_id(
        self, room_id: str, sync_config: SyncConfig
    ) -> RoomNotifCounts:
        if not self.should_calculate_push_rules:
            # If push rules have been universally disabled then we know we won't
            # have any unread counts in the DB, so we may as well skip asking
            # the DB.
            return RoomNotifCounts.empty()

        with Measure(self.clock, "unread_notifs_for_room_id"):
            return await self.store.get_unread_event_push_actions_by_room_for_user(
                room_id,
                sync_config.user.to_string(),
            )

    async def generate_sync_result(
        self,
        sync_config: SyncConfig,
        since_token: Optional[StreamToken] = None,
        full_state: bool = False,
    ) -> SyncResult:
        """Generates the response body of a sync result.

        This is represented by a `SyncResult` struct, which is built from small pieces
        using a `SyncResultBuilder`. See also
            https://spec.matrix.org/v1.1/client-server-api/#get_matrixclientv3sync
        the `sync_result_builder` is passed as a mutable ("inout") parameter to various
        helper functions. These retrieve and process the data which forms the sync body,
        often writing to the `sync_result_builder` to store their output.

        At the end, we transfer data from the `sync_result_builder` to a new `SyncResult`
        instance to signify that the sync calculation is complete.
        """

        user_id = sync_config.user.to_string()
        app_service = self.store.get_app_service_by_user_id(user_id)
        if app_service:
            # We no longer support AS users using /sync directly.
            # See https://github.com/matrix-org/matrix-doc/issues/1144
            raise NotImplementedError()

        sync_result_builder = await self.get_sync_result_builder(
            sync_config,
            since_token,
            full_state,
        )

        logger.debug(
            "Calculating sync response for %r between %s and %s",
            sync_config.user,
            sync_result_builder.since_token,
            sync_result_builder.now_token,
        )

        logger.debug("Fetching account data")

        # Global account data is included if it is not filtered out.
        if not sync_config.filter_collection.blocks_all_global_account_data():
            await self._generate_sync_entry_for_account_data(sync_result_builder)

        # Presence data is included if the server has it enabled and not filtered out.
        include_presence_data = bool(
            self.hs_config.server.presence_enabled
            and not sync_config.filter_collection.blocks_all_presence()
        )
        # Device list updates are sent if a since token is provided.
        include_device_list_updates = bool(since_token and since_token.device_list_key)

        # If we do not care about the rooms or things which depend on the room
        # data (namely presence and device list updates), then we can skip
        # this process completely.
        device_lists = DeviceListUpdates()
        if (
            not sync_result_builder.sync_config.filter_collection.blocks_all_rooms()
            or include_presence_data
            or include_device_list_updates
        ):
            logger.debug("Fetching room data")

            # Note that _generate_sync_entry_for_rooms sets sync_result_builder.joined, which
            # is used in calculate_user_changes below.
            (
                newly_joined_rooms,
                newly_left_rooms,
            ) = await self._generate_sync_entry_for_rooms(sync_result_builder)

            # Work out which users have joined or left rooms we're in. We use this
            # to build the presence and device_list parts of the sync response in
            # `_generate_sync_entry_for_presence` and
            # `_generate_sync_entry_for_device_list` respectively.
            if include_presence_data or include_device_list_updates:
                # This uses the sync_result_builder.joined which is set in
                # `_generate_sync_entry_for_rooms`, if that didn't find any joined
                # rooms for some reason it is a no-op.
                (
                    newly_joined_or_invited_or_knocked_users,
                    newly_left_users,
                ) = sync_result_builder.calculate_user_changes()

                if include_presence_data:
                    logger.debug("Fetching presence data")
                    await self._generate_sync_entry_for_presence(
                        sync_result_builder,
                        newly_joined_rooms,
                        newly_joined_or_invited_or_knocked_users,
                    )

                if include_device_list_updates:
                    # include_device_list_updates can only be True if we have a
                    # since token.
                    assert since_token is not None

                    device_lists = await self._device_handler.generate_sync_entry_for_device_list(
                        user_id=user_id,
                        since_token=since_token,
                        now_token=sync_result_builder.now_token,
                        joined_room_ids=sync_result_builder.joined_room_ids,
                        newly_joined_rooms=newly_joined_rooms,
                        newly_joined_or_invited_or_knocked_users=newly_joined_or_invited_or_knocked_users,
                        newly_left_rooms=newly_left_rooms,
                        newly_left_users=newly_left_users,
                    )

        logger.debug("Fetching to-device data")
        await self._generate_sync_entry_for_to_device(sync_result_builder)

        logger.debug("Fetching OTK data")
        device_id = sync_config.device_id
        one_time_keys_count: JsonMapping = {}
        unused_fallback_key_types: List[str] = []
        if device_id:
            # TODO: We should have a way to let clients differentiate between the states of:
            #   * no change in OTK count since the provided since token
            #   * the server has zero OTKs left for this device
            #  Spec issue: https://github.com/matrix-org/matrix-doc/issues/3298
            one_time_keys_count = await self.store.count_e2e_one_time_keys(
                user_id, device_id
            )
            unused_fallback_key_types = list(
                await self.store.get_e2e_unused_fallback_key_types(user_id, device_id)
            )

        num_events = 0

        # debug for https://github.com/matrix-org/synapse/issues/9424
        for joined_room in sync_result_builder.joined:
            num_events += len(joined_room.timeline.events)

        log_kv(
            {
                "joined_rooms_in_result": len(sync_result_builder.joined),
                "events_in_result": num_events,
            }
        )

        logger.debug("Sync response calculation complete")
        return SyncResult(
            presence=sync_result_builder.presence,
            account_data=sync_result_builder.account_data,
            joined=sync_result_builder.joined,
            invited=sync_result_builder.invited,
            knocked=sync_result_builder.knocked,
            archived=sync_result_builder.archived,
            to_device=sync_result_builder.to_device,
            device_lists=device_lists,
            device_one_time_keys_count=one_time_keys_count,
            device_unused_fallback_key_types=unused_fallback_key_types,
            next_batch=sync_result_builder.now_token,
        )

    async def generate_e2ee_sync_result(
        self,
        sync_config: SyncConfig,
        since_token: Optional[StreamToken] = None,
    ) -> E2eeSyncResult:
        """
        Generates the response body of a MSC3575 Sliding Sync `/sync/e2ee` result.

        This is represented by a `E2eeSyncResult` struct, which is built from small
        pieces using a `SyncResultBuilder`. The `sync_result_builder` is passed as a
        mutable ("inout") parameter to various helper functions. These retrieve and
        process the data which forms the sync body, often writing to the
        `sync_result_builder` to store their output.

        At the end, we transfer data from the `sync_result_builder` to a new `E2eeSyncResult`
        instance to signify that the sync calculation is complete.
        """
        user_id = sync_config.user.to_string()
        app_service = self.store.get_app_service_by_user_id(user_id)
        if app_service:
            # We no longer support AS users using /sync directly.
            # See https://github.com/matrix-org/matrix-doc/issues/1144
            raise NotImplementedError()

        sync_result_builder = await self.get_sync_result_builder(
            sync_config,
            since_token,
            full_state=False,
        )

        # 1. Calculate `to_device` events
        await self._generate_sync_entry_for_to_device(sync_result_builder)

        # 2. Calculate `device_lists`
        # Device list updates are sent if a since token is provided.
        device_lists = DeviceListUpdates()
        include_device_list_updates = bool(since_token and since_token.device_list_key)
        if include_device_list_updates:
            # Note that _generate_sync_entry_for_rooms sets sync_result_builder.joined, which
            # is used in calculate_user_changes below.
            #
            # TODO: Running `_generate_sync_entry_for_rooms()` is a lot of work just to
            # figure out the membership changes/derived info needed for
            # `_generate_sync_entry_for_device_list()`. In the future, we should try to
            # refactor this away.
            (
                newly_joined_rooms,
                newly_left_rooms,
            ) = await self._generate_sync_entry_for_rooms(sync_result_builder)

            # This uses the sync_result_builder.joined which is set in
            # `_generate_sync_entry_for_rooms`, if that didn't find any joined
            # rooms for some reason it is a no-op.
            (
                newly_joined_or_invited_or_knocked_users,
                newly_left_users,
            ) = sync_result_builder.calculate_user_changes()

            # include_device_list_updates can only be True if we have a
            # since token.
            assert since_token is not None
            device_lists = await self._device_handler.generate_sync_entry_for_device_list(
                user_id=user_id,
                since_token=since_token,
                now_token=sync_result_builder.now_token,
                joined_room_ids=sync_result_builder.joined_room_ids,
                newly_joined_rooms=newly_joined_rooms,
                newly_joined_or_invited_or_knocked_users=newly_joined_or_invited_or_knocked_users,
                newly_left_rooms=newly_left_rooms,
                newly_left_users=newly_left_users,
            )

        # 3. Calculate `device_one_time_keys_count` and `device_unused_fallback_key_types`
        device_id = sync_config.device_id
        one_time_keys_count: JsonMapping = {}
        unused_fallback_key_types: List[str] = []
        if device_id:
            # TODO: We should have a way to let clients differentiate between the states of:
            #   * no change in OTK count since the provided since token
            #   * the server has zero OTKs left for this device
            #  Spec issue: https://github.com/matrix-org/matrix-doc/issues/3298
            one_time_keys_count = await self.store.count_e2e_one_time_keys(
                user_id, device_id
            )
            unused_fallback_key_types = list(
                await self.store.get_e2e_unused_fallback_key_types(user_id, device_id)
            )

        return E2eeSyncResult(
            to_device=sync_result_builder.to_device,
            device_lists=device_lists,
            device_one_time_keys_count=one_time_keys_count,
            device_unused_fallback_key_types=unused_fallback_key_types,
            next_batch=sync_result_builder.now_token,
        )

    async def get_sync_result_builder(
        self,
        sync_config: SyncConfig,
        since_token: Optional[StreamToken] = None,
        full_state: bool = False,
    ) -> "SyncResultBuilder":
        """
        Assemble a `SyncResultBuilder` with all of the initial context to
        start building up the sync response:

        - Membership changes between the last sync and the current sync.
        - Joined room IDs (minus any rooms to exclude).
        - Rooms that became fully-stated/un-partial stated since the last sync.

        Args:
            sync_config: Config/info necessary to process the sync request.
            since_token: The point in the stream to sync from.
            full_state: Whether to return the full state for each room.

        Returns:
            `SyncResultBuilder` ready to start generating parts of the sync response.
        """
        user_id = sync_config.user.to_string()

        # Note: we get the users room list *before* we get the `now_token`, this
        # avoids checking back in history if rooms are joined after the token is fetched.
        token_before_rooms = self.event_sources.get_current_token()
        mutable_joined_room_ids = set(await self.store.get_rooms_for_user(user_id))

        # NB: The `now_token` gets changed by some of the `generate_sync_*` methods,
        # this is due to some of the underlying streams not supporting the ability
        # to query up to a given point.
        # Always use the `now_token` in `SyncResultBuilder`
        now_token = self.event_sources.get_current_token()
        log_kv({"now_token": now_token})

        # Since we fetched the users room list before calculating the `now_token` (see
        # above), there's a small window during which membership events may have been
        # persisted, so we fetch these now and modify the joined room list for any
        # changes between the get_rooms_for_user call and the get_current_token call.
        membership_change_events = []
        if since_token:
            membership_change_events = await self.store.get_membership_changes_for_user(
                user_id,
                since_token.room_key,
                now_token.room_key,
                self.rooms_to_exclude_globally,
            )

            last_membership_change_by_room_id: Dict[str, EventBase] = {}
            for event in membership_change_events:
                last_membership_change_by_room_id[event.room_id] = event

            # For the latest membership event in each room found, add/remove the room ID
            # from the joined room list accordingly. In this case we only care if the
            # latest change is JOIN.

            for room_id, event in last_membership_change_by_room_id.items():
                assert event.internal_metadata.stream_ordering
                # As a shortcut, skip any events that happened before we got our
                # `get_rooms_for_user()` snapshot (any changes are already represented
                # in that list).
                if (
                    event.internal_metadata.stream_ordering
                    < token_before_rooms.room_key.stream
                ):
                    continue

                logger.info(
                    "User membership change between getting rooms and current token: %s %s %s",
                    user_id,
                    event.membership,
                    room_id,
                )
                # User joined a room - we have to then check the room state to ensure we
                # respect any bans if there's a race between the join and ban events.
                if event.membership == Membership.JOIN:
                    user_ids_in_room = await self.store.get_users_in_room(room_id)
                    if user_id in user_ids_in_room:
                        mutable_joined_room_ids.add(room_id)
                # The user left the room, or left and was re-invited but not joined yet
                else:
                    mutable_joined_room_ids.discard(room_id)

        # Tweak the set of rooms to return to the client for eager (non-lazy) syncs.
        mutable_rooms_to_exclude = set(self.rooms_to_exclude_globally)
        if not sync_config.filter_collection.lazy_load_members():
            # Non-lazy syncs should never include partially stated rooms.
            # Exclude all partially stated rooms from this sync.
            results = await self.store.is_partial_state_room_batched(
                mutable_joined_room_ids
            )
            mutable_rooms_to_exclude.update(
                room_id
                for room_id, is_partial_state in results.items()
                if is_partial_state
            )
            membership_change_events = [
                event
                for event in membership_change_events
                if not results.get(event.room_id, False)
            ]

        # Incremental eager syncs should additionally include rooms that
        # - we are joined to
        # - are full-stated
        # - became fully-stated at some point during the sync period
        #   (These rooms will have been omitted during a previous eager sync.)
        forced_newly_joined_room_ids: Set[str] = set()
        if since_token and not sync_config.filter_collection.lazy_load_members():
            un_partial_stated_rooms = (
                await self.store.get_un_partial_stated_rooms_between(
                    since_token.un_partial_stated_rooms_key,
                    now_token.un_partial_stated_rooms_key,
                    mutable_joined_room_ids,
                )
            )
            results = await self.store.is_partial_state_room_batched(
                un_partial_stated_rooms
            )
            forced_newly_joined_room_ids.update(
                room_id
                for room_id, is_partial_state in results.items()
                if not is_partial_state
            )

        # Now we have our list of joined room IDs, exclude as configured and freeze
        joined_room_ids = frozenset(
            room_id
            for room_id in mutable_joined_room_ids
            if room_id not in mutable_rooms_to_exclude
        )

        sync_result_builder = SyncResultBuilder(
            sync_config,
            full_state,
            since_token=since_token,
            now_token=now_token,
            joined_room_ids=joined_room_ids,
            excluded_room_ids=frozenset(mutable_rooms_to_exclude),
            forced_newly_joined_room_ids=frozenset(forced_newly_joined_room_ids),
            membership_change_events=membership_change_events,
        )

        return sync_result_builder

    @trace
    async def _generate_sync_entry_for_to_device(
        self, sync_result_builder: "SyncResultBuilder"
    ) -> None:
        """Generates the portion of the sync response. Populates
        `sync_result_builder` with the result.
        """
        user_id = sync_result_builder.sync_config.user.to_string()
        device_id = sync_result_builder.sync_config.device_id
        now_token = sync_result_builder.now_token
        since_stream_id = 0
        if sync_result_builder.since_token is not None:
            since_stream_id = int(sync_result_builder.since_token.to_device_key)

        if device_id is not None and since_stream_id != int(now_token.to_device_key):
            messages, stream_id = await self.store.get_messages_for_device(
                user_id, device_id, since_stream_id, now_token.to_device_key
            )

            for message in messages:
                log_kv(
                    {
                        "event": "to_device_message",
                        "sender": message["sender"],
                        "type": message["type"],
                        EventContentFields.TO_DEVICE_MSGID: message["content"].get(
                            EventContentFields.TO_DEVICE_MSGID
                        ),
                    }
                )

            if messages and issue9533_logger.isEnabledFor(logging.DEBUG):
                issue9533_logger.debug(
                    "Returning to-device messages with stream_ids (%d, %d]; now: %d;"
                    " msgids: %s",
                    since_stream_id,
                    stream_id,
                    now_token.to_device_key,
                    [
                        message["content"].get(EventContentFields.TO_DEVICE_MSGID)
                        for message in messages
                    ],
                )
            sync_result_builder.now_token = now_token.copy_and_replace(
                StreamKeyType.TO_DEVICE, stream_id
            )
            sync_result_builder.to_device = messages
        else:
            sync_result_builder.to_device = []

    async def _generate_sync_entry_for_account_data(
        self, sync_result_builder: "SyncResultBuilder"
    ) -> None:
        """Generates the global account data portion of the sync response.

        Account data (called "Client Config" in the spec) can be set either globally
        or for a specific room. Account data consists of a list of events which
        accumulate state, much like a room.

        This function retrieves global account data and writes it to the given
        `sync_result_builder`. See `_generate_sync_entry_for_rooms` for handling
         of per-room account data.

        Args:
            sync_result_builder
        """
        sync_config = sync_result_builder.sync_config
        user_id = sync_result_builder.sync_config.user.to_string()
        since_token = sync_result_builder.since_token

        if since_token and not sync_result_builder.full_state:
            global_account_data = (
                await self.store.get_updated_global_account_data_for_user(
                    user_id, since_token.account_data_key
                )
            )

            push_rules_changed = await self.store.have_push_rules_changed_for_user(
                user_id, int(since_token.push_rules_key)
            )

            if push_rules_changed:
                global_account_data = dict(global_account_data)
                global_account_data[
                    AccountDataTypes.PUSH_RULES
                ] = await self._push_rules_handler.push_rules_for_user(sync_config.user)
        else:
            all_global_account_data = await self.store.get_global_account_data_for_user(
                user_id
            )

            global_account_data = dict(all_global_account_data)
            global_account_data[
                AccountDataTypes.PUSH_RULES
            ] = await self._push_rules_handler.push_rules_for_user(sync_config.user)

        account_data_for_user = (
            await sync_config.filter_collection.filter_global_account_data(
                [
                    {"type": account_data_type, "content": content}
                    for account_data_type, content in global_account_data.items()
                ]
            )
        )

        sync_result_builder.account_data = account_data_for_user

    async def _generate_sync_entry_for_presence(
        self,
        sync_result_builder: "SyncResultBuilder",
        newly_joined_rooms: AbstractSet[str],
        newly_joined_or_invited_users: AbstractSet[str],
    ) -> None:
        """Generates the presence portion of the sync response. Populates the
        `sync_result_builder` with the result.

        Args:
            sync_result_builder
            newly_joined_rooms: Set of rooms that the user has joined since
                the last sync (or empty if an initial sync)
            newly_joined_or_invited_users: Set of users that have joined or
                been invited to rooms since the last sync (or empty if an
                initial sync)
        """
        now_token = sync_result_builder.now_token
        sync_config = sync_result_builder.sync_config
        user = sync_result_builder.sync_config.user

        presence_source = self.event_sources.sources.presence

        since_token = sync_result_builder.since_token
        presence_key = None
        include_offline = False
        if since_token and not sync_result_builder.full_state:
            presence_key = since_token.presence_key
            include_offline = True

        presence, presence_key = await presence_source.get_new_events(
            user=user,
            from_key=presence_key,
            is_guest=sync_config.is_guest,
            include_offline=(
                True
                if self.hs_config.server.presence_include_offline_users_on_sync
                else include_offline
            ),
        )
        assert presence_key
        sync_result_builder.now_token = now_token.copy_and_replace(
            StreamKeyType.PRESENCE, presence_key
        )

        extra_users_ids = set(newly_joined_or_invited_users)
        for room_id in newly_joined_rooms:
            users = await self.store.get_users_in_room(room_id)
            extra_users_ids.update(users)
        extra_users_ids.discard(user.to_string())

        if extra_users_ids:
            states = await self.presence_handler.get_states(extra_users_ids)
            presence.extend(states)

            # Deduplicate the presence entries so that there's at most one per user
            presence = list({p.user_id: p for p in presence}.values())

        presence = await sync_config.filter_collection.filter_presence(presence)

        sync_result_builder.presence = presence

    async def _generate_sync_entry_for_rooms(
        self, sync_result_builder: "SyncResultBuilder"
    ) -> Tuple[AbstractSet[str], AbstractSet[str]]:
        """Generates the rooms portion of the sync response. Populates the
        `sync_result_builder` with the result.

        In the response that reaches the client, rooms are divided into four categories:
        `invite`, `join`, `knock`, `leave`. These aren't the same as the four sets of
        room ids returned by this function.

        Args:
            sync_result_builder

        Returns:
            Returns a 2-tuple describing rooms the user has joined or left.

            Its entries are:
            - newly_joined_rooms
            - newly_left_rooms
        """

        since_token = sync_result_builder.since_token
        user_id = sync_result_builder.sync_config.user.to_string()

        blocks_all_rooms = (
            sync_result_builder.sync_config.filter_collection.blocks_all_rooms()
        )

        # 0. Start by fetching room account data (if required).
        if (
            blocks_all_rooms
            or sync_result_builder.sync_config.filter_collection.blocks_all_room_account_data()
        ):
            account_data_by_room: Mapping[str, Mapping[str, JsonMapping]] = {}
        elif since_token and not sync_result_builder.full_state:
            account_data_by_room = (
                await self.store.get_updated_room_account_data_for_user(
                    user_id, since_token.account_data_key
                )
            )
        else:
            account_data_by_room = await self.store.get_room_account_data_for_user(
                user_id
            )

        # 1. Start by fetching all ephemeral events in rooms we've joined (if required).
        block_all_room_ephemeral = (
            blocks_all_rooms
            or sync_result_builder.sync_config.filter_collection.blocks_all_room_ephemeral()
        )
        if block_all_room_ephemeral:
            ephemeral_by_room: Dict[str, List[JsonDict]] = {}
        else:
            now_token, ephemeral_by_room = await self.ephemeral_by_room(
                sync_result_builder,
                now_token=sync_result_builder.now_token,
                since_token=sync_result_builder.since_token,
            )
            sync_result_builder.now_token = now_token

        # 2. We check up front if anything has changed, if it hasn't then there is
        # no point in going further.
        if not sync_result_builder.full_state:
            if since_token and not ephemeral_by_room and not account_data_by_room:
                have_changed = await self._have_rooms_changed(sync_result_builder)
                log_kv({"rooms_have_changed": have_changed})
                if not have_changed:
                    tags_by_room = await self.store.get_updated_tags(
                        user_id, since_token.account_data_key
                    )
                    if not tags_by_room:
                        logger.debug("no-oping sync")
                        return set(), set()

        # 3. Work out which rooms need reporting in the sync response.
        ignored_users = await self.store.ignored_users(user_id)
        if since_token:
            room_changes = await self._get_room_changes_for_incremental_sync(
                sync_result_builder, ignored_users
            )
            tags_by_room = await self.store.get_updated_tags(
                user_id, since_token.account_data_key
            )
        else:
            room_changes = await self._get_room_changes_for_initial_sync(
                sync_result_builder, ignored_users
            )
            tags_by_room = await self.store.get_tags_for_user(user_id)

        log_kv({"rooms_changed": len(room_changes.room_entries)})

        room_entries = room_changes.room_entries
        invited = room_changes.invited
        knocked = room_changes.knocked
        newly_joined_rooms = room_changes.newly_joined_rooms
        newly_left_rooms = room_changes.newly_left_rooms

        # 4. We need to apply further processing to `room_entries` (rooms considered
        # joined or archived).
        async def handle_room_entries(room_entry: "RoomSyncResultBuilder") -> None:
            logger.debug("Generating room entry for %s", room_entry.room_id)
            # Note that this mutates sync_result_builder.{joined,archived}.
            await self._generate_room_entry(
                sync_result_builder,
                room_entry,
                ephemeral=ephemeral_by_room.get(room_entry.room_id, []),
                tags=tags_by_room.get(room_entry.room_id),
                account_data=account_data_by_room.get(room_entry.room_id, {}),
                always_include=sync_result_builder.full_state,
            )
            logger.debug("Generated room entry for %s", room_entry.room_id)

        with start_active_span("sync.generate_room_entries"):
            await concurrently_execute(handle_room_entries, room_entries, 10)

        sync_result_builder.invited.extend(invited)
        sync_result_builder.knocked.extend(knocked)

        return set(newly_joined_rooms), set(newly_left_rooms)

    async def _have_rooms_changed(
        self, sync_result_builder: "SyncResultBuilder"
    ) -> bool:
        """Returns whether there may be any new events that should be sent down
        the sync. Returns True if there are.

        Does not modify the `sync_result_builder`.
        """
        since_token = sync_result_builder.since_token
        membership_change_events = sync_result_builder.membership_change_events

        assert since_token

        if membership_change_events or sync_result_builder.forced_newly_joined_room_ids:
            return True

        stream_id = since_token.room_key.stream
        for room_id in sync_result_builder.joined_room_ids:
            if self.store.has_room_changed_since(room_id, stream_id):
                return True
        return False

    async def _get_room_changes_for_incremental_sync(
        self,
        sync_result_builder: "SyncResultBuilder",
        ignored_users: FrozenSet[str],
    ) -> _RoomChanges:
        """Determine the changes in rooms to report to the user.

        This function is a first pass at generating the rooms part of the sync response.
        It determines which rooms have changed during the sync period, and categorises
        them into four buckets: "knock", "invite", "join" and "leave". It also excludes
        from that list any room that appears in the list of rooms to exclude from sync
        results in the server configuration.

        1. Finds all membership changes for the user in the sync period (from
           `since_token` up to `now_token`).
        2. Uses those to place the room in one of the four categories above.
        3. Builds a `_RoomChanges` struct to record this, and return that struct.

        For rooms classified as "knock", "invite" or "leave", we just need to report
        a single membership event in the eventual /sync response. For "join" we need
        to fetch additional non-membership events, e.g. messages in the room. That is
        more complicated, so instead we report an intermediary `RoomSyncResultBuilder`
        struct, and leave the additional work to `_generate_room_entry`.

        The sync_result_builder is not modified by this function.
        """
        user_id = sync_result_builder.sync_config.user.to_string()
        since_token = sync_result_builder.since_token
        now_token = sync_result_builder.now_token
        sync_config = sync_result_builder.sync_config
        membership_change_events = sync_result_builder.membership_change_events

        assert since_token

        mem_change_events_by_room_id: Dict[str, List[EventBase]] = {}
        for event in membership_change_events:
            mem_change_events_by_room_id.setdefault(event.room_id, []).append(event)

        newly_joined_rooms: List[str] = list(
            sync_result_builder.forced_newly_joined_room_ids
        )
        newly_left_rooms: List[str] = []
        room_entries: List[RoomSyncResultBuilder] = []
        invited: List[InvitedSyncResult] = []
        knocked: List[KnockedSyncResult] = []
        for room_id, events in mem_change_events_by_room_id.items():
            # The body of this loop will add this room to at least one of the five lists
            # above. Things get messy if you've e.g. joined, left, joined then left the
            # room all in the same sync period.
            logger.debug(
                "Membership changes in %s: [%s]",
                room_id,
                ", ".join("%s (%s)" % (e.event_id, e.membership) for e in events),
            )

            non_joins = [e for e in events if e.membership != Membership.JOIN]
            has_join = len(non_joins) != len(events)

            # We want to figure out if we joined the room at some point since
            # the last sync (even if we have since left). This is to make sure
            # we do send down the room, and with full state, where necessary

            old_state_ids = None
            if room_id in sync_result_builder.joined_room_ids and non_joins:
                # Always include if the user (re)joined the room, especially
                # important so that device list changes are calculated correctly.
                # If there are non-join member events, but we are still in the room,
                # then the user must have left and joined
                newly_joined_rooms.append(room_id)

                # User is in the room so we don't need to do the invite/leave checks
                continue

            if room_id in sync_result_builder.joined_room_ids or has_join:
                old_state_ids = await self._state_storage_controller.get_state_ids_at(
                    room_id,
                    since_token,
                    state_filter=StateFilter.from_types([(EventTypes.Member, user_id)]),
                )
                old_mem_ev_id = old_state_ids.get((EventTypes.Member, user_id), None)
                old_mem_ev = None
                if old_mem_ev_id:
                    old_mem_ev = await self.store.get_event(
                        old_mem_ev_id, allow_none=True
                    )

                if not old_mem_ev or old_mem_ev.membership != Membership.JOIN:
                    newly_joined_rooms.append(room_id)

            # If user is in the room then we don't need to do the invite/leave checks
            if room_id in sync_result_builder.joined_room_ids:
                continue

            if not non_joins:
                continue
            last_non_join = non_joins[-1]

            # Check if we have left the room. This can either be because we were
            # joined before *or* that we since joined and then left.
            if events[-1].membership != Membership.JOIN:
                if has_join:
                    newly_left_rooms.append(room_id)
                else:
                    if not old_state_ids:
                        old_state_ids = (
                            await self._state_storage_controller.get_state_ids_at(
                                room_id,
                                since_token,
                                state_filter=StateFilter.from_types(
                                    [(EventTypes.Member, user_id)]
                                ),
                            )
                        )
                        old_mem_ev_id = old_state_ids.get(
                            (EventTypes.Member, user_id), None
                        )
                        old_mem_ev = None
                        if old_mem_ev_id:
                            old_mem_ev = await self.store.get_event(
                                old_mem_ev_id, allow_none=True
                            )
                    if old_mem_ev and old_mem_ev.membership == Membership.JOIN:
                        newly_left_rooms.append(room_id)

            # Only bother if we're still currently invited
            should_invite = last_non_join.membership == Membership.INVITE
            if should_invite:
                if last_non_join.sender not in ignored_users:
                    invite_room_sync = InvitedSyncResult(room_id, invite=last_non_join)
                    if invite_room_sync:
                        invited.append(invite_room_sync)

            # Only bother if our latest membership in the room is knock (and we haven't
            # been accepted/rejected in the meantime).
            should_knock = last_non_join.membership == Membership.KNOCK
            if should_knock:
                knock_room_sync = KnockedSyncResult(room_id, knock=last_non_join)
                if knock_room_sync:
                    knocked.append(knock_room_sync)

            # Always include leave/ban events. Just take the last one.
            # TODO: How do we handle ban -> leave in same batch?
            leave_events = [
                e
                for e in non_joins
                if e.membership in (Membership.LEAVE, Membership.BAN)
            ]

            if leave_events:
                leave_event = leave_events[-1]
                leave_position = await self.store.get_position_for_event(
                    leave_event.event_id
                )

                # If the leave event happened before the since token then we
                # bail.
                if since_token and not leave_position.persisted_after(
                    since_token.room_key
                ):
                    continue

                # We can safely convert the position of the leave event into a
                # stream token as it'll only be used in the context of this
                # room. (c.f. the docstring of `to_room_stream_token`).
                leave_token = since_token.copy_and_replace(
                    StreamKeyType.ROOM, leave_position.to_room_stream_token()
                )

                # If this is an out of band message, like a remote invite
                # rejection, we include it in the recents batch. Otherwise, we
                # let _load_filtered_recents handle fetching the correct
                # batches.
                #
                # This is all screaming out for a refactor, as the logic here is
                # subtle and the moving parts numerous.
                if leave_event.internal_metadata.is_out_of_band_membership():
                    batch_events: Optional[List[EventBase]] = [leave_event]
                else:
                    batch_events = None

                room_entries.append(
                    RoomSyncResultBuilder(
                        room_id=room_id,
                        rtype="archived",
                        events=batch_events,
                        newly_joined=room_id in newly_joined_rooms,
                        full_state=False,
                        since_token=since_token,
                        upto_token=leave_token,
                        end_token=leave_token,
                        out_of_band=leave_event.internal_metadata.is_out_of_band_membership(),
                    )
                )

        timeline_limit = sync_config.filter_collection.timeline_limit()

        # Get all events since the `from_key` in rooms we're currently joined to.
        # If there are too many, we get the most recent events only. This leaves
        # a "gap" in the timeline, as described by the spec for /sync.
        room_to_events = await self.store.get_room_events_stream_for_rooms(
            room_ids=sync_result_builder.joined_room_ids,
            from_key=now_token.room_key,
            to_key=since_token.room_key,
            limit=timeline_limit + 1,
            direction=Direction.BACKWARDS,
        )

        # We loop through all room ids, even if there are no new events, in case
        # there are non room events that we need to notify about.
        for room_id in sync_result_builder.joined_room_ids:
            room_entry = room_to_events.get(room_id, None)

            newly_joined = room_id in newly_joined_rooms
            if room_entry:
                events, start_key, _ = room_entry
                # We want to return the events in ascending order (the last event is the
                # most recent).
                events.reverse()

                prev_batch_token = now_token.copy_and_replace(
                    StreamKeyType.ROOM, start_key
                )

                entry = RoomSyncResultBuilder(
                    room_id=room_id,
                    rtype="joined",
                    events=events,
                    newly_joined=newly_joined,
                    full_state=False,
                    since_token=None if newly_joined else since_token,
                    upto_token=prev_batch_token,
                    end_token=now_token,
                )
            else:
                entry = RoomSyncResultBuilder(
                    room_id=room_id,
                    rtype="joined",
                    events=[],
                    newly_joined=newly_joined,
                    full_state=False,
                    since_token=since_token,
                    upto_token=since_token,
                    end_token=now_token,
                )

            room_entries.append(entry)

        return _RoomChanges(
            room_entries,
            invited,
            knocked,
            newly_joined_rooms,
            newly_left_rooms,
        )

    async def _get_room_changes_for_initial_sync(
        self,
        sync_result_builder: "SyncResultBuilder",
        ignored_users: FrozenSet[str],
    ) -> _RoomChanges:
        """Returns entries for all rooms for the user.

        Like `_get_rooms_changed`, but assumes the `since_token` is `None`.

        This function does not modify the sync_result_builder.

        Args:
            sync_result_builder
            ignored_users: Set of users ignored by user.
            ignored_rooms: List of rooms to ignore.
        """

        user_id = sync_result_builder.sync_config.user.to_string()
        since_token = sync_result_builder.since_token
        now_token = sync_result_builder.now_token
        sync_config = sync_result_builder.sync_config

        room_list = await self.store.get_rooms_for_local_user_where_membership_is(
            user_id=user_id,
            membership_list=Membership.LIST,
            excluded_rooms=sync_result_builder.excluded_room_ids,
        )

        room_entries = []
        invited = []
        knocked = []

        for event in room_list:
            if event.room_version_id not in KNOWN_ROOM_VERSIONS:
                continue

            if event.membership == Membership.JOIN:
                room_entries.append(
                    RoomSyncResultBuilder(
                        room_id=event.room_id,
                        rtype="joined",
                        events=None,
                        newly_joined=False,
                        full_state=True,
                        since_token=since_token,
                        upto_token=now_token,
                        end_token=now_token,
                    )
                )
            elif event.membership == Membership.INVITE:
                if event.sender in ignored_users:
                    continue
                invite = await self.store.get_event(event.event_id)
                invited.append(InvitedSyncResult(room_id=event.room_id, invite=invite))
            elif event.membership == Membership.KNOCK:
                knock = await self.store.get_event(event.event_id)
                knocked.append(KnockedSyncResult(room_id=event.room_id, knock=knock))
            elif event.membership in (Membership.LEAVE, Membership.BAN):
                # Always send down rooms we were banned from or kicked from.
                if not sync_config.filter_collection.include_leave:
                    if event.membership == Membership.LEAVE:
                        if user_id == event.sender:
                            continue

                leave_token = now_token.copy_and_replace(
                    StreamKeyType.ROOM, RoomStreamToken(stream=event.event_pos.stream)
                )
                room_entries.append(
                    RoomSyncResultBuilder(
                        room_id=event.room_id,
                        rtype="archived",
                        events=None,
                        newly_joined=False,
                        full_state=True,
                        since_token=since_token,
                        upto_token=leave_token,
                        end_token=leave_token,
                    )
                )

        return _RoomChanges(room_entries, invited, knocked, [], [])

    async def _generate_room_entry(
        self,
        sync_result_builder: "SyncResultBuilder",
        room_builder: "RoomSyncResultBuilder",
        ephemeral: List[JsonDict],
        tags: Optional[Mapping[str, JsonMapping]],
        account_data: Mapping[str, JsonMapping],
        always_include: bool = False,
    ) -> None:
        """Populates the `joined` and `archived` section of `sync_result_builder`
        based on the `room_builder`.

        Ideally, we want to report all events whose stream ordering `s` lies in the
        range `since_token < s <= now_token`, where the two tokens are read from the
        sync_result_builder.

        If there are too many events in that range to report, things get complicated.
        In this situation we return a truncated list of the most recent events, and
        indicate in the response that there is a "gap" of omitted events. Lots of this
        is handled in `_load_filtered_recents`, but some of is handled in this method.

        Additionally:
        - we include a "state_delta", to describe the changes in state over the gap,
        - we include all membership events applying to the user making the request,
          even those in the gap.

        See the spec for the rationale:
            https://spec.matrix.org/v1.1/client-server-api/#syncing

        Args:
            sync_result_builder
            room_builder
            ephemeral: List of new ephemeral events for room
            tags: List of *all* tags for room, or None if there has been
                no change.
            account_data: List of new account data for room
            always_include: Always include this room in the sync response,
                even if empty.
        """
        newly_joined = room_builder.newly_joined
        full_state = (
            room_builder.full_state or newly_joined or sync_result_builder.full_state
        )
        events = room_builder.events

        # We want to shortcut out as early as possible.
        if not (always_include or account_data or ephemeral or full_state):
            if events == [] and tags is None:
                return

        now_token = sync_result_builder.now_token
        sync_config = sync_result_builder.sync_config

        room_id = room_builder.room_id
        since_token = room_builder.since_token
        upto_token = room_builder.upto_token

        with start_active_span("sync.generate_room_entry"):
            set_tag("room_id", room_id)
            log_kv({"events": len(events or ())})

            log_kv(
                {
                    "since_token": since_token,
                    "upto_token": upto_token,
                    "end_token": room_builder.end_token,
                }
            )

            batch = await self._load_filtered_recents(
                room_id,
                sync_result_builder,
                sync_config,
                upto_token=upto_token,
                since_token=since_token,
                potential_recents=events,
                newly_joined_room=newly_joined,
            )
            log_kv(
                {
                    "batch_events": len(batch.events),
                    "prev_batch": batch.prev_batch,
                    "batch_limited": batch.limited,
                }
            )

            # Note: `batch` can be both empty and limited here in the case where
            # `_load_filtered_recents` can't find any events the user should see
            # (e.g. due to having ignored the sender of the last 50 events).

            # When we join the room (or the client requests full_state), we should
            # send down any existing tags. Usually the user won't have tags in a
            # newly joined room, unless either a) they've joined before or b) the
            # tag was added by synapse e.g. for server notice rooms.
            if full_state:
                user_id = sync_result_builder.sync_config.user.to_string()
                tags = await self.store.get_tags_for_room(user_id, room_id)

                # If there aren't any tags, don't send the empty tags list down
                # sync
                if not tags:
                    tags = None

            account_data_events = []
            if tags is not None:
                account_data_events.append(
                    {"type": AccountDataTypes.TAG, "content": {"tags": tags}}
                )

            for account_data_type, content in account_data.items():
                account_data_events.append(
                    {"type": account_data_type, "content": content}
                )

            account_data_events = (
                await sync_config.filter_collection.filter_room_account_data(
                    account_data_events
                )
            )

            ephemeral = await sync_config.filter_collection.filter_room_ephemeral(
                ephemeral
            )

            if not (
                always_include
                or batch
                or account_data_events
                or ephemeral
                or full_state
            ):
                return

            if not room_builder.out_of_band:
                state = await self.compute_state_delta(
                    room_id,
                    batch,
                    sync_config,
                    since_token,
                    room_builder.end_token,
                    full_state=full_state,
                    joined=room_builder.rtype == "joined",
                )
            else:
                # An out of band room won't have any state changes.
                state = {}

            summary: Optional[JsonDict] = {}

            # we include a summary in room responses when we're lazy loading
            # members (as the client otherwise doesn't have enough info to form
            # the name itself).
            if (
                not room_builder.out_of_band
                and sync_config.filter_collection.lazy_load_members()
                and (
                    # we recalculate the summary:
                    #   if there are membership changes in the timeline, or
                    #   if membership has changed during a gappy sync, or
                    #   if this is an initial sync.
                    any(ev.type == EventTypes.Member for ev in batch.events)
                    or (
                        # XXX: this may include false positives in the form of LL
                        # members which have snuck into state
                        batch.limited
                        and any(t == EventTypes.Member for (t, k) in state)
                    )
                    or since_token is None
                )
            ):
                summary = await self.compute_summary(
                    room_id, sync_config, batch, state, now_token
                )

            if room_builder.rtype == "joined":
                unread_notifications: Dict[str, int] = {}
                room_sync = JoinedSyncResult(
                    room_id=room_id,
                    timeline=batch,
                    state=state,
                    ephemeral=ephemeral,
                    account_data=account_data_events,
                    unread_notifications=unread_notifications,
                    unread_thread_notifications={},
                    summary=summary,
                    unread_count=0,
                )

                if room_sync or always_include:
                    notifs = await self.unread_notifs_for_room_id(room_id, sync_config)

                    # Notifications for the main timeline.
                    notify_count = notifs.main_timeline.notify_count
                    highlight_count = notifs.main_timeline.highlight_count
                    unread_count = notifs.main_timeline.unread_count

                    # Check the sync configuration.
                    if sync_config.filter_collection.unread_thread_notifications():
                        # And add info for each thread.
                        room_sync.unread_thread_notifications = {
                            thread_id: {
                                "notification_count": thread_notifs.notify_count,
                                "highlight_count": thread_notifs.highlight_count,
                            }
                            for thread_id, thread_notifs in notifs.threads.items()
                            if thread_id is not None
                        }

                    else:
                        # Combine the unread counts for all threads and main timeline.
                        for thread_notifs in notifs.threads.values():
                            notify_count += thread_notifs.notify_count
                            highlight_count += thread_notifs.highlight_count
                            unread_count += thread_notifs.unread_count

                    unread_notifications["notification_count"] = notify_count
                    unread_notifications["highlight_count"] = highlight_count
                    room_sync.unread_count = unread_count

                    sync_result_builder.joined.append(room_sync)

                if batch.limited and since_token:
                    user_id = sync_result_builder.sync_config.user.to_string()
                    logger.debug(
                        "Incremental gappy sync of %s for user %s with %d state events"
                        % (room_id, user_id, len(state))
                    )
            elif room_builder.rtype == "archived":
                archived_room_sync = ArchivedSyncResult(
                    room_id=room_id,
                    timeline=batch,
                    state=state,
                    account_data=account_data_events,
                )
                if archived_room_sync or always_include:
                    sync_result_builder.archived.append(archived_room_sync)
            else:
                raise Exception("Unrecognized rtype: %r", room_builder.rtype)


def _action_has_highlight(actions: List[JsonDict]) -> bool:
    for action in actions:
        try:
            if action.get("set_tweak", None) == "highlight":
                return action.get("value", True)
        except AttributeError:
            pass

    return False


def _calculate_state(
    timeline_contains: StateMap[str],
    timeline_start: StateMap[str],
    timeline_end: StateMap[str],
    previous_timeline_end: StateMap[str],
    lazy_load_members: bool,
) -> StateMap[str]:
    """Works out what state to include in a sync response.

    Args:
        timeline_contains: state in the timeline
        timeline_start: state at the start of the timeline
        timeline_end: state at the end of the timeline
        previous_timeline_end: state at the end of the previous sync (or empty dict
            if this is an initial sync)
        lazy_load_members: whether to return members from timeline_start
            or not.  assumes that timeline_start has already been filtered to
            include only the members the client needs to know about.
    """
    event_id_to_state_key = {
        event_id: state_key
        for state_key, event_id in itertools.chain(
            timeline_contains.items(),
            timeline_start.items(),
            timeline_end.items(),
            previous_timeline_end.items(),
        )
    }

    timeline_end_ids = set(timeline_end.values())
    timeline_start_ids = set(timeline_start.values())
    previous_timeline_end_ids = set(previous_timeline_end.values())
    timeline_contains_ids = set(timeline_contains.values())

    # If we are lazyloading room members, we explicitly add the membership events
    # for the senders in the timeline into the state block returned by /sync,
    # as we may not have sent them to the client before.  We find these membership
    # events by filtering them out of timeline_start, which has already been filtered
    # to only include membership events for the senders in the timeline.
    # In practice, we can do this by removing them from the previous_timeline_end_ids
    # list, which is the list of relevant state we know we have already sent to the
    # client.
    # see https://github.com/matrix-org/synapse/pull/2970/files/efcdacad7d1b7f52f879179701c7e0d9b763511f#r204732809

    if lazy_load_members:
        previous_timeline_end_ids.difference_update(
            e for t, e in timeline_start.items() if t[0] == EventTypes.Member
        )

    # Naively, we would just return the difference between the state at the start
    # of the timeline (`timeline_start_ids`) and that at the end of the previous sync
    # (`previous_timeline_end_ids`). However, that fails in the presence of forks in
    # the DAG.
    #
    # For example, consider a DAG such as the following:
    #
    #       E1
    #     ↗    ↖
    #    |      S2
    #    |      ↑
    #  --|------|----
    #    |      |
    #    E3     |
    #     ↖    /
    #       E4
    #
    # ... and a filter that means we only return 2 events, represented by the dashed
    # horizontal line. Assuming S2 was *not* included in the previous sync, we need to
    # include it in the `state` section.
    #
    # Note that the state at the start of the timeline (E3) does not include S2. So,
    # to make sure it gets included in the calculation here, we actually look at
    # the state at the *end* of the timeline, and subtract any events that are present
    # in the timeline.
    #
    # ----------
    #
    # Aside 1: You may then wonder if we need to include `timeline_start` in the
    # calculation. Consider a linear DAG:
    #
    #      E1
    #      ↑
    #      S2
    #      ↑
    #  ----|------
    #      |
    #      E3
    #      ↑
    #      S4
    #      ↑
    #      E5
    #
    # ... where S2 and S4 change the same piece of state; and where we have a filter
    # that returns 3 events (E3, S4, E5). We still need to tell the client about S2,
    # because it might affect the display of E3. However, the state at the end of the
    # timeline only tells us about S4; if we don't inspect `timeline_start` we won't
    # find out about S2.
    #
    # (There are yet more complicated cases in which a state event is excluded from the
    # timeline, but whose effect actually lands in the DAG in the *middle* of the
    # timeline. We have no way to represent that in the /sync response, and we don't
    # even try; it is ether omitted or plonked into `state` as if it were at the start
    # of the timeline, depending on what else is in the timeline.)

    state_ids = (
        (timeline_end_ids | timeline_start_ids)
        - previous_timeline_end_ids
        - timeline_contains_ids
    )

    return {event_id_to_state_key[e]: e for e in state_ids}


@attr.s(slots=True, auto_attribs=True)
class SyncResultBuilder:
    """Used to help build up a new SyncResult for a user

    Attributes:
        sync_config
        full_state: The full_state flag as specified by user
        since_token: The token supplied by user, or None.
        now_token: The token to sync up to.
        joined_room_ids: List of rooms the user is joined to
        excluded_room_ids: Set of room ids we should omit from the /sync response.
        forced_newly_joined_room_ids:
            Rooms that should be presented in the /sync response as if they were
            newly joined during the sync period, even if that's not the case.
            (This is useful if the room was previously excluded from a /sync response,
            and now the client should be made aware of it.)
            Only used by incremental syncs.

        # The following mirror the fields in a sync response
        presence
        account_data
        joined
        invited
        knocked
        archived
        to_device
    """

    sync_config: SyncConfig
    full_state: bool
    since_token: Optional[StreamToken]
    now_token: StreamToken
    joined_room_ids: FrozenSet[str]
    excluded_room_ids: FrozenSet[str]
    forced_newly_joined_room_ids: FrozenSet[str]
    membership_change_events: List[EventBase]

    presence: List[UserPresenceState] = attr.Factory(list)
    account_data: List[JsonDict] = attr.Factory(list)
    joined: List[JoinedSyncResult] = attr.Factory(list)
    invited: List[InvitedSyncResult] = attr.Factory(list)
    knocked: List[KnockedSyncResult] = attr.Factory(list)
    archived: List[ArchivedSyncResult] = attr.Factory(list)
    to_device: List[JsonDict] = attr.Factory(list)

    def calculate_user_changes(self) -> Tuple[AbstractSet[str], AbstractSet[str]]:
        """Work out which other users have joined or left rooms we are joined to.

        This data only is only useful for an incremental sync.

        The SyncResultBuilder is not modified by this function.
        """
        newly_joined_or_invited_or_knocked_users = set()
        newly_left_users = set()
        if self.since_token:
            for joined_sync in self.joined:
                it = itertools.chain(
                    joined_sync.state.values(), joined_sync.timeline.events
                )
                for event in it:
                    if event.type == EventTypes.Member:
                        if (
                            event.membership == Membership.JOIN
                            or event.membership == Membership.INVITE
                            or event.membership == Membership.KNOCK
                        ):
                            newly_joined_or_invited_or_knocked_users.add(
                                event.state_key
                            )
                            # If the user left and rejoined in the same batch, they
                            # count as a newly-joined user, *not* a newly-left user.
                            newly_left_users.discard(event.state_key)
                        else:
                            prev_content = event.unsigned.get("prev_content", {})
                            prev_membership = prev_content.get("membership", None)
                            if prev_membership == Membership.JOIN:
                                newly_left_users.add(event.state_key)
                            # If the user joined and left in the same batch, they
                            # count as a newly-left user, not a newly-joined user.
                            newly_joined_or_invited_or_knocked_users.discard(
                                event.state_key
                            )

        return newly_joined_or_invited_or_knocked_users, newly_left_users


@attr.s(slots=True, auto_attribs=True)
class RoomSyncResultBuilder:
    """Stores information needed to create either a `JoinedSyncResult` or
    `ArchivedSyncResult`.

    Attributes:
        room_id

        rtype: One of `"joined"` or `"archived"`

        events: List of events to include in the room (more events may be added
            when generating result).

        newly_joined: If the user has newly joined the room

        full_state: Whether the full state should be sent in result

        since_token: Earliest point to return events from, or None

        upto_token: Latest point to return events from. If `events` is populated,
           this is set to the token at the start of `events`

        end_token: The last point in the timeline that the client should see events
           from. Normally this will be the same as the global `now_token`, but in
           the case of rooms where the user has left the room, this will be the point
           just after their leave event.

           This is used in the calculation of the state which is returned in `state`:
           any state changes *up to* `end_token` (and not beyond!) which are not
           reflected in the timeline need to be returned in `state`.

        out_of_band: whether the events in the room are "out of band" events
            and the server isn't in the room.
    """

    room_id: str
    rtype: str
    events: Optional[List[EventBase]]
    newly_joined: bool
    full_state: bool
    since_token: Optional[StreamToken]
    upto_token: StreamToken
    end_token: StreamToken
    out_of_band: bool = False
