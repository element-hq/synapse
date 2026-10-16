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

//! `RoomSyncConfig`: what to fetch for a room, from the lists and room
//! subscriptions that include it.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::bail;
use pyo3::{prelude::*, types::PyModule};

/// `StateValues.WILDCARD`: every state key, or every type.
pub const WILDCARD: &str = "*";
/// `StateValues.LAZY`: the members the timeline needs. Only a state key of
/// `m.room.member`.
pub const LAZY: &str = "$LAZY";
/// `StateValues.ME`: the requester's user ID.
pub const ME: &str = "$ME";

pub const MEMBER: &str = "m.room.member";

/// A type to its state keys, with the `*`, `$LAZY` and `$ME` values still
/// in them. Python's is a dict of sets. Neither order is observable.
pub type RequiredStateMap = BTreeMap<String, BTreeSet<String>>;

/// `RoomSyncConfig`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RoomSyncConfig {
    pub timeline_limit: i64,
    pub required_state_map: RequiredStateMap,
}

/// Remove `state_key` from every type's set, and drop the sets it empties:
/// a wildcard type is about to cover it.
fn remove_state_key_everywhere(map: &mut RequiredStateMap, state_key: &str) {
    map.retain(|_, state_keys| {
        state_keys.remove(state_key);
        !state_keys.is_empty()
    });
}

fn has_key(map: &RequiredStateMap, event_type: &str, state_key: &str) -> bool {
    map.get(event_type)
        .is_some_and(|state_keys| state_keys.contains(state_key))
}

impl RoomSyncConfig {
    /// `from_room_config`: the config of a list or a room subscription,
    /// with the wildcards collapsed.
    pub fn from_room_config(timeline_limit: i64, required_state: &[(String, String)]) -> Self {
        let mut map = RequiredStateMap::new();
        for (state_type, state_key) in required_state {
            // A wildcard type already covers this state key.
            if has_key(&map, WILDCARD, state_key) {
                continue;
            }
            // A wildcard state key already covers this type.
            if has_key(&map, state_type, WILDCARD) {
                continue;
            }
            if state_type == WILDCARD && state_key == WILDCARD {
                map = RequiredStateMap::from([(
                    WILDCARD.to_owned(),
                    BTreeSet::from([WILDCARD.to_owned()]),
                )]);
                break;
            } else if state_type == WILDCARD {
                remove_state_key_everywhere(&mut map, state_key);
            }

            if state_key == WILDCARD {
                map.insert(state_type.clone(), BTreeSet::from([state_key.clone()]));
            } else {
                map.entry(state_type.clone())
                    .or_default()
                    .insert(state_key.clone());
            }
        }
        RoomSyncConfig {
            timeline_limit,
            required_state_map: map,
        }
    }

    /// `combine_room_sync_config`: the union of the two.
    pub fn combine(&self, other: &RoomSyncConfig) -> RoomSyncConfig {
        let timeline_limit = self.timeline_limit.max(other.timeline_limit);
        let mut map = self.required_state_map.clone();

        for (state_type, state_key_set) in &other.required_state_map {
            // Everything is already wanted.
            if has_key(&map, WILDCARD, WILDCARD) {
                break;
            }
            // Every state key of this type is already wanted.
            if has_key(&map, state_type, WILDCARD) {
                continue;
            }
            if state_type == WILDCARD && state_key_set.contains(WILDCARD) {
                map = RequiredStateMap::from([(
                    state_type.clone(),
                    BTreeSet::from([WILDCARD.to_owned()]),
                )]);
                break;
            }

            for state_key in state_key_set {
                // A wildcard type already covers this state key.
                if has_key(&map, WILDCARD, state_key) {
                    continue;
                }
                if state_type == WILDCARD {
                    remove_state_key_everywhere(&mut map, state_key);
                }
                if state_key == WILDCARD {
                    map.insert(state_type.clone(), BTreeSet::from([state_key.clone()]));
                    break;
                }
                map.entry(state_type.clone())
                    .or_default()
                    .insert(state_key.clone());
            }
        }

        RoomSyncConfig {
            timeline_limit,
            required_state_map: map,
        }
    }

    /// `must_await_full_state`: whether the room's partial state cannot
    /// satisfy the config, so a partially-stated room must be left out.
    /// `server_name` is ours, for `is_mine_id`.
    ///
    /// Fails where Python raises: an empty state key under the wildcard
    /// type, which Python indexes into.
    pub fn must_await_full_state(&self, server_name: &str) -> anyhow::Result<bool> {
        let is_mine_id = |user_id: &str| {
            user_id
                .split_once(':')
                .is_some_and(|(_, host)| host == server_name)
        };

        let wildcard_state_keys = self.required_state_map.get(WILDCARD);
        if let Some(state_keys) = wildcard_state_keys {
            if state_keys.contains(WILDCARD) {
                return Ok(true);
            }
            for possible_user_id in state_keys {
                if possible_user_id.is_empty() {
                    bail!("an empty state key under the wildcard type");
                }
                if !possible_user_id.starts_with('@') {
                    continue;
                }
                if !possible_user_id.contains(':') {
                    continue;
                }
                if !is_mine_id(possible_user_id) {
                    return Ok(true);
                }
            }
        }

        let Some(membership_state_keys) = self.required_state_map.get(MEMBER) else {
            return Ok(false);
        };
        for user_id in membership_state_keys {
            match user_id.as_str() {
                ME | LAZY => continue,
                WILDCARD => return Ok(false),
                _ if !is_mine_id(user_id) => return Ok(true),
                _ => {}
            }
        }
        Ok(false)
    }
}

/// A `RoomSyncConfig` from `(timeline_limit, {type: state keys})`.
pub fn config_from_py(timeline_limit: i64, map: BTreeMap<String, Vec<String>>) -> RoomSyncConfig {
    RoomSyncConfig {
        timeline_limit,
        required_state_map: map
            .into_iter()
            .map(|(k, v)| (k, v.into_iter().collect()))
            .collect(),
    }
}

/// `RoomSyncConfig.from_room_config` through the Rust implementation, as
/// `(timeline_limit, {type: state keys})`.
#[pyfunction]
fn room_sync_config_from_room_config_for_test(
    timeline_limit: i64,
    required_state: Vec<(String, String)>,
) -> (i64, BTreeMap<String, Vec<String>>) {
    to_test_tuple(&RoomSyncConfig::from_room_config(
        timeline_limit,
        &required_state,
    ))
}

/// `combine_room_sync_config` through the Rust implementation.
#[pyfunction]
fn combine_room_sync_config_for_test(
    a: (i64, BTreeMap<String, Vec<String>>),
    b: (i64, BTreeMap<String, Vec<String>>),
) -> (i64, BTreeMap<String, Vec<String>>) {
    let a = config_from_py(a.0, a.1);
    let b = config_from_py(b.0, b.1);
    to_test_tuple(&a.combine(&b))
}

/// `must_await_full_state` through the Rust implementation. Raises
/// `ValueError` where Python raises.
#[pyfunction]
fn must_await_full_state_for_test(
    config: (i64, BTreeMap<String, Vec<String>>),
    server_name: &str,
) -> PyResult<bool> {
    config_from_py(config.0, config.1)
        .must_await_full_state(server_name)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
}

pub fn to_test_tuple(config: &RoomSyncConfig) -> (i64, BTreeMap<String, Vec<String>>) {
    (
        config.timeline_limit,
        config
            .required_state_map
            .iter()
            .map(|(k, v)| (k.clone(), v.iter().cloned().collect()))
            .collect(),
    )
}

pub fn register_test_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(
        room_sync_config_from_room_config_for_test,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(combine_room_sync_config_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(must_await_full_state_for_test, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rs(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(t, k)| (t.to_string(), k.to_string()))
            .collect()
    }

    fn map(entries: &[(&str, &[&str])]) -> RequiredStateMap {
        entries
            .iter()
            .map(|(t, keys)| (t.to_string(), keys.iter().map(|k| k.to_string()).collect()))
            .collect()
    }

    #[test]
    fn wildcards_collapse() {
        let config = RoomSyncConfig::from_room_config(
            5,
            &rs(&[
                ("m.a", "x"),
                ("m.a", "y"),
                ("*", "x"),
                ("m.b", "*"),
                ("m.b", "z"),
            ]),
        );
        assert_eq!(
            config.required_state_map,
            map(&[("m.a", &["y"]), ("*", &["x"]), ("m.b", &["*"])])
        );

        let config =
            RoomSyncConfig::from_room_config(5, &rs(&[("m.a", "x"), ("*", "*"), ("m.b", "y")]));
        assert_eq!(config.required_state_map, map(&[("*", &["*"])]));
    }

    #[test]
    fn combine_takes_the_union() {
        let a = RoomSyncConfig {
            timeline_limit: 1,
            required_state_map: map(&[("m.a", &["x"]), ("m.member", &["$LAZY"])]),
        };
        let b = RoomSyncConfig {
            timeline_limit: 3,
            required_state_map: map(&[("*", &["x"]), ("m.c", &["*", "q"])]),
        };
        let combined = a.combine(&b);
        assert_eq!(combined.timeline_limit, 3);
        assert_eq!(
            combined.required_state_map,
            map(&[("m.member", &["$LAZY"]), ("*", &["x"]), ("m.c", &["*"])])
        );
    }

    #[test]
    fn full_state_needed_for_remote_members() {
        let config = |entries: &[(&str, &[&str])]| RoomSyncConfig {
            timeline_limit: 1,
            required_state_map: map(entries),
        };
        assert!(!config(&[("m.room.member", &["$LAZY", "$ME", "@a:here"])])
            .must_await_full_state("here")
            .unwrap());
        assert!(config(&[("m.room.member", &["@a:there"])])
            .must_await_full_state("here")
            .unwrap());
        assert!(config(&[("*", &["@a:there"])])
            .must_await_full_state("here")
            .unwrap());
        assert!(config(&[("*", &[""])])
            .must_await_full_state("here")
            .is_err());
    }
}
