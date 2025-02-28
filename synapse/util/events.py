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


def get_plain_text_topic_from_event_content(content: JsonDict):
    """
    Given the content of an m.room.topic event returns the plain text topic
    representation if any exists.

    Returns:
        A string representing the plain text topic.
    """
    topic = content.get("topic")

    m_topic = content.get("m.topic")
    if not m_topic:
        return topic

    m_text = m_topic.get("m.text")
    if not m_text:
        return topic

    representation = next(
        (r for r in m_text if "mimetype" not in r or r["mimetype"] == "text/plain"),
        None,
    )
    if not representation or "body" not in representation:
        return topic

    return representation["body"]
