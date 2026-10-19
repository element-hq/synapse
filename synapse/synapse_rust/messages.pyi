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

"""`/messages` in Rust. See `rust/src/messages/`."""

from collections.abc import Set

from twisted.internet.defer import Deferred

from synapse.events import EventBase
from synapse.handlers.relations import BundledAggregations
from synapse.types import JsonMapping, RoomStreamToken

class RustMessagesFallback(Exception):
    """The request is one Rust leaves to Python."""

class MessagesRequest:
    """One `/messages` request, after the auth checks and token handling of
    `PaginationHandler.get_messages`."""

    def __init__(
        self,
        *,
        room_id: str,
        user_id: str,
        from_key: RoomStreamToken,
        to_key: RoomStreamToken | None,
        leave_key: RoomStreamToken | None,
        direction: str,
        limit: int,
        is_peeking: bool,
        lazy_load_members: bool,
        event_filter: JsonMapping | None,
        ignored_users: Set[str],
        is_server_admin: bool,
        return_soft_failed_events: bool,
        return_policy_server_spammy_events: bool,
        msc3874_enabled: bool,
        msc4354_enabled: bool,
    ) -> None:
        """`event_filter` is the filter's JSON. It may not filter on
        relations (`related_by_senders`, `related_by_rel_types`)."""
    def with_curr_topo(self, curr_topo: int) -> "MessagesRequest":
        """This request with the topological position of its `from` token
        known, so that reading the page again does not work it out again."""

class MessagesResult:
    """A page of events computed by Rust."""

    @property
    def events(self) -> list[tuple[EventBase, str]]:
        """The events the user may see as `(event, membership)` pairs, in the
        order of the page."""
    @property
    def bundled_aggregations(self) -> dict[str, BundledAggregations]: ...
    @property
    def state(self) -> list[EventBase] | None:
        """The lazy-loaded members, when asked for and there are any."""
    @property
    def start_key(self) -> RoomStreamToken:
        """The room key of the response's `start` token: the request's, or
        the user's leave."""
    @property
    def curr_topo(self) -> int | None:
        """Going backwards, the topological position of the request's `from`
        token, which backfill starts from."""
    @property
    def next_key(self) -> RoomStreamToken:
        """The room key of the response's `end` token."""
    @property
    def end_of_history(self) -> bool:
        """Whether the page is empty with nothing after it: the response has
        no `end` token."""
    @property
    def needs_backfill(self) -> bool:
        """Whether Python should backfill before answering, going backwards."""

class MessagesHandler:
    """Computes `/messages` pages in Rust, reading the database through the
    native Postgres driver. Built by `RustHandlers`."""

    def get_messages(self, request: MessagesRequest) -> Deferred[MessagesResult]:
        """The page of `request`.

        Raises:
            RustMessagesFallback if Python has to compute it.
        """
    def get_messages_blocking(self, request: MessagesRequest) -> MessagesResult: ...
