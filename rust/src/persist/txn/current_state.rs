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

//! `_update_current_state_txn`: the room's current state, the tables
//! derived from it, and the caches that depend on it. The profile updates
//! at its end are in [`super::profile`].

use std::collections::BTreeSet;

use anyhow::anyhow;
use serde_json::Value;
use tokio_postgres::types::ToSql;

use super::{
    caches::{invalidate_cache_and_stream, send_invalidation_to_replication},
    profile, TxnInput,
};
use crate::{
    database::postgres::write::{WriteError, WriteTxn},
    events::constants::event_type::{M_ROOM_CREATE, M_ROOM_MEMBER},
    persist::{
        effects::{PersistEffects, StreamChangeCache},
        extremities::is_mine_id,
        sliding_sync::{SlidingSyncStateValues, SlidingSyncTableChanges},
        state_delta::DeltaState,
    },
    state::StateKey,
};

/// `CURRENT_STATE_CACHE_NAME`: the cache stream's name for
/// `_invalidate_state_caches`.
const CURRENT_STATE_CACHE_NAME: &str = "cs_cache_fake";

/// How many members a `cs_cache_fake` row names at most, so a row fits in a
/// replication line.
const MEMBERS_PER_INVALIDATION: usize = 50;

/// `_update_current_state_txn`: apply `delta` to the room's current state
/// and `local_current_membership`, and `changes` to the sliding sync
/// tables. Writes the cache stream rows for the state caches, and stops
/// tracking the device lists of remote members the server no longer
/// shares a room with, and records the profile updates of the membership
/// changes. `stream_id` is the chunk's first stream ordering.
pub async fn update_current_state(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    delta: &DeltaState,
    changes: &SlidingSyncTableChanges,
    stream_id: i64,
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    let room_id = input.room_id;
    if *changes.room_id != *room_id {
        return Err(anyhow!("sliding sync changes for {} in {room_id}", changes.room_id).into());
    }

    // Each changed key with its new event, if it has one. `to_delete` and
    // `to_insert` do not share a key.
    let mut to_insert: Vec<Change<'_>> = delta
        .to_insert
        .iter()
        .map(|(StateKey(event_type, state_key), event_id)| Change {
            event_type,
            state_key,
            event_id: Some(event_id),
        })
        .collect();
    to_insert.sort_unstable_by_key(|change| (change.event_type, change.state_key));
    let changed: Vec<Change<'_>> = delta
        .to_delete
        .iter()
        .map(|StateKey(event_type, state_key)| Change {
            event_type,
            state_key,
            event_id: None,
        })
        .chain(to_insert.iter().copied())
        .collect();

    // The members whose membership caches go.
    let mut members: BTreeSet<String> = changed
        .iter()
        .filter(|change| change.event_type == M_ROOM_MEMBER)
        .map(|change| change.state_key.to_owned())
        .collect();

    if delta.no_longer_in_room {
        // The server has left the room, so its current state goes. The
        // room version is recorded first, and the delta stream told what
        // went.
        upsert_room_version(txn, room_id).await?;
        txn.execute(
            "INSERT INTO current_state_delta_stream
                (stream_id, instance_name, room_id, type, state_key, event_id, prev_event_id)
            SELECT $1, $2, room_id, type, state_key, NULL, event_id
            FROM current_state_events
            WHERE room_id = $3",
            &[&stream_id, &input.instance_name, &room_id],
        )
        .await?;

        let rows = txn
            .query(
                "SELECT state_key FROM current_state_events
                WHERE type = 'm.room.member' AND room_id = $1 AND membership = 'join'",
                &[&room_id],
            )
            .await?;
        for row in rows {
            let user_id: String = row.try_get(0).map_err(anyhow::Error::new)?;
            members.insert(user_id);
        }

        txn.execute(
            "DELETE FROM current_state_events WHERE room_id = $1",
            &[&room_id],
        )
        .await?;
        txn.execute(
            "DELETE FROM sliding_sync_joined_rooms WHERE room_id = $1",
            &[&room_id],
        )
        .await?;
    } else {
        // The delta stream reads each key's event before it changes.
        let delta_rows: Vec<Vec<&(dyn ToSql + Sync)>> = changed
            .iter()
            .map(|change| {
                vec![
                    &stream_id as &(dyn ToSql + Sync),
                    &input.instance_name,
                    &room_id,
                    &change.event_type,
                    &change.state_key,
                    &change.event_id,
                ]
            })
            .collect();
        txn.execute_batch(
            "INSERT INTO current_state_delta_stream
                (stream_id, instance_name, room_id, type, state_key, event_id, prev_event_id)
            SELECT $1::bigint, $2::text, $3::text, $4::text, $5::text, $6::text, (
                SELECT event_id FROM current_state_events
                WHERE room_id = $3::text AND type = $4::text AND state_key = $5::text
            )",
            &delta_rows,
        )
        .await?;

        let key_rows: Vec<Vec<&(dyn ToSql + Sync)>> = changed
            .iter()
            .map(|change| {
                vec![
                    &room_id as &(dyn ToSql + Sync),
                    &change.event_type,
                    &change.state_key,
                ]
            })
            .collect();
        txn.execute_batch(
            "DELETE FROM current_state_events
            WHERE room_id = $1 AND type = $2 AND state_key = $3",
            &key_rows,
        )
        .await?;

        // The membership comes from `room_memberships`, which the members
        // step has filled.
        let insert_rows: Vec<Vec<&(dyn ToSql + Sync)>> = to_insert
            .iter()
            .map(|change| {
                vec![
                    &room_id as &(dyn ToSql + Sync),
                    &change.event_type,
                    &change.state_key,
                    &change.event_id,
                ]
            })
            .collect();
        txn.execute_batch(
            "INSERT INTO current_state_events
                (room_id, type, state_key, event_id, membership, event_stream_ordering)
            VALUES (
                $1, $2, $3, $4,
                (SELECT membership FROM room_memberships WHERE event_id = $4),
                (SELECT stream_ordering FROM events WHERE event_id = $4)
            )",
            &insert_rows,
        )
        .await?;

        update_joined_room(txn, room_id, changes).await?;
    }

    // Even when the server has left, as a local user may have been banned.
    let local_members: Vec<Change<'_>> = changed
        .iter()
        .filter(|change| {
            change.event_type == M_ROOM_MEMBER && is_mine_id(change.state_key, input.server_name)
        })
        .copied()
        .collect();
    if !local_members.is_empty() {
        let user_ids: Vec<&str> = local_members
            .iter()
            .map(|change| change.state_key)
            .collect();
        txn.execute(
            "DELETE FROM local_current_membership WHERE room_id = $1 AND user_id = ANY($2)",
            &[&room_id, &user_ids],
        )
        .await?;
    }
    let membership_rows: Vec<Vec<&(dyn ToSql + Sync)>> = local_members
        .iter()
        .filter(|change| change.event_id.is_some())
        .map(|change| {
            vec![
                &room_id as &(dyn ToSql + Sync),
                &change.state_key,
                &change.event_id,
            ]
        })
        .collect();
    txn.execute_batch(
        "INSERT INTO local_current_membership
            (room_id, user_id, event_id, membership, event_stream_ordering)
        VALUES (
            $1, $2, $3,
            (SELECT membership FROM room_memberships WHERE event_id = $3),
            (SELECT stream_ordering FROM events WHERE event_id = $3)
        )",
        &membership_rows,
    )
    .await?;

    update_membership_snapshots(txn, room_id, changes).await?;

    effects.add_stream_change(
        StreamChangeCache::CurrentStateDeltas,
        room_id.to_owned(),
        stream_id,
    );
    for user_id in &members {
        effects.add_stream_change(StreamChangeCache::Membership, user_id.clone(), stream_id);
    }

    // `_invalidate_state_caches_and_stream`.
    let members: Vec<String> = members.into_iter().collect();
    for keys in state_cache_invalidation_keys(room_id, &members) {
        send_invalidation_to_replication(txn, input, effects, CURRENT_STATE_CACHE_NAME, &keys)
            .await?;
    }

    let remote_members: Vec<&str> = members
        .iter()
        .map(String::as_str)
        .filter(|user_id| !is_mine_id(user_id, input.server_name))
        .collect();
    handle_potentially_left_users(txn, input, &remote_members, effects).await?;
    effects.add_state_caches(room_id.to_owned(), members);

    profile::record_profile_updates_for_state_delta(txn, input, changes, effects).await
}

/// A key of the room's state that changes, and its new event, if it has
/// one.
#[derive(Clone, Copy)]
struct Change<'a> {
    event_type: &'a str,
    state_key: &'a str,
    event_id: Option<&'a str>,
}

/// The keys of the `cs_cache_fake` rows for a change to the room's state
/// that changed `members`: the room, then up to
/// [`MEMBERS_PER_INVALIDATION`] of them a row. One row with only the room
/// if no member changed.
fn state_cache_invalidation_keys(room_id: &str, members: &[String]) -> Vec<Vec<String>> {
    if members.is_empty() {
        return vec![vec![room_id.to_owned()]];
    }
    members
        .chunks(MEMBERS_PER_INVALIDATION)
        .map(|chunk| {
            std::iter::once(room_id.to_owned())
                .chain(chunk.iter().cloned())
                .collect()
        })
        .collect()
}

/// `_upsert_room_version_txn`: record the room version from the create
/// event in the room's current state, before the current state goes.
async fn upsert_room_version(txn: &WriteTxn, room_id: &str) -> Result<(), WriteError> {
    let rows = txn
        .query(
            "SELECT json FROM event_json
            INNER JOIN current_state_events USING (room_id, event_id)
            WHERE room_id = $1 AND type = $2 AND state_key = ''",
            &[&room_id, &M_ROOM_CREATE],
        )
        .await?;
    let Some(row) = rows.first() else {
        return Ok(());
    };
    let json: &str = row.try_get(0).map_err(anyhow::Error::new)?;
    let event: Value = serde_json::from_str(json).map_err(anyhow::Error::new)?;
    let (room_version, creator) = room_version_and_creator(&event)?;
    txn.execute(
        "INSERT INTO rooms (room_id, room_version, is_public, creator)
        VALUES ($1, $2, FALSE, $3)
        ON CONFLICT (room_id) DO UPDATE SET room_version = EXCLUDED.room_version",
        &[&room_id, &room_version, &creator],
    )
    .await?;
    Ok(())
}

/// The create event's `room_version`, `"1"` if it has none, and `creator`.
///
/// Python stores whatever the content holds, which fails for an object,
/// casts a number and stores NULL for a null `room_version`. Here anything
/// but a string fails, but for a null `creator`. Event auth requires a
/// string `room_version` matching the room's, so only old or corrupt data
/// can differ.
fn room_version_and_creator(event: &Value) -> anyhow::Result<(&str, Option<&str>)> {
    let content = match event.get("content") {
        None => None,
        Some(Value::Object(content)) => Some(content),
        Some(_) => return Err(anyhow!("create event content is not an object")),
    };
    let field = |name: &str| content.and_then(|c| c.get(name));
    let room_version = match field("room_version") {
        None => "1",
        Some(Value::String(version)) => version,
        Some(other) => return Err(anyhow!("create event room_version is {other}")),
    };
    let creator = match field("creator") {
        None | Some(Value::Null) => None,
        Some(Value::String(creator)) => Some(creator.as_str()),
        Some(other) => return Err(anyhow!("create event creator is {other}")),
    };
    Ok((room_version, creator))
}

/// The columns of a [`SlidingSyncStateValues`] that are set, as Python's
/// `SlidingSyncStateInsertValues` dict holds them.
fn state_value_columns(
    values: &SlidingSyncStateValues,
) -> Vec<(&'static str, Box<dyn ToSql + Sync + Send + '_>)> {
    let mut columns: Vec<(&'static str, Box<dyn ToSql + Sync + Send + '_>)> = Vec::new();
    if let Some(room_type) = &values.room_type {
        columns.push(("room_type", Box::new(room_type.as_deref())));
    }
    if let Some(is_encrypted) = values.is_encrypted {
        columns.push(("is_encrypted", Box::new(is_encrypted)));
    }
    if let Some(room_name) = &values.room_name {
        columns.push(("room_name", Box::new(room_name.as_deref())));
    }
    if let Some(successor) = &values.tombstone_successor_room_id {
        columns.push((
            "tombstone_successor_room_id",
            Box::new(successor.as_deref()),
        ));
    }
    columns
}

/// The state columns of the room's `sliding_sync_joined_rooms` row, if one
/// changed. The stream ordering and bump stamp of an existing row are left
/// to the sliding sync step.
async fn update_joined_room(
    txn: &WriteTxn,
    room_id: &str,
    changes: &SlidingSyncTableChanges,
) -> Result<(), WriteError> {
    let columns = state_value_columns(&changes.joined_room_updates);
    if columns.is_empty() {
        return Ok(());
    }
    let names: Vec<&str> = columns.iter().map(|(name, _)| *name).collect();
    let placeholders: Vec<String> = (3..3 + columns.len()).map(|i| format!("${i}")).collect();
    let updates: Vec<String> = names
        .iter()
        .map(|name| format!("{name} = EXCLUDED.{name}"))
        .collect();
    // The stream ordering is only for a new row, which needs one. It is
    // read here, as the chunk's own may be of events persisted before as
    // outliers, which keep their first stream ordering.
    let sql = format!(
        "INSERT INTO sliding_sync_joined_rooms
            (room_id, event_stream_ordering, bump_stamp, {})
        VALUES (
            $1,
            (SELECT stream_ordering FROM events WHERE room_id = $1
                ORDER BY stream_ordering DESC LIMIT 1),
            $2,
            {}
        )
        ON CONFLICT (room_id) DO UPDATE SET {}",
        names.join(", "),
        placeholders.join(", "),
        updates.join(", ")
    );
    let mut params: Vec<&(dyn ToSql + Sync)> =
        vec![&room_id, &changes.joined_room_bump_stamp_to_fully_insert];
    params.extend(
        columns
            .iter()
            .map(|(_, value)| &**value as &(dyn ToSql + Sync)),
    );
    txn.execute(&sql, &params).await?;
    Ok(())
}

/// The room's `sliding_sync_membership_snapshots`: the deleted ones of
/// members state reset out of the room, and the new ones of members whose
/// membership changed, whether or not the server is still in the room.
async fn update_membership_snapshots(
    txn: &WriteTxn,
    room_id: &str,
    changes: &SlidingSyncTableChanges,
) -> Result<(), WriteError> {
    if !changes.to_delete_membership_snapshots.is_empty() {
        let user_ids: Vec<&str> = changes
            .to_delete_membership_snapshots
            .iter()
            .map(|user_id| &**user_id)
            .collect();
        txn.execute(
            "DELETE FROM sliding_sync_membership_snapshots
            WHERE room_id = $1 AND user_id = ANY($2)",
            &[&room_id, &user_ids],
        )
        .await?;
    }

    if changes.to_insert_membership_snapshots.is_empty() {
        return Ok(());
    }
    let shared = &changes.membership_snapshot_shared_insert_values;
    let mut columns = state_value_columns(&shared.state);
    if let Some(has_known_state) = shared.has_known_state {
        columns.push(("has_known_state", Box::new(has_known_state)));
    }
    let names: Vec<&str> = columns.iter().map(|(name, _)| *name).collect();
    let extra_names: String = names.iter().map(|name| format!(", {name}")).collect();
    let extra_placeholders: String = (7..7 + columns.len()).map(|i| format!(", ${i}")).collect();
    let extra_updates: String = names
        .iter()
        .map(|name| format!(", {name} = EXCLUDED.{name}"))
        .collect();
    // The stream ordering and instance are read from `events`, as the
    // event may have been persisted before as an outlier. An existing
    // row keeps its instance name.
    let sql = format!(
        "INSERT INTO sliding_sync_membership_snapshots
            (room_id, user_id, sender, membership_event_id, membership, forgotten,
            event_stream_ordering, event_instance_name{extra_names})
        VALUES (
            $1, $2, $3, $4, $5, $6,
            (SELECT stream_ordering FROM events WHERE event_id = $4),
            (SELECT COALESCE(instance_name, 'master') FROM events WHERE event_id = $4)
            {extra_placeholders}
        )
        ON CONFLICT (room_id, user_id) DO UPDATE SET
            sender = EXCLUDED.sender,
            membership_event_id = EXCLUDED.membership_event_id,
            membership = EXCLUDED.membership,
            forgotten = EXCLUDED.forgotten,
            event_stream_ordering = EXCLUDED.event_stream_ordering
            {extra_updates}"
    );
    // A new membership is not forgotten.
    let forgotten = 0i32;
    // As `&str`s, which the rows borrow: an `Arc<str>` is not `ToSql`.
    let snapshots: Vec<[&str; 4]> = changes
        .to_insert_membership_snapshots
        .iter()
        .map(|info| {
            [
                &*info.user_id,
                &*info.sender,
                &*info.membership_event_id,
                &*info.membership,
            ]
        })
        .collect();
    let rows: Vec<Vec<&(dyn ToSql + Sync)>> = snapshots
        .iter()
        .map(|[user_id, sender, event_id, membership]| {
            let mut row: Vec<&(dyn ToSql + Sync)> =
                vec![&room_id, user_id, sender, event_id, membership, &forgotten];
            row.extend(
                columns
                    .iter()
                    .map(|(_, value)| &**value as &(dyn ToSql + Sync)),
            );
            row
        })
        .collect();
    txn.execute_batch(&sql, &rows).await?;
    Ok(())
}

/// `handle_potentially_left_users_txn`: stop tracking the device lists of
/// the remote users in `user_ids` who share no room with the server now.
/// Their cache stream rows follow the order of `user_ids`, where Python's
/// follow a set's.
async fn handle_potentially_left_users(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    user_ids: &[&str],
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    if user_ids.is_empty() {
        return Ok(());
    }
    let rows = txn
        .query(
            "SELECT state_key FROM current_state_events
            WHERE type = 'm.room.member' AND membership = 'join' AND state_key = ANY($1)
            GROUP BY state_key",
            &[&user_ids],
        )
        .await?;
    let mut joined = BTreeSet::new();
    for row in rows {
        let user_id: String = row.try_get(0).map_err(anyhow::Error::new)?;
        joined.insert(user_id);
    }
    for &user_id in user_ids {
        if joined.contains(user_id) {
            continue;
        }
        // `mark_remote_user_device_list_as_unsubscribed_txn`.
        txn.execute(
            "DELETE FROM device_lists_remote_extremeties WHERE user_id = $1",
            &[&user_id],
        )
        .await?;
        invalidate_cache_and_stream(
            txn,
            input,
            effects,
            "get_device_list_last_stream_id_for_remote",
            vec![user_id.to_owned()],
        )
        .await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use futures::FutureExt;
    use serde_json::json;

    use super::*;
    use crate::database::postgres::{
        pool::{create_pool, PoolConfig},
        write::run_rolled_back_interaction,
    };

    /// The room version upsert, against a real server when
    /// `SYNAPSE_TEST_POSTGRES_DSN` is set, on temporary tables that shadow
    /// the real ones. The twin test cannot see it: the `rooms` row it
    /// updates always holds the version already.
    #[test]
    fn upsert_room_version_from_current_state() {
        let Ok(dsn) = std::env::var("SYNAPSE_TEST_POSTGRES_DSN") else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let pool = create_pool(&dsn, rt.handle().clone(), PoolConfig::default()).unwrap();
        let rows = rt
            .block_on(run_rolled_back_interaction(&pool, "test", |txn, _| {
                async move {
                    txn.execute(
                        "CREATE TEMPORARY TABLE event_json (
                            room_id TEXT, event_id TEXT, json TEXT
                        ) ON COMMIT DROP",
                        &[],
                    )
                    .await?;
                    txn.execute(
                        "CREATE TEMPORARY TABLE current_state_events (
                            room_id TEXT, type TEXT, state_key TEXT, event_id TEXT
                        ) ON COMMIT DROP",
                        &[],
                    )
                    .await?;
                    txn.execute(
                        "CREATE TEMPORARY TABLE rooms (
                            room_id TEXT PRIMARY KEY, room_version TEXT,
                            is_public BOOLEAN, creator TEXT
                        ) ON COMMIT DROP",
                        &[],
                    )
                    .await?;
                    for (room_id, content) in [
                        ("!old", r#"{"room_version": "10", "creator": "@a:hs"}"#),
                        ("!new", r#"{"creator": "@b:hs"}"#),
                    ] {
                        let json = format!(r#"{{"content": {content}}}"#);
                        let event_id = format!("$create{room_id}");
                        txn.execute(
                            "INSERT INTO event_json VALUES ($1, $2, $3)",
                            &[&room_id, &event_id, &json],
                        )
                        .await?;
                        txn.execute(
                            "INSERT INTO current_state_events
                            VALUES ($1, 'm.room.create', '', $2)",
                            &[&room_id, &event_id],
                        )
                        .await?;
                    }
                    // An existing row keeps its other columns.
                    txn.execute(
                        "INSERT INTO rooms VALUES ('!old', NULL, TRUE, '@x:hs')",
                        &[],
                    )
                    .await?;
                    for room_id in ["!old", "!new", "!none"] {
                        upsert_room_version(txn, room_id).await?;
                    }
                    let rows = txn
                        .query(
                            "SELECT room_id, room_version, is_public, creator
                            FROM rooms ORDER BY room_id",
                            &[],
                        )
                        .await?;
                    Ok(rows
                        .iter()
                        .map(|row| {
                            (
                                row.get::<_, String>(0),
                                row.get::<_, Option<String>>(1),
                                row.get::<_, Option<bool>>(2),
                                row.get::<_, Option<String>>(3),
                            )
                        })
                        .collect::<Vec<_>>())
                }
                .boxed()
            }))
            .unwrap();
        let row = |room_id: &str, version: &str, public: bool, creator: &str| {
            (
                room_id.to_owned(),
                Some(version.to_owned()),
                Some(public),
                Some(creator.to_owned()),
            )
        };
        assert_eq!(
            rows,
            vec![
                row("!new", "1", false, "@b:hs"),
                row("!old", "10", true, "@x:hs"),
            ]
        );
    }

    #[test]
    fn state_cache_rows_name_fifty_members_each() {
        assert_eq!(
            state_cache_invalidation_keys("!r", &[]),
            vec![vec!["!r".to_owned()]]
        );
        let members: Vec<String> = (0..101).map(|i| format!("@u{i:03}:hs")).collect();
        let rows = state_cache_invalidation_keys("!r", &members);
        let sizes: Vec<usize> = rows.iter().map(Vec::len).collect();
        assert_eq!(sizes, vec![51, 51, 2]);
        assert!(rows.iter().all(|row| row[0] == "!r"));
        assert_eq!(rows[2][1], "@u100:hs");
    }

    #[test]
    fn room_version_and_creator_from_create_event() {
        assert_eq!(room_version_and_creator(&json!({})).unwrap(), ("1", None));
        assert_eq!(
            room_version_and_creator(&json!({
                "content": {"room_version": "10", "creator": "@a:hs"}
            }))
            .unwrap(),
            ("10", Some("@a:hs"))
        );
        assert_eq!(
            room_version_and_creator(&json!({"content": {"creator": null}})).unwrap(),
            ("1", None)
        );
        assert!(room_version_and_creator(&json!({"content": []})).is_err());
        assert!(room_version_and_creator(&json!({"content": {"creator": 1}})).is_err());
        assert!(room_version_and_creator(&json!({"content": {"room_version": null}})).is_err());
        assert!(room_version_and_creator(&json!({"content": {"room_version": 5}})).is_err());
    }
}
