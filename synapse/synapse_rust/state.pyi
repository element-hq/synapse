# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import Collection, Sequence

from synapse.synapse_rust.database.postgres import ConnectionPool
from synapse.synapse_rust.events import Event

def get_auth_chain_difference_for_test(
    pool: ConnectionPool,
    room_id: str,
    state_sets: Sequence[Collection[str]],
    conflicted: Collection[str] | None = None,
    additional_backwards: Collection[str] | None = None,
) -> tuple[set[str], set[str] | None]:
    """The auth chain difference computed by the Rust store with the chain
    cover index, as `_get_auth_chain_difference_using_cover_index_txn` does.
    For tests."""

def load_events_for_test(
    pool: ConnectionPool, event_ids: Sequence[str]
) -> dict[str, Event]:
    """Events loaded by the Rust store. For tests."""
