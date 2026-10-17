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

//! The relations handler's `get_bundled_aggregations` for a room's
//! timeline: the references, the latest edit and the thread summary of
//! each event, which the serializer adds to its `unsigned`.
//!
//! The relations caches of the Python store are not mirrored: the queries
//! read the database each time. Only limited and newly joined timelines
//! bundle aggregations.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use serde_json::{json, Map, Value};

use crate::events::constants::event_field::M_RELATES_TO;
use crate::events::constants::relation_type;
use crate::events::serialize::{BundleRef, ThreadRef};
use crate::events::Event;
use crate::storage::events::RedactBehaviour;
use crate::storage::sync_store::SyncReader;
use crate::sync::room::Fallback;
use crate::visibility::FilteredEvent;

/// Relation type: m.annotation. Annotations are no longer aggregated.
const ANNOTATION: &str = "m.annotation";

/// A thread's bundled summary.
pub struct ThreadBundle {
    pub latest_event: Event,
    pub count: i64,
    pub current_user_participated: bool,
}

/// One event's bundled aggregations: a `BundledAggregations`.
#[derive(Default)]
pub struct Bundle {
    /// The IDs of the events that reference the event, oldest first.
    pub references: Option<Vec<Arc<str>>>,
    pub replace: Option<Event>,
    pub thread: Option<ThreadBundle>,
}

impl Bundle {
    pub fn as_ref(&self) -> BundleRef<'_> {
        BundleRef {
            references: self.references.as_ref().map(|references| {
                let chunk: Vec<Value> = references
                    .iter()
                    .map(|event_id| json!({ "event_id": &**event_id }))
                    .collect();
                let mut map = Map::new();
                map.insert("chunk".to_owned(), Value::Array(chunk));
                map
            }),
            replace: self.replace.as_ref(),
            thread: self.thread.as_ref().map(|thread| ThreadRef {
                latest_event: &thread.latest_event,
                count: thread.count,
                current_user_participated: thread.current_user_participated,
            }),
        }
    }

    /// The events bundled with the event, which the serializer serializes
    /// too.
    pub fn events(&self) -> impl Iterator<Item = &Event> {
        self.replace
            .iter()
            .chain(self.thread.as_ref().map(|thread| &thread.latest_event))
    }
}

/// The `(rel_type, parent event ID)` of an event's relation, if it has a
/// valid one: `relation_from_event`.
fn relation_of(event: &Event) -> Option<(&str, &str)> {
    let relation = event.content().get_field(M_RELATES_TO)?.as_object()?;
    let rel_type = relation.get("rel_type")?.as_str()?;
    let parent_id = relation.get("event_id")?.as_str()?;
    Some((rel_type, parent_id))
}

/// `get_bundled_aggregations` for `timeline`, by event ID. May include
/// events bundled with others, such as a thread's latest event.
///
/// Fails with [`Fallback`] when a thread's latest reply is from an ignored
/// user: finding the latest reply that is not takes the relations
/// pagination, which is not ported.
pub async fn bundled_aggregations(
    reader: &SyncReader<'_>,
    timeline: &[FilteredEvent],
    user_id: &str,
    ignored_users: &HashSet<Arc<str>>,
) -> anyhow::Result<HashMap<Arc<str>, Bundle>> {
    // The events to bundle aggregations for, in order and without
    // duplicates, and the relation type of those that have a relation.
    let mut event_ids: Vec<Arc<str>> = Vec::new();
    let mut events_by_id: HashMap<Arc<str>, &Event> = HashMap::new();
    let mut relations_by_id: HashMap<Arc<str>, &str> = HashMap::new();
    for filtered in timeline {
        let event = &filtered.event;
        // State events do not get bundled aggregations.
        if event.is_state() {
            continue;
        }
        let event_id: Arc<str> = Arc::from(event.event_id());
        if let Some((rel_type, _)) = relation_of(event) {
            // An edit or an annotation may not have any other event related
            // to it.
            if rel_type == ANNOTATION || rel_type == relation_type::REPLACE {
                continue;
            }
            relations_by_id.insert(Arc::clone(&event_id), rel_type);
        }
        if events_by_id.insert(Arc::clone(&event_id), event).is_none() {
            event_ids.push(event_id);
        }
    }

    let mut bundles: HashMap<Arc<str>, Bundle> = HashMap::new();

    // Threads come first, as a thread's latest event gets aggregations of
    // its own. It is not valid to start a thread on an event which itself
    // relates to another event.
    let thread_candidates: Vec<Arc<str>> = event_ids
        .iter()
        .filter(|id| !relations_by_id.contains_key(*id))
        .cloned()
        .collect();
    let summaries = reader.thread_summary_ids(&thread_candidates).await?;
    let latest_ids: Vec<Arc<str>> = summaries.values().map(|(id, _)| Arc::clone(id)).collect();
    let mut latest_events: HashMap<Arc<str>, Event> = reader
        .events(&latest_ids, RedactBehaviour::Redact, false, false)
        .await?
        .into_iter()
        .map(|event| (Arc::from(event.event_id()), event))
        .collect();

    // The threads with a summary: those whose latest event is known.
    let thread_ids: Vec<Arc<str>> = thread_candidates
        .iter()
        .filter(|id| {
            summaries
                .get(*id)
                .is_some_and(|(latest_id, _)| latest_events.contains_key(latest_id))
        })
        .cloned()
        .collect();
    let not_sent_by_user: Vec<Arc<str>> = thread_ids
        .iter()
        .filter(|id| events_by_id[*id].sender() != user_id)
        .cloned()
        .collect();
    let participated = reader
        .threads_participated(&not_sent_by_user, user_id)
        .await?;
    let ignored: Vec<&str> = ignored_users.iter().map(|user| &**user).collect();
    let ignored_counts = reader
        .threaded_messages_per_user(&thread_ids, &ignored)
        .await?;

    let mut thread_latest: Vec<Event> = Vec::new();
    for event_id in &thread_ids {
        let (latest_id, mut count) = summaries[event_id].clone();
        let Some(latest_event) = latest_events.remove(&latest_id) else {
            continue;
        };
        for ignored_user in ignored_users {
            count -= ignored_counts
                .get(&(Arc::clone(event_id), Arc::clone(ignored_user)))
                .copied()
                .unwrap_or(0);
        }
        if ignored_users.contains(latest_event.sender()) {
            return Err(Fallback(format!(
                "the latest reply in the thread of {event_id} is from an ignored user"
            ))
            .into());
        }
        let current_user_participated =
            events_by_id[event_id].sender() == user_id || participated.contains(event_id);
        thread_latest.push(latest_event.shallow_clone());
        bundles.entry(Arc::clone(event_id)).or_default().thread = Some(ThreadBundle {
            latest_event,
            count,
            current_user_participated,
        });
    }

    // A thread's latest event gets its references and edit bundled too.
    for latest_event in &thread_latest {
        let event_id: Arc<str> = Arc::from(latest_event.event_id());
        if !events_by_id.contains_key(&event_id) {
            events_by_id.insert(Arc::clone(&event_id), latest_event);
            event_ids.push(event_id);
        }
    }

    let references = reader.references_for_events(&event_ids).await?;
    for (event_id, refs) in references {
        let refs: Vec<Arc<str>> = refs
            .iter()
            .filter(|(_, sender)| !ignored_users.contains(sender))
            .map(|(id, _)| Arc::clone(id))
            .collect();
        if !refs.is_empty() {
            bundles.entry(event_id).or_default().references = Some(refs);
        }
    }

    // There is no use in limiting edits by ignored users: the edited event
    // would have been ignored in the first place. Redacted events get no
    // edit.
    let mut unredacted: Vec<Arc<str>> = Vec::new();
    for event_id in &event_ids {
        if !events_by_id[event_id].internal_metadata().redacted()? {
            unredacted.push(Arc::clone(event_id));
        }
    }
    let edit_ids = reader.applicable_edit_ids(&unredacted).await?;
    let edit_id_list: Vec<Arc<str>> = edit_ids.values().cloned().collect();
    let mut edits: HashMap<Arc<str>, Event> = reader
        .events(&edit_id_list, RedactBehaviour::Redact, false, false)
        .await?
        .into_iter()
        .map(|event| (Arc::from(event.event_id()), event))
        .collect();
    for (event_id, edit_id) in edit_ids {
        // Two events cannot share an edit, so each is taken once.
        if let Some(edit) = edits.remove(&edit_id) {
            bundles.entry(event_id).or_default().replace = Some(edit);
        }
    }

    Ok(bundles)
}
