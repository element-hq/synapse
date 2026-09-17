#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
import logging
from enum import Enum
from typing import Awaitable, Callable, Collection

import attr

from synapse.events import EventBase

logger = logging.getLogger(__name__)


class FederatedEventDeliveryMethod(str, Enum):
    """
    Method by which an event was 'delivered' to another server.

    Note that depending on the specific method, delivery may not have
    actually been acknowledged by the other homeserver.

    Modules should anticipate more methods being added to this enum
    over time (it is non-exhaustive).

    Only methods that deliver full, signed PDUs are included in this mechanism.
    Some notable examples of excluded endpoints:
       - `/send_knock` is excluded as it only returns unsigned 'stripped state'.
       - `/timestamp_to_event` is excluded as it only returns event IDs, not events themselves.
    """

    SEND = "/send"
    """
    The events were pushed over [`/send`](https://spec.matrix.org/v1.19/server-server-api/#put_matrixfederationv1sendtxnid).

    When a callback is triggered with this method, the events have been acknowledged
    without error.
    """

    BACKFILL = "/backfill"
    """
    The events were pulled over [`/backfill`](https://spec.matrix.org/v1.19/server-server-api/#get_matrixfederationv1backfillroomid).

    When a callback is triggered with this method, the events have _not_ been
    acknowledged by the remote.
    Actual delivery depends on network conditions and other factors influencing
    the successful processing of the response at the remote homeserver.
    """

    GET_MISSING_EVENTS = "/get_missing_events"
    """
    The events were pulled over [`/get_missing_events`](https://spec.matrix.org/v1.19/server-server-api/#post_matrixfederationv1get_missing_eventsroomid).

    When a callback is triggered with this method, the events have _not_ been
    acknowledged by the remote.
    Actual delivery depends on network conditions and other factors influencing
    the successful processing of the response at the remote homeserver.
    """

    EVENT = "/event"
    """
    The event was pulled over [`/event`](https://spec.matrix.org/v1.19/server-server-api/#get_matrixfederationv1eventeventid).

    When a callback is triggered with this method, the events have _not_ been
    acknowledged by the remote.
    Actual delivery depends on network conditions and other factors influencing
    the successful processing of the response at the remote homeserver.
    """

    EVENT_AUTH = "/event_auth"
    """
    The events were pulled over [`/event_auth`](https://spec.matrix.org/v1.19/server-server-api/#get_matrixfederationv1event_authroomideventid).

    When a callback is triggered with this method, the events have _not_ been
    acknowledged by the remote.
    Actual delivery depends on network conditions and other factors influencing
    the successful processing of the response at the remote homeserver.
    """

    STATE = "/state"
    """
    The events were pulled over [`/state`](https://spec.matrix.org/v1.19/server-server-api/#get_matrixfederationv1stateroomid).

    When a callback is triggered with this method, the events have _not_ been
    acknowledged by the remote.
    Actual delivery depends on network conditions and other factors influencing
    the successful processing of the response at the remote homeserver.
    """

    SEND_JOIN = "/send_join"
    """
    The events were pulled over [`/send_join`](https://spec.matrix.org/v1.19/server-server-api/#put_matrixfederationv2send_joinroomideventid).

    When a callback is triggered with this method, the events have _not_ been
    acknowledged by the remote.
    Actual delivery depends on network conditions and other factors influencing
    the successful processing of the response at the remote homeserver.
    """


@attr.s(frozen=True, slots=True, auto_attribs=True)
class FederationEventDeliveryEvent:
    """
    Represents the delivery of some events.

    Note that depending on `method`,
    delivery may not be acknowledged.
    """

    server_name: str
    """
    The server name of the destination the events were delivered to.
    """

    events: Collection[EventBase]
    """
    The events that were delivered.

    Modules should not rely on this being the exhaustive list of all events that
    were delivered in a single request;
    delivery hooks may be triggered in multiple batches.
    """

    method: FederatedEventDeliveryMethod
    """
    How the events were delivered to the server.
    """


ON_EVENT_DELIVERED_OVER_FEDERATION_CALLBACK = Callable[
    [FederationEventDeliveryEvent], Awaitable[None]
]


class FederationModuleApiCallbacks:
    """
    Module API callbacks for generic federation events.
    """

    def __init__(self) -> None:
        self._on_event_delivered_over_federation_callbacks: list[
            ON_EVENT_DELIVERED_OVER_FEDERATION_CALLBACK
        ] = []

    def interested_in_events_delivered_over_federation(self) -> bool:
        """
        Whether any `on_event_delivered_over_federation` callbacks are registered.
        """
        return len(self._on_event_delivered_over_federation_callbacks) > 0

    def register_callbacks(
        self,
        on_event_delivered_over_federation: ON_EVENT_DELIVERED_OVER_FEDERATION_CALLBACK
        | None = None,
    ) -> None:
        """
        Register callbacks from module for each hook.

        on_event_delivered_over_federation:
            Callback fired when an event is delivered over federation.
            See `FederationEventDeliveryEvent` for details.

            Performance note:
                Registering this hook causes a performance (caching) optimisation on the
                Federation `/state` endpoint to be bypassed.
        """
        if on_event_delivered_over_federation is not None:
            self._on_event_delivered_over_federation_callbacks.append(
                on_event_delivered_over_federation
            )

    async def notify_on_event_delivered_over_federation(
        self,
        server_name: str,
        events: Collection[EventBase],
        method: FederatedEventDeliveryMethod,
    ) -> None:
        """Fire the registered callbacks to notify modules that some events were
        delivered to another homeserver over federation.

        Does nothing if no callbacks are registered or if there are no events to
        report. A callback that raises is logged and does not interrupt the others.
        """
        if not events or not self._on_event_delivered_over_federation_callbacks:
            return

        delivery = FederationEventDeliveryEvent(
            server_name=server_name,
            events=events,
            method=method,
        )
        for callback in self._on_event_delivered_over_federation_callbacks:
            try:
                await callback(delivery)
            except Exception:
                logger.exception(
                    "Error running on_event_delivered_over_federation callback"
                )
