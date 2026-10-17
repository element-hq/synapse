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

//! The profile updates of a change to the room's current state, with
//! `include_profile_updates_in_sync`: `_record_profile_updates_for_state_delta_txn`
//! and the helpers it calls. Their rows take IDs from the profile updates
//! stream inside the transaction.

use std::collections::BTreeSet;

use super::TxnInput;
use crate::{
    database::postgres::write::{Column, WriteError, WriteTxn},
    persist::{
        effects::PersistEffects, extremities::is_mine_id, sliding_sync::SlidingSyncTableChanges,
    },
};

/// `ProfileUpdateAction.JOINED_ROOM`.
const JOINED_ROOM: &str = "joined_room";
/// `ProfileUpdateAction.LEFT_ROOM`.
const LEFT_ROOM: &str = "left_room";

/// `_record_profile_updates_for_state_delta_txn`: the local members whose
/// new membership is a join get a joined room update. The local members
/// whose new membership is anything else, and every member whose snapshot
/// was deleted as they were state reset out of the room, get left room
/// updates. The members are taken in user ID order, where Python's follow
/// a set's.
pub async fn record_profile_updates_for_state_delta(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    changes: &SlidingSyncTableChanges,
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    if !input.include_profile_updates_in_sync {
        return Ok(());
    }
    let local_snapshots = changes
        .to_insert_membership_snapshots
        .iter()
        .filter(|info| is_mine_id(&info.user_id, input.server_name));
    let mut additions = BTreeSet::new();
    let mut leaves = BTreeSet::new();
    for info in local_snapshots {
        if &*info.membership == "join" {
            additions.insert(&*info.user_id);
        } else {
            leaves.insert(&*info.user_id);
        }
    }
    leaves.extend(changes.to_delete_membership_snapshots.iter().map(|u| &**u));

    if !additions.is_empty() {
        // `record_profile_updates_for_user_joined_room_txn`.
        let members = local_joined_members(txn, input.room_id).await?;
        record_profile_updates(txn, input, &additions, JOINED_ROOM, &members, effects).await?;
    }
    for user_id in leaves {
        record_profile_updates_for_user_left_room(txn, input, user_id, effects).await?;
    }
    Ok(())
}

/// The room's local members who are joined, from `local_current_membership`.
async fn local_joined_members(
    txn: &WriteTxn,
    room_id: &str,
) -> Result<BTreeSet<String>, WriteError> {
    let rows = txn
        .query(
            "SELECT user_id FROM local_current_membership
            WHERE room_id = $1 AND membership = 'join'",
            &[&room_id],
        )
        .await?;
    let mut members = BTreeSet::new();
    for row in rows {
        members.insert(row.try_get(0).map_err(anyhow::Error::new)?);
    }
    Ok(members)
}

/// `_record_profile_updates_for_user_left_room_txn`: the room's local
/// members who no longer share a room with `user_id` get a left room
/// update about them, and they about each of those members. The rows
/// that told either side about the other's earlier updates are deleted
/// first.
async fn record_profile_updates_for_user_left_room(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    user_id: &str,
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    let members = local_joined_members(txn, input.room_id).await?;
    let member_ids: Vec<&str> = members.iter().map(String::as_str).collect();
    // `do_users_share_a_room_txn`.
    let rows = txn
        .query(
            "SELECT DISTINCT b.state_key
            FROM (
                SELECT room_id FROM current_state_events
                WHERE type = 'm.room.member' AND membership = 'join' AND state_key = $1
            ) AS a
            INNER JOIN (
                SELECT room_id, state_key FROM current_state_events
                WHERE type = 'm.room.member' AND membership = 'join'
                    AND state_key = ANY($2)
            ) AS b USING (room_id)",
            &[&user_id, &member_ids],
        )
        .await?;
    let mut no_longer_sharing = members.clone();
    for row in rows {
        let sharing: String = row.try_get(0).map_err(anyhow::Error::new)?;
        no_longer_sharing.remove(&sharing);
    }
    let no_longer_sharing_ids: Vec<&str> = no_longer_sharing.iter().map(String::as_str).collect();

    txn.execute(
        "DELETE FROM profile_updates_per_user
        WHERE user_id = ANY($1)
            AND stream_id IN (SELECT stream_id FROM profile_updates WHERE user_id = $2)",
        &[&no_longer_sharing_ids, &user_id],
    )
    .await?;
    let user = BTreeSet::from([user_id]);
    record_profile_updates(txn, input, &user, LEFT_ROOM, &no_longer_sharing, effects).await?;

    txn.execute(
        "DELETE FROM profile_updates_per_user
        WHERE user_id = $1
            AND stream_id IN (SELECT stream_id FROM profile_updates WHERE user_id = ANY($2))",
        &[&user_id, &no_longer_sharing_ids],
    )
    .await?;
    let no_longer_sharing: BTreeSet<&str> = no_longer_sharing_ids.into_iter().collect();
    let target = BTreeSet::from([user_id.to_owned()]);
    record_profile_updates(txn, input, &no_longer_sharing, LEFT_ROOM, &target, effects).await
}

/// `record_profile_updates_txn` for a joined room or left room `action`,
/// with the target users given: one `profile_updates` row for each of
/// `users`, and a `profile_updates_per_user` row for each local target
/// but the user itself. A user with no other target gets no row.
async fn record_profile_updates(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    users: &BTreeSet<&str>,
    action: &str,
    target_users: &BTreeSet<String>,
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    let targets: Vec<&str> = target_users
        .iter()
        .map(String::as_str)
        .filter(|user_id| is_mine_id(user_id, input.server_name))
        .collect();
    let updated: Vec<&str> = users
        .iter()
        .copied()
        .filter(|user_id| targets.iter().any(|target| target != user_id))
        .collect();
    if updated.is_empty() {
        return Ok(());
    }

    let inserted_ts = input.now_ms()?;
    let stream_ids = input.profile_stream.next_ids(txn, updated.len()).await?;
    effects.add_profile_stream_ids(&stream_ids);
    let instance_names = vec![input.instance_name; updated.len()];
    let actions = vec![action; updated.len()];
    // `affected_fields` is left NULL: it is set only for profile changes.
    let inserted = vec![inserted_ts; updated.len()];
    txn.insert_many(
        "profile_updates",
        &[
            Column::new("stream_id", "bigint", &stream_ids),
            Column::new("instance_name", "text", &instance_names),
            Column::new("user_id", "text", &updated),
            Column::new("action", "text", &actions),
            Column::new("inserted_ts", "bigint", &inserted),
        ],
        None,
    )
    .await?;

    let mut per_user_stream_ids = Vec::new();
    let mut per_user_ids = Vec::new();
    for (&stream_id, &user_id) in stream_ids.iter().zip(&updated) {
        for &target in &targets {
            if target != user_id {
                per_user_stream_ids.push(stream_id);
                per_user_ids.push(target);
            }
        }
    }
    let inserted = vec![inserted_ts; per_user_ids.len()];
    txn.insert_many(
        "profile_updates_per_user",
        &[
            Column::new("stream_id", "bigint", &per_user_stream_ids),
            Column::new("user_id", "text", &per_user_ids),
            Column::new("inserted_ts", "bigint", &inserted),
        ],
        None,
    )
    .await?;
    Ok(())
}
