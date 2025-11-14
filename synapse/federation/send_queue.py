#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

"""A federation sender that forwards things to be sent across replication to
a worker process.

It assumes there is a single worker process feeding off of it.

Each row in the replication stream consists of a type and some json, where the
types indicate whether they are presence, or edus, etc.

Ephemeral or non-event data are queued up in-memory. When the worker requests
updates since a particular point, all in-memory data since before that point is
dropped. We also expire things in the queue after 5 minutes, to ensure that a
dead worker doesn't cause the queues to grow limitlessly.

Events are replicated via a separate events stream.
"""

import logging
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Hashable,
    Iterable,
    Sized,
)

import attr
from sortedcontainers import SortedDict

from synapse.api.presence import UserPresenceState
from synapse.federation.sender import AbstractFederationSender, FederationSender
from synapse.metrics import SERVER_NAME_LABEL, LaterGauge
from synapse.replication.tcp.streams.federation import FederationStream
from synapse.types import JsonDict, ReadReceipt, RoomStreamToken, StrCollection
from synapse.util.metrics import Measure

from .units import Edu

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class QueueNames(str, Enum):
    PRESENCE_MAP = "presence_map"
    KEYED_EDU = "keyed_edu"
    KEYED_EDU_CHANGED = "keyed_edu_changed"
    EDUS = "edus"
    POS_TIME = "pos_time"
    PRESENCE_DESTINATIONS = "presence_destinations"


queue_name_to_gauge_map: dict[QueueNames, LaterGauge] = {}

for queue_name in QueueNames:
    queue_name_to_gauge_map[queue_name] = LaterGauge(
        name=f"synapse_federation_send_queue_{queue_name.value}_size",
        desc="",
        labelnames=[SERVER_NAME_LABEL],
    )


class FederationRemoteSendQueue(AbstractFederationSender):
    """A drop in replacement for FederationSender"""

    def __init__(self, hs: "HomeServer"):
        self.server_name = hs.hostname
        self.clock = hs.get_clock()
        self.notifier = hs.get_notifier()
        self.is_mine_id = hs.is_mine_id
        self.is_mine_server_name = hs.is_mine_server_name

        # We may have multiple federation sender instances, so we need to track
        # their positions separately.
        self._sender_instances = hs.config.worker.federation_shard_config.instances
        self._sender_positions: dict[str, int] = {}

        # Pending presence map user_id -> UserPresenceState
        self.presence_map: dict[str, UserPresenceState] = {}

        # Stores the destinations we need to explicitly send presence to about a
        # given user.
        # Stream position -> (user_id, destinations)
        self.presence_destinations: SortedDict[int, tuple[str, Iterable[str]]] = (
            SortedDict()
        )

        # (destination, key) -> EDU
        self.keyed_edu: dict[tuple[str, tuple], Edu] = {}

        # stream position -> (destination, key)
        self.keyed_edu_changed: SortedDict[int, tuple[str, tuple]] = SortedDict()

        self.edus: SortedDict[int, Edu] = SortedDict()

        # stream ID for the next entry into keyed_edu_changed/edus.
        self.pos = 1

        # map from stream ID to the time that stream entry was generated, so that we
        # can clear out entries after a while
        self.pos_time: SortedDict[int, int] = SortedDict()

        # EVERYTHING IS SAD. In particular, python only makes new scopes when
        # we make a new function, so we need to make a new function so the inner
        # lambda binds to the queue rather than to the name of the queue which
        # changes. ARGH.
        def register(queue_name: QueueNames, queue: Sized) -> None:
            queue_name_to_gauge_map[queue_name].register_hook(
                homeserver_instance_id=hs.get_instance_id(),
                hook=lambda: {(self.server_name,): len(queue)},
            )

        for queue_name in QueueNames:
            queue = getattr(self, queue_name.value)
            assert isinstance(queue, Sized)
            register(queue_name, queue=queue)

        self.clock.looping_call(self._clear_queue, 30 * 1000)

    def shutdown(self) -> None:
        """Stops this federation sender instance from sending further transactions."""

    def _next_pos(self) -> int:
        pos = self.pos
        self.pos += 1
        self.pos_time[self.clock.time_msec()] = pos
        return pos

    def _clear_queue(self) -> None:
        """Clear the queues for anything older than N minutes"""

        FIVE_MINUTES_AGO = 5 * 60 * 1000
        now = self.clock.time_msec()

        keys = self.pos_time.keys()
        time = self.pos_time.bisect_left(now - FIVE_MINUTES_AGO)
        if not keys[:time]:
            return

        position_to_delete = max(keys[:time])
        for key in keys[:time]:
            del self.pos_time[key]

        self._clear_queue_before_pos(position_to_delete)

    def _clear_queue_before_pos(self, position_to_delete: int) -> None:
        """Clear all the queues from before a given position"""
        with Measure(
            self.clock, name="send_queue._clear", server_name=self.server_name
        ):
            # Delete things out of presence maps
            keys = self.presence_destinations.keys()
            i = self.presence_destinations.bisect_left(position_to_delete)
            for key in keys[:i]:
                del self.presence_destinations[key]

            user_ids = {user_id for user_id, _ in self.presence_destinations.values()}

            to_del = [
                user_id for user_id in self.presence_map if user_id not in user_ids
            ]
            for user_id in to_del:
                del self.presence_map[user_id]

            # Delete things out of keyed edus
            keys = self.keyed_edu_changed.keys()
            i = self.keyed_edu_changed.bisect_left(position_to_delete)
            for key in keys[:i]:
                del self.keyed_edu_changed[key]

            live_keys = set()
            for edu_key in self.keyed_edu_changed.values():
                live_keys.add(edu_key)

            keys_to_del = [
                edu_key for edu_key in self.keyed_edu if edu_key not in live_keys
            ]
            for edu_key in keys_to_del:
                del self.keyed_edu[edu_key]

            # Delete things out of edu map
            keys = self.edus.keys()
            i = self.edus.bisect_left(position_to_delete)
            for key in keys[:i]:
                del self.edus[key]

    def notify_new_events(self, max_token: RoomStreamToken) -> None:
        """As per FederationSender"""
        # This should never get called.
        raise NotImplementedError()

    def build_and_send_edu(
        self,
        destination: str,
        edu_type: str,
        content: JsonDict,
        key: Hashable | None = None,
    ) -> None:
        """As per FederationSender"""
        if self.is_mine_server_name(destination):
            logger.info("Not sending EDU to ourselves")
            return

        pos = self._next_pos()

        edu = Edu(
            origin=self.server_name,
            destination=destination,
            edu_type=edu_type,
            content=content,
        )

        if key:
            assert isinstance(key, tuple)
            self.keyed_edu[(destination, key)] = edu
            self.keyed_edu_changed[pos] = (destination, key)
        else:
            self.edus[pos] = edu

        self.notifier.on_new_replication_data()

    async def send_read_receipt(self, receipt: ReadReceipt) -> None:
        """As per FederationSender

        Args:
            receipt:
        """
        # nothing to do here: the replication listener will handle it.

    async def send_presence_to_destinations(
        self, states: Iterable[UserPresenceState], destinations: Iterable[str]
    ) -> None:
        """As per FederationSender

        Args:
            states
            destinations
        """
        for state in states:
            pos = self._next_pos()
            self.presence_map.update({state.user_id: state for state in states})
            self.presence_destinations[pos] = (state.user_id, destinations)

        self.notifier.on_new_replication_data()

    async def send_device_messages(
        self, destinations: StrCollection, immediate: bool = True
    ) -> None:
        """As per FederationSender"""
        # We don't need to replicate this as it gets sent down a different
        # stream.

    def wake_destination(self, server: str) -> None:
        pass

    def get_current_token(self) -> int:
        return self.pos - 1

    def federation_ack(self, instance_name: str, token: int) -> None:
        if self._sender_instances:
            # If we have configured multiple federation sender instances we need
            # to track their positions separately, and only clear the queue up
            # to the token all instances have acked.
            self._sender_positions[instance_name] = token
            token = min(self._sender_positions.values())

        self._clear_queue_before_pos(token)

    async def get_replication_rows(
        self, instance_name: str, from_token: int, to_token: int, target_row_count: int
    ) -> tuple[list[tuple[int, tuple]], int, bool]:
        """Get rows to be sent over federation between the two tokens

        Args:
            instance_name: the name of the current process
            from_token: the previous stream token: the starting point for fetching the
                updates
            to_token: the new stream token: the point to get updates up to
            target_row_count: a target for the number of rows to be returned.

        Returns: a triplet `(updates, new_last_token, limited)`, where:
           * `updates` is a list of `(token, row)` entries.
           * `new_last_token` is the new position in stream.
           * `limited` is whether there are more updates to fetch.
        """
        # TODO: Handle target_row_count.

        # To handle restarts where we wrap around
        if from_token > self.pos:
            from_token = -1

        # list of tuple(int, BaseFederationRow), where the first is the position
        # of the federation stream.
        rows: list[tuple[int, BaseFederationRow]] = []

        # Fetch presence to send to destinations
        i = self.presence_destinations.bisect_right(from_token)
        j = self.presence_destinations.bisect_right(to_token) + 1

        for pos, (user_id, dests) in self.presence_destinations.items()[i:j]:
            rows.append(
                (
                    pos,
                    PresenceDestinationsRow(
                        state=self.presence_map[user_id], destinations=list(dests)
                    ),
                )
            )

        # Fetch changes keyed edus
        i = self.keyed_edu_changed.bisect_right(from_token)
        j = self.keyed_edu_changed.bisect_right(to_token) + 1
        # We purposefully clobber based on the key here, python dict comprehensions
        # always use the last value, so this will correctly point to the last
        # stream position.
        keyed_edus = {v: k for k, v in self.keyed_edu_changed.items()[i:j]}

        for (destination, edu_key), pos in keyed_edus.items():
            rows.append(
                (
                    pos,
                    KeyedEduRow(
                        key=edu_key, edu=self.keyed_edu[(destination, edu_key)]
                    ),
                )
            )

        # Fetch changed edus
        i = self.edus.bisect_right(from_token)
        j = self.edus.bisect_right(to_token) + 1
        edus = self.edus.items()[i:j]

        for pos, edu in edus:
            rows.append((pos, EduRow(edu)))

        # Sort rows based on pos
        rows.sort()

        return (
            [(pos, (row.TypeId, row.to_data())) for pos, row in rows],
            to_token,
            False,
        )


class BaseFederationRow:
    """Base class for rows to be sent in the federation stream.

    Specifies how to identify, serialize and deserialize the different types.
    """

    TypeId = ""  # Unique string that ids the type. Must be overridden in sub classes.

    @staticmethod
    def from_data(data: JsonDict) -> "BaseFederationRow":
        """Parse the data from the federation stream into a row.

        Args:
            data: The value of ``data`` from FederationStreamRow.data, type
                depends on the type of stream
        """
        raise NotImplementedError()

    def to_data(self) -> JsonDict:
        """Serialize this row to be sent over the federation stream.

        Returns:
            The value to be sent in FederationStreamRow.data. The type depends
            on the type of stream.
        """
        raise NotImplementedError()

    def add_to_buffer(self, buff: "ParsedFederationStreamData") -> None:
        """Add this row to the appropriate field in the buffer ready for this
        to be sent over federation.

        We use a buffer so that we can batch up events that have come in at
        the same time and send them all at once.

        Args:
            buff (BufferedToSend)
        """
        raise NotImplementedError()


@attr.s(slots=True, frozen=True, auto_attribs=True)
class PresenceDestinationsRow(BaseFederationRow):
    state: UserPresenceState
    destinations: list[str]

    TypeId = "pd"

    @staticmethod
    def from_data(data: JsonDict) -> "PresenceDestinationsRow":
        return PresenceDestinationsRow(
            state=UserPresenceState(**data["state"]), destinations=data["dests"]
        )

    def to_data(self) -> JsonDict:
        return {"state": self.state.as_dict(), "dests": self.destinations}

    def add_to_buffer(self, buff: "ParsedFederationStreamData") -> None:
        buff.presence_destinations.append((self.state, self.destinations))


@attr.s(slots=True, frozen=True, auto_attribs=True)
class KeyedEduRow(BaseFederationRow):
    """Streams EDUs that have an associated key that is ued to clobber. For example,
    typing EDUs clobber based on room_id.
    """

    key: tuple[str, ...]  # the edu key passed to send_edu
    edu: Edu

    TypeId = "k"

    @staticmethod
    def from_data(data: JsonDict) -> "KeyedEduRow":
        return KeyedEduRow(key=tuple(data["key"]), edu=Edu(**data["edu"]))

    def to_data(self) -> JsonDict:
        return {"key": self.key, "edu": self.edu.get_internal_dict()}

    def add_to_buffer(self, buff: "ParsedFederationStreamData") -> None:
        buff.keyed_edus.setdefault(self.edu.destination, {})[self.key] = self.edu


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EduRow(BaseFederationRow):
    """Streams EDUs that don't have keys. See KeyedEduRow"""

    edu: Edu

    TypeId = "e"

    @staticmethod
    def from_data(data: JsonDict) -> "EduRow":
        return EduRow(Edu(**data))

    def to_data(self) -> JsonDict:
        return self.edu.get_internal_dict()

    def add_to_buffer(self, buff: "ParsedFederationStreamData") -> None:
        buff.edus.setdefault(self.edu.destination, []).append(self.edu)


_rowtypes: tuple[type[BaseFederationRow], ...] = (
    PresenceDestinationsRow,
    KeyedEduRow,
    EduRow,
)

TypeToRow = {Row.TypeId: Row for Row in _rowtypes}


@attr.s(slots=True, frozen=True, auto_attribs=True)
class ParsedFederationStreamData:
    # list of tuples of UserPresenceState and destinations
    presence_destinations: list[tuple[UserPresenceState, list[str]]]
    # dict of destination -> { key -> Edu }
    keyed_edus: dict[str, dict[tuple[str, ...], Edu]]
    # dict of destination -> [Edu]
    edus: dict[str, list[Edu]]


async def process_rows_for_federation(
    transaction_queue: FederationSender,
    rows: list[FederationStream.FederationStreamRow],
) -> None:
    """Parse a list of rows from the federation stream and put them in the
    transaction queue ready for sending to the relevant homeservers.

    Args:
        transaction_queue
        rows
    """

    # The federation stream contains a bunch of different types of
    # rows that need to be handled differently. We parse the rows, put
    # them into the appropriate collection and then send them off.

    buff = ParsedFederationStreamData(
        presence_destinations=[],
        keyed_edus={},
        edus={},
    )

    # Parse the rows in the stream and add to the buffer
    for row in rows:
        if row.type not in TypeToRow:
            logger.error("Unrecognized federation row type %r", row.type)
            continue

        RowType = TypeToRow[row.type]
        parsed_row = RowType.from_data(row.data)
        parsed_row.add_to_buffer(buff)

    for state, destinations in buff.presence_destinations:
        await transaction_queue.send_presence_to_destinations(
            states=[state], destinations=destinations
        )

    for edu_map in buff.keyed_edus.values():
        for key, edu in edu_map.items():
            transaction_queue.send_edu(edu, key)

    for edu_list in buff.edus.values():
        for edu in edu_list:
            transaction_queue.send_edu(edu, None)
