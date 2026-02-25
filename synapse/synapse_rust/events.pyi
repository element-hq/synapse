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

from typing import Mapping

from synapse.types import JsonDict

class EventInternalMetadata:
    def __init__(self, internal_metadata_dict: JsonDict): ...

    stream_ordering: int | None
    """the stream ordering of this event. None, until it has been persisted."""
    instance_name: str | None
    """the instance name of the server that persisted this event. None, until it has been persisted."""

    outlier: bool
    """whether this event is an outlier (ie, whether we have the state at that
    point in the DAG)"""

    out_of_band_membership: bool
    send_on_behalf_of: str
    recheck_redaction: bool
    soft_failed: bool
    proactively_send: bool
    redacted: bool

    policy_server_spammy: bool
    """whether the policy server indicated that this event is spammy"""

    txn_id: str
    """The transaction ID, if it was set when the event was created."""
    token_id: int
    """The access token ID of the user who sent this event, if any."""
    device_id: str
    """The device ID of the user who sent this event, if any."""

    def get_dict(self) -> JsonDict: ...
    def is_outlier(self) -> bool: ...
    def copy(self) -> "EventInternalMetadata": ...
    def is_out_of_band_membership(self) -> bool:
        """Whether this event is an out-of-band membership.

        OOB memberships are a special case of outlier events: they are membership events
        for federated rooms that we aren't full members of. Examples include invites
        received over federation, and rejections for such invites.

        The concept of an OOB membership is needed because these events need to be
        processed as if they're new regular events (e.g. updating membership state in
        the database, relaying to clients via /sync, etc) despite being outliers.

        See also https://element-hq.github.io/synapse/develop/development/room-dag-concepts.html#out-of-band-membership-events.

        (Added in synapse 0.99.0, so may be unreliable for events received before that)
        """

    def get_send_on_behalf_of(self) -> str | None:
        """Whether this server should send the event on behalf of another server.
        This is used by the federation "send_join" API to forward the initial join
        event for a server in the room.

        returns a str with the name of the server this event is sent on behalf of.
        """

    def need_to_check_redaction(self) -> bool:
        """Whether the redaction event needs to be rechecked when fetching
        from the database.

        Starting in room v3 redaction events are accepted up front, and later
        checked to see if the redacter and redactee's domains match.

        If the sender of the redaction event is allowed to redact any event
        due to auth rules, then this will always return false.
        """

    def is_soft_failed(self) -> bool:
        """Whether the event has been soft failed.

        Soft failed events should be handled as usual, except:
            1. They should not go down sync or event streams, or generally
               sent to clients.
            2. They should not be added to the forward extremities (and
               therefore not to current state).
        """

    def should_proactively_send(self) -> bool:
        """Whether the event, if ours, should be sent to other clients and
        servers.

        This is used for sending dummy events internally. Servers and clients
        can still explicitly fetch the event.
        """

    def is_redacted(self) -> bool:
        """Whether the event has been redacted.

        This is used for efficiently checking whether an event has been
        marked as redacted without needing to make another database call.
        """

    def is_notifiable(self) -> bool:
        """Whether this event can trigger a push notification"""

def event_visible_to_server(
    sender: str,
    target_server_name: str,
    history_visibility: str,
    erased_senders: Mapping[str, bool],
    partial_state_invisible: bool,
    memberships: list[tuple[str, str]],
) -> bool:
    """Determine whether the server is allowed to see the unredacted event.

    Args:
        sender: The sender of the event.
        target_server_name: The server we want to send the event to.
        history_visibility: The history_visibility value at the event.
        erased_senders: A mapping of users and whether they have requested erasure. If a
            user is not in the map, it is treated as though they haven't requested erasure.
        partial_state_invisible: Whether the event should be treated as invisible due to
            the partial state status of the room.
        memberships: A list of membership state information at the event for users
            matching the `target_server_name`. Each list item must contain a tuple of
            (state_key, membership).

    Returns:
        Whether the server is allowed to see the unredacted event.
    """
