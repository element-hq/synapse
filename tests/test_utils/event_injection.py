#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C
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
from typing import Any, List, Optional, Tuple

import synapse.server
from synapse.api.constants import EventTypes
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.events import EventBase
from synapse.events.snapshot import EventContext

"""
Utility functions for poking events into the storage of the server under test.
"""


async def inject_member_event(
    hs: synapse.server.HomeServer,
    room_id: str,
    sender: str,
    membership: str,
    target: Optional[str] = None,
    extra_content: Optional[dict] = None,
    **kwargs: Any,
) -> EventBase:
    """Inject a membership event into a room."""
    if target is None:
        target = sender

    content = {"membership": membership}
    if extra_content:
        content.update(extra_content)

    return await inject_event(
        hs,
        room_id=room_id,
        type=EventTypes.Member,
        sender=sender,
        state_key=target,
        content=content,
        **kwargs,
    )


async def inject_event(
    hs: synapse.server.HomeServer,
    room_version: Optional[str] = None,
    prev_event_ids: Optional[List[str]] = None,
    **kwargs: Any,
) -> EventBase:
    """Inject a generic event into a room

    Args:
        hs: the homeserver under test
        room_version: the version of the room we're inserting into.
            if not specified, will be looked up
        prev_event_ids: prev_events for the event. If not specified, will be looked up
        kwargs: fields for the event to be created
    """
    event, context = await create_event(hs, room_version, prev_event_ids, **kwargs)

    persistence = hs.get_storage_controllers().persistence
    assert persistence is not None

    await persistence.persist_event(event, context)

    return event


async def create_event(
    hs: synapse.server.HomeServer,
    room_version: Optional[str] = None,
    prev_event_ids: Optional[List[str]] = None,
    **kwargs: Any,
) -> Tuple[EventBase, EventContext]:
    if room_version is None:
        room_version = await hs.get_datastores().main.get_room_version_id(
            kwargs["room_id"]
        )

    builder = hs.get_event_builder_factory().for_room_version(
        KNOWN_ROOM_VERSIONS[room_version], kwargs
    )
    (
        event,
        unpersisted_context,
    ) = await hs.get_event_creation_handler().create_new_client_event(
        builder, prev_event_ids=prev_event_ids
    )

    context = await unpersisted_context.persist(event)

    return event, context


async def mark_event_as_partial_state(
    hs: synapse.server.HomeServer,
    event_id: str,
    room_id: str,
) -> None:
    """
    (Falsely) mark an event as having partial state.

    Naughty, but occasionally useful when checking that partial state doesn't
    block something from happening.

    If the event already has partial state, this insert will fail (event_id is unique
    in this table).
    """
    store = hs.get_datastores().main
    # Use the store helper to insert into the database so the caches are busted
    await store.store_partial_state_room(
        room_id=room_id,
        servers={hs.hostname},
        device_lists_stream_id=0,
        joined_via=hs.hostname,
    )

    # FIXME: Bust the cache
    await store.db_pool.simple_insert(
        table="partial_state_events",
        values={
            "room_id": room_id,
            "event_id": event_id,
        },
    )
