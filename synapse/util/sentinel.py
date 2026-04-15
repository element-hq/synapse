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

import enum


class Sentinel(enum.Enum):
    """
    Internal marker sentinel for distinguishing a default state from user-suppliable values.
    Has no meaning on its own.

    Use this when you want to be absolutely sure that the marker came from Synapse code
    and not from request body parsing.

    If you want a Pydantic-compatible Sentinel that is suitable for expressing
    'absent from some parsed JSON payload' or equivalent, see `Absent`.
    """

    # defining a sentinel in this way allows mypy to correctly handle the
    # type of a dictionary lookup and subsequent type narrowing.
    UNSET_SENTINEL = object()
