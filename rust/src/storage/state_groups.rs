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
 */

//! Writing state groups on the native pool.
//!
//! This is the database half of Python's
//! `StateGroupDataStore.store_state_deltas_for_batched`: a linear chain of
//! new state groups, one per state event, each a delta from the one
//! before. The chain follows on from an existing group, or from a new full
//! group holding the state before the first event.

use futures::FutureExt;

use crate::database::postgres::{
    pool::ConnectionPool,
    write::{run_write_interaction, OwnedColumn, WriteError},
};

/// A `(type, state_key, event_id)` state entry.
pub type StateRow = (String, String, String);

/// The chain of state groups to store for a batch of new events.
#[derive(Debug, Clone)]
pub struct StateGroupChain {
    pub room_id: String,
    /// The group the chain follows on from, which must still exist.
    pub prev_group: Option<i64>,
    /// When there is no `prev_group`: the first event's ID and the state
    /// before it, stored as a full group.
    pub full_state_before: Option<(String, Vec<StateRow>)>,
    /// The batch's state events, in order, as `(type, state_key, event_id)`.
    /// Each gets a group: its state delta from the group before it.
    pub state_events: Vec<StateRow>,
}

/// The groups [`store_state_group_chain`] stored or followed on from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredStateGroups {
    /// The group before the first event: `prev_group`, or the new full group.
    pub group_before: i64,
    /// The group after each state event, in order.
    pub groups_after: Vec<i64>,
}

/// Why [`store_state_group_chain`] failed.
#[derive(Debug)]
pub enum StateGroupWriteError {
    /// `prev_group` has been deleted, or never existed.
    MissingPrevGroup(i64),
    Write(WriteError),
}

impl std::fmt::Display for StateGroupWriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            // Python's message for the same failure.
            StateGroupWriteError::MissingPrevGroup(group) => write!(
                f,
                "Trying to persist state with unpersisted prev_group: {group}"
            ),
            StateGroupWriteError::Write(error) => write!(f, "{error:#}"),
        }
    }
}

impl std::error::Error for StateGroupWriteError {}

impl From<WriteError> for StateGroupWriteError {
    fn from(error: WriteError) -> Self {
        StateGroupWriteError::Write(error)
    }
}

/// Store `chain` in one transaction.
///
/// As Python does, a `prev_group` is locked against deletion (`FOR KEY
/// SHARE`) and any pending deletion of it is pushed back. The new group IDs
/// come from `state_group_id_seq`, the sequence Python's
/// `_state_group_seq_gen` reads.
pub async fn store_state_group_chain(
    pool: &ConnectionPool,
    chain: &StateGroupChain,
    now_ms: i64,
) -> Result<StoredStateGroups, StateGroupWriteError> {
    if chain.prev_group.is_none() && chain.full_state_before.is_none() {
        return Err(StateGroupWriteError::Write(WriteError::Other(
            anyhow::anyhow!("a state group chain needs a prev_group or the state before it"),
        )));
    }

    let result = run_write_interaction(pool, "store_state_group_chain", |txn, _| {
        async move {
            if let Some(prev_group) = chain.prev_group {
                let rows = txn
                    .query(
                        "SELECT id FROM state_groups WHERE id = $1 FOR KEY SHARE",
                        &[&prev_group],
                    )
                    .await?;
                if rows.is_empty() {
                    return Ok(Err(prev_group));
                }
                // Sent with the next statement.
                txn.execute_later(
                    "UPDATE state_groups_pending_deletion
                    SET sequence_number = DEFAULT, insertion_ts = $1
                    WHERE state_group = $2",
                    crate::owned_params![now_ms, prev_group],
                );
            }

            let needed = chain.state_events.len() as i64 + i64::from(chain.prev_group.is_none());
            let mut ids: Vec<i64> = if needed > 0 {
                txn.query(
                    "SELECT nextval('state_group_id_seq') FROM generate_series(1, $1::bigint)",
                    &[&needed],
                )
                .await?
                .iter()
                .map(|row| row.try_get::<_, i64>(0))
                .collect::<Result<_, _>>()
                .map_err(|e| WriteError::Other(e.into()))?
            } else {
                Vec::new()
            };

            // Rows for the three tables, built column by column.
            let mut group_ids = Vec::new();
            let mut group_event_ids = Vec::new();
            let mut edge_groups = Vec::new();
            let mut edge_prev_groups = Vec::new();
            let mut state_group_col = Vec::new();
            let mut state_type_col = Vec::new();
            let mut state_key_col = Vec::new();
            let mut state_event_col = Vec::new();

            let group_before = match (chain.prev_group, &chain.full_state_before) {
                (Some(prev_group), _) => prev_group,
                (None, Some((first_event_id, state))) => {
                    let group = ids.remove(0);
                    group_ids.push(group);
                    group_event_ids.push(first_event_id.as_str());
                    for (event_type, state_key, event_id) in state {
                        state_group_col.push(group);
                        state_type_col.push(event_type.as_str());
                        state_key_col.push(state_key.as_str());
                        state_event_col.push(event_id.as_str());
                    }
                    group
                }
                (None, None) => unreachable!("checked above"),
            };

            let mut prev = group_before;
            for ((event_type, state_key, event_id), &group) in chain.state_events.iter().zip(&ids) {
                group_ids.push(group);
                group_event_ids.push(event_id.as_str());
                edge_groups.push(group);
                edge_prev_groups.push(prev);
                state_group_col.push(group);
                state_type_col.push(event_type.as_str());
                state_key_col.push(state_key.as_str());
                state_event_col.push(event_id.as_str());
                prev = group;
            }

            // The rows go out with the COMMIT.
            let room_ids = vec![chain.room_id.as_str(); group_ids.len()];
            txn.insert_many_later(
                "state_groups",
                vec![
                    OwnedColumn::copy("id", "bigint", &group_ids),
                    OwnedColumn::copy("room_id", "text", &room_ids),
                    OwnedColumn::copy("event_id", "text", &group_event_ids),
                ],
                None,
            )?;
            txn.insert_many_later(
                "state_group_edges",
                vec![
                    OwnedColumn::copy("state_group", "bigint", &edge_groups),
                    OwnedColumn::copy("prev_state_group", "bigint", &edge_prev_groups),
                ],
                None,
            )?;
            let state_room_ids = vec![chain.room_id.as_str(); state_group_col.len()];
            txn.insert_many_later(
                "state_groups_state",
                vec![
                    OwnedColumn::copy("state_group", "bigint", &state_group_col),
                    OwnedColumn::copy("room_id", "text", &state_room_ids),
                    OwnedColumn::copy("type", "text", &state_type_col),
                    OwnedColumn::copy("state_key", "text", &state_key_col),
                    OwnedColumn::copy("event_id", "text", &state_event_col),
                ],
                None,
            )?;

            Ok(Ok(StoredStateGroups {
                group_before,
                groups_after: ids,
            }))
        }
        .boxed()
    })
    .await?;

    result.map_err(StateGroupWriteError::MissingPrevGroup)
}
