#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from typing import Optional

from synapse.api.constants import Direction
from synapse.types import RoomStreamToken


def generate_next_token(
    direction: Direction, last_topo_ordering: Optional[int], last_stream_ordering: int
) -> RoomStreamToken:
    """
    Generate the next room stream token based on the currently returned data.

    Args:
        direction: Whether pagination is going forwards or backwards.
        last_topo_ordering: The last topological ordering being returned.
        last_stream_ordering: The last stream ordering being returned.

    Returns:
        A new RoomStreamToken to return to the client.
    """
    if direction == Direction.BACKWARDS:
        # Tokens are positions between events.
        # This token points *after* the last event in the chunk.
        # We need it to point to the event before it in the chunk
        # when we are going backwards so we subtract one from the
        # stream part.
        last_stream_ordering -= 1

        # TODO: Is this okay to do? Kinda seems more correct
        if last_topo_ordering is not None:
            last_topo_ordering -= 1

    return RoomStreamToken(topological=last_topo_ordering, stream=last_stream_ordering)
