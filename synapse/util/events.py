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

from typing import Any

from pydantic import Field, StrictStr, ValidationError, field_validator

from synapse.types import JsonDict
from synapse.util.pydantic_models import ParseModel
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


class MTextRepresentation(ParseModel):
    """
    See `TextualRepresentation` in the Matrix specification.
    """

    body: StrictStr
    mimetype: StrictStr | None = None


class MTopic(ParseModel):
    """
    `m.room.topic` -> `content` -> `m.topic`

    Textual representation of the room topic in different mimetypes. Added in Matrix v1.15.

    See `TopicContentBlock` in the Matrix specification.
    """

    m_text: list[MTextRepresentation] | None = Field(None, alias="m.text")
    """
    An ordered array of textual representations in different mimetypes.
    """

    # Because "Receivers SHOULD use the first representation in the array that they
    # understand.", we ignore invalid representations in the `m.text` field and use
    # what we can.
    @field_validator("m_text", mode="before")
    @classmethod
    def ignore_invalid_representations(
        cls, m_text: Any
    ) -> list[MTextRepresentation] | None:
        if not isinstance(m_text, (list, tuple)):
            raise ValueError("m.text must be a list or a tuple")
        representations = []
        for element in m_text:
            try:
                representations.append(MTextRepresentation.model_validate(element))
            except ValidationError:
                continue
        return representations


class TopicContent(ParseModel):
    """
    Represents the `content` field of an `m.room.topic` event
    """

    topic: StrictStr
    """
    The topic in plain text.
    """

    m_topic: MTopic | None = Field(None, alias="m.topic")
    """
    Textual representation of the room topic in different mimetypes.
    """

    # We ignore invalid `m.topic` fields as we can always fall back to the plain-text
    # `topic` field.
    @field_validator("m_topic", mode="before")
    @classmethod
    def ignore_invalid_m_topic(cls, m_topic: Any) -> MTopic | None:
        try:
            return MTopic.model_validate(m_topic)
        except ValidationError:
            return None


def get_plain_text_topic_from_event_content(content: JsonDict) -> str | None:
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
        topic_content = TopicContent.model_validate(content, strict=False)
    except ValidationError:
        return None

    # Find the first `text/plain` topic ("Receivers SHOULD use the first
    # representationin the array that they understand.")
    if topic_content.m_topic and topic_content.m_topic.m_text:
        for representation in topic_content.m_topic.m_text:
            # The mimetype property defaults to `text/plain` if omitted.
            if not representation.mimetype or representation.mimetype == "text/plain":
                return representation.body

    # Fallback to the plain-old `topic` field if there isn't any `text/plain` topic
    # representation available.
    return topic_content.topic
