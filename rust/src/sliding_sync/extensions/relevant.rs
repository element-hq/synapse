/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 */

//! `find_relevant_room_ids_for_extension`: the rooms an extension covers,
//! from its `lists` and `rooms` and what the response holds.

use std::collections::HashSet;
use std::sync::Arc;

use anyhow::{bail, Context};
use indexmap::IndexMap;
use pyo3::{exceptions::PyAssertionError, prelude::*, types::PyModule};

use crate::sliding_sync::room_lists::lists::SyncOp;

/// The response's lists, by name, each with its operations.
pub type ActualLists = IndexMap<Arc<str>, Vec<SyncOp>>;

/// The rooms of a list's single `SYNC` operation. Python asserts there is
/// exactly one, so a list with no ranges, or several, fails here.
fn single_op_rooms<'a>(name: &str, ops: &'a [SyncOp]) -> anyhow::Result<&'a [Arc<str>]> {
    match ops {
        [op] => Ok(&op.room_ids),
        _ => bail!("list {name} has {} operations, not one", ops.len()),
    }
}

/// `find_relevant_room_ids_for_extension`: of the response, the rooms
/// `requested_room_ids` names among `actual_room_ids` (the room
/// subscriptions), and the rooms of the lists `requested_lists` names. `*`
/// means all of them.
pub fn find_relevant_room_ids_for_extension(
    requested_lists: Option<&[String]>,
    requested_room_ids: Option<&[String]>,
    actual_lists: &ActualLists,
    actual_room_ids: &HashSet<Arc<str>>,
) -> anyhow::Result<HashSet<Arc<str>>> {
    let mut relevant: HashSet<Arc<str>> = HashSet::new();

    for room_id in requested_room_ids.unwrap_or_default() {
        if room_id == "*" {
            relevant.extend(actual_room_ids.iter().cloned());
            break;
        }
        if let Some(room_id) = actual_room_ids.get(room_id.as_str()) {
            relevant.insert(Arc::clone(room_id));
        }
    }

    for list_key in requested_lists.unwrap_or_default() {
        if list_key == "*" {
            for (name, ops) in actual_lists {
                relevant.extend(single_op_rooms(name, ops)?.iter().cloned());
            }
            break;
        }
        if let Some(ops) = actual_lists.get(list_key.as_str()) {
            relevant.extend(
                single_op_rooms(list_key, ops)
                    .context("an extension's list")?
                    .iter()
                    .cloned(),
            );
        }
    }

    Ok(relevant)
}

/// `find_relevant_room_ids_for_extension` through the Rust implementation,
/// each list as its operations' room IDs. Raises `AssertionError` as
/// Python does.
#[pyfunction]
fn find_relevant_room_ids_for_test(
    requested_lists: Option<Vec<String>>,
    requested_room_ids: Option<Vec<String>>,
    actual_lists: Vec<(String, Vec<Vec<String>>)>,
    actual_room_ids: HashSet<String>,
) -> PyResult<Vec<String>> {
    let actual_lists: ActualLists = actual_lists
        .into_iter()
        .map(|(name, ops)| {
            let ops = ops
                .into_iter()
                .map(|room_ids| SyncOp {
                    range: (0, 0),
                    room_ids: room_ids.into_iter().map(Arc::from).collect(),
                })
                .collect();
            (Arc::from(name), ops)
        })
        .collect();
    let actual_room_ids: HashSet<Arc<str>> = actual_room_ids.into_iter().map(Arc::from).collect();
    let relevant = find_relevant_room_ids_for_extension(
        requested_lists.as_deref(),
        requested_room_ids.as_deref(),
        &actual_lists,
        &actual_room_ids,
    )
    .map_err(|e| PyAssertionError::new_err(e.to_string()))?;
    let mut relevant: Vec<String> = relevant.iter().map(|r| r.to_string()).collect();
    relevant.sort();
    Ok(relevant)
}

pub fn register_test_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(find_relevant_room_ids_for_test, m)?)?;
    Ok(())
}
