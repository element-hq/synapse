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

//! `filter_to_clause` of `synapse.storage.databases.main.stream`: an event
//! filter as SQL over the `events` table, for the page queries.
//!
//! The SQL narrows the rows the page reads; the caller still checks each
//! event with [`Filter::check`], as the Python does, since not every field
//! is in the SQL (`not_labels`, a `false` `contains_url`).

use tokio_postgres::types::ToSql;

use crate::sync::filter::Filter;

/// One parameter of the filter's SQL.
pub type FilterParam = Box<dyn ToSql + Sync + Send>;

/// The parts of a page query a filter adds.
#[derive(Default)]
pub struct FilterSql {
    /// Joins after `FROM events`.
    pub joins: String,
    /// The conditions, joined by `AND`, or empty for none.
    pub clause: String,
    /// The values of the clause's parameters, in order.
    pub params: Vec<FilterParam>,
    /// Whether the joins can repeat an event, so that the query needs
    /// `DISTINCT`.
    pub distinct: bool,
}

/// `filter_to_clause` with the joins of
/// `_paginate_room_events_by_topological_ordering_txn`. Parameters are
/// numbered from `first_param`.
///
/// `related_by_senders` and `related_by_rel_types` are not supported: the
/// caller must not pass a filter that has them.
pub fn filter_to_sql(filter: &Filter, first_param: usize) -> FilterSql {
    let mut sql = FilterSql::default();
    let mut clauses: Vec<String> = Vec::new();
    let mut next = first_param;
    let mut param = |sql: &mut FilterSql, value: FilterParam| -> String {
        sql.params.push(value);
        let placeholder = format!("${next}");
        next += 1;
        placeholder
    };

    // Event types, with `*` as a wildcard.
    if let Some(types) = filter.types.as_ref().filter(|types| !types.is_empty()) {
        let mut type_clauses = Vec::new();
        for event_type in types {
            if event_type.contains('*') {
                let p = param(&mut sql, Box::new(event_type.replace('*', "%")));
                type_clauses.push(format!("type LIKE {p}"));
            } else {
                let p = param(&mut sql, Box::new(event_type.clone()));
                type_clauses.push(format!("type = {p}"));
            }
        }
        clauses.push(format!("({})", type_clauses.join(" OR ")));
    }
    for event_type in &filter.not_types {
        if event_type.contains('*') {
            let p = param(&mut sql, Box::new(event_type.replace('*', "%")));
            clauses.push(format!("type NOT LIKE {p}"));
        } else {
            let p = param(&mut sql, Box::new(event_type.clone()));
            clauses.push(format!("type != {p}"));
        }
    }

    if let Some(senders) = filter.senders.as_ref().filter(|s| !s.is_empty()) {
        let ors: Vec<String> = senders
            .iter()
            .map(|sender| format!("sender = {}", param(&mut sql, Box::new(sender.clone()))))
            .collect();
        clauses.push(format!("({})", ors.join(" OR ")));
    }
    for sender in &filter.not_senders {
        let p = param(&mut sql, Box::new(sender.clone()));
        clauses.push(format!("sender != {p}"));
    }

    if let Some(rooms) = filter.rooms.as_ref().filter(|r| !r.is_empty()) {
        let ors: Vec<String> = rooms
            .iter()
            .map(|room| format!("room_id = {}", param(&mut sql, Box::new(room.clone()))))
            .collect();
        clauses.push(format!("({})", ors.join(" OR ")));
    }
    for room in &filter.not_rooms {
        let p = param(&mut sql, Box::new(room.clone()));
        clauses.push(format!("room_id != {p}"));
    }

    // Only a `true` filter is applied here, as in the Python. The event
    // check applies a `false` one.
    if filter.contains_url == Some(true) {
        let p = param(&mut sql, Box::new(true));
        clauses.push(format!("contains_url = {p}"));
    }

    // Only `labels` is in the SQL. `not_labels` is left to the event check.
    if let Some(labels) = filter.labels.as_ref().filter(|l| !l.is_empty()) {
        // Joining on the labels gives one row per label of an event, so
        // only join when filtering on them.
        sql.joins
            .push_str(" LEFT JOIN event_labels USING (event_id, room_id, topological_ordering)");
        if labels.len() > 1 {
            sql.distinct = true;
        }
        let ors: Vec<String> = labels
            .iter()
            .map(|label| format!("label = {}", param(&mut sql, Box::new(label.clone()))))
            .collect();
        clauses.push(format!("({})", ors.join(" OR ")));
    }

    let rel_types = filter.rel_types.as_ref().filter(|r| !r.is_empty());
    if rel_types.is_some() || !filter.not_rel_types.is_empty() {
        sql.joins
            .push_str(" LEFT JOIN event_relations AS event_relation USING (event_id)");
    }
    if let Some(rel_types) = rel_types {
        let ors: Vec<String> = rel_types
            .iter()
            .map(|rel_type| {
                format!(
                    "event_relation.relation_type = {}",
                    param(&mut sql, Box::new(rel_type.clone()))
                )
            })
            .collect();
        clauses.push(format!("({})", ors.join(" OR ")));
    }
    if !filter.not_rel_types.is_empty() {
        let ands: Vec<String> = filter
            .not_rel_types
            .iter()
            .map(|rel_type| {
                format!(
                    "event_relation.relation_type != {}",
                    param(&mut sql, Box::new(rel_type.clone()))
                )
            })
            .collect();
        clauses.push(format!(
            "(({}) OR event_relation.relation_type IS NULL)",
            ands.join(" AND ")
        ));
    }

    sql.clause = clauses.join(" AND ");
    sql
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::sync::filter::FilterFlags;

    fn sql_for(filter: serde_json::Value) -> FilterSql {
        let flags = FilterFlags {
            msc3874_enabled: true,
            ..FilterFlags::default()
        };
        filter_to_sql(&Filter::new(Some(&filter), flags), 3)
    }

    #[test]
    fn empty_filter() {
        let sql = sql_for(json!({"lazy_load_members": true}));
        assert_eq!(sql.clause, "");
        assert_eq!(sql.joins, "");
        assert!(sql.params.is_empty());
        assert!(!sql.distinct);
    }

    #[test]
    fn types_and_senders() {
        let sql = sql_for(json!({
            "types": ["m.room.message", "m.call.*"],
            "not_types": ["m.room.*"],
            "senders": ["@a:test"],
            "not_senders": ["@b:test", "@c:test"],
        }));
        assert_eq!(
            sql.clause,
            "(type = $3 OR type LIKE $4) AND type NOT LIKE $5 AND (sender = $6) \
             AND sender != $7 AND sender != $8"
        );
        assert_eq!(sql.params.len(), 6);
    }

    #[test]
    fn empty_lists_add_no_sql() {
        // An empty allow list matches nothing, which the event check
        // applies, as in the Python.
        let sql =
            sql_for(json!({"types": [], "senders": [], "rooms": [], "org.matrix.labels": []}));
        assert_eq!(sql.clause, "");
        assert_eq!(sql.joins, "");
    }

    #[test]
    fn contains_url_only_when_true() {
        assert_eq!(
            sql_for(json!({"contains_url": true})).clause,
            "contains_url = $3"
        );
        assert_eq!(sql_for(json!({"contains_url": false})).clause, "");
    }

    #[test]
    fn labels_join_and_distinct() {
        let one = sql_for(json!({"org.matrix.labels": ["#a"]}));
        assert!(one.joins.contains("event_labels"));
        assert!(!one.distinct);
        assert_eq!(one.clause, "(label = $3)");
        let two =
            sql_for(json!({"org.matrix.labels": ["#a", "#b"], "org.matrix.not_labels": ["#c"]}));
        assert!(two.distinct);
        assert_eq!(two.clause, "(label = $3 OR label = $4)");
    }

    #[test]
    fn rel_types() {
        let sql = sql_for(json!({
            "org.matrix.msc3874.rel_types": ["m.thread"],
            "org.matrix.msc3874.not_rel_types": ["m.replace", "m.annotation"],
        }));
        assert!(sql.joins.contains("event_relations AS event_relation"));
        assert_eq!(
            sql.clause,
            "(event_relation.relation_type = $3) AND ((event_relation.relation_type != $4 \
             AND event_relation.relation_type != $5) OR event_relation.relation_type IS NULL)"
        );
    }
}
