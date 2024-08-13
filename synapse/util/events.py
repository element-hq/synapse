#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

from synapse.util.stringutils import random_string


def generate_fake_event_id() -> str:
    """
    Generate an event ID from random ASCII characters.

    This is primarily useful for generating fake event IDs in response to
    requests from shadow-banned users.

    Returns:
        A string intended to look like an event ID, but with no actual meaning.
    """
    return "$" + random_string(43)
