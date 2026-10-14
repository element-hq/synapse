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

from collections.abc import Mapping

from synapse.synapse_rust.database.postgres import ConnectionPool
from synapse.synapse_rust.events import Event
from synapse.types import JsonDict, JsonMapping, RoomStreamToken
from synapse.types.state import StateFilter

class RustSyncFallback(Exception):
    """The Rust sync handler cannot compute this room's entry. Compute it
    with the Python implementation."""

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
