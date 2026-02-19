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
from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from typing_extensions import Annotated, Self

from synapse.types.rest import RequestBodyModel
from synapse.util.threepids import validate_email


class AuthenticationData(RequestBodyModel):
    """
    Data used during user-interactive authentication.

    (The name "Authentication Data" is taken directly from the spec.)

    Additional keys will be present, depending on the `type` field. Use
    `.dict(exclude_unset=True)` to access them.
    """

    model_config = ConfigDict(extra="allow")

    session: StrictStr | None = None
    type: StrictStr | None = None


# See also assert_valid_client_secret()
ClientSecretStr = Annotated[
    str,
    StringConstraints(
        pattern="[0-9a-zA-Z.=_-]",
        min_length=1,
        max_length=255,
        strict=True,
    ),
]


class ThreepidRequestTokenBody(RequestBodyModel):
    client_secret: ClientSecretStr
    id_server: StrictStr | None = None
    id_access_token: StrictStr | None = None
    next_link: StrictStr | None = None
    send_attempt: StrictInt

    @model_validator(mode="after")
    def token_required_for_identity_server(self) -> Self:
        if self.id_server is not None and self.id_access_token is None:
            raise ValueError("id_access_token is required if an id_server is supplied.")
        return self


class EmailRequestTokenBody(ThreepidRequestTokenBody):
    email: StrictStr

    # Canonicalise the email address. The addresses are all stored canonicalised
    # in the database. This allows the user to reset his password without having to
    # know the exact spelling (eg. upper and lower case) of address in the database.
    # Without this, an email stored in the database as "foo@bar.com" would cause
    # user requests for "FOO@bar.com" to raise a Not Found error.
    @field_validator("email")
    @classmethod
    def _email_validator(cls, email: StrictStr) -> StrictStr:
        try:
            return validate_email(email)
        except ValueError as e:
            # To ensure backward compatibility of HTTP error codes, we return a
            # Pydantic error with the custom, unrecognized error type
            # "email_custom_err_type" instead of the default error type
            # "value_error". This results in the more generic BAD_JSON HTTP
            # error instead of the more specific INVALID_PARAM one.
            raise PydanticCustomError("email_custom_err_type", str(e), None) from e


ISO3116_1_Alpha_2 = Annotated[str, StringConstraints(pattern="[A-Z]{2}", strict=True)]


class MsisdnRequestTokenBody(ThreepidRequestTokenBody):
    country: ISO3116_1_Alpha_2
    phone_number: StrictStr


class SlidingSyncBody(RequestBodyModel):
    """
    Sliding Sync API request body.

    Attributes:
        conn_id: An optional string to identify this connection to the server.
            Only one sliding sync connection is allowed per given conn_id (empty
            or not).
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
        """

        required_state: list[
            Annotated[tuple[StrictStr, StrictStr], Field(strict=False)]
        ]
        timeline_limit: Annotated[int, Field(le=1000, strict=True)]

    class SlidingSyncList(CommonRoomParameters):
        """
        Attributes:
            ranges: Sliding window ranges. If this field is missing, no sliding window
                is used and all rooms are returned in this list. Integers are
                *inclusive*.
            slow_get_all_rooms: Just get all rooms (for clients that don't want to deal with
                sliding windows). When true, the `ranges` field is ignored.
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
            filters: Filters to apply to the list before sorting.
        """

        class Filters(RequestBodyModel):
            """
            All fields are applied with AND operators, hence if `is_dm: True` and
            `is_encrypted: True` then only Encrypted DM rooms will be returned. The
            absence of fields implies no filter on that criteria: it does NOT imply
            `False`. These fields may be expanded through use of extensions.

            Attributes:
                is_dm: Flag which only returns rooms present (or not) in the DM section
                    of account data. If unset, both DM rooms and non-DM rooms are returned.
                    If False, only non-DM rooms are returned. If True, only DM rooms are
                    returned.
                spaces: Filter the room based on the space they belong to according to
                    `m.space.child` state events. If multiple spaces are present, a room can
                    be part of any one of the listed spaces (OR'd). The server will inspect
                    the `m.space.child` state events for the JOINED space room IDs given.
                    Servers MUST NOT navigate subspaces. It is up to the client to give a
                    complete list of spaces to navigate. Only rooms directly mentioned as
                    `m.space.child` events in these spaces will be returned. Unknown spaces
                    or spaces the user is not joined to will be ignored.
                is_encrypted: Flag which only returns rooms which have an
                    `m.room.encryption` state event. If unset, both encrypted and
                    unencrypted rooms are returned. If `False`, only unencrypted rooms are
                    returned. If `True`, only encrypted rooms are returned.
                is_invite: Flag which only returns rooms the user is currently invited
                    to. If unset, both invited and joined rooms are returned. If `False`, no
                    invited rooms are returned. If `True`, only invited rooms are returned.
                room_types: If specified, only rooms where the `m.room.create` event has
                    a `type` matching one of the strings in this array will be returned. If
                    this field is unset, all rooms are returned regardless of type. This can
                    be used to get the initial set of spaces for an account. For rooms which
                    do not have a room type, use `null`/`None` to include them.
                not_room_types: Same as `room_types` but inverted. This can be used to
                    filter out spaces from the room list. If a type is in both `room_types`
                    and `not_room_types`, then `not_room_types` wins and they are not included
                    in the result.
                room_name_like: Filter the room name. Case-insensitive partial matching
                    e.g 'foo' matches 'abFooab'. The term 'like' is inspired by SQL 'LIKE',
                    and the text here is similar to '%foo%'.
                tags: Filter the room based on its room tags. If multiple tags are
                    present, a room can have any one of the listed tags (OR'd).
                not_tags: Filter the room based on its room tags. Takes priority over
                    `tags`. For example, a room with tags A and B with filters `tags: [A]`
                    `not_tags: [B]` would NOT be included because `not_tags` takes priority over
                    `tags`. This filter is useful if your rooms list does NOT include the
                    list of favourite rooms again.
            """

            is_dm: StrictBool | None = None
            spaces: list[StrictStr] | None = None
            is_encrypted: StrictBool | None = None
            is_invite: StrictBool | None = None
            room_types: list[StrictStr | None] | None = None
            not_room_types: list[StrictStr | None] | None = None
            room_name_like: StrictStr | None = None
            tags: list[StrictStr] | None = None
            not_tags: list[StrictStr] | None = None

        ranges: (
            list[
                Annotated[
                    tuple[
                        Annotated[int, Field(ge=0, strict=True)],
                        Annotated[int, Field(ge=0, strict=True)],
                    ],
                    Field(strict=False),
                ]
            ]
            | None
        ) = None
        slow_get_all_rooms: StrictBool | None = False
        filters: Filters | None = None

    class RoomSubscription(CommonRoomParameters):
        pass

    class Extensions(RequestBodyModel):
        """The extensions section of the request.

        Extensions MUST have an `enabled` flag which defaults to `false`. If a client
        sends an unknown extension name, the server MUST ignore it (or else backwards
        compatibility between clients and servers is broken when a newer client tries to
        communicate with an older server).
        """

        class ToDeviceExtension(RequestBodyModel):
            """The to-device extension (MSC3885)

            Attributes:
                enabled
                limit: Maximum number of to-device messages to return
                since: The `next_batch` from the previous sync response
            """

            enabled: StrictBool | None = False
            limit: StrictInt = 100
            since: StrictStr | None = None

            @field_validator("since")
            @classmethod
            def since_token_check(cls, value: StrictStr | None) -> StrictStr | None:
                # `since` comes in as an opaque string token but we know that it's just
                # an integer representing the position in the device inbox stream. We
                # want to pre-validate it to make sure it works fine in downstream code.
                if value is None:
                    return value

                try:
                    int(value)
                except ValueError:
                    raise ValueError(
                        "'extensions.to_device.since' is invalid (should look like an int)"
                    )

                return value

        class E2eeExtension(RequestBodyModel):
            """The E2EE device extension (MSC3884)

            Attributes:
                enabled
            """

            enabled: StrictBool | None = False

        class AccountDataExtension(RequestBodyModel):
            """The Account Data extension (MSC3959)

            Attributes:
                enabled
                lists: List of list keys (from the Sliding Window API) to apply this
                    extension to.
                rooms: List of room IDs (from the Room Subscription API) to apply this
                    extension to.
            """

            enabled: StrictBool | None = False
            # Process all lists defined in the Sliding Window API. (This is the default.)
            lists: list[StrictStr] | None = ["*"]
            # Process all room subscriptions defined in the Room Subscription API. (This is the default.)
            rooms: list[StrictStr] | None = ["*"]

        class ReceiptsExtension(RequestBodyModel):
            """The Receipts extension (MSC3960)

            Attributes:
                enabled
                lists: List of list keys (from the Sliding Window API) to apply this
                    extension to.
                rooms: List of room IDs (from the Room Subscription API) to apply this
                    extension to.
            """

            enabled: StrictBool | None = False
            # Process all lists defined in the Sliding Window API. (This is the default.)
            lists: list[StrictStr] | None = ["*"]
            # Process all room subscriptions defined in the Room Subscription API. (This is the default.)
            rooms: list[StrictStr] | None = ["*"]

        class TypingExtension(RequestBodyModel):
            """The Typing Notification extension (MSC3961)

            Attributes:
                enabled
                lists: List of list keys (from the Sliding Window API) to apply this
                    extension to.
                rooms: List of room IDs (from the Room Subscription API) to apply this
                    extension to.
            """

            enabled: StrictBool | None = False
            # Process all lists defined in the Sliding Window API. (This is the default.)
            lists: list[StrictStr] | None = ["*"]
            # Process all room subscriptions defined in the Room Subscription API. (This is the default.)
            rooms: list[StrictStr] | None = ["*"]

        class ThreadSubscriptionsExtension(RequestBodyModel):
            """The Thread Subscriptions extension (MSC4308)

            Attributes:
                enabled
                limit: maximum number of subscription changes to return (default 100)
            """

            enabled: StrictBool | None = False
            limit: StrictInt = 100

        to_device: ToDeviceExtension | None = None
        e2ee: E2eeExtension | None = None
        account_data: AccountDataExtension | None = None
        receipts: ReceiptsExtension | None = None
        typing: TypingExtension | None = None
        thread_subscriptions: ThreadSubscriptionsExtension | None = Field(
            None, alias="io.element.msc4308.thread_subscriptions"
        )

    conn_id: StrictStr | None = None
    lists: (
        dict[
            Annotated[str, StringConstraints(max_length=64, strict=True)],
            SlidingSyncList,
        ]
        | None
    ) = None
    room_subscriptions: dict[StrictStr, RoomSubscription] | None = None
    extensions: Extensions | None = None

    @field_validator("lists")
    @classmethod
    def lists_length_check(
        cls, value: dict[str, SlidingSyncList] | None
    ) -> dict[str, SlidingSyncList] | None:
        if value is not None:
            assert len(value) <= 100, f"Max lists: 100 but saw {len(value)}"
        return value
