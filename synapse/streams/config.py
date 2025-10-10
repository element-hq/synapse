#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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
import logging
from typing import Optional

import attr

from synapse.api.constants import Direction
from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_enum, parse_integer, parse_string
from synapse.http.site import SynapseRequest
from synapse.storage.databases.main import DataStore
from synapse.types import StreamToken

logger = logging.getLogger(__name__)


MAX_LIMIT = 1000


@attr.s(slots=True, auto_attribs=True)
class PaginationConfig:
    """A configuration object which stores pagination parameters."""

    from_token: Optional[StreamToken]
    to_token: Optional[StreamToken]
    direction: Direction
    limit: int

    @classmethod
    async def from_request(
        cls,
        store: "DataStore",
        request: SynapseRequest,
        default_limit: int,
        default_dir: Direction = Direction.FORWARDS,
    ) -> "PaginationConfig":
        direction = parse_enum(request, "dir", Direction, default=default_dir)

        from_tok_str = parse_string(request, "from")
        to_tok_str = parse_string(request, "to")

        # Helper function to extract StreamToken from either StreamToken or SlidingSyncStreamToken format
        def extract_stream_token(token_str: str) -> str:
            """
            Extract the StreamToken portion from a token string.

            Handles both:
            - StreamToken format: "s123_456_..."
            - SlidingSyncStreamToken format: "5/s123_456_..." (extracts part after /)

            This allows clients using sliding sync to use their pos tokens
            with endpoints like /relations and /messages.
            """
            if "/" in token_str:
                # SlidingSyncStreamToken format: "connection_position/stream_token"
                # Split and return just the stream_token part
                parts = token_str.split("/", 1)
                if len(parts) == 2:
                    return parts[1]
            return token_str

        try:
            from_tok = None
            if from_tok_str == "END":
                from_tok = None  # For backwards compat.
            elif from_tok_str:
                stream_token_str = extract_stream_token(from_tok_str)
                from_tok = await StreamToken.from_string(store, stream_token_str)
        except Exception:
            raise SynapseError(400, "'from' parameter is invalid")

        try:
            to_tok = None
            if to_tok_str:
                stream_token_str = extract_stream_token(to_tok_str)
                to_tok = await StreamToken.from_string(store, stream_token_str)
        except Exception:
            raise SynapseError(400, "'to' parameter is invalid")

        limit = parse_integer(request, "limit", default=default_limit)
        limit = min(limit, MAX_LIMIT)

        try:
            return PaginationConfig(from_tok, to_tok, direction, limit)
        except Exception:
            logger.exception("Failed to create pagination config")
            raise SynapseError(400, "Invalid request.")

    def __repr__(self) -> str:
        return "PaginationConfig(from_tok=%r, to_tok=%r, direction=%r, limit=%r)" % (
            self.from_token,
            self.to_token,
            self.direction,
            self.limit,
        )
