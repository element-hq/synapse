# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from collections.abc import Collection, Mapping, Sequence

from twisted.internet.defer import Deferred

from synapse.synapse_rust.database.postgres import ConnectionPool
from synapse.synapse_rust.events import (
    BundledAggregations,
    Event,
    SerializeEventConfig,
)
from synapse.types import JsonDict, JsonMapping, RoomStreamToken
from synapse.types.state import StateFilter

class SyncHandler:
    """Computes sync responses with the Rust implementation, reading the
    database through the native Rust Postgres driver.

    Get one from `RustHandlers.sync`; it is only built when the main database
    uses that driver.
    """

    def start_request(
        self, user_id: str, device_id: str | None, since: RoomStreamToken | None
    ) -> None:
        """Forget the lazy-loaded members the device did not receive, at the
        start of its request from `since` (None for an initial sync)."""

    def ll_member_was_sent(
        self, user_id: str, device_id: str | None, member: str, event_id: str
    ) -> bool:
        """Whether `event_id` is the membership of `member` last sent to the
        device. For rooms Python computes in `rust_sync: on` mode, which share
        the Rust handler's lazy-loaded members caches."""

    def ll_member_mark_sent(
        self,
        user_id: str,
        device_id: str | None,
        member: str,
        event_id: str,
        next_batch: RoomStreamToken,
    ) -> None:
        """Record that `event_id` is being sent to the device as the membership
        of `member`, in the response ending at `next_batch`."""

    def room_changes(
        self, request: "SyncRequest", input: "RoomsInput"
    ) -> "Deferred[RoomChanges]":
        """`_get_room_changes_for_incremental_sync` or
        `_get_room_changes_for_initial_sync`. Consumes `input`. Cancelling the
        deferred stops the computation."""

    def room_changes_blocking(
        self, request: "SyncRequest", input: "RoomsInput"
    ) -> "RoomChanges":
        """As `room_changes`, but blocks. For tests."""

    def generate_room_entries(
        self,
        request: "SyncRequest",
        changes: "RoomChanges",
        *,
        ephemeral_by_room: Mapping[str, Sequence[JsonMapping]],
        tags_by_room: Mapping[str, Mapping[str, JsonMapping]],
        account_data_by_room: Mapping[str, Mapping[str, JsonMapping]],
        sticky_by_room: Mapping[str, Sequence[str]],
    ) -> "Deferred[RoomsResult]":
        """The entries of the joined and archived rooms of `changes`, ten at
        a time. The `*_by_room` mappings are what `handle_room_entries` reads
        for each room. Cancelling the deferred stops the computation."""

    def generate_room_entries_blocking(
        self,
        request: "SyncRequest",
        changes: "RoomChanges",
        *,
        ephemeral_by_room: Mapping[str, Sequence[JsonMapping]],
        tags_by_room: Mapping[str, Mapping[str, JsonMapping]],
        account_data_by_room: Mapping[str, Mapping[str, JsonMapping]],
        sticky_by_room: Mapping[str, Sequence[str]],
    ) -> "RoomsResult":
        """As `generate_room_entries`, but blocks. For tests."""

class SyncRequest:
    """One `/sync` request, as the per-room computation sees it."""

    def __init__(
        self,
        *,
        user_id: str,
        device_id: str | None,
        filter: "FilterCollection",
        use_state_after: bool,
        now_room_key: RoomStreamToken,
        ignored_users: frozenset[str],
        is_server_admin: bool,
        return_soft_failed_events: bool,
        return_policy_server_spammy_events: bool,
        enable_push: bool,
        msc4354_enabled: bool,
    ) -> None: ...

class RoomsInput:
    """What `room_changes` reads besides the request, from the
    `SyncResultBuilder`."""

    def __init__(
        self,
        *,
        since: RoomStreamToken | None,
        full_state: bool,
        joined_room_ids: Sequence[str],
        changed_room_ids: Collection[str],
        forced_newly_joined_room_ids: Sequence[str],
        membership_change_events: Sequence[Event],
        initial_rooms: Sequence[tuple[str, str, str, str, int]],
    ) -> None:
        """`changed_room_ids` are the joined rooms that may have events after
        `since`. `initial_rooms` are `(room_id, sender, membership, event_id,
        stream)` for each of the user's rooms in a known room version, for an
        initial sync."""

class RoomChanges:
    """A `_RoomChanges`, with every invite: Python drops those of ignored
    users and those the invite rules ignore."""

    @property
    def newly_joined_rooms(self) -> list[str]: ...
    @property
    def newly_left_rooms(self) -> list[str]: ...
    @property
    def invited(self) -> list[tuple[str, Event]]: ...
    @property
    def knocked(self) -> list[tuple[str, Event]]: ...
    def __len__(self) -> int:
        """The number of joined and archived rooms to compute."""

    def room_entry(
        self, room_id: str
    ) -> tuple[
        str,
        list[Event] | None,
        bool,
        bool,
        bool,
        bool,
        RoomStreamToken,
        bool,
        RoomStreamToken,
        bool,
    ]:
        """The `RoomSyncResultBuilder` fields of a room, for Python to compute
        it: `(rtype, events, newly_joined, full_state, has_since,
        upto_from_now, upto_room_key, end_from_now, end_room_key,
        out_of_band)`."""

class RoomsResult:
    """What `generate_room_entries` made of the rooms."""

    def take_entries(self) -> list["RoomSyncResult"]:
        """The entries. A second call returns an empty list."""

    @property
    def empty(self) -> list[str]:
        """The rooms computed with nothing to send."""

    @property
    def fallbacks(self) -> list[tuple[str, str]]:
        """`(room_id, reason)` for each room Python must compute."""

    @property
    def errors(self) -> list[tuple[str, str]]:
        """`(room_id, error)` for each room the Rust handler failed on."""

class RoomSyncResult:
    """A room's entry."""

    def __bool__(self) -> bool:
        """Whether the entry has anything to send."""
    @property
    def room_id(self) -> str: ...
    @property
    def rtype(self) -> str: ...
    @property
    def timeline(self) -> list[tuple[Event, str]]:
        """`(event, membership)` pairs, oldest first."""
    @property
    def prev_batch_from_now(self) -> bool:
        """Whether `prev_batch` builds on the response's now token rather
        than the room's upto token."""
    @property
    def prev_batch_room_key(self) -> RoomStreamToken | None:
        """The room key of `prev_batch`, if it differs from the base
        token's."""
    @property
    def limited(self) -> bool: ...
    @property
    def upto_from_now(self) -> bool:
        """Whether the room's upto token builds on the response's now token
        rather than its since token."""
    @property
    def upto_room_key(self) -> RoomStreamToken:
        """The room key of the room's upto token."""
    @property
    def bundled_aggregations(self) -> dict[str, BundledAggregations]:
        """The bundled aggregations of the timeline, by event ID."""
    @property
    def bundled_events(self) -> list[Event]:
        """The events bundled with the timeline's events: edits and the
        latest events of threads."""
    @property
    def state(self) -> list[Event]: ...
    @property
    def ephemeral(self) -> list[JsonDict]: ...
    @property
    def account_data(self) -> list[JsonDict]: ...
    @property
    def summary(self) -> JsonDict | None: ...
    @property
    def unread_notifications(self) -> JsonDict: ...
    @property
    def unread_thread_notifications(self) -> JsonDict: ...
    @property
    def unread_count(self) -> int: ...
    @property
    def sticky(self) -> list[tuple[Event, str]]: ...

class RoomsEncoder:
    """Encodes the joined and archived rooms of one sync response, as the
    servlet's `encode_room` does."""

    def __init__(
        self,
        *,
        now_room_key: RoomStreamToken,
        now_suffix: str,
        since_suffix: str | None,
        instance_ids: Mapping[str, int],
        state_key_name: str,
        msc3773_enabled: bool,
        msc2654_enabled: bool,
    ) -> None:
        """
        Args:
            now_suffix: The response's now token after its room key:
                everything after the first `_`.
            since_suffix: The same for the since token, if there is one.
            instance_ids: `get_id_for_instance` of each writer the two
                tokens' room keys name.
            state_key_name: `state`, `state_after`, or MSC4222's unstable
                name for it.
        """
    def encode(
        self,
        rooms: Sequence[RoomSyncResult],
        time_now_ms: int,
        config: SerializeEventConfig,
        *,
        bundle_aggregations: Mapping[str, BundledAggregations] | None = None,
        redaction_map: Mapping[str, Event] | None = None,
        unsigned_additions: Mapping[str, JsonMapping] | None = None,
    ) -> list[JsonDict]:
        """The encoded rooms, in order. The maps are as for
        `serialize_events`, across all the rooms."""

class FilterCollection:
    """The Rust port of `synapse.api.filtering.FilterCollection`, built from
    the same filter JSON. The keyword flags are what the Python one reads
    from the homeserver config."""

    def __init__(
        self,
        filter_json: JsonMapping,
        *,
        msc3773_enabled: bool = False,
        msc3874_enabled: bool = False,
        include_profile_updates_in_sync: bool = False,
    ) -> None: ...
    def timeline_limit(self) -> int: ...
    def presence_limit(self) -> int: ...
    def ephemeral_limit(self) -> int: ...
    def lazy_load_members(self) -> bool: ...
    def include_redundant_members(self) -> bool: ...
    def unread_thread_notifications(self) -> bool: ...
    @property
    def include_leave(self) -> bool: ...
    @property
    def event_fields(self) -> list[str]: ...
    @property
    def event_format(self) -> str: ...
    @property
    def profile_fields(self) -> set[str]: ...
    def blocks_all_rooms(self) -> bool: ...
    def blocks_all_presence(self) -> bool: ...
    def blocks_all_global_account_data(self) -> bool: ...
    def blocks_all_room_ephemeral(self) -> bool: ...
    def blocks_all_room_account_data(self) -> bool: ...
    def blocks_all_room_timeline(self) -> bool: ...
    def timeline_needs_relation_check(self) -> bool:
        """Whether the timeline filter has a `related_by_*` condition, which
        `check_room_timeline` leaves to the store."""

    def check_room_timeline(self, event: Event) -> bool:
        """Whether the event passes the room and timeline filters, the
        relation check aside."""

    def check_room_state(self, event: Event) -> bool: ...
    def check_room_ephemeral(self, event: JsonDict) -> bool: ...
    def check_room_account_data(self, event: JsonDict) -> bool: ...
    def check_presence(self, user_id: str) -> bool: ...
    def check_global_account_data(self, event: JsonDict) -> bool: ...

class LazyLoadedMembersCache:
    """The Rust port of `synapse.handlers.sync.LazyLoadedMembersCache`: the
    members a lazy-loading device has been sent. Positions are the room part
    of the sync tokens."""

    def __init__(self) -> None: ...
    def start_request(self, since: RoomStreamToken | None) -> None: ...
    def was_sent(self, user_id: str, event_id: str) -> bool: ...
    def mark_sent(
        self, user_id: str, event_id: str, next_batch: RoomStreamToken
    ) -> None: ...
    def __len__(self) -> int: ...

# The Rust ports of the state delta arithmetic in `synapse.handlers.sync`.
# Deltas are `(event_type, state_key, event_id, prev_event_id)` in stream
# order.

def calculate_state_for_test(
    timeline_contains: Mapping[tuple[str, str], str],
    timeline_start: Mapping[tuple[str, str], str],
    timeline_end: Mapping[tuple[str, str], str],
    previous_timeline_end: Mapping[tuple[str, str], str],
    lazy_load_members: bool,
) -> dict[tuple[str, str], str]:
    """`_calculate_state`."""

def roll_back_state_for_test(
    state: Mapping[tuple[str, str], str],
    deltas: list[tuple[str, str, str | None, str | None]],
) -> dict[tuple[str, str], str]:
    """`_roll_back_state_over_deltas`."""

def apply_state_deltas_for_test(
    initial: Mapping[tuple[str, str], str],
    deltas: list[tuple[str, str, str | None, str | None]],
    timeline_state: Mapping[tuple[str, str], str],
    user_id: str,
) -> tuple[dict[tuple[str, str], str], set[tuple[str, str]], set[tuple[str, str]]]:
    """`_apply_current_state_deltas`."""

def extract_heroes_for_test(
    details: Mapping[str, tuple[list[tuple[str, str]], int]], me: str
) -> list[str]:
    """`extract_heroes_from_room_summary`, with each membership's summary as
    `(members, count)`."""

class SyncStoreForTest:
    """The sync store's queries (`rust/src/storage/sync_store.rs`), each run
    to completion on the tokio runtime, over the native driver's pool and the
    Rust sync handler's caches. For `tests/storage/test_sync_store.py`."""

    def __init__(self, pool: ConnectionPool, handler: SyncHandler) -> None: ...
    def filter_events_for_server(
        self,
        target_server_name: str,
        local_server_name: str,
        events: Sequence[Event],
        *,
        redact: bool,
        filter_out_erased_senders: bool,
        filter_out_remote_partial_state_events: bool,
    ) -> list[Event] | None:
        """`synapse.visibility.filter_events_for_server` in Rust. `None` where
        Python would wait for the full state."""

    def rooms_for_user(self, user_id: str) -> set[str]:
        """`get_rooms_for_user`."""

    def events_in_current_state(self, event_ids: list[str]) -> set[str]:
        """`check_if_events_in_current_state`."""

    def timeline_gap(
        self,
        room_id: str,
        from_token: RoomStreamToken | None,
        to_token: RoomStreamToken,
    ) -> RoomStreamToken | None:
        """`get_timeline_gaps`."""

    def partial_state_rooms(self, room_ids: list[str]) -> dict[str, bool]:
        """`is_partial_state_room_batched`."""

    def room_summary(
        self, room_id: str
    ) -> dict[str, tuple[list[tuple[str, str]], int]]:
        """`get_room_summary`, with each membership's summary as `(members, count)`."""

    def tags_for_room(self, user_id: str, room_id: str) -> dict[str, JsonDict]:
        """`get_tags_for_room`."""

    def global_account_data_by_type(
        self, user_id: str, data_type: str
    ) -> JsonDict | None:
        """`get_global_account_data_by_type_for_user`."""

    def ignored_users(self, user_id: str) -> set[str]:
        """`ignored_users`."""

    def are_users_erased(self, user_ids: list[str]) -> dict[str, bool]:
        """`are_users_erased`."""

    def retention_policy(self, room_id: str) -> tuple[int | None, int | None]:
        """`get_retention_policy_for_room`, as `(min_lifetime, max_lifetime)`."""

    def is_server_admin(self, user_id: str) -> bool:
        """`is_server_admin`."""

    def events(
        self,
        event_ids: list[str],
        redact_behaviour: str = "redact",
        allow_rejected: bool = False,
        get_prev_content: bool = False,
    ) -> list[Event]:
        """`get_events_as_list`, with `redact_behaviour` as the name of a
        member of `EventRedactBehaviour`."""

    def paginate_room_events(
        self,
        room_id: str,
        from_key: RoomStreamToken,
        to_key: RoomStreamToken | None,
        direction: str,
        limit: int,
        order: str,
    ) -> tuple[list[Event], RoomStreamToken, bool]:
        """`paginate_room_events_by_stream_ordering` (`order="stream"`) or
        `paginate_room_events_by_topological_ordering` (`"topological"`),
        with `direction` as `"f"` or `"b"`."""

    def recent_event_ids(
        self, room_id: str, limit: int, end_token: RoomStreamToken
    ) -> tuple[list[tuple[str, int | None, int]], RoomStreamToken]:
        """`get_recent_event_ids_for_room`, with each row as
        `(event_id, topological_ordering, stream_ordering)`."""

    def membership_changes(
        self,
        user_id: str,
        from_key: RoomStreamToken,
        to_key: RoomStreamToken,
        excluded_rooms: list[str] | None = None,
    ) -> list[Event]:
        """`get_membership_changes_for_user`."""

    def last_event_before(
        self, room_id: str, end_token: RoomStreamToken
    ) -> tuple[str, str, int] | None:
        """`get_last_event_pos_in_room_before_stream_ordering`, as
        `(event_id, instance_name, stream_ordering)`."""

    def last_event_of_types_before(
        self, room_id: str, end_token: RoomStreamToken, event_types: list[str]
    ) -> tuple[str, str, int] | None:
        """`get_last_event_pos_in_room_before_stream_ordering` with
        `event_types`, as `(event_id, instance_name, stream_ordering)`."""

    def member_counts(self, room_id: str) -> dict[str, int]:
        """`get_member_counts`."""

    def latest_bump_stamp(self, room_id: str) -> int | None:
        """`get_latest_bump_stamp_for_room`."""

    def unread_counts(
        self, room_id: str, user_id: str
    ) -> tuple[tuple[int, int, int], dict[str, tuple[int, int, int]]]:
        """`get_unread_event_push_actions_by_room_for_user`, with each
        timeline's counts as `(notify_count, unread_count, highlight_count)`:
        the main timeline's, and the threads' by thread."""

    def current_state_ids(
        self, room_id: str, state_filter: StateFilter
    ) -> dict[tuple[str, str], str]:
        """`get_current_state_ids` with a filter."""

    def state_deltas(
        self,
        room_id: str,
        from_token: RoomStreamToken | None,
        to_token: RoomStreamToken | None,
    ) -> list[tuple[int, str, str, str | None, str | None]]:
        """`get_current_state_deltas_for_room_by_event_position`, with each
        delta as `(stream_id, type, state_key, event_id, prev_event_id)`."""

    def state_deltas_by_stream_id(
        self,
        room_id: str,
        from_token: RoomStreamToken | None,
        to_token: RoomStreamToken | None,
    ) -> list[tuple[int, str, str, str | None, str | None]]:
        """`get_current_state_deltas_for_room`, with each delta as
        `(stream_id, type, state_key, event_id, prev_event_id)`."""

    def state_group_for_events(self, event_ids: list[str]) -> dict[str, int]:
        """`_get_state_group_for_events`."""

    def state_ids_for_event(
        self, event_id: str, state_filter: StateFilter
    ) -> dict[tuple[str, str], str]:
        """`get_state_ids_for_event`."""

    def state_after_event(
        self, event_id: str, state_filter: StateFilter
    ) -> dict[tuple[str, str], str]:
        """`get_state_after_event`."""

    def state_ids_at(
        self, room_id: str, token: RoomStreamToken, state_filter: StateFilter
    ) -> dict[tuple[str, str], str]:
        """`get_state_ids_at`, with the room part of the stream position."""
