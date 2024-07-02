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
from typing import TYPE_CHECKING, Sequence, Tuple

import attr

from synapse.handlers.account_data import AccountDataEventSource
from synapse.handlers.presence import PresenceEventSource
from synapse.handlers.receipts import ReceiptEventSource
from synapse.handlers.room import RoomEventSource
from synapse.handlers.typing import TypingNotificationEventSource
from synapse.logging.opentracing import trace
from synapse.streams import EventSource
from synapse.types import (
    AbstractMultiWriterStreamToken,
    MultiWriterStreamToken,
    StreamKeyType,
    StreamToken,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


@attr.s(frozen=True, slots=True, auto_attribs=True)
class _EventSourcesInner:
    room: RoomEventSource
    presence: PresenceEventSource
    typing: TypingNotificationEventSource
    receipt: ReceiptEventSource
    account_data: AccountDataEventSource

    def get_sources(self) -> Sequence[Tuple[StreamKeyType, EventSource]]:
        return [
            (StreamKeyType.ROOM, self.room),
            (StreamKeyType.PRESENCE, self.presence),
            (StreamKeyType.TYPING, self.typing),
            (StreamKeyType.RECEIPT, self.receipt),
            (StreamKeyType.ACCOUNT_DATA, self.account_data),
        ]


class EventSources:
    def __init__(self, hs: "HomeServer"):
        self.sources = _EventSourcesInner(
            # attribute.type is `Optional`, but we know it's
            # never `None` here since all the attributes of `_EventSourcesInner` are
            # annotated.
            *(
                attribute.type(hs)  # type: ignore[misc]
                for attribute in attr.fields(_EventSourcesInner)
            )
        )
        self.store = hs.get_datastores().main
        self._instance_name = hs.get_instance_name()

    def get_current_token(self) -> StreamToken:
        push_rules_key = self.store.get_max_push_rules_stream_id()
        to_device_key = self.store.get_to_device_stream_token()
        device_list_key = self.store.get_device_stream_token()
        un_partial_stated_rooms_key = self.store.get_un_partial_stated_rooms_token(
            self._instance_name
        )

        token = StreamToken(
            room_key=self.sources.room.get_current_key(),
            presence_key=self.sources.presence.get_current_key(),
            typing_key=self.sources.typing.get_current_key(),
            receipt_key=self.sources.receipt.get_current_key(),
            account_data_key=self.sources.account_data.get_current_key(),
            push_rules_key=push_rules_key,
            to_device_key=to_device_key,
            device_list_key=device_list_key,
            # Groups key is unused.
            groups_key=0,
            un_partial_stated_rooms_key=un_partial_stated_rooms_key,
        )
        return token

    async def bound_future_token(self, token: StreamToken) -> StreamToken:
        """Bound a token that is ahead of the current token to the maximum
        persisted values.

        This ensures that if we wait for the given token we know the stream will
        eventually advance to that point.

        This works around a bug where older Synapse versions will give out
        tokens for streams, and then after a restart will give back tokens where
        the stream has "gone backwards".
        """

        current_token = self.get_current_token()

        stream_key_to_id_gen = {
            StreamKeyType.ROOM: self.store.get_events_stream_id_generator(),
            StreamKeyType.PRESENCE: self.store.get_presence_stream_id_gen(),
            StreamKeyType.RECEIPT: self.store.get_receipts_stream_id_gen(),
            StreamKeyType.ACCOUNT_DATA: self.store.get_account_data_id_generator(),
            StreamKeyType.PUSH_RULES: self.store.get_push_rules_stream_id_gen(),
            StreamKeyType.TO_DEVICE: self.store.get_to_device_id_generator(),
            StreamKeyType.DEVICE_LIST: self.store.get_device_stream_id_generator(),
            StreamKeyType.UN_PARTIAL_STATED_ROOMS: self.store.get_un_partial_stated_rooms_id_generator(),
        }

        for _, key in StreamKeyType.__members__.items():
            if key == StreamKeyType.TYPING:
                # Typing stream is allowed to "reset", and so comparisons don't
                # really make sense as is.
                # TODO: Figure out a better way of tracking resets.
                continue

            token_value = token.get_field(key)
            current_value = current_token.get_field(key)

            if isinstance(token_value, AbstractMultiWriterStreamToken):
                assert type(current_value) is type(token_value)

                if not token_value.is_before_or_eq(current_value):  # type: ignore[arg-type]
                    max_token = await stream_key_to_id_gen[
                        key
                    ].get_max_allocated_token()

                    if max_token < token_value.get_max_stream_pos():
                        logger.error(
                            "Bounding token from the future '%s': token: %s, bound: %s",
                            key,
                            token_value,
                            max_token,
                        )
                        token = token.copy_and_replace(
                            key, token_value.bound_stream_token(max_token)
                        )
            else:
                assert isinstance(current_value, int)
                if current_value < token_value:
                    max_token = await stream_key_to_id_gen[
                        key
                    ].get_max_allocated_token()

                    if max_token < token_value:
                        logger.error(
                            "Bounding token from the future '%s': token: %s, bound: %s",
                            key,
                            token_value,
                            max_token,
                        )
                        token = token.copy_and_replace(key, max_token)

        return token

    @trace
    async def get_start_token_for_pagination(self, room_id: str) -> StreamToken:
        """Get the start token for a given room to be used to paginate
        events.

        The returned token does not have the current values for fields other
        than `room`, since they are not used during pagination.

        Returns:
            The start token for pagination.
        """
        return StreamToken.START

    @trace
    async def get_current_token_for_pagination(self, room_id: str) -> StreamToken:
        """Get the current token for a given room to be used to paginate
        events.

        The returned token does not have the current values for fields other
        than `room`, since they are not used during pagination.

        Returns:
            The current token for pagination.
        """
        token = StreamToken(
            room_key=await self.sources.room.get_current_key_for_room(room_id),
            presence_key=0,
            typing_key=0,
            receipt_key=MultiWriterStreamToken(stream=0),
            account_data_key=0,
            push_rules_key=0,
            to_device_key=0,
            device_list_key=0,
            groups_key=0,
            un_partial_stated_rooms_key=0,
        )
        return token
