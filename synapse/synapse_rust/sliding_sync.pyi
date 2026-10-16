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

from synapse.types import JsonDict, RoomStreamToken
from synapse.types.state import StateFilter

class SlidingSyncHandler:
    """Computes sliding sync responses in Rust, reading the database through
    the native Postgres driver. Built by `RustHandlers`."""

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
