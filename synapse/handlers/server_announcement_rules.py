#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import TYPE_CHECKING, Sequence

from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import AuthError, NotFoundError
from synapse.events.snapshot import EventPersistencePair
from synapse.storage.databases.main.events_worker import EventRedactBehaviour
from synapse.types import Requester

if TYPE_CHECKING:
    from synapse.server import HomeServer


class ServerAnnouncementRules:
    """Checks specifically for events sent in local server announcement rooms."""

    def __init__(self, hs: "HomeServer") -> None:
        self._store = hs.get_datastores().main
        self._config = hs.config.serverannouncements
        self._server_name = hs.hostname

    async def check_events(
        self, requester: Requester, events_and_context: Sequence[EventPersistencePair]
    ) -> None:
        """Check that events may be sent in a local server announcement room.

        Prevent users other than those configured from sending announcements.
        Also prevent state changes, apart from membership and internal management
        events, since the rooms are managed through homeserver configuration.
        """
        if not self._config.rooms:
            return

        batched_events = {event.event_id: event for event, _ in events_and_context}
        for event, _ in events_and_context:
            announcement_id = await self._store.get_server_announcement_id(
                event.room_id
            )
            # Get the configuration for this announcement room.
            room = next(
                (room for room in self._config.rooms if room.id == announcement_id),
                None,
            )
            if room is None:
                continue

            creator = self._config.server_announcements_mxid
            is_sent_by_server_announcements_mxid = (
                event.sender == creator
                and requester.user.to_string() == creator
                and requester.authenticated_entity == self._server_name
            )

            if event.type == EventTypes.Member:
                # Even server-authenticated requests must not invite or join an
                # unconfigured user. Only the internal creator's own join is exempt.
                if (
                    event.membership in (Membership.INVITE, Membership.JOIN)
                    and event.state_key not in room.allowed_user_ids
                    and not (
                        is_sent_by_server_announcements_mxid
                        and event.state_key == creator
                        and event.membership == Membership.JOIN
                    )
                ):
                    raise AuthError(
                        403, "User is not allowed in this server announcement room"
                    )

                if is_sent_by_server_announcements_mxid:
                    continue

                # Allow self-joins so the homeserver can join configured users on
                # their behalf, and self-leaves so it can remove users during room
                # shutdown. These transitions also let allowed users update their
                # own membership or leave voluntarily.
                if (
                    event.sender != creator
                    and event.state_key == event.sender
                    and event.membership in (Membership.JOIN, Membership.LEAVE)
                ):
                    continue

                raise AuthError(
                    403, "Server announcement room membership is managed by the server"
                )

            if event.is_state():
                if is_sent_by_server_announcements_mxid:
                    continue
                raise AuthError(
                    403, "Server announcement room state is managed by the server"
                )

            # The creator is an internal state-management identity, and is
            # not listed in `room.allowed_user_ids`.
            if event.sender == creator or event.sender not in room.allowed_user_ids:
                raise AuthError(
                    403, "Sender is not allowed in this server announcement room"
                )

            # Prevent redaction of any state events.
            if event.type == EventTypes.Redaction:
                assert event.redacts is not None
                original = batched_events.get(event.redacts)
                if original is None:
                    original = await self._store.get_event(
                        event.redacts,
                        redact_behaviour=EventRedactBehaviour.as_is,
                        get_prev_content=False,
                        allow_rejected=False,
                        allow_none=True,
                    )
                if original is None:
                    raise NotFoundError("Could not find event %s" % (event.redacts,))
                if original.is_state():
                    raise AuthError(
                        403, "Server announcement room state cannot be redacted"
                    )
