/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 */

//! `_update_sliding_sync_tables_with_new_persisted_events_txn`: the room's
//! latest stream ordering and bump stamp in `sliding_sync_joined_rooms`.
//! The state columns are written with the current state, in
//! [`super::current_state`].

use super::Persisting;
use crate::{
    database::postgres::write::{WriteError, WriteTxn},
    persist::sliding_sync::DEFAULT_BUMP_EVENT_TYPES,
};

/// Move the room's `sliding_sync_joined_rooms` row on to the last of
/// `events`, and its bump stamp to the last of them that bumps the room.
/// Not for backfilled events. `events` are the chunk's new events, not
/// rejected, in stream order. If the server is not in the room, there is
/// no row to update.
pub async fn update_sliding_sync_joined_room(
    txn: &WriteTxn,
    room_id: &str,
    events: &[Persisting<'_>],
) -> Result<(), WriteError> {
    let Some(last) = events.last() else {
        return Ok(());
    };
    let max_stream_ordering = last.stream_ordering();
    // One backfilled event means the chunk is backfilled.
    if max_stream_ordering < 0 {
        return Ok(());
    }
    let max_bump_stamp = events
        .iter()
        .rev()
        .find(|e| DEFAULT_BUMP_EVENT_TYPES.contains(&e.event().type_()))
        .map(Persisting::stream_ordering);

    txn.execute(
        "UPDATE sliding_sync_joined_rooms
        SET
            event_stream_ordering = CASE
                WHEN event_stream_ordering IS NULL OR event_stream_ordering < $1
                    THEN $1
                ELSE event_stream_ordering
            END,
            bump_stamp = CASE
                WHEN bump_stamp IS NULL OR bump_stamp < $2
                    THEN $2
                ELSE bump_stamp
            END
        WHERE room_id = $3",
        &[&max_stream_ordering, &max_bump_stamp, &room_id],
    )
    .await?;
    Ok(())
}
