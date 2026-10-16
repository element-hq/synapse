#
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

class StreamIdTracker:
    """The in-memory bookkeeping of a multi-writer stream, for
    `MultiWriterIdGenerator`: which IDs this instance has allocated and not
    finished, each writer's position, and from those the current token.

    IDs are stored positive, also for streams that count down. `advance` and
    the `get_*` methods take and return signed IDs. The bookkeeping methods
    (`load`, `begin_fetch`, `end_fetch`, `mark_finished`) take positive IDs,
    as the sequence returns them.
    """

    def __init__(
        self, stream_name: str, instance_name: str, positive: bool
    ) -> None: ...
    def load(
        self,
        current_positions: dict[str, int],
        persisted_upto_position: int,
        persisted_rows: list[tuple[str | None, int]],
        writers: list[str],
    ) -> None:
        """Set the start state from the database: each writer's position, the
        persisted position, the rows above it sorted by stream ID, and the
        configured writers."""

    def begin_fetch(self) -> int:
        """Start allocating IDs. Returns a token for `end_fetch`."""

    def end_fetch(self, token: int, allocated: list[int] | None) -> None:
        """Finish allocating IDs: the IDs allocated, or None on failure."""

    def mark_finished(self, ids: list[int]) -> None:
        """The transactions that wrote these IDs have finished."""

    def advance(self, instance_name: str, new_id: int) -> None: ...
    def get_persisted_upto_position(self) -> int: ...
    def get_current_token_for_writer(self, instance_name: str) -> int: ...
    def get_minimal_local_current_token(self) -> int: ...
    def get_positions(self) -> dict[str, int]: ...
    def persisted_upto_position_abs(self) -> int:
        """The persisted position, positive also for streams that count down."""
