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

//! `synapse.types.state.StateFilter`: which state to fetch.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use pyo3::{
    prelude::*,
    types::{PyAnyMethods, PyDict},
};

use crate::state::{StateKey, StateMap};

/// Which `(type, state_key)` pairs a reader wants.
///
/// `types` maps a type to the state keys wanted of it, or to `None` for
/// every state key of the type. `include_others` is whether types not in
/// `types` are wanted. As in Python, the filter is kept canonical: with
/// `include_others`, a type wanted in full is left out of `types`, and
/// without it, a type with no state keys is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateFilter {
    types: HashMap<Arc<str>, Option<HashSet<Arc<str>>>>,
    include_others: bool,
}

impl StateFilter {
    pub fn new(types: HashMap<Arc<str>, Option<HashSet<Arc<str>>>>, include_others: bool) -> Self {
        let types = types
            .into_iter()
            .filter(|(_, state_keys)| {
                if include_others {
                    state_keys.is_some()
                } else {
                    state_keys.as_ref().is_none_or(|keys| !keys.is_empty())
                }
            })
            .collect();
        StateFilter {
            types,
            include_others,
        }
    }

    /// A filter that fetches everything.
    pub fn all() -> Self {
        StateFilter {
            types: HashMap::new(),
            include_others: true,
        }
    }

    /// A filter that fetches nothing.
    pub fn none() -> Self {
        StateFilter {
            types: HashMap::new(),
            include_others: false,
        }
    }

    /// `StateFilter.from_types`: the given types, with `None` for every
    /// state key of a type.
    pub fn from_types<'a>(types: impl IntoIterator<Item = (&'a str, Option<&'a str>)>) -> Self {
        let mut map: HashMap<Arc<str>, Option<HashSet<Arc<str>>>> = HashMap::new();
        for (event_type, state_key) in types {
            let entry = map
                .entry(Arc::from(event_type))
                .or_insert_with(|| Some(HashSet::new()));
            match state_key {
                None => *entry = None,
                Some(state_key) => {
                    if let Some(keys) = entry {
                        keys.insert(Arc::from(state_key));
                    }
                }
            }
        }
        StateFilter::new(map, false)
    }

    pub fn types(&self) -> &HashMap<Arc<str>, Option<HashSet<Arc<str>>>> {
        &self.types
    }

    pub fn include_others(&self) -> bool {
        self.include_others
    }

    /// Whether the filter fetches everything.
    pub fn is_full(&self) -> bool {
        self.include_others && self.types.is_empty()
    }

    /// Whether the filter has a wildcard: other types, or every state key
    /// of a type.
    pub fn has_wildcards(&self) -> bool {
        self.include_others || self.types.values().any(|keys| keys.is_none())
    }

    /// Whether the filter wants `(event_type, state_key)`.
    pub fn matches(&self, event_type: &str, state_key: &str) -> bool {
        match self.types.get(event_type) {
            Some(None) => true,
            Some(Some(keys)) => keys.contains(state_key),
            None => self.include_others,
        }
    }

    /// `filter_state`: the entries of `state` the filter wants.
    pub fn filter_state(&self, state: &StateMap) -> StateMap {
        if self.is_full() {
            return state.clone();
        }
        state
            .iter()
            .filter(|(key, _)| self.matches(&key.0, &key.1))
            .map(|(key, event_id)| (key.clone(), Arc::clone(event_id)))
            .collect()
    }

    /// `make_sql_filter_clause`, with the values as parameters numbered from
    /// `first_param`: an SQL expression over `type` and `state_key`, and
    /// the values for it. Empty for a filter that fetches everything, and
    /// `1 = 2` for one that fetches nothing.
    pub fn sql_clause(&self, first_param: usize) -> (String, Vec<&str>) {
        if self.is_full() {
            return (String::new(), Vec::new());
        }
        if !self.include_others && self.types.is_empty() {
            return ("1 = 2".to_owned(), Vec::new());
        }

        fn param<'a>(args: &mut Vec<&'a str>, value: &'a str, first_param: usize) -> String {
            args.push(value);
            format!("${}", first_param + args.len() - 1)
        }
        let mut args: Vec<&str> = Vec::new();
        let mut clauses: Vec<String> = Vec::new();
        // The types in a fixed order, so the statement text repeats.
        let mut types: Vec<_> = self.types.iter().collect();
        types.sort_by(|a, b| a.0.cmp(b.0));
        for (event_type, state_keys) in &types {
            match state_keys {
                None => {
                    let p = param(&mut args, event_type, first_param);
                    clauses.push(format!("(type = {p})"));
                }
                Some(state_keys) => {
                    let mut keys: Vec<&Arc<str>> = state_keys.iter().collect();
                    keys.sort();
                    for state_key in keys {
                        let p1 = param(&mut args, event_type, first_param);
                        let p2 = param(&mut args, state_key, first_param);
                        clauses.push(format!("(type = {p1} AND state_key = {p2})"));
                    }
                }
            }
        }
        let mut clause = clauses.join(" OR ");
        if self.include_others {
            if !clause.is_empty() {
                clause.push_str(" OR ");
            }
            let placeholders: Vec<String> = types
                .iter()
                .map(|(event_type, _)| param(&mut args, event_type, first_param))
                .collect();
            clause.push_str(&format!("type NOT IN ({})", placeholders.join(",")));
        }
        (clause, args)
    }
}

impl<'py> FromPyObject<'_, 'py> for StateFilter {
    type Error = PyErr;

    /// Read a Python `StateFilter`'s `types` and `include_others`.
    fn extract(obj: Borrowed<'_, 'py, PyAny>) -> PyResult<Self> {
        let include_others: bool = obj.getattr("include_others")?.extract()?;
        let types = obj.getattr("types")?;
        let mut map: HashMap<Arc<str>, Option<HashSet<Arc<str>>>> = HashMap::new();
        for item in types.call_method0("items")?.try_iter()? {
            let (event_type, state_keys): (String, Option<HashSet<String>>) = item?.extract()?;
            map.insert(
                Arc::from(event_type),
                state_keys.map(|keys| keys.into_iter().map(Arc::from).collect()),
            );
        }
        Ok(StateFilter::new(map, include_others))
    }
}

/// Convert a [`StateMap`] to a Python dict of `(type, state_key)` to event
/// ID.
pub fn state_map_to_py<'py>(py: Python<'py>, state: &StateMap) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (key, event_id) in state {
        dict.set_item((&*key.0, &*key.1), &**event_id)?;
    }
    Ok(dict)
}

/// A [`StateKey`] from its parts.
pub fn state_key(event_type: &str, state_key: &str) -> StateKey {
    StateKey::new(event_type, state_key)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn map(entries: &[(&str, &str, &str)]) -> StateMap {
        entries
            .iter()
            .map(|(t, k, id)| (StateKey::new(t, k), Arc::from(*id)))
            .collect()
    }

    #[test]
    fn canonical_form_and_predicates() {
        let all = StateFilter::all();
        assert!(all.is_full() && all.has_wildcards() && all.matches("a", "b"));
        let none = StateFilter::none();
        assert!(!none.is_full() && !none.has_wildcards() && !none.matches("a", "b"));

        let members =
            StateFilter::from_types([("m.room.member", Some("@a:x")), ("m.room.name", None)]);
        assert!(!members.is_full() && members.has_wildcards());
        assert!(members.matches("m.room.member", "@a:x"));
        assert!(!members.matches("m.room.member", "@b:x"));
        assert!(members.matches("m.room.name", "anything"));
        assert!(!members.matches("m.room.topic", ""));

        // With `include_others`, a wildcard type is dropped from `types`.
        let mut types = HashMap::new();
        types.insert(Arc::from("m.room.name"), None);
        types.insert(Arc::from("m.room.member"), Some(HashSet::new()));
        let others = StateFilter::new(types, true);
        assert_eq!(others.types().len(), 1);
        assert!(others.matches("m.room.name", ""));
        assert!(!others.matches("m.room.member", "@a:x"));
    }

    #[test]
    fn filters_state_and_builds_sql() {
        let state = map(&[
            ("m.room.member", "@a:x", "$1"),
            ("m.room.member", "@b:x", "$2"),
            ("m.room.name", "", "$3"),
            ("m.room.topic", "", "$4"),
        ]);
        let filter =
            StateFilter::from_types([("m.room.member", Some("@a:x")), ("m.room.name", None)]);
        let filtered = filter.filter_state(&state);
        assert_eq!(filtered.len(), 2);
        assert!(filtered.contains_key(&StateKey::new("m.room.member", "@a:x")));
        assert!(filtered.contains_key(&StateKey::new("m.room.name", "")));

        let (clause, args) = filter.sql_clause(2);
        assert_eq!(clause, "(type = $2 AND state_key = $3) OR (type = $4)");
        assert_eq!(args, vec!["m.room.member", "@a:x", "m.room.name"]);

        let mut types = HashMap::new();
        types.insert(
            Arc::from("m.room.member"),
            Some(HashSet::from([Arc::from("@a:x")])),
        );
        let others = StateFilter::new(types, true);
        let (clause, args) = others.sql_clause(2);
        assert_eq!(clause, "(type = $2 AND state_key = $3) OR type NOT IN ($4)");
        assert_eq!(args, vec!["m.room.member", "@a:x", "m.room.member"]);

        assert_eq!(StateFilter::all().sql_clause(1).0, "");
        assert_eq!(StateFilter::none().sql_clause(1).0, "1 = 2");
    }
}
