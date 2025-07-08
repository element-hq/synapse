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

from typing import Optional

from synapse.api.constants import EventContentFields, MTextFields
from synapse.types import JsonDict
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


def get_plain_text_topic_from_event_content(content: JsonDict) -> Optional[str]:
    """
    Given the `content` of an `m.room.topic` event, returns the plain-text topic
    representation. Prefers pulling plain-text from the newer `m.topic` field if
    available with a fallback to `topic`.

    Args:
        content: The `content` field of an `m.room.topic` event.

    Returns:
        A string representing the plain text topic.
    """
    topic = content.get(EventContentFields.TOPIC)

    m_topic = content.get(EventContentFields.M_TOPIC)
    if not m_topic:
        return topic

    m_text = m_topic.get(EventContentFields.M_TEXT)
    if not m_text:
        return topic

    # Find the first `text/plain` topic ("Receivers SHOULD use the first
    # representationin the array that they understand.")
    representation = next(
        (
            r
            for r in m_text
            if (
                MTextFields.MIMETYPE not in r or r[MTextFields.MIMETYPE] == "text/plain"
            )
            and MTextFields.BODY in r
            # Scrutinize user input
            and isinstance(r[MTextFields.BODY], str)
        ),
        None,
    )
    if not representation:
        return topic

    return representation[MTextFields.BODY]
