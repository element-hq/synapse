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

"""Simplified sliding sync (MSC4186) in Rust. See `rust/src/sliding_sync/`."""

from collections.abc import Mapping
from typing import AbstractSet, Generic, TypeVar

from twisted.internet.defer import Deferred

from synapse.events import EventBase
from synapse.synapse_rust.database.postgres import ConnectionPool
from synapse.synapse_rust.events import SerializeEventConfig
from synapse.types import (
    JsonDict,
    JsonMapping,
    MultiWriterStreamToken,
    RoomStreamToken,
    StrCollection,
)
from synapse.types.handlers.sliding_sync import HaveSentRoom
from synapse.types.state import StateFilter
from synapse.util.caches.stream_change_cache import StreamChangeCache

T = TypeVar("T", str, RoomStreamToken, MultiWriterStreamToken, int)

class SlidingSyncHandler:
    """Computes sliding sync responses in Rust, reading the database through
    the native Postgres driver. Built by `RustHandlers`."""

    def load_connection_state(
        self, request: SlidingSyncRequest, *, instance_ids: Mapping[str, int]
    ) -> Deferred[ConnectionState]:
        """`get_and_clear_connection_positions`. `instance_ids` are
        `get_id_for_instance` for the writers of the request's tokens.

        Raises:
            SlidingSyncUnknownPosition for a position the store does not know.
        """
    def load_connection_state_blocking(
        self, request: SlidingSyncRequest, *, instance_ids: Mapping[str, int]
    ) -> ConnectionState: ...
    def compute_interested_rooms(
        self,
        request: SlidingSyncRequest,
        state: ConnectionState,
        *,
        server_name: str,
        msc4155_enabled: bool,
        num_rooms_threshold: int,
        membership_changed_since_from: bool,
        membership_changed_since_to: bool,
        events_changed_since_from: AbstractSet[str] | None,
        room_max_token: RoomStreamToken,
        events_stream_cache: StreamChangeCache,
    ) -> Deferred[InterestedRooms]:
        """`compute_interested_rooms` on the sliding sync tables.
        `membership_changed_since_*` are `_membership_stream_cache`'s
        `has_entity_changed` at the from and to tokens' streams (true without a
        from token), `events_changed_since_from` is `_events_stream_cache`'s
        `get_all_entities_changed` at the from token's stream, and
        `events_stream_cache` is read for the rooms a sort needs.

        Raises:
            RustSlidingSyncFallback for a filter Python does not implement.
            SlidingSyncUnknownPosition for a connection to reset.
        """
    def compute_interested_rooms_blocking(
        self,
        request: SlidingSyncRequest,
        state: ConnectionState,
        *,
        server_name: str,
        msc4155_enabled: bool,
        num_rooms_threshold: int,
        membership_changed_since_from: bool,
        membership_changed_since_to: bool,
        events_changed_since_from: AbstractSet[str] | None,
        room_max_token: RoomStreamToken,
        events_stream_cache: StreamChangeCache,
    ) -> InterestedRooms: ...
    def generate_room_entries(
        self,
        request: SlidingSyncRequest,
        state: ConnectionState,
        interested: InterestedRooms,
    ) -> Deferred[RoomsResult]:
        """`get_room_sync_data` for each of the rooms to send, twenty at a
        time. The rooms' changes to the connection state go into `state`."""
    def generate_room_entries_blocking(
        self,
        request: SlidingSyncRequest,
        state: ConnectionState,
        interested: InterestedRooms,
    ) -> RoomsResult: ...
    def finish_request(
        self,
        request: SlidingSyncRequest,
        state: ConnectionState,
        interested: InterestedRooms,
    ) -> Deferred[tuple[int, SlidingSyncStats]]:
        """The rest of `current_sync_for_user` after the extensions: record
        the sent and unsent rooms, and persist the state if it changed.
        Resolves to the connection position for `pos`."""
    def finish_request_blocking(
        self,
        request: SlidingSyncRequest,
        state: ConnectionState,
        interested: InterestedRooms,
    ) -> tuple[int, SlidingSyncStats]: ...

class RustSlidingSyncFallback(Exception):
    """The request is one Rust leaves to Python."""

class ConnectionState:
    """A request's connection state, owned by Rust from load to persist."""

    @property
    def stats(self) -> SlidingSyncStats:
        """What loading the state cost."""
    @property
    def previous(self) -> ConnectionStateView:
        """The state as loaded: `PerConnectionState`."""
    @property
    def new(self) -> ConnectionStateView:
        """The state with the request's updates: `MutablePerConnectionState`."""
    def python_state_for_rooms(
        self, room_ids: list[str]
    ) -> tuple[
        int | None,
        dict[str, tuple[int, dict[str, set[str]]]],
        dict[str, HaveSentRoom[RoomStreamToken]],
    ]:
        """`(last_used_ts, room_configs, room_statuses)` of the previous state
        for `room_ids`, for the rooms Python computes."""
    def apply_python_rooms(
        self,
        room_configs: list[tuple[str, int, Mapping[str, AbstractSet[str]]]],
        lazy_memberships: list[tuple[str, Mapping[str, int | None], AbstractSet[str]]],
    ) -> None:
        """Record the changes of the rooms Python computed."""
    def updates_for_test(
        self,
    ) -> tuple[
        dict[str, tuple[int, dict[str, set[str]]]],
        dict[str, tuple[dict[str, int | None], set[str]]],
    ]: ...

class ConnectionStateView:
    """`PerConnectionState` or `MutablePerConnectionState` over a
    `ConnectionState`, as the extensions read them."""

    @property
    def rooms(self) -> RoomStatusMapView[RoomStreamToken]: ...
    @property
    def receipts(self) -> RoomStatusMapView[MultiWriterStreamToken]: ...
    @property
    def account_data(self) -> RoomStatusMapView[int]: ...
    @property
    def last_used_ts(self) -> int | None: ...

class RoomStatusMapView(Generic[T]):
    """One stream's `RoomStatusMap` of a `ConnectionStateView`. The record
    methods raise on the previous state's."""

    def have_sent_room(self, room_id: str) -> HaveSentRoom[T]: ...
    @property
    def _statuses(self) -> dict[str, HaveSentRoom[T]]: ...
    def record_sent_rooms(self, room_ids: StrCollection) -> None: ...
    def record_unsent_rooms(self, room_ids: StrCollection, from_token: T) -> None: ...
    def get_updates(self) -> dict[str, HaveSentRoom[T]]: ...

class InterestedRooms:
    """`SlidingSyncInterestedRooms`, as Python reads it."""

    @property
    def lists(
        self,
    ) -> list[tuple[str, int, list[tuple[tuple[int, int], list[str]]]]]:
        """`(name, count, [((start, end), room_ids)])` for each list."""
    @property
    def relevant_room_ids(self) -> list[str]: ...
    @property
    def rooms_to_send(self) -> list[str]: ...
    @property
    def all_rooms(self) -> set[str]: ...
    @property
    def stats(self) -> SlidingSyncStats: ...
    def python_room_inputs(
        self, room_ids: list[str]
    ) -> list[
        tuple[
            str,
            tuple[int, dict[str, set[str]]],
            tuple[
                str | None,
                str,
                str | None,
                str,
                int,
                str | None,
                bool,
                str | None,
                bool,
            ],
            bool,
            bool,
            bool,
        ]
    ]:
        """`(room_id, (timeline_limit, required_state_map), (sender,
        membership, event_id, instance_name, stream, room_version_id,
        has_known_state, room_type, is_encrypted), newly_joined, newly_left,
        is_dm)` for each room, for the rooms Python computes."""

class RoomResult:
    """One room's entry, computed in Rust: what `current_sync_for_user` and the
    extensions read of a `SlidingSyncResult.RoomResult`."""

    def __bool__(self) -> bool: ...
    @property
    def room_id(self) -> str: ...
    @property
    def initial(self) -> bool: ...
    @property
    def unstable_expanded_timeline(self) -> bool: ...
    @property
    def timeline_event_ids(self) -> list[str]: ...
    @property
    def timeline_senders(self) -> list[str]: ...
    @property
    def required_state_member_keys(self) -> list[str]: ...
    @property
    def hero_user_ids(self) -> list[str]: ...
    def events_to_serialize(self) -> list[EventBase]:
        """Every event the room's encoding serializes, for module callbacks."""

class RoomsResult:
    """What `generate_room_entries` made of the rooms."""

    def take_entries(self) -> list[RoomResult]:
        """The entries to send. A second call returns an empty list."""
    @property
    def empty(self) -> list[str]:
        """The rooms computed with nothing to send."""
    @property
    def fallbacks(self) -> list[tuple[str, str]]: ...
    @property
    def errors(self) -> list[tuple[str, str]]: ...
    @property
    def stats(self) -> SlidingSyncStats: ...

def encode_rooms(
    rooms: list[RoomResult],
    time_now_ms: int,
    config: SerializeEventConfig,
    *,
    unsigned_additions: Mapping[str, JsonDict] | None = None,
) -> bytes:
    """The servlet's `encode_rooms` for rooms computed in Rust, as JSON object
    members (`"room_id":{...}` joined by commas, no braces). Encodes without
    the GIL."""

class SlidingSyncStats:
    """What the Rust implementation did for one request."""

    def __init__(
        self,
        *,
        db_time: float = 0.0,
        checkout_time: float = 0.0,
        transactions: int = 0,
        queries: int = 0,
        rooms_computed: int = 0,
        lazy_member_lookups: int = 0,
    ) -> None: ...
    @property
    def db_time(self) -> float:
        """Time spent waiting on statements, in seconds."""
    @property
    def checkout_time(self) -> float:
        """Time spent waiting for a database connection, in seconds."""
    @property
    def transactions(self) -> int: ...
    @property
    def queries(self) -> int: ...
    @property
    def rooms_computed(self) -> int: ...
    @property
    def lazy_member_lookups(self) -> int: ...

class SlidingSyncRequest:
    """One sliding sync request as the Rust implementation sees it: the body,
    parsed from the bytes pydantic validated, plus what Python adds."""

    def __init__(
        self,
        body: bytes,
        *,
        user_id: str,
        device_id: str | None,
        from_room_key: RoomStreamToken | None,
        connection_position: int | None,
        to_room_key: RoomStreamToken,
        to_suffix: str,
        is_server_admin: bool,
        return_soft_failed_events: bool,
        return_policy_server_spammy_events: bool,
        have_finished_bg_jobs: bool,
        rooms_to_exclude: set[str],
        msc4306_enabled: bool,
        msc4354_enabled: bool,
        profiles_enabled: bool,
    ) -> None:
        """Raises `ValueError` if the body does not parse."""
    def body_for_test(self) -> JsonDict:
        """The parsed body with every default filled in, and the lists and
        room subscriptions as lists of `[name, value]` pairs."""
    @property
    def user_id(self) -> str: ...
    @property
    def device_id(self) -> str | None: ...
    @property
    def from_room_key(self) -> RoomStreamToken | None: ...
    @property
    def connection_position(self) -> int | None: ...
    @property
    def to_room_key(self) -> RoomStreamToken: ...
    @property
    def to_suffix(self) -> str: ...
    @property
    def is_server_admin(self) -> bool: ...
    @property
    def have_finished_bg_jobs(self) -> bool: ...
    @property
    def rooms_to_exclude(self) -> set[str]: ...

# Test exports of the pure logic, for `tests/handlers/test_sliding_sync_rust_pure.py`.
# A `RoomSyncConfig` crosses as `(timeline_limit, {type: state keys})`.

def room_sync_config_from_room_config_for_test(
    timeline_limit: int, required_state: list[tuple[str, str]]
) -> tuple[int, dict[str, list[str]]]: ...
def combine_room_sync_config_for_test(
    a: tuple[int, dict[str, list[str]]], b: tuple[int, dict[str, list[str]]]
) -> tuple[int, dict[str, list[str]]]: ...
def must_await_full_state_for_test(
    config: tuple[int, dict[str, list[str]]], server_name: str
) -> bool:
    """Raises `ValueError` where Python raises `IndexError`."""

def required_state_changes_for_test(
    user_id: str,
    prev_required_state_map: dict[str, list[str]],
    request_required_state_map: dict[str, list[str]],
    previously_returned_lazy_user_ids: set[str],
    request_lazy_load_user_ids: set[str],
    state_deltas: list[tuple[str, str]],
) -> tuple[
    dict[str, list[str]] | None,
    tuple[dict[str, list[str] | None], bool],
    list[str],
    list[str],
]:
    """`(changed_required_state_map, added_state_filter,
    extra_users_to_add_to_lazy_cache, lazy_members_invalidated)`, with a
    state filter as `({type: state keys or None}, include_others)`."""

def required_state_request_for_test(
    user_id: str,
    membership: str,
    required_state_map: dict[str, list[str]],
    timeline: list[tuple[str, str, str | None]],
    limited: bool | None,
    initial: bool,
    state_delta_keys: list[tuple[str, str]],
) -> tuple[tuple[dict[str, list[str] | None], bool], bool, list[str], list[str]]:
    """`(required_state_filter, lazy_load_room_members, lazy_load_user_ids,
    explicit_user_state)`, the timeline as `(sender, type, state_key)`."""

def state_filter_with_meta_for_test(
    required_state_filter: StateFilter, hero_user_ids: list[str], include_avatar: bool
) -> tuple[dict[str, list[str] | None], bool]: ...
def room_status_map_ops_for_test(
    statuses: dict[str, tuple[str, int | None]],
    ops: list[tuple[str, list[str], int | None]],
) -> tuple[dict[str, tuple[str, int | None]], dict[str, tuple[str, int | None]], int]:
    """`(updates, flattened, len)` of a `MutableRoomStatusMap[int]` after
    `ops` of `("sent", room_ids, None)` or `("unsent", room_ids, token)`."""

def lazy_membership_changes_for_test(
    returned_user_id_to_last_seen_ts_map: dict[str, int | None],
    invalidated_user_ids: set[str],
    now_ms: int,
) -> tuple[list[str], bool]:
    """`(get_returned_user_ids_to_update, has_updates)`."""

def per_connection_state_for_test(
    rooms: dict[str, tuple[str, int | None]],
    rooms_sent: list[str],
    receipts: dict[str, tuple[str, int | None]],
    receipts_sent: list[str],
    account_data: dict[str, tuple[str, int | None]],
    account_data_sent: list[str],
    room_configs: dict[str, tuple[int, dict[str, list[str]]]],
    room_config_updates: dict[str, tuple[int, dict[str, list[str]]]],
    lazy: dict[str, tuple[dict[str, int | None], set[str]]],
    now_ms: int,
) -> tuple[bool, int, int]:
    """`(has_updates, len(state), len(mutable state))`."""

# A room for user in the test form `(room_id, sender, membership, event_id,
# stream, has_known_state, room_type, is_encrypted)`.
_PyRoom = tuple[str, str | None, str, str | None, int, bool, str | None, bool]

def filter_membership_for_sync_for_test(
    user_id: str, room: _PyRoom, newly_left: bool
) -> bool: ...
def filter_rooms_for_test(
    rooms: list[_PyRoom],
    filters: str,
    dm_room_ids: set[str],
    room_tags: dict[str, set[str]],
    sent_before: set[str],
) -> list[str]:
    """`filters` is JSON. Raises `NotImplementedError` as Python does."""

def sort_rooms_for_test(
    rooms: list[_PyRoom],
    earliest_cache_position: int,
    cache: dict[str, int],
    to_stream: int,
    limit: int | None,
    recheck: dict[str, int | None],
    joined_positions: dict[str, int],
) -> list[str]:
    """Raises `KeyError` as Python does."""

def sync_list_ops_for_test(
    ranges: list[tuple[int, int]],
    sorted_room_ids: list[str],
    room_sync_config: tuple[int, dict[str, list[str]]],
    relevant_room_map: list[tuple[str, tuple[int, dict[str, list[str]]]]],
) -> tuple[
    list[tuple[tuple[int, int], list[str]]],
    list[tuple[str, tuple[int, dict[str, list[str]]]]],
]: ...
def rooms_to_send_for_test(
    relevant_room_map: list[tuple[str, tuple[int, dict[str, list[str]]]]],
    room_configs: dict[str, tuple[int, dict[str, list[str]]]],
    statuses: dict[str, tuple[str, int | None]],
    last_used_ts: int | None,
    rooms_that_have_updates: set[str],
    now_ms: int,
) -> list[str]:
    """Raises `ValueError` where Python raises `SlidingSyncUnknownPosition`."""

def timeline_bounds_for_test(
    room_status: tuple[str, int | None],
    from_stream: int | None,
    skip_previous_bounds: bool,
    prev_room_sync_config: tuple[int, dict[str, list[str]]] | None,
    room_sync_config: tuple[int, dict[str, list[str]]],
) -> tuple[int | None, bool, bool]:
    """`(from_bound, initial, ignore_timeline_bound)`, tokens as streams."""

def stripped_state_for_test(event: str) -> str:
    """The event and the stripped state as JSON."""

def get_bump_stamp_for_test(
    timeline: list[tuple[str, int]],
    check_outside_timeline: bool,
    latest_room_bump_stamp: int | None,
    have_finished_bg_jobs: bool,
    to_stream: int,
    last_event_stream: int | None,
) -> int | None: ...
def room_config_to_persist_for_test(
    room_sync_config: tuple[int, dict[str, list[str]]],
    prev_room_sync_config: tuple[int, dict[str, list[str]]] | None,
    changed_required_state_map: dict[str, list[str]] | None,
    ignore_timeline_bound: bool,
    limited: bool | None,
) -> tuple[int, dict[str, list[str]]] | None: ...
def find_relevant_room_ids_for_test(
    requested_lists: list[str] | None,
    requested_room_ids: list[str] | None,
    actual_lists: list[tuple[str, list[list[str]]]],
    actual_room_ids: set[str],
) -> list[str]:
    """Each list as its operations' room IDs. Raises `AssertionError` as
    Python does."""

# A membership snapshot as `(room_id, sender, membership, event_id,
# room_version_id, instance_name, stream, has_known_state, room_type,
# is_encrypted)`.
_PySnapshot = tuple[
    str, str | None, str, str | None, str | None, str, int, bool, str | None, bool
]
# A room status as `(flag, token)`, and a room config as `(timeline_limit,
# {type: state keys})`.
_PyStatus = tuple[str, str | None]
_PyConfig = tuple[int, dict[str, list[str]]]

class SlidingSyncStoreForTest:
    """The sliding sync store's queries (`rust/src/storage/sliding_sync_store.rs`),
    each run to completion on the tokio runtime, over the native driver's pool
    and the Rust store's caches. For `tests/storage/test_sliding_sync_store.py`.
    Raises `SlidingSyncUnknownPosition` as the Python does."""

    def __init__(self, pool: ConnectionPool, handler: SlidingSyncHandler) -> None: ...
    def rooms_from_snapshots(self, user_id: str) -> dict[str, _PySnapshot]: ...
    def self_leave_rooms_after(
        self, user_id: str, to_token: RoomStreamToken
    ) -> dict[str, _PySnapshot]: ...
    def snapshot_room(self, user_id: str, room_id: str) -> _PySnapshot | None: ...
    def snapshot_rooms(
        self, user_id: str, room_ids: list[str]
    ) -> dict[str, _PySnapshot]: ...
    def snapshot_membership_changes(
        self,
        user_id: str,
        from_token: RoomStreamToken,
        to_token: RoomStreamToken,
        excluded_room_ids: list[str],
        membership_may_have_changed: bool,
    ) -> list[tuple[str, str | None, str, str | None, tuple[str, int], str | None]]: ...
    def membership_delta_changes(
        self,
        user_id: str,
        from_token: RoomStreamToken,
        to_token: RoomStreamToken,
        excluded_room_ids: list[str],
        membership_may_have_changed: bool,
    ) -> list[
        tuple[
            str,
            str | None,
            tuple[str, int],
            str,
            str | None,
            str | None,
            tuple[str, int] | None,
            str | None,
            str | None,
        ]
    ]: ...
    def rooms_with_updates_since(
        self,
        room_ids: list[str],
        from_token: RoomStreamToken,
        changed_since: set[str] | None,
    ) -> set[str]: ...
    def max_event_pos(
        self,
        room_ids: list[str],
        now_token: RoomStreamToken,
        last_change_pos: dict[str, int],
    ) -> dict[str, int | None]: ...
    def last_event_pos_before(
        self,
        room_ids: list[str],
        end_token: RoomStreamToken,
        now_token: RoomStreamToken,
        last_change_pos: dict[str, int],
    ) -> dict[str, int]: ...
    def cached_member_counts(self, room_id: str) -> dict[str, int]: ...
    def room_type(self, room_id: str) -> tuple[bool, str | None]:
        """`(known, room_type)`: not known is `ROOM_UNKNOWN_SENTINEL`."""
    def room_version_id(self, room_id: str) -> str | None: ...
    def partial_rooms(self) -> set[str]: ...
    def tags_for_user(self, user_id: str) -> dict[str, dict[str, JsonMapping]]: ...
    def is_encrypted_at(
        self, room_id: str, to_token: RoomStreamToken, now_token: RoomStreamToken
    ) -> bool: ...
    def connection_lazy_members(
        self, connection_position: int, room_id: str, user_ids: list[str]
    ) -> dict[str, int]: ...
    def load_connection_state(
        self,
        user_id: str,
        device_id: str,
        conn_id: str,
        connection_position: int,
        now_ms: int,
    ) -> tuple[
        int | None,
        dict[str, _PyStatus],
        dict[str, _PyStatus],
        dict[str, _PyStatus],
        dict[str, _PyConfig],
    ]:
        """`(last_used_ts, rooms, receipts, account_data, room_configs)`."""
    def persist_connection_state(
        self,
        user_id: str,
        device_id: str,
        conn_id: str,
        previous_position: int | None,
        *,
        rooms: dict[str, _PyStatus],
        receipts: dict[str, _PyStatus],
        account_data: dict[str, _PyStatus],
        room_configs: dict[str, _PyConfig],
        room_lazy_membership: dict[str, tuple[dict[str, int | None], set[str]]],
        now_ms: int,
    ) -> int: ...

def multi_writer_token_round_trip_for_test(
    token: MultiWriterStreamToken, instances: list[tuple[int, str]]
) -> tuple[str, MultiWriterStreamToken]:
    """A receipts token's database form, as Rust writes it, and the token Rust
    parses back from it."""
