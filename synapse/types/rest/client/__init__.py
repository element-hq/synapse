#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

from synapse._pydantic_compat import HAS_PYDANTIC_V2

if TYPE_CHECKING or HAS_PYDANTIC_V2:
    from pydantic.v1 import (
        Extra,
        StrictBool,
        StrictInt,
        StrictStr,
        conint,
        constr,
        validator,
    )
else:
    from pydantic import (
        Extra,
        StrictBool,
        StrictInt,
        StrictStr,
        conint,
        constr,
        validator,
    )

from synapse.types.rest import RequestBodyModel
from synapse.util.threepids import validate_email


class AuthenticationData(RequestBodyModel):
    """
    Data used during user-interactive authentication.

    (The name "Authentication Data" is taken directly from the spec.)

    Additional keys will be present, depending on the `type` field. Use
    `.dict(exclude_unset=True)` to access them.
    """

    class Config:
        extra = Extra.allow

    session: Optional[StrictStr] = None
    type: Optional[StrictStr] = None


if TYPE_CHECKING:
    ClientSecretStr = StrictStr
else:
    # See also assert_valid_client_secret()
    ClientSecretStr = constr(
        regex="[0-9a-zA-Z.=_-]",  # noqa: F722
        min_length=1,
        max_length=255,
        strict=True,
    )


class ThreepidRequestTokenBody(RequestBodyModel):
    client_secret: ClientSecretStr
    id_server: Optional[StrictStr]
    id_access_token: Optional[StrictStr]
    next_link: Optional[StrictStr]
    send_attempt: StrictInt

    @validator("id_access_token", always=True)
    def token_required_for_identity_server(
        cls, token: Optional[str], values: Dict[str, object]
    ) -> Optional[str]:
        if values.get("id_server") is not None and token is None:
            raise ValueError("id_access_token is required if an id_server is supplied.")
        return token


class EmailRequestTokenBody(ThreepidRequestTokenBody):
    email: StrictStr

    # Canonicalise the email address. The addresses are all stored canonicalised
    # in the database. This allows the user to reset his password without having to
    # know the exact spelling (eg. upper and lower case) of address in the database.
    # Without this, an email stored in the database as "foo@bar.com" would cause
    # user requests for "FOO@bar.com" to raise a Not Found error.
    _email_validator = validator("email", allow_reuse=True)(validate_email)


if TYPE_CHECKING:
    ISO3116_1_Alpha_2 = StrictStr
else:
    # Per spec: two-letter uppercase ISO-3166-1-alpha-2
    ISO3116_1_Alpha_2 = constr(regex="[A-Z]{2}", strict=True)


class MsisdnRequestTokenBody(ThreepidRequestTokenBody):
    country: ISO3116_1_Alpha_2
    phone_number: StrictStr


class SlidingSyncBody(RequestBodyModel):
    """
    Sliding Sync API request body.

    Attributes:
        lists: Sliding window API. A map of list key to list information
            (:class:`SlidingSyncList`). Max lists: 100. The list keys should be
            arbitrary strings which the client is using to refer to the list. Keep this
            small as it needs to be sent a lot. Max length: 64 bytes.
        room_subscriptions: Room subscription API. A map of room ID to room subscription
            information. Used to subscribe to a specific room. Sometimes clients know
            exactly which room they want to get information about e.g by following a
            permalink or by refreshing a webapp currently viewing a specific room. The
            sliding window API alone is insufficient for this use case because there's
            no way to say "please track this room explicitly".
        extensions: Extensions API. A map of extension key to extension config.
    """

    class CommonRoomParameters(RequestBodyModel):
        """
        Common parameters shared between the sliding window and room subscription APIs.

        Attributes:
            required_state: Required state for each room returned. An array of event
                type and state key tuples. Elements in this array are ORd together to
                produce the final set of state events to return. One unique exception is
                when you request all state events via `["*", "*"]`. When used, all state
                events are returned by default, and additional entries FILTER OUT the
                returned set of state events. These additional entries cannot use `*`
                themselves. For example, `["*", "*"], ["m.room.member",
                "@alice:example.com"]` will *exclude* every `m.room.member` event
                *except* for `@alice:example.com`, and include every other state event.
                In addition, `["*", "*"], ["m.space.child", "*"]` is an error, the
                `m.space.child` filter is not required as it would have been returned
                anyway.
            timeline_limit: The maximum number of timeline events to return per response.
                (Max 1000 messages)
            include_old_rooms: Determines if `predecessor` rooms are included in the
                `rooms` response. The user MUST be joined to old rooms for them to show up
                in the response.
        """

        class IncludeOldRooms(RequestBodyModel):
            timeline_limit: StrictInt
            required_state: List[Tuple[StrictStr, StrictStr]]

        required_state: List[Tuple[StrictStr, StrictStr]]
        # mypy workaround via https://github.com/pydantic/pydantic/issues/156#issuecomment-1130883884
        if TYPE_CHECKING:
            timeline_limit: int
        else:
            timeline_limit: conint(le=1000, strict=True)  # type: ignore[valid-type]
        include_old_rooms: Optional[IncludeOldRooms] = None

    class SlidingSyncList(CommonRoomParameters):
        """
        Attributes:
            ranges: Sliding window ranges. If this field is missing, no sliding window
                is used and all rooms are returned in this list. Integers are
                *inclusive*.
            slow_get_all_rooms: Just get all rooms (for clients that don't want to deal with
                sliding windows). When true, the `ranges` and `sort` fields are ignored.
            required_state: Required state for each room returned. An array of event
                type and state key tuples. Elements in this array are ORd together to
                produce the final set of state events to return.

                One unique exception is when you request all state events via `["*",
                "*"]`. When used, all state events are returned by default, and
                additional entries FILTER OUT the returned set of state events. These
                additional entries cannot use `*` themselves. For example, `["*", "*"],
                ["m.room.member", "@alice:example.com"]` will *exclude* every
                `m.room.member` event *except* for `@alice:example.com`, and include
                every other state event. In addition, `["*", "*"], ["m.space.child",
                "*"]` is an error, the `m.space.child` filter is not required as it
                would have been returned anyway.

                Room members can be lazily-loaded by using the special `$LAZY` state key
                (`["m.room.member", "$LAZY"]`). Typically, when you view a room, you
                want to retrieve all state events except for m.room.member events which
                you want to lazily load. To get this behaviour, clients can send the
                following::

                    {
                        "required_state": [
                            // activate lazy loading
                            ["m.room.member", "$LAZY"],
                            // request all state events _except_ for m.room.member
                            events which are lazily loaded
                            ["*", "*"]
                        ]
                    }

            timeline_limit: The maximum number of timeline events to return per response.
            include_old_rooms: Determines if `predecessor` rooms are included in the
                `rooms` response. The user MUST be joined to old rooms for them to show up
                in the response.
            include_heroes: Return a stripped variant of membership events (containing
                `user_id` and optionally `avatar_url` and `displayname`) for the users used
                to calculate the room name.
            filters: Filters to apply to the list before sorting.
            bump_event_types: Allowlist of event types which should be considered recent activity
                when sorting `by_recency`. By omitting event types from this field,
                clients can ensure that uninteresting events (e.g. a profile rename) do
                not cause a room to jump to the top of its list(s). Empty or omitted
                `bump_event_types` have no effect—all events in a room will be
                considered recent activity.
        """

        class Filters(RequestBodyModel):
            is_dm: Optional[StrictBool] = None
            spaces: Optional[List[StrictStr]] = None
            is_encrypted: Optional[StrictBool] = None
            is_invite: Optional[StrictBool] = None
            room_types: Optional[List[Union[StrictStr, None]]] = None
            not_room_types: Optional[List[StrictStr]] = None
            room_name_like: Optional[StrictStr] = None
            tags: Optional[List[StrictStr]] = None
            not_tags: Optional[List[StrictStr]] = None

        # mypy workaround via https://github.com/pydantic/pydantic/issues/156#issuecomment-1130883884
        if TYPE_CHECKING:
            ranges: Optional[List[Tuple[int, int]]] = None
        else:
            ranges: Optional[List[Tuple[conint(ge=0, strict=True), conint(ge=0, strict=True)]]] = None  # type: ignore[valid-type]
        slow_get_all_rooms: Optional[StrictBool] = False
        include_heroes: Optional[StrictBool] = False
        filters: Optional[Filters] = None
        bump_event_types: Optional[List[StrictStr]] = None

    class RoomSubscription(CommonRoomParameters):
        pass

    class Extension(RequestBodyModel):
        enabled: Optional[StrictBool] = False
        lists: Optional[List[StrictStr]] = None
        rooms: Optional[List[StrictStr]] = None

    # mypy workaround via https://github.com/pydantic/pydantic/issues/156#issuecomment-1130883884
    if TYPE_CHECKING:
        lists: Optional[Dict[str, SlidingSyncList]] = None
    else:
        lists: Optional[Dict[constr(max_length=64, strict=True), SlidingSyncList]] = None  # type: ignore[valid-type]
    room_subscriptions: Optional[Dict[StrictStr, RoomSubscription]] = None
    extensions: Optional[Dict[StrictStr, Extension]] = None

    @validator("lists")
    def lists_length_check(
        cls, value: Optional[Dict[str, SlidingSyncList]]
    ) -> Optional[Dict[str, SlidingSyncList]]:
        if value is not None:
            assert len(value) <= 100, f"Max lists: 100 but saw {len(value)}"
        return value
