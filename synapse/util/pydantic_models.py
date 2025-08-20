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

import re
from typing import Any, Callable, Generator

from synapse._pydantic_compat import BaseModel, Extra, StrictStr
from synapse.types import EventID


class ParseModel(BaseModel):
    """A custom version of Pydantic's BaseModel which

     - ignores unknown fields and
     - does not allow fields to be overwritten after construction,

    but otherwise uses Pydantic's default behaviour.

    For now, ignore unknown fields. In the future, we could change this so that unknown
    config values cause a ValidationError, provided the error messages are meaningful to
    server operators.

    Subclassing in this way is recommended by
    https://pydantic-docs.helpmanual.io/usage/model_config/#change-behaviour-globally
    """

    class Config:
        # By default, ignore fields that we don't recognise.
        extra = Extra.ignore
        # By default, don't allow fields to be reassigned after parsing.
        allow_mutation = False


class AnyEventId(StrictStr):
    """
    A validator for strings that need to be an Event ID.

    Accepts any valid grammar of Event ID from any room version.
    """

    EVENT_ID_HASH_ROOM_VERSION_3_PLUS = re.compile(
        r"^([a-zA-Z0-9-_]{43}|[a-zA-Z0-9+/]{43})$"
    )

    @classmethod
    def __get_validators__(cls) -> Generator[Callable[..., Any], Any, Any]:
        yield from super().__get_validators__()  # type: ignore
        yield cls.validate_event_id

    @classmethod
    def validate_event_id(cls, value: str) -> str:
        if not value.startswith("$"):
            raise ValueError("Event ID must start with `$`")

        if ":" in value:
            # Room versions 1 and 2
            EventID.from_string(value)  # throws on fail
        else:
            # Room versions 3+: event ID is $ + a base64 sha256 hash
            # Room version 3 is base64, 4+ are base64Url
            # In both cases, the base64 is unpadded.
            # refs:
            # - https://spec.matrix.org/v1.15/rooms/v3/ e.g. $acR1l0raoZnm60CBwAVgqbZqoO/mYU81xysh1u7XcJk
            # - https://spec.matrix.org/v1.15/rooms/v4/ e.g. $Rqnc-F-dvnEYJTyHq_iKxU2bZ1CI92-kuZq3a5lr5Zg
            b64_hash = value[1:]
            if cls.EVENT_ID_HASH_ROOM_VERSION_3_PLUS.fullmatch(b64_hash) is None:
                raise ValueError(
                    "Event ID must either have a domain part or be a valid hash"
                )

        return value
