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

from typing import List, Optional

from synapse._pydantic_compat import Field, StrictStr, ValidationError, validator
from synapse.types import JsonDict
from synapse.types.rest import RequestBodyModel
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


class MTextRepresentation(RequestBodyModel):
    body: StrictStr
    mimetype: Optional[StrictStr]


class MTopic(RequestBodyModel):
    m_text: Optional[List[MTextRepresentation]] = Field(alias="m.text")

    @validator("m_text", pre=True)
    def ignore_invalid_representations(
        cls, m_text: any
    ) -> Optional[List[MTextRepresentation]]:
        if not isinstance(m_text, list):
            raise ValueError("m.text must be a list")
        representations = []
        for element in m_text:
            try:
                representations.append(MTextRepresentation.parse_obj(element))
            except ValidationError:
                continue
        return representations


class TopicContent(RequestBodyModel):
    topic: StrictStr
    m_topic: Optional[MTopic] = Field(alias="m.topic", default=None)

    @validator("m_topic", pre=True)
    def ignore_invalid_m_topic(cls, m_topic: any) -> Optional[MTopic]:
        try:
            return MTopic.parse_obj(m_topic)
        except ValidationError:
            return None


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

    try:
        topic_content: TopicContent = TopicContent.parse_obj(content)
    except ValidationError:
        return None

    if not topic_content.m_topic or not topic_content.m_topic.m_text:
        return topic_content.topic

    # Find the first `text/plain` topic ("Receivers SHOULD use the first
    # representationin the array that they understand.")
    representation = next(
        (
            r
            for r in topic_content.m_topic.m_text
            if not r.mimetype or r.mimetype == "text/plain"
        ),
        None,
    )

    if not representation:
        return topic_content.topic

    return representation.body
