#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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

"""Defines all the valid streams that clients can subscribe to, and the format
of the rows returned by each stream.

Each stream is defined by the following information:

    stream name:        The name of the stream
    row type:           The type that is used to serialise/deserialse the row
    current_token:      The function that returns the current token for the stream
    update_function:    The function that returns a list of updates between two tokens
"""

from synapse.replication.tcp.streams._base import (
    AccountDataStream,
    BackfillStream,
    CachesStream,
    DeviceListsStream,
    PresenceFederationStream,
    PresenceStream,
    PushersStream,
    PushRulesStream,
    ReceiptsStream,
    Stream,
    ThreadSubscriptionsStream,
    ToDeviceStream,
    TypingStream,
)
from synapse.replication.tcp.streams.events import EventsStream
from synapse.replication.tcp.streams.federation import FederationStream
from synapse.replication.tcp.streams.partial_state import (
    UnPartialStatedEventStream,
    UnPartialStatedRoomStream,
)

STREAMS_MAP = {
    stream.NAME: stream
    for stream in (
        EventsStream,
        BackfillStream,
        PresenceStream,
        PresenceFederationStream,
        TypingStream,
        ReceiptsStream,
        PushRulesStream,
        PushersStream,
        CachesStream,
        DeviceListsStream,
        ToDeviceStream,
        FederationStream,
        AccountDataStream,
        ThreadSubscriptionsStream,
        UnPartialStatedRoomStream,
        UnPartialStatedEventStream,
    )
}

__all__ = [
    "STREAMS_MAP",
    "Stream",
    "EventsStream",
    "BackfillStream",
    "PresenceStream",
    "PresenceFederationStream",
    "TypingStream",
    "ReceiptsStream",
    "PushRulesStream",
    "PushersStream",
    "CachesStream",
    "DeviceListsStream",
    "ToDeviceStream",
    "FederationStream",
    "AccountDataStream",
    "ThreadSubscriptionsStream",
    "UnPartialStatedRoomStream",
    "UnPartialStatedEventStream",
]
