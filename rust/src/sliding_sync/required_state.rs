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

//! What a room's `required_state` asks for: the state filter and member
//! sets it derives from the timeline (`_required_state_request`), and how
//! it changed since the connection last sent the room
//! (`_required_state_changes`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use pyo3::{prelude::*, types::PyModule};

use crate::sliding_sync::config::{RequiredStateMap, LAZY, ME, MEMBER, WILDCARD};
use crate::types::state_filter::StateFilter;

/// `MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER`: the most state keys of one
/// type the connection remembers having sent.
pub const MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER: usize = 100;

const ROOM_AVATAR: &str = "m.room.avatar";

/// `_RequiredStateChangesReturn`.
#[derive(Debug, Clone, PartialEq)]
pub struct RequiredStateChanges {
    /// The required state map to store in the room config, or `None` if it
    /// is unchanged.
    pub changed_required_state_map: Option<RequiredStateMap>,
    /// The current state to fetch on top of the deltas, for what the
    /// request newly asks for.
    pub added_state_filter: StateFilter,
    /// Users sent down explicitly before, now lazy-loaded, to add to the
    /// lazy members table.
    pub extra_users_to_add_to_lazy_cache: HashSet<String>,
    /// Users whose membership changed but is not sent down, to remove from
    /// the lazy members table.
    pub lazy_members_invalidated: HashSet<String>,
}

fn from_types(added: &[(String, Option<String>)]) -> StateFilter {
    StateFilter::from_types(added.iter().map(|(t, k)| (t.as_str(), k.as_deref())))
}

/// `_required_state_changes`: compare the required state the connection
/// last stored for the room with the request's.
///
/// `state_deltas` are the keys of the room's state deltas in the token
/// range, in the order of Python's dict.
///
/// Two places depend on iteration order in Python, and the Rust picks one
/// of Python's possible outcomes:
/// - The state keys kept when a type has more than
///   [`MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER`]: Python slices a set,
///   Rust takes the first in sorted order.
/// - The check for explicit members converted to lazy ones reads a loop
///   variable that leaked from the loops before it. With state deltas
///   that is the request's state keys for the last changed type (the
///   order of the deltas). Without, Python's is the last type of a set
///   union, in hash order, and Rust takes `m.room.member`'s.
pub fn required_state_changes(
    user_id: &str,
    prev_required_state_map: &RequiredStateMap,
    request_required_state_map: &RequiredStateMap,
    previously_returned_lazy_user_ids: &HashSet<String>,
    request_lazy_load_user_ids: &HashSet<String>,
    state_deltas: &[(String, String)],
) -> RequiredStateChanges {
    let empty = BTreeSet::new();

    // Lazy members whose membership changed but is not sent down.
    let lazy_members_invalidated: HashSet<String> = state_deltas
        .iter()
        .filter(|(event_type, state_key)| {
            event_type == MEMBER
                && !request_lazy_load_user_ids.contains(state_key)
                && previously_returned_lazy_user_ids.contains(state_key)
        })
        .map(|(_, state_key)| state_key.clone())
        .collect();

    if prev_required_state_map == request_required_state_map {
        // Only lazy members can need fetching.
        let added: Vec<(String, Option<String>)> = request_lazy_load_user_ids
            .difference(previously_returned_lazy_user_ids)
            .map(|user_id| (MEMBER.to_owned(), Some(user_id.clone())))
            .collect();
        return RequiredStateChanges {
            changed_required_state_map: None,
            added_state_filter: if added.is_empty() {
                StateFilter::none()
            } else {
                from_types(&added)
            },
            extra_users_to_add_to_lazy_cache: HashSet::new(),
            lazy_members_invalidated,
        };
    }

    let prev_wildcard = prev_required_state_map.get(WILDCARD).unwrap_or(&empty);
    let request_wildcard = request_required_state_map.get(WILDCARD).unwrap_or(&empty);

    // Everything was fetched before, so nothing is new now.
    if prev_wildcard.contains(WILDCARD) {
        return RequiredStateChanges {
            changed_required_state_map: Some(request_required_state_map.clone()),
            added_state_filter: StateFilter::none(),
            extra_users_to_add_to_lazy_cache: HashSet::new(),
            lazy_members_invalidated,
        };
    }
    // A wildcard type gained state keys: fetch everything.
    if !request_wildcard.is_subset(prev_wildcard) {
        return RequiredStateChanges {
            changed_required_state_map: Some(request_required_state_map.clone()),
            added_state_filter: StateFilter::all(),
            extra_users_to_add_to_lazy_cache: HashSet::new(),
            lazy_members_invalidated,
        };
    }
    // A wildcard type only lost state keys.
    if !prev_wildcard.is_subset(request_wildcard) {
        return RequiredStateChanges {
            changed_required_state_map: Some(request_required_state_map.clone()),
            added_state_filter: StateFilter::none(),
            extra_users_to_add_to_lazy_cache: HashSet::new(),
            lazy_members_invalidated,
        };
    }

    let mut changes: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut added: Vec<(String, Option<String>)> = Vec::new();

    // The changed state keys by type, the types in the order they first
    // appear in the deltas.
    let mut changed_types: Vec<&str> = Vec::new();
    let mut changed_types_to_state_keys: HashMap<&str, BTreeSet<String>> = HashMap::new();
    for (event_type, state_key) in state_deltas {
        changed_types_to_state_keys
            .entry(event_type.as_str())
            .or_insert_with(|| {
                changed_types.push(event_type.as_str());
                BTreeSet::new()
            })
            .insert(state_key.clone());
    }
    let delta_keys: HashSet<(&str, &str)> = state_deltas
        .iter()
        .map(|(t, k)| (t.as_str(), k.as_str()))
        .collect();

    // First, what has been added.
    let all_types: BTreeSet<&String> = prev_required_state_map
        .keys()
        .chain(request_required_state_map.keys())
        .collect();
    for event_type in all_types {
        let old_state_keys = prev_required_state_map.get(event_type).unwrap_or(&empty);
        let request_state_keys = request_required_state_map.get(event_type).unwrap_or(&empty);
        let changed_state_keys = changed_types_to_state_keys
            .get(event_type.as_str())
            .unwrap_or(&empty);

        if old_state_keys == request_state_keys {
            continue;
        }
        if request_state_keys.is_subset(old_state_keys) {
            // Nothing added. Removals are below.
            continue;
        }

        // Adding `$LAZY` is always recorded.
        if event_type == MEMBER
            && !old_state_keys.contains(LAZY)
            && request_state_keys.contains(LAZY)
        {
            changes.insert(event_type.clone(), request_state_keys.clone());
            continue;
        }

        // A removed state key is only forgotten once its state changes.
        let invalidated_state_keys: BTreeSet<&String> = old_state_keys
            .difference(request_state_keys)
            .filter(|key| changed_state_keys.contains(*key))
            .collect();
        let inheritable_previous_state_keys: BTreeSet<&String> = old_state_keys
            .iter()
            .filter(|key| {
                key.as_str() != WILDCARD
                    && key.as_str() != LAZY
                    && !invalidated_state_keys.contains(key)
            })
            .collect();

        let mut changed: BTreeSet<String> = request_state_keys.clone();
        changed.extend(inheritable_previous_state_keys.iter().map(|k| (*k).clone()));
        if changed.len() > MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER {
            changed = request_state_keys.clone();
            if request_state_keys.len() < MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER {
                // Python takes an arbitrary slice of the set, without
                // leaving out the requested keys first.
                let room = MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER - request_state_keys.len();
                changed.extend(
                    inheritable_previous_state_keys
                        .iter()
                        .take(room)
                        .map(|k| (*k).clone()),
                );
            }
        }
        changes.insert(event_type.clone(), changed);

        if old_state_keys.contains(WILDCARD) {
            // Everything of the type was fetched before.
            continue;
        }

        if request_state_keys.contains(WILDCARD) {
            added.push((event_type.clone(), None));
        } else {
            for state_key in request_state_keys.difference(old_state_keys) {
                if state_key == ME {
                    added.push((event_type.clone(), Some(user_id.to_owned())));
                } else if state_key == LAZY {
                    // Lazy members are handled below.
                } else if event_type == MEMBER {
                    // Only explicit members not sent down before.
                    if !previously_returned_lazy_user_ids.contains(state_key) {
                        added.push((event_type.clone(), Some(state_key.clone())));
                    }
                } else {
                    added.push((event_type.clone(), Some(state_key.clone())));
                }
            }
        }
    }

    let mut previously_required_state_members: HashSet<&str> = prev_required_state_map
        .get(MEMBER)
        .map(|keys| keys.iter().map(String::as_str).collect())
        .unwrap_or_default();
    if previously_required_state_members.contains(ME) {
        previously_required_state_members.insert(user_id);
    }

    // The lazy members not returned before, explicitly or lazily.
    for required_user_id in request_lazy_load_user_ids {
        if previously_returned_lazy_user_ids.contains(required_user_id)
            || previously_required_state_members.contains(required_user_id.as_str())
        {
            continue;
        }
        added.push((MEMBER.to_owned(), Some(required_user_id.clone())));
    }

    let added_state_filter = from_types(&added);

    // Then, what has been removed.
    for event_type in &changed_types {
        let mut changed_state_keys = changed_types_to_state_keys[event_type].clone();
        let old_state_keys = prev_required_state_map.get(*event_type).unwrap_or(&empty);
        let request_state_keys = request_required_state_map
            .get(*event_type)
            .unwrap_or(&empty);

        if old_state_keys == request_state_keys {
            continue;
        }

        // `$ME` changes with the user's own state.
        if changed_state_keys.contains(user_id) {
            changed_state_keys.insert(ME.to_owned());
        }

        let invalidated_state_keys: BTreeSet<String> = old_state_keys
            .difference(request_state_keys)
            .filter(|key| changed_state_keys.contains(*key))
            .cloned()
            .collect();

        // Additions were handled above.
        if !request_state_keys.is_subset(old_state_keys) {
            continue;
        }

        if old_state_keys.contains(WILDCARD) != request_state_keys.contains(WILDCARD) {
            changes.insert((*event_type).to_owned(), request_state_keys.clone());
            continue;
        }

        if *event_type == MEMBER {
            let old_lazy = old_state_keys.contains(LAZY);
            let request_lazy = request_state_keys.contains(LAZY);
            if old_lazy != request_lazy {
                changes.insert((*event_type).to_owned(), request_state_keys.clone());
                continue;
            }
            if (old_lazy || request_lazy) && !invalidated_state_keys.is_empty() {
                changes.insert((*event_type).to_owned(), request_state_keys.clone());
                continue;
            }
        }

        if !invalidated_state_keys.is_empty() {
            changes.insert(
                (*event_type).to_owned(),
                old_state_keys
                    .difference(&invalidated_state_keys)
                    .cloned()
                    .collect(),
            );
        }
    }

    // The loop variable Python reads here: see the doc comment.
    let leaked_type = changed_types.last().copied().unwrap_or(MEMBER);
    let leaked_request_state_keys = request_required_state_map
        .get(leaked_type)
        .unwrap_or(&empty);

    // Explicit members, now lazy-loaded, that need no fetching.
    let mut extra_users_to_add_to_lazy_cache = HashSet::new();
    let membership_changes = changes.get(MEMBER).is_some_and(|keys| !keys.is_empty());
    if membership_changes && leaked_request_state_keys.contains(LAZY) {
        for state_key in prev_required_state_map.get(MEMBER).unwrap_or(&empty) {
            if state_key == WILDCARD || state_key == LAZY {
                continue;
            }
            let state_key = if state_key == ME { user_id } else { state_key };
            if !delta_keys.contains(&(MEMBER, state_key)) {
                extra_users_to_add_to_lazy_cache.insert(state_key.to_owned());
            }
        }
    }

    let changed_required_state_map = if changes.is_empty() {
        None
    } else {
        let mut map = prev_required_state_map.clone();
        for (event_type, state_keys) in changes {
            if state_keys.is_empty() {
                map.remove(&event_type);
            } else {
                map.insert(event_type, state_keys);
            }
        }
        Some(map)
    };

    RequiredStateChanges {
        changed_required_state_map,
        added_state_filter,
        extra_users_to_add_to_lazy_cache,
        lazy_members_invalidated,
    }
}

/// One timeline event, as [`required_state_request`] reads it.
pub struct TimelineMember<'a> {
    pub sender: &'a str,
    pub event_type: &'a str,
    pub state_key: Option<&'a str>,
}

/// `_RequiredStateRequest`.
#[derive(Debug, Clone, PartialEq)]
pub struct RequiredStateRequest {
    /// The state the request asks for.
    pub required_state_filter: StateFilter,
    /// Whether `m.room.member` asks for `$LAZY`.
    pub lazy_load_room_members: bool,
    /// The lazy-loaded members, less the explicit ones.
    pub lazy_load_user_ids: HashSet<String>,
    /// The members asked for by user ID (or `$ME`).
    pub explicit_user_state: HashSet<String>,
}

/// `_required_state_request`: the state filter and member sets a room's
/// `required_state` derives, for a user with `membership` in the room and
/// the timeline returned. `state_delta_keys` are the keys of the room's
/// state deltas.
pub fn required_state_request<'a>(
    user_id: &str,
    membership: &str,
    required_state_map: &RequiredStateMap,
    timeline: &[TimelineMember<'a>],
    limited: Option<bool>,
    initial: bool,
    state_delta_keys: impl IntoIterator<Item = (&'a str, &'a str)>,
) -> RequiredStateRequest {
    let mut explicit_user_state: HashSet<String> = HashSet::new();
    let mut lazy_load_user_ids: HashSet<String> = HashSet::new();
    let mut lazy_load_room_members = false;
    let mut required_state_filter = StateFilter::none();

    // No required state for invites and knocks, only stripped state.
    if membership != "invite" && membership != "knock" {
        let wildcard_keys = required_state_map.get(WILDCARD);
        if wildcard_keys.is_some() {
            // `*` state key of `*`, or a wildcard type, which the state
            // filter cannot express: everything.
            required_state_filter = StateFilter::all();
        } else {
            let mut required_state_types: Vec<(String, Option<String>)> = Vec::new();
            let mut state_delta_keys = Some(state_delta_keys);
            for (state_type, state_key_set) in required_state_map {
                for state_key in state_key_set {
                    if state_key == WILDCARD {
                        required_state_types.push((state_type.clone(), None));
                    } else if state_type == MEMBER && state_key == LAZY {
                        lazy_load_room_members = true;

                        // Everyone in the timeline: senders, and the
                        // targets of membership events.
                        let mut timeline_membership: HashSet<&str> = HashSet::new();
                        for event in timeline {
                            timeline_membership.insert(event.sender);
                            if event.event_type == MEMBER {
                                if let Some(state_key) = event.state_key {
                                    timeline_membership.insert(state_key);
                                }
                            }
                        }
                        lazy_load_user_ids
                            .extend(timeline_membership.iter().map(|u| u.to_string()));

                        if limited == Some(true) || initial {
                            for user_id in &timeline_membership {
                                required_state_types
                                    .push((MEMBER.to_owned(), Some(user_id.to_string())));
                            }
                        } else {
                            // Every membership change, for clients that
                            // keep the full member list.
                            required_state_types.push((MEMBER.to_owned(), None));
                            if let Some(keys) = state_delta_keys.take() {
                                lazy_load_user_ids.extend(
                                    keys.into_iter()
                                        .filter(|(event_type, _)| *event_type == MEMBER)
                                        .map(|(_, state_key)| state_key.to_owned()),
                                );
                            }
                        }
                    } else {
                        let normalized = if state_key == ME { user_id } else { state_key };
                        if state_type == MEMBER {
                            explicit_user_state.insert(normalized.to_owned());
                        }
                        required_state_types
                            .push((state_type.clone(), Some(normalized.to_owned())));
                    }
                }
            }
            required_state_filter = from_types(&required_state_types);
        }
    }

    for user_id in &explicit_user_state {
        lazy_load_user_ids.remove(user_id);
    }

    RequiredStateRequest {
        required_state_filter,
        lazy_load_room_members,
        lazy_load_user_ids,
        explicit_user_state,
    }
}

/// `_state_filter_with_meta`: `required_state_filter` plus the heroes'
/// memberships and, with `include_avatar`, the room avatar.
pub fn state_filter_with_meta(
    required_state_filter: &StateFilter,
    hero_user_ids: &[Arc<str>],
    include_avatar: bool,
) -> StateFilter {
    if *required_state_filter == StateFilter::all() {
        return StateFilter::all();
    }
    let mut types: Vec<(&str, Option<&str>)> = hero_user_ids
        .iter()
        .map(|user_id| (MEMBER, Some(&**user_id)))
        .collect();
    if include_avatar {
        types.push((ROOM_AVATAR, Some("")));
    }
    for (event_type, state_keys) in required_state_filter.types() {
        match state_keys {
            None => types.push((event_type, None)),
            Some(keys) => types.extend(keys.iter().map(|key| (&**event_type, Some(&**key)))),
        }
    }
    StateFilter::new(
        StateFilter::from_types(types).types().clone(),
        required_state_filter.include_others(),
    )
}

type PyRequiredStateMap = BTreeMap<String, Vec<String>>;
type PyStateFilter = (BTreeMap<String, Option<Vec<String>>>, bool);

fn map_from_py(map: PyRequiredStateMap) -> RequiredStateMap {
    map.into_iter()
        .map(|(k, v)| (k, v.into_iter().collect()))
        .collect()
}

fn map_to_py(map: &RequiredStateMap) -> PyRequiredStateMap {
    map.iter()
        .map(|(k, v)| (k.clone(), v.iter().cloned().collect()))
        .collect()
}

/// A [`StateFilter`] as `({type: state keys or None}, include_others)`.
pub fn state_filter_to_py(filter: &StateFilter) -> PyStateFilter {
    (
        filter
            .types()
            .iter()
            .map(|(t, keys)| {
                (
                    t.to_string(),
                    keys.as_ref().map(|keys| {
                        let mut keys: Vec<String> = keys.iter().map(|k| k.to_string()).collect();
                        keys.sort();
                        keys
                    }),
                )
            })
            .collect(),
        filter.include_others(),
    )
}

fn sorted(set: HashSet<String>) -> Vec<String> {
    let mut v: Vec<String> = set.into_iter().collect();
    v.sort();
    v
}

/// `_required_state_changes` through the Rust implementation. Returns
/// `(changed_required_state_map, added_state_filter,
/// extra_users_to_add_to_lazy_cache, lazy_members_invalidated)`.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn required_state_changes_for_test(
    user_id: &str,
    prev_required_state_map: PyRequiredStateMap,
    request_required_state_map: PyRequiredStateMap,
    previously_returned_lazy_user_ids: HashSet<String>,
    request_lazy_load_user_ids: HashSet<String>,
    state_deltas: Vec<(String, String)>,
) -> (
    Option<PyRequiredStateMap>,
    PyStateFilter,
    Vec<String>,
    Vec<String>,
) {
    let changes = required_state_changes(
        user_id,
        &map_from_py(prev_required_state_map),
        &map_from_py(request_required_state_map),
        &previously_returned_lazy_user_ids,
        &request_lazy_load_user_ids,
        &state_deltas,
    );
    (
        changes.changed_required_state_map.as_ref().map(map_to_py),
        state_filter_to_py(&changes.added_state_filter),
        sorted(changes.extra_users_to_add_to_lazy_cache),
        sorted(changes.lazy_members_invalidated),
    )
}

/// `_required_state_request` through the Rust implementation, with the
/// timeline as `(sender, type, state_key)`. Returns `(required_state_filter,
/// lazy_load_room_members, lazy_load_user_ids, explicit_user_state)`.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn required_state_request_for_test(
    user_id: &str,
    membership: &str,
    required_state_map: PyRequiredStateMap,
    timeline: Vec<(String, String, Option<String>)>,
    limited: Option<bool>,
    initial: bool,
    state_delta_keys: Vec<(String, String)>,
) -> (PyStateFilter, bool, Vec<String>, Vec<String>) {
    let timeline: Vec<TimelineMember<'_>> = timeline
        .iter()
        .map(|(sender, event_type, state_key)| TimelineMember {
            sender,
            event_type,
            state_key: state_key.as_deref(),
        })
        .collect();
    let request = required_state_request(
        user_id,
        membership,
        &map_from_py(required_state_map),
        &timeline,
        limited,
        initial,
        state_delta_keys
            .iter()
            .map(|(t, k)| (t.as_str(), k.as_str())),
    );
    (
        state_filter_to_py(&request.required_state_filter),
        request.lazy_load_room_members,
        sorted(request.lazy_load_user_ids),
        sorted(request.explicit_user_state),
    )
}

/// `_state_filter_with_meta` through the Rust implementation.
#[pyfunction]
fn state_filter_with_meta_for_test(
    required_state_filter: StateFilter,
    hero_user_ids: Vec<String>,
    include_avatar: bool,
) -> PyStateFilter {
    let heroes: Vec<Arc<str>> = hero_user_ids.into_iter().map(Arc::from).collect();
    state_filter_to_py(&state_filter_with_meta(
        &required_state_filter,
        &heroes,
        include_avatar,
    ))
}

pub fn register_test_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(required_state_changes_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(required_state_request_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(state_filter_with_meta_for_test, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn map(entries: &[(&str, &[&str])]) -> RequiredStateMap {
        entries
            .iter()
            .map(|(t, keys)| (t.to_string(), keys.iter().map(|k| k.to_string()).collect()))
            .collect()
    }

    fn set(items: &[&str]) -> HashSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn unchanged_config_fetches_new_lazy_members() {
        let config = map(&[(MEMBER, &[LAZY])]);
        let changes = required_state_changes(
            "@me:x",
            &config,
            &config,
            &set(&["@a:x"]),
            &set(&["@a:x", "@b:x"]),
            &[(MEMBER.to_owned(), "@c:x".to_owned())],
        );
        assert_eq!(changes.changed_required_state_map, None);
        assert_eq!(
            changes.added_state_filter,
            StateFilter::from_types([(MEMBER, Some("@b:x"))])
        );
        assert!(changes.lazy_members_invalidated.is_empty());
    }

    #[test]
    fn removed_key_is_kept_until_its_state_changes() {
        let prev = map(&[("m.a", &["x", "y"])]);
        let request = map(&[("m.a", &["x"])]);
        let none = HashSet::new();
        let changes = required_state_changes("@me:x", &prev, &request, &none, &none, &[]);
        assert_eq!(changes.changed_required_state_map, None);

        let deltas = [("m.a".to_owned(), "y".to_owned())];
        let changes = required_state_changes("@me:x", &prev, &request, &none, &none, &deltas);
        assert_eq!(changes.changed_required_state_map, Some(request));
    }

    #[test]
    fn explicit_members_converted_to_lazy_join_the_lazy_cache() {
        let prev = map(&[(MEMBER, &["@a:x", ME])]);
        let request = map(&[(MEMBER, &[LAZY])]);
        let none = HashSet::new();
        let changes = required_state_changes("@me:x", &prev, &request, &none, &none, &[]);
        assert_eq!(
            changes.extra_users_to_add_to_lazy_cache,
            set(&["@a:x", "@me:x"])
        );
    }

    #[test]
    fn remembered_keys_are_capped() {
        let old: Vec<String> = (0..150).map(|i| format!("@u{i}:x")).collect();
        let old_refs: Vec<&str> = old.iter().map(String::as_str).collect();
        let prev = map(&[("m.a", &old_refs)]);
        let request = map(&[("m.a", &["new"])]);
        let none = HashSet::new();
        let changes = required_state_changes("@me:x", &prev, &request, &none, &none, &[]);
        let kept = &changes.changed_required_state_map.unwrap()["m.a"];
        assert_eq!(kept.len(), MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER);
        assert!(kept.contains("new"));
    }

    #[test]
    fn lazy_members_from_the_timeline() {
        let config = map(&[(MEMBER, &[LAZY, "@a:x"]), ("m.room.name", &[""])]);
        let timeline = [
            TimelineMember {
                sender: "@a:x",
                event_type: "m.room.message",
                state_key: None,
            },
            TimelineMember {
                sender: "@b:x",
                event_type: MEMBER,
                state_key: Some("@c:x"),
            },
        ];
        let request =
            required_state_request("@me:x", "join", &config, &timeline, Some(true), false, []);
        assert!(request.lazy_load_room_members);
        assert_eq!(request.lazy_load_user_ids, set(&["@b:x", "@c:x"]));
        assert_eq!(request.explicit_user_state, set(&["@a:x"]));
        assert!(request.required_state_filter.matches(MEMBER, "@c:x"));
        assert!(request.required_state_filter.matches("m.room.name", ""));

        // An unlimited incremental sync returns every membership change.
        let request = required_state_request(
            "@me:x",
            "join",
            &config,
            &timeline,
            Some(false),
            false,
            [(MEMBER, "@d:x")],
        );
        assert!(request.required_state_filter.matches(MEMBER, "@anyone:x"));
        assert!(request.lazy_load_user_ids.contains("@d:x"));
    }
}
