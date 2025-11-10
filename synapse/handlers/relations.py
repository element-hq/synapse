#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
import enum
import logging
from collections import defaultdict
from typing import (
    TYPE_CHECKING,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)

import attr

from synapse.api.constants import Direction, EventTypes, Membership, RelationTypes
from synapse.api.errors import SynapseError
from synapse.events import EventBase, relation_from_event
from synapse.events.utils import SerializeEventConfig
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.logging.opentracing import trace
from synapse.storage.databases.main.relations import (
    ThreadsNextBatch,
    ThreadUpdateInfo,
    _RelatedEvent,
)
from synapse.streams.config import PaginationConfig
from synapse.types import (
    JsonDict,
    Requester,
    RoomStreamToken,
    StreamKeyType,
    StreamToken,
    UserID,
)
from synapse.util.async_helpers import gather_results
from synapse.visibility import filter_events_for_client

if TYPE_CHECKING:
    from synapse.events.utils import EventClientSerializer
    from synapse.handlers.sliding_sync.room_lists import RoomsForUserType
    from synapse.server import HomeServer
    from synapse.storage.databases.main import DataStore


logger = logging.getLogger(__name__)

# Type aliases for thread update processing
ThreadUpdatesMap = dict[str, list[ThreadUpdateInfo]]
ThreadRootsMap = dict[str, EventBase]
AggregationsMap = dict[str, "BundledAggregations"]


@attr.s(slots=True, frozen=True, auto_attribs=True)
class ThreadUpdate:
    """
    Data for a single thread update.

    Attributes:
        thread_root: The thread root event, or None if not requested/not visible
        prev_batch: Per-thread pagination token for fetching older events in this thread
        bundled_aggregations: Bundled aggregations for the thread root event
    """

    thread_root: EventBase | None
    prev_batch: StreamToken | None
    bundled_aggregations: "BundledAggregations | None" = None


class ThreadsListInclude(str, enum.Enum):
    """Valid values for the 'include' flag of /threads."""

    all = "all"
    participated = "participated"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _ThreadAggregation:
    # The latest event in the thread.
    latest_event: EventBase
    # The total number of events in the thread.
    count: int
    # True if the current user has sent an event to the thread.
    current_user_participated: bool


@attr.s(slots=True, auto_attribs=True)
class BundledAggregations:
    """
    The bundled aggregations for an event.

    Some values require additional processing during serialization.
    """

    references: JsonDict | None = None
    replace: EventBase | None = None
    thread: _ThreadAggregation | None = None

    def __bool__(self) -> bool:
        return bool(self.references or self.replace or self.thread)


class RelationsHandler:
    def __init__(self, hs: "HomeServer"):
        self._main_store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self._auth = hs.get_auth()
        self._clock = hs.get_clock()
        self._event_handler = hs.get_event_handler()
        self._event_serializer = hs.get_event_client_serializer()
        self._event_creation_handler = hs.get_event_creation_handler()

    async def get_relations(
        self,
        requester: Requester,
        event_id: str,
        room_id: str,
        pagin_config: PaginationConfig,
        recurse: bool,
        include_original_event: bool,
        relation_type: str | None = None,
        event_type: str | None = None,
    ) -> JsonDict:
        """Get related events of a event, ordered by topological ordering.

        Args:
            requester: The user requesting the relations.
            event_id: Fetch events that relate to this event ID.
            room_id: The room the event belongs to.
            pagin_config: The pagination config rules to apply, if any.
            recurse: Whether to recursively find relations.
            include_original_event: Whether to include the parent event.
            relation_type: Only fetch events with this relation type, if given.
            event_type: Only fetch events with this event type, if given.

        Returns:
            The pagination chunk.
        """

        user_id = requester.user.to_string()

        # TODO Properly handle a user leaving a room.
        (_, member_event_id) = await self._auth.check_user_in_room_or_world_readable(
            room_id, requester, allow_departed_users=True
        )

        # This gets the original event and checks that a) the event exists and
        # b) the user is allowed to view it.
        event = await self._event_handler.get_event(requester.user, room_id, event_id)
        if event is None:
            raise SynapseError(404, "Unknown parent event.")

        # Note that ignored users are not passed into get_relations_for_event
        # below. Ignored users are handled in filter_events_for_client (and by
        # not passing them in here we should get a better cache hit rate).
        related_events, next_token = await self._main_store.get_relations_for_event(
            event_id=event_id,
            event=event,
            room_id=room_id,
            relation_type=relation_type,
            event_type=event_type,
            limit=pagin_config.limit,
            direction=pagin_config.direction,
            from_token=pagin_config.from_token,
            to_token=pagin_config.to_token,
            recurse=recurse,
        )

        events = await self._main_store.get_events_as_list(
            [e.event_id for e in related_events]
        )

        events = await filter_events_for_client(
            self._storage_controllers,
            user_id,
            events,
            is_peeking=(member_event_id is None),
        )

        # The relations returned for the requested event do include their
        # bundled aggregations.
        aggregations = await self.get_bundled_aggregations(
            events, requester.user.to_string()
        )

        now = self._clock.time_msec()
        serialize_options = SerializeEventConfig(requester=requester)
        return_value: JsonDict = {
            "chunk": await self._event_serializer.serialize_events(
                events,
                now,
                bundle_aggregations=aggregations,
                config=serialize_options,
            ),
        }

        if recurse:
            return_value["recursion_depth"] = 3

        if include_original_event:
            # Do not bundle aggregations when retrieving the original event because
            # we want the content before relations are applied to it.
            return_value[
                "original_event"
            ] = await self._event_serializer.serialize_event(
                event,
                now,
                bundle_aggregations=None,
                config=serialize_options,
            )

        if next_token:
            return_value["next_batch"] = await next_token.to_string(self._main_store)

        if pagin_config.from_token:
            return_value["prev_batch"] = await pagin_config.from_token.to_string(
                self._main_store
            )

        return return_value

    async def redact_events_related_to(
        self,
        requester: Requester,
        event_id: str,
        initial_redaction_event: EventBase,
        relation_types: list[str],
    ) -> None:
        """Redacts all events related to the given event ID with one of the given
        relation types.

        This method is expected to be called when redacting the event referred to by
        the given event ID.

        If an event cannot be redacted (e.g. because of insufficient permissions), log
        the error and try to redact the next one.

        Args:
            requester: The requester to redact events on behalf of.
            event_id: The event IDs to look and redact relations of.
            initial_redaction_event: The redaction for the event referred to by
                event_id.
            relation_types: The types of relations to look for. If "*" is in the list,
                all related events will be redacted regardless of the type.

        Raises:
            ShadowBanError if the requester is shadow-banned
        """
        if "*" in relation_types:
            related_event_ids = await self._main_store.get_all_relations_for_event(
                event_id
            )
        else:
            related_event_ids = (
                await self._main_store.get_all_relations_for_event_with_types(
                    event_id, relation_types
                )
            )

        for related_event_id in related_event_ids:
            try:
                await self._event_creation_handler.create_and_send_nonmember_event(
                    requester,
                    {
                        "type": EventTypes.Redaction,
                        "content": initial_redaction_event.content,
                        "room_id": initial_redaction_event.room_id,
                        "sender": requester.user.to_string(),
                        "redacts": related_event_id,
                    },
                    ratelimit=False,
                )
            except SynapseError as e:
                logger.warning(
                    "Failed to redact event %s (related to event %s): %s",
                    related_event_id,
                    event_id,
                    e.msg,
                )

    async def get_references_for_events(
        self, event_ids: Collection[str], ignored_users: frozenset[str] = frozenset()
    ) -> Mapping[str, Sequence[_RelatedEvent]]:
        """Get a list of references to the given events.

        Args:
            event_ids: Fetch events that relate to this event ID.
            ignored_users: The users ignored by the requesting user.

        Returns:
            A map of event IDs to a list related events.
        """

        related_events = await self._main_store.get_references_for_events(event_ids)

        # Avoid additional logic if there are no ignored users.
        if not ignored_users:
            return {
                event_id: results
                for event_id, results in related_events.items()
                if results
            }

        # Filter out ignored users.
        results = {}
        for event_id, events in related_events.items():
            # If no references, skip.
            if not events:
                continue

            # Filter ignored users out.
            events = [event for event in events if event.sender not in ignored_users]
            # If there are no events left, skip this event.
            if not events:
                continue

            results[event_id] = events

        return results

    async def _get_threads_for_events(
        self,
        events_by_id: dict[str, EventBase],
        relations_by_id: dict[str, str],
        user_id: str,
        ignored_users: frozenset[str],
    ) -> dict[str, _ThreadAggregation]:
        """Get the bundled aggregations for threads for the requested events.

        Args:
            events_by_id: A map of event_id to events to get aggregations for threads.
            relations_by_id: A map of event_id to the relation type, if one exists
                for that event.
            user_id: The user requesting the bundled aggregations.
            ignored_users: The users ignored by the requesting user.

        Returns:
            A dictionary mapping event ID to the thread information.

            May not contain a value for all requested event IDs.
        """
        user = UserID.from_string(user_id)

        # It is not valid to start a thread on an event which itself relates to another event.
        event_ids = [eid for eid in events_by_id.keys() if eid not in relations_by_id]

        # Fetch thread summaries.
        summaries = await self._main_store.get_thread_summaries(event_ids)

        # Limit fetching whether the requester has participated in a thread to
        # events which are thread roots.
        thread_event_ids = [
            event_id for event_id, summary in summaries.items() if summary
        ]

        # Pre-seed thread participation with whether the requester sent the event.
        participated = {
            event_id: events_by_id[event_id].sender == user_id
            for event_id in thread_event_ids
        }
        # For events the requester did not send, check the database for whether
        # the requester sent a threaded reply.
        participated.update(
            await self._main_store.get_threads_participated(
                [
                    event_id
                    for event_id in thread_event_ids
                    if not participated[event_id]
                ],
                user_id,
            )
        )

        # Then subtract off the results for any ignored users.
        ignored_results = await self._main_store.get_threaded_messages_per_user(
            thread_event_ids, ignored_users
        )

        # A map of event ID to the thread aggregation.
        results = {}

        for event_id, summary in summaries.items():
            # If no thread, skip.
            if not summary:
                continue

            thread_count, latest_thread_event = summary

            # Subtract off the count of any ignored users.
            for ignored_user in ignored_users:
                thread_count -= ignored_results.get((event_id, ignored_user), 0)

            # This is gnarly, but if the latest event is from an ignored user,
            # attempt to find one that isn't from an ignored user.
            if latest_thread_event.sender in ignored_users:
                room_id = latest_thread_event.room_id

                # If the root event is not found, something went wrong, do
                # not include a summary of the thread.
                event = await self._event_handler.get_event(user, room_id, event_id)
                if event is None:
                    continue

                # Attempt to find another event to use as the latest event.
                potential_events, _ = await self._main_store.get_relations_for_event(
                    room_id,
                    event_id,
                    event,
                    RelationTypes.THREAD,
                    direction=Direction.FORWARDS,
                )

                # Filter out ignored users.
                potential_events = [
                    event
                    for event in potential_events
                    if event.sender not in ignored_users
                ]

                # If all found events are from ignored users, do not include
                # a summary of the thread.
                if not potential_events:
                    continue

                # The *last* event returned is the one that is cared about.
                event = await self._event_handler.get_event(
                    user, room_id, potential_events[-1].event_id
                )
                # It is unexpected that the event will not exist.
                if event is None:
                    logger.warning(
                        "Unable to fetch latest event in a thread with event ID: %s",
                        potential_events[-1].event_id,
                    )
                    continue
                latest_thread_event = event

            results[event_id] = _ThreadAggregation(
                latest_event=latest_thread_event,
                count=thread_count,
                # If there's a thread summary it must also exist in the
                # participated dictionary.
                current_user_participated=events_by_id[event_id].sender == user_id
                or participated[event_id],
            )

        return results

    @trace
    async def get_bundled_aggregations(
        self, events: Iterable[EventBase], user_id: str
    ) -> dict[str, BundledAggregations]:
        """Generate bundled aggregations for events.

        Args:
            events: The iterable of events to calculate bundled aggregations for.
            user_id: The user requesting the bundled aggregations.

        Returns:
            A map of event ID to the bundled aggregations for the event.

            Not all requested events may exist in the results (if they don't have
            bundled aggregations).

            The results may include additional events which are related to the
            requested events.
        """
        # De-duplicated events by ID to handle the same event requested multiple times.
        events_by_id = {}
        # A map of event ID to the relation in that event, if there is one.
        relations_by_id: dict[str, str] = {}
        for event in events:
            # State events do not get bundled aggregations.
            if event.is_state():
                continue

            relates_to = relation_from_event(event)
            if relates_to:
                # An event which is a replacement (ie edit) or annotation (ie,
                # reaction) may not have any other event related to it.
                if relates_to.rel_type in (
                    RelationTypes.ANNOTATION,
                    RelationTypes.REPLACE,
                ):
                    continue

                # Track the event's relation information for later.
                relations_by_id[event.event_id] = relates_to.rel_type

            # The event should get bundled aggregations.
            events_by_id[event.event_id] = event

        # event ID -> bundled aggregation in non-serialized form.
        results: dict[str, BundledAggregations] = {}

        # Fetch any ignored users of the requesting user.
        ignored_users = await self._main_store.ignored_users(user_id)

        # Threads are special as the latest event of a thread might cause additional
        # events to be fetched. Thus, we check those first!

        # Fetch thread summaries (but only for the directly requested events).
        threads = await self._get_threads_for_events(
            events_by_id,
            relations_by_id,
            user_id,
            ignored_users,
        )
        for event_id, thread in threads.items():
            results.setdefault(event_id, BundledAggregations()).thread = thread

            # If the latest event in a thread is not already being fetched,
            # add it. This ensures that the bundled aggregations for the
            # latest thread event is correct.
            latest_thread_event = thread.latest_event
            if latest_thread_event and latest_thread_event.event_id not in events_by_id:
                events_by_id[latest_thread_event.event_id] = latest_thread_event
                # Keep relations_by_id in sync with events_by_id:
                #
                # We know that the latest event in a thread has a thread relation
                # (as that is what makes it part of the thread).
                relations_by_id[latest_thread_event.event_id] = RelationTypes.THREAD

        async def _fetch_references() -> None:
            """Fetch any references to bundle with this event."""
            references_by_event_id = await self.get_references_for_events(
                events_by_id.keys(), ignored_users=ignored_users
            )
            for event_id, references in references_by_event_id.items():
                if references:
                    results.setdefault(event_id, BundledAggregations()).references = {
                        "chunk": [{"event_id": ev.event_id} for ev in references]
                    }

        async def _fetch_edits() -> None:
            """
            Fetch any edits (but not for redacted events).

            Note that there is no use in limiting edits by ignored users since the
            parent event should be ignored in the first place if the user is ignored.
            """
            edits = await self._main_store.get_applicable_edits(
                [
                    event_id
                    for event_id, event in events_by_id.items()
                    if not event.internal_metadata.is_redacted()
                ]
            )
            for event_id, edit in edits.items():
                results.setdefault(event_id, BundledAggregations()).replace = edit

        # Parallelize the calls for annotations, references, and edits since they
        # are unrelated.
        await make_deferred_yieldable(
            gather_results(
                (
                    run_in_background(_fetch_references),
                    run_in_background(_fetch_edits),
                )
            )
        )

        return results

    async def _filter_thread_updates_for_user(
        self,
        all_thread_updates: ThreadUpdatesMap,
        user_id: str,
    ) -> ThreadUpdatesMap:
        """Process thread updates by filtering for visibility.

        Takes raw thread updates from storage and filters them based on whether the
        user can see the events. Preserves the ordering of updates within each thread.

        Args:
            all_thread_updates: Map of thread_id to list of ThreadUpdateInfo objects
            user_id: The user ID to filter events for

        Returns:
            Filtered map of thread_id to list of ThreadUpdateInfo objects, containing
            only updates for events the user can see.
        """
        # Build a mapping of event_id -> (thread_id, update) for efficient lookup
        # during visibility filtering.
        event_to_thread_map: dict[str, tuple[str, ThreadUpdateInfo]] = {}
        for thread_id, updates in all_thread_updates.items():
            for update in updates:
                event_to_thread_map[update.event_id] = (thread_id, update)

        # Fetch and filter events for visibility
        all_events = await self._main_store.get_events_as_list(
            event_to_thread_map.keys()
        )
        filtered_events = await filter_events_for_client(
            self._storage_controllers, user_id, all_events
        )

        # Rebuild thread updates from filtered events
        filtered_updates: ThreadUpdatesMap = defaultdict(list)
        for event in filtered_events:
            if event.event_id in event_to_thread_map:
                thread_id, update = event_to_thread_map[event.event_id]
                filtered_updates[thread_id].append(update)

        return filtered_updates

    def _build_thread_updates_response(
        self,
        filtered_updates: ThreadUpdatesMap,
        thread_root_event_map: ThreadRootsMap,
        aggregations_map: AggregationsMap,
        global_prev_batch_token: StreamToken | None,
    ) -> dict[str, dict[str, ThreadUpdate]]:
        """Build thread update response structure with per-thread prev_batch tokens.

        Args:
            filtered_updates: Map of thread_root_id to list of ThreadUpdateInfo
            thread_root_event_map: Map of thread_root_id to EventBase
            aggregations_map: Map of thread_root_id to BundledAggregations
            global_prev_batch_token: Global pagination token, or None if no more results

        Returns:
            Map of room_id to thread_root_id to ThreadUpdate
        """
        thread_updates: dict[str, dict[str, ThreadUpdate]] = {}

        for thread_root_id, updates in filtered_updates.items():
            # We only care about the latest update for the thread
            # Updates are already sorted by stream_ordering DESC from the database query,
            # and filter_events_for_client preserves order, so updates[0] is guaranteed to be
            # the latest event for each thread.
            latest_update = updates[0]
            room_id = latest_update.room_id

            # Generate per-thread prev_batch token if this thread has multiple visible updates
            # or if we hit the global limit.
            # When we hit the global limit, we generate prev_batch tokens for all threads, even if
            # we only saw 1 update for them. This is to cover the case where we only saw
            # a single update for a given thread, but the global limit prevents us from
            # obtaining other updates which would have otherwise been included in the range.
            per_thread_prev_batch = None
            if len(updates) > 1 or global_prev_batch_token is not None:
                # Create a token pointing to one position before the latest event's stream position.
                # This makes it exclusive - /relations with dir=b won't return the latest event again.
                # Use StreamToken.START as base (all other streams at 0) since only room position matters.
                per_thread_prev_batch = StreamToken.START.copy_and_replace(
                    StreamKeyType.ROOM,
                    RoomStreamToken(stream=latest_update.stream_ordering - 1),
                )

            if room_id not in thread_updates:
                thread_updates[room_id] = {}

            thread_updates[room_id][thread_root_id] = ThreadUpdate(
                thread_root=thread_root_event_map.get(thread_root_id),
                prev_batch=per_thread_prev_batch,
                bundled_aggregations=aggregations_map.get(thread_root_id),
            )

        return thread_updates

    async def _fetch_thread_updates(
        self,
        room_ids: frozenset[str],
        room_membership_map: Mapping[str, "RoomsForUserType"],
        from_token: StreamToken | None,
        to_token: StreamToken,
        limit: int,
        exclude_thread_ids: set[str] | None = None,
    ) -> tuple[ThreadUpdatesMap, StreamToken | None]:
        """Fetch thread updates across multiple rooms, handling membership states properly.

        This method separates rooms based on membership status (LEAVE/BAN vs others)
        and queries them appropriately to prevent data leaks. For rooms where the user
        has left or been banned, we bound the query to their leave/ban event position.

        Args:
            room_ids: The set of room IDs to fetch thread updates for
            room_membership_map: Map of room_id to RoomsForUserType containing membership info
            from_token: Lower bound (exclusive) for the query, or None for no lower bound
            to_token: Upper bound for the query (for joined/invited/knocking rooms)
            limit: Maximum number of thread updates to return across all rooms
            exclude_thread_ids: Optional set of thread IDs to exclude from results

        Returns:
            A tuple of:
            - Map of thread_id to list of ThreadUpdateInfo objects
            - Global prev_batch token if there are more results, None otherwise
        """
        # Separate rooms based on membership to handle LEAVE/BAN rooms specially
        leave_ban_rooms: set[str] = set()
        other_rooms: set[str] = set()

        for room_id in room_ids:
            membership_info = room_membership_map.get(room_id)
            if membership_info and membership_info.membership in (
                Membership.LEAVE,
                Membership.BAN,
            ):
                leave_ban_rooms.add(room_id)
            else:
                other_rooms.add(room_id)

        # Fetch thread updates from storage, handling LEAVE/BAN rooms separately
        all_thread_updates: ThreadUpdatesMap = {}
        prev_batch_token: StreamToken | None = None
        remaining_limit = limit

        # Query LEAVE/BAN rooms with bounded to_token to prevent data leaks
        if leave_ban_rooms:
            for room_id in leave_ban_rooms:
                if remaining_limit <= 0:
                    # We've hit the limit, set prev_batch to indicate more results
                    prev_batch_token = to_token
                    break

                membership_info = room_membership_map[room_id]
                bounded_to_token = membership_info.event_pos.to_room_stream_token()

                (
                    room_thread_updates,
                    room_prev_batch,
                ) = await self._main_store.get_thread_updates_for_rooms(
                    room_ids={room_id},
                    from_token=from_token.room_key if from_token else None,
                    to_token=bounded_to_token,
                    limit=remaining_limit,
                    exclude_thread_ids=exclude_thread_ids,
                )

                # Count updates and reduce remaining limit
                num_updates = sum(
                    len(updates) for updates in room_thread_updates.values()
                )
                remaining_limit -= num_updates

                # Merge updates
                for thread_id, updates in room_thread_updates.items():
                    all_thread_updates.setdefault(thread_id, []).extend(updates)

                # Merge prev_batch tokens (take the maximum for backward pagination)
                if room_prev_batch is not None:
                    if prev_batch_token is None:
                        prev_batch_token = room_prev_batch
                    elif (
                        room_prev_batch.room_key.stream
                        > prev_batch_token.room_key.stream
                    ):
                        prev_batch_token = room_prev_batch

        # Query other rooms (joined/invited/knocking) with normal to_token
        if other_rooms and remaining_limit > 0:
            (
                other_thread_updates,
                other_prev_batch,
            ) = await self._main_store.get_thread_updates_for_rooms(
                room_ids=other_rooms,
                from_token=from_token.room_key if from_token else None,
                to_token=to_token.room_key,
                limit=remaining_limit,
                exclude_thread_ids=exclude_thread_ids,
            )

            # Merge updates
            for thread_id, updates in other_thread_updates.items():
                all_thread_updates.setdefault(thread_id, []).extend(updates)

            # Merge prev_batch tokens
            if other_prev_batch is not None:
                if prev_batch_token is None:
                    prev_batch_token = other_prev_batch
                elif (
                    other_prev_batch.room_key.stream > prev_batch_token.room_key.stream
                ):
                    prev_batch_token = other_prev_batch

        return all_thread_updates, prev_batch_token

    async def get_thread_updates_for_rooms(
        self,
        room_ids: frozenset[str],
        room_membership_map: Mapping[str, "RoomsForUserType"],
        user_id: str,
        from_token: StreamToken | None,
        to_token: StreamToken,
        limit: int,
        include_roots: bool = False,
        exclude_thread_ids: set[str] | None = None,
    ) -> tuple[dict[str, dict[str, ThreadUpdate]], StreamToken | None]:
        """Get thread updates across multiple rooms with full processing pipeline.

        This is the main entry point for fetching thread updates. It handles:
        - Fetching updates with membership-based security
        - Filtering for visibility
        - Optionally fetching thread roots and aggregations
        - Building the response structure

        Args:
            room_ids: The set of room IDs to fetch updates for
            room_membership_map: Map of room_id to RoomsForUserType for membership info
            user_id: The user requesting the updates
            from_token: Lower bound (exclusive) for the query
            to_token: Upper bound for the query
            limit: Maximum number of updates to return
            include_roots: Whether to fetch and include thread root events (default: False)
            exclude_thread_ids: Optional set of thread IDs to exclude

        Returns:
            A tuple of:
            - Map of room_id to thread_root_id to ThreadUpdate
            - Global prev_batch token if there are more results, None otherwise
        """
        # Fetch thread updates with membership handling
        all_thread_updates, prev_batch_token = await self._fetch_thread_updates(
            room_ids=room_ids,
            room_membership_map=room_membership_map,
            from_token=from_token,
            to_token=to_token,
            limit=limit,
            exclude_thread_ids=exclude_thread_ids,
        )

        if not all_thread_updates:
            return {}, prev_batch_token

        # Filter thread updates for visibility
        filtered_updates = await self._filter_thread_updates_for_user(
            all_thread_updates, user_id
        )

        if not filtered_updates:
            return {}, prev_batch_token

        # Optionally fetch thread root events and their bundled aggregations
        thread_root_event_map: ThreadRootsMap = {}
        aggregations_map: AggregationsMap = {}
        if include_roots:
            # Fetch thread root events
            thread_root_events = await self._main_store.get_events_as_list(
                filtered_updates.keys()
            )
            thread_root_event_map = {e.event_id: e for e in thread_root_events}

            # Fetch bundled aggregations for the thread roots
            if thread_root_event_map:
                aggregations_map = await self.get_bundled_aggregations(
                    thread_root_event_map.values(),
                    user_id,
                )

        # Build response structure with per-thread prev_batch tokens
        thread_updates = self._build_thread_updates_response(
            filtered_updates=filtered_updates,
            thread_root_event_map=thread_root_event_map,
            aggregations_map=aggregations_map,
            global_prev_batch_token=prev_batch_token,
        )

        return thread_updates, prev_batch_token

    @staticmethod
    async def serialize_thread_updates(
        thread_updates: Mapping[str, Mapping[str, ThreadUpdate]],
        prev_batch_token: StreamToken | None,
        event_serializer: "EventClientSerializer",
        time_now: int,
        store: "DataStore",
        serialize_options: SerializeEventConfig,
    ) -> JsonDict:
        """
        Serialize thread updates to JSON format.

        This helper handles serialization of ThreadUpdate objects for both the
        companion endpoint and the sliding sync extension.

        Args:
            thread_updates: Map of room_id to thread_root_id to ThreadUpdate
            prev_batch_token: Global pagination token for fetching more updates
            event_serializer: The event serializer to use
            time_now: Current time in milliseconds for event serialization
            store: Datastore for serializing stream tokens
            serialize_options: Serialization config

        Returns:
            JSON-serializable dict with "updates" and optionally "prev_batch"
        """
        updates_dict: JsonDict = {}

        for room_id, room_threads in thread_updates.items():
            room_updates: JsonDict = {}
            for thread_root_id, update in room_threads.items():
                update_dict: JsonDict = {}

                # Serialize thread_root event if present
                if update.thread_root is not None:
                    bundle_aggs_map = (
                        {thread_root_id: update.bundled_aggregations}
                        if update.bundled_aggregations is not None
                        else None
                    )
                    serialized_events = await event_serializer.serialize_events(
                        [update.thread_root],
                        time_now,
                        config=serialize_options,
                        bundle_aggregations=bundle_aggs_map,
                    )
                    if serialized_events:
                        update_dict["thread_root"] = serialized_events[0]

                # Add per-thread prev_batch if present
                if update.prev_batch is not None:
                    update_dict["prev_batch"] = await update.prev_batch.to_string(store)

                room_updates[thread_root_id] = update_dict

            updates_dict[room_id] = room_updates

        result: JsonDict = {"updates": updates_dict}

        # Add global prev_batch token if present
        if prev_batch_token is not None:
            result["prev_batch"] = await prev_batch_token.to_string(store)

        return result

    async def get_threads(
        self,
        requester: Requester,
        room_id: str,
        include: ThreadsListInclude,
        limit: int = 5,
        from_token: ThreadsNextBatch | None = None,
    ) -> JsonDict:
        """Get related events of a event, ordered by topological ordering.

        Args:
            requester: The user requesting the relations.
            room_id: The room the event belongs to.
            include: One of "all" or "participated" to indicate which threads should
                be returned.
            limit: Only fetch the most recent `limit` events.
            from_token: Fetch rows from the given token, or from the start if None.

        Returns:
            The pagination chunk.
        """

        user_id = requester.user.to_string()

        # TODO Properly handle a user leaving a room.
        (_, member_event_id) = await self._auth.check_user_in_room_or_world_readable(
            room_id, requester, allow_departed_users=True
        )

        # Note that ignored users are not passed into get_threads
        # below. Ignored users are handled in filter_events_for_client (and by
        # not passing them in here we should get a better cache hit rate).
        thread_roots, next_batch = await self._main_store.get_threads(
            room_id=room_id, limit=limit, from_token=from_token
        )

        events = await self._main_store.get_events_as_list(thread_roots)

        if include == ThreadsListInclude.participated:
            # Pre-seed thread participation with whether the requester sent the event.
            participated = {event.event_id: event.sender == user_id for event in events}
            # For events the requester did not send, check the database for whether
            # the requester sent a threaded reply.
            participated.update(
                await self._main_store.get_threads_participated(
                    [eid for eid, p in participated.items() if not p],
                    user_id,
                )
            )

            # Limit the returned threads to those the user has participated in.
            events = [event for event in events if participated[event.event_id]]

        events = await filter_events_for_client(
            self._storage_controllers,
            user_id,
            events,
            is_peeking=(member_event_id is None),
        )

        aggregations = await self.get_bundled_aggregations(
            events, requester.user.to_string()
        )

        now = self._clock.time_msec()
        serialized_events = await self._event_serializer.serialize_events(
            events, now, bundle_aggregations=aggregations
        )

        return_value: JsonDict = {"chunk": serialized_events}

        if next_batch:
            return_value["next_batch"] = str(next_batch)

        return return_value
