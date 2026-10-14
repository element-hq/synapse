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

from synapse.synapse_rust.events import Event
from synapse.types import JsonDict, JsonMapping, RoomStreamToken

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
