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

//! A queued batch of one room's events, as `_persist_event_batch` persists
//! it: the events already persisted under the same transaction ID are
//! dropped, the rest go in chunks of [`CHUNK_SIZE`], and each chunk is
//! written while its state groups are held back from deletion.
//!
//! The loop itself is [`super::python::Persister::persist_batch`], which
//! hands each written chunk to Python. This holds its steps.

use std::collections::{BTreeSet, HashMap, HashSet};

use anyhow::anyhow;
use futures::FutureExt;

use super::context::PersistContext;
use crate::{
    clock::Clock,
    database::postgres::{
        pool::ConnectionPool,
        write::{run_write_interaction, Column, WriteError},
    },
    events::Event,
    storage::events::Querier,
};

/// How many events `_persist_event_batch` writes in one transaction.
pub const CHUNK_SIZE: usize = 100;

/// `get_already_persisted_events`: for each event sent with a transaction
/// ID from a device, the event already persisted with the same room,
/// sender, device and transaction ID, if there is one. An event that
/// repeats an earlier one of the batch maps to that one.
pub async fn already_persisted_events(
    querier: &impl Querier,
    events: &[(Event, PersistContext)],
) -> anyhow::Result<HashMap<String, String>> {
    let mut mapping = HashMap::new();
    let mut by_txn_id: HashMap<(String, String, String, String), String> = HashMap::new();
    for (event, _) in events {
        let metadata = event.internal_metadata();
        let (Some(device_id), Some(txn_id)) = (metadata.device_id()?, metadata.txn_id()?) else {
            continue;
        };
        // Python skips an empty device or transaction ID.
        if device_id.is_empty() || txn_id.is_empty() {
            continue;
        }
        let key = (
            event.room_id().to_owned(),
            event.sender().to_owned(),
            device_id,
            txn_id,
        );
        if let Some(existing) = by_txn_id.get(&key) {
            mapping.insert(event.event_id().to_owned(), existing.clone());
            continue;
        }
        let rows = querier
            .query(
                "SELECT event_id FROM event_txn_id_device_id
                WHERE room_id = $1 AND user_id = $2 AND device_id = $3 AND txn_id = $4",
                &[&key.0, &key.1, &key.2, &key.3],
            )
            .await?;
        let existing: Option<String> = rows.first().map(|row| row.try_get(0)).transpose()?;
        match existing {
            Some(existing) => {
                mapping.insert(event.event_id().to_owned(), existing.clone());
                by_txn_id.insert(key, existing);
            }
            None => {
                by_txn_id.insert(key, event.event_id().to_owned());
            }
        }
    }
    Ok(mapping)
}

/// The state groups `persisting_state_group_references` holds back from
/// deletion for `events`: those of each event that is neither rejected nor
/// an outlier, and the groups before them.
pub fn referenced_state_groups(
    events: &[(Event, PersistContext)],
) -> anyhow::Result<BTreeSet<i64>> {
    let mut groups = BTreeSet::new();
    for (event, context) in events {
        if context.is_rejected() || event.internal_metadata().is_outlier()? {
            continue;
        }
        let group = context
            .state_group
            .ok_or_else(|| anyhow!("{} has no state group", event.event_id()))?;
        groups.insert(group);
        // Python skips a falsy group before the event.
        if let Some(before) = context.state_group_before_event.filter(|g| *g != 0) {
            groups.insert(before);
        }
    }
    Ok(groups)
}

/// The state groups were deleted before they could be marked.
#[derive(Debug)]
pub struct StateGroupsDeleted(pub Vec<i64>);

impl std::fmt::Display for StateGroupsDeleted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // As Python's `shortstr`.
        let shown: Vec<String> = self.0.iter().take(5).map(i64::to_string).collect();
        let more = if self.0.len() > 5 { ", ..." } else { "" };
        write!(
            f,
            "state groups have been deleted: [{}{more}]",
            shown.join(", ")
        )
    }
}

impl std::error::Error for StateGroupsDeleted {}

/// `_mark_state_groups_as_persisting_txn`: check the groups still exist,
/// locking them against a concurrent delete, and record that this instance
/// is persisting them.
pub async fn mark_state_groups_as_persisting(
    pool: &ConnectionPool,
    clock: &Clock,
    instance_name: &str,
    groups: &BTreeSet<i64>,
) -> Result<(), WriteError> {
    let groups: Vec<i64> = groups.iter().copied().collect();
    run_write_interaction(pool, "mark_state_groups_as_persisting", |txn, _| {
        let groups = &groups;
        async move {
            let rows = txn
                .query(
                    "SELECT id FROM state_groups WHERE id = ANY($1) FOR KEY SHARE",
                    &[groups],
                )
                .await?;
            let existing = rows
                .iter()
                .map(|row| row.try_get(0))
                .collect::<Result<HashSet<i64>, _>>()
                .map_err(anyhow::Error::new)?;
            let missing: Vec<i64> = groups
                .iter()
                .copied()
                .filter(|g| !existing.contains(g))
                .collect();
            if !missing.is_empty() {
                return Err(anyhow::Error::new(StateGroupsDeleted(missing)).into());
            }
            let now = i64::try_from(clock.now_millis()).map_err(anyhow::Error::new)?;
            let instance_names = vec![instance_name; groups.len()];
            let inserted = vec![now; groups.len()];
            txn.upsert_many(
                "state_groups_persisting",
                &[
                    Column::new("state_group", "bigint", groups),
                    Column::new("instance_name", "text", &instance_names),
                ],
                &[Column::new("inserted_ts", "bigint", &inserted)],
            )
            .await?;
            Ok(())
        }
        .boxed()
    })
    .await
}

/// `_finish_persisting_txn`: this instance no longer persists the groups.
/// If the persist failed, any pending deletion of them is pushed back, as
/// they may now be referenced. Otherwise it is dropped, as they are.
pub async fn finish_persisting(
    pool: &ConnectionPool,
    clock: &Clock,
    instance_name: &str,
    groups: &BTreeSet<i64>,
    error: bool,
) -> Result<(), WriteError> {
    let groups: Vec<i64> = groups.iter().copied().collect();
    run_write_interaction(pool, "finish_persisting", |txn, _| {
        let groups = &groups;
        async move {
            txn.execute(
                "DELETE FROM state_groups_persisting
                WHERE instance_name = $1 AND state_group = ANY($2)",
                &[&instance_name, groups],
            )
            .await?;
            if error {
                // `_bump_deletion_txn`.
                let now = i64::try_from(clock.now_millis()).map_err(anyhow::Error::new)?;
                txn.execute(
                    "UPDATE state_groups_pending_deletion
                    SET sequence_number = DEFAULT, insertion_ts = $1
                    WHERE state_group = ANY($2)",
                    &[&now, groups],
                )
                .await?;
            } else {
                txn.execute(
                    "DELETE FROM state_groups_pending_deletion WHERE state_group = ANY($1)",
                    &[groups],
                )
                .await?;
            }
            Ok(())
        }
        .boxed()
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deleted_groups_are_shown_as_python_shows_them() {
        assert_eq!(
            StateGroupsDeleted(vec![1, 2]).to_string(),
            "state groups have been deleted: [1, 2]"
        );
        assert_eq!(
            StateGroupsDeleted((1..=6).collect()).to_string(),
            "state groups have been deleted: [1, 2, 3, 4, 5, ...]"
        );
    }
}
