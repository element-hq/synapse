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

//! The room summary: the heroes that represent a room to a user.

use std::{collections::HashMap, sync::Arc};

use pyo3::{
    prelude::*,
    types::{PyModule, PyModuleMethods},
};

pub use crate::storage::sync_store::MemberSummary;

/// `extract_heroes_from_room_summary`: up to five users to represent the
/// room from `me`'s point of view, as the spec's room summary section says:
/// joined and invited users first, and failing those, users who left or
/// were banned. `details` maps a membership to its summary, with members
/// already in stream order.
pub fn extract_heroes_from_room_summary(
    details: &HashMap<String, MemberSummary>,
    me: &str,
) -> Vec<Arc<str>> {
    let others = |membership: &str| -> Vec<Arc<str>> {
        details
            .get(membership)
            .map(|summary| {
                summary
                    .members
                    .iter()
                    .filter(|(user_id, _)| &**user_id != me)
                    .map(|(user_id, _)| Arc::clone(user_id))
                    .collect()
            })
            .unwrap_or_default()
    };

    let mut present = others("join");
    present.extend(others("invite"));
    let heroes = if present.is_empty() {
        let mut gone = others("leave");
        gone.extend(others("ban"));
        gone
    } else {
        present
    };
    heroes.into_iter().take(5).collect()
}

/// `extract_heroes_from_room_summary` through the Rust implementation, with
/// `details` as a dict of membership to `(members, count)`.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn extract_heroes_for_test(
    details: HashMap<String, (Vec<(String, String)>, i64)>,
    me: &str,
) -> Vec<String> {
    let details = details
        .into_iter()
        .map(|(membership, (members, count))| {
            (
                membership,
                MemberSummary {
                    members: members
                        .into_iter()
                        .map(|(u, e)| (Arc::from(u), Arc::from(e)))
                        .collect(),
                    count,
                },
            )
        })
        .collect();
    extract_heroes_from_room_summary(&details, me)
        .into_iter()
        .map(|user_id| user_id.to_string())
        .collect()
}

pub fn register_test_functions(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(extract_heroes_for_test, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn summary(users: &[&str]) -> MemberSummary {
        MemberSummary {
            members: users
                .iter()
                .map(|u| (Arc::from(*u), Arc::from("$e")))
                .collect(),
            count: users.len() as i64,
        }
    }

    #[test]
    fn present_members_before_gone_ones_and_never_me() {
        let details = HashMap::from([
            ("join".to_owned(), summary(&["@me:x", "@a:x"])),
            ("invite".to_owned(), summary(&["@b:x"])),
            ("leave".to_owned(), summary(&["@c:x"])),
        ]);
        let heroes = extract_heroes_from_room_summary(&details, "@me:x");
        assert_eq!(heroes, vec![Arc::from("@a:x"), Arc::from("@b:x")]);

        let details = HashMap::from([
            ("join".to_owned(), summary(&["@me:x"])),
            ("leave".to_owned(), summary(&["@c:x"])),
            ("ban".to_owned(), summary(&["@d:x"])),
        ]);
        let heroes = extract_heroes_from_room_summary(&details, "@me:x");
        assert_eq!(heroes, vec![Arc::from("@c:x"), Arc::from("@d:x")]);
    }

    #[test]
    fn at_most_five() {
        let users: Vec<String> = (0..8).map(|i| format!("@u{i}:x")).collect();
        let users: Vec<&str> = users.iter().map(String::as_str).collect();
        let details = HashMap::from([("join".to_owned(), summary(&users))]);
        let heroes = extract_heroes_from_room_summary(&details, "@me:x");
        assert_eq!(heroes.len(), 5);
        assert_eq!(&*heroes[0], "@u0:x");
    }
}
