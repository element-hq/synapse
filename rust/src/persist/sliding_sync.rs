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

//! The changes to the sliding sync tables that a chunk's state delta
//! makes: a port of `_calculate_sliding_sync_table_changes` and
//! `_get_sliding_sync_insert_values_from_state_map` from
//! `synapse/storage/databases/main/events.py`.
//!
//! The step runs before the write transaction and only reads. The reads go
//! through [`SlidingSyncReads`], so that the unit tests can fake them. The
//! result matches Python's except in two orders:
//!
//! - The membership snapshots to insert are sorted by user ID. Python
//!   lists them in the order of the delta's `to_insert`.
//! - The snapshots to delete follow the order of the delta's `to_delete`,
//!   which the Rust state delta sorts by key. Python's is in another order.
//!
//! The write transaction treats both as sets.
//!
//! `_get_sliding_sync_insert_values_from_stripped_state` belongs to the
//! write transaction, which calls it for out-of-band memberships, and is
//! ported with it.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use anyhow::{bail, ensure};
use async_trait::async_trait;
use serde_json::Value;

use super::{extremities::is_mine_id, reader::PersistReader, state_delta::DeltaState};
use crate::{
    events::Event,
    state::{StateKey, StateMap},
    storage::events::RedactBehaviour,
    types::state_filter::StateFilter,
};

const MEMBER: &str = "m.room.member";
const CREATE: &str = "m.room.create";
const ENCRYPTION: &str = "m.room.encryption";
const NAME: &str = "m.room.name";
const TOMBSTONE: &str = "m.room.tombstone";

/// `SLIDING_SYNC_RELEVANT_STATE_SET`: the state the tables have columns
/// for. Each has the empty state key.
const RELEVANT_STATE_TYPES: [&str; 4] = [CREATE, ENCRYPTION, NAME, TOMBSTONE];

/// `SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES`.
pub const DEFAULT_BUMP_EVENT_TYPES: [&str; 7] = [
    CREATE,
    "m.room.message",
    "m.room.encrypted",
    "m.sticker",
    "m.call.invite",
    "m.poll.start",
    "m.beacon_info",
];

/// Whether a state key is in `SLIDING_SYNC_RELEVANT_STATE_SET`.
fn is_relevant(key: &StateKey) -> bool {
    key.1.is_empty() && RELEVANT_STATE_TYPES.contains(&&*key.0)
}

/// The content fields the columns come from. Each is `None` if the
/// content does not have it.
#[derive(Debug, Clone, Default)]
pub struct ColumnFields {
    /// `type`, for `room_type`.
    pub room_type: Option<Value>,
    /// `algorithm`, for `is_encrypted`.
    pub algorithm: Option<Value>,
    /// `name`, for `room_name`.
    pub name: Option<Value>,
    /// `replacement_room`, for `tombstone_successor_room_id`.
    pub replacement_room: Option<Value>,
}

impl ColumnFields {
    fn new(get: impl Fn(&str) -> Option<Value>) -> Self {
        ColumnFields {
            room_type: get("type"),
            algorithm: get("algorithm"),
            name: get("name"),
            replacement_room: get("replacement_room"),
        }
    }
}

/// What the step reads from a state event, whether it is in the chunk or
/// loaded from the database.
#[derive(Debug, Clone)]
pub struct SlidingSyncEvent {
    pub event_id: Arc<str>,
    pub event_type: Arc<str>,
    pub state_key: Arc<str>,
    pub sender: Arc<str>,
    /// `content.membership`, if it is a string.
    pub membership: Option<Arc<str>>,
    /// The content fields of all four columns. Python takes the field from
    /// the type of the state key the event is under, which is the event's
    /// own type unless the state is inconsistent.
    pub columns: ColumnFields,
}

impl SlidingSyncEvent {
    /// The view of a state event. `None` if it is not one.
    pub fn new(event: &Event) -> Option<Self> {
        let state_key = event.state_key()?;
        let content = event.content();
        let membership = match content.get_field("membership") {
            Some(Value::String(membership)) => Some(Arc::from(membership.as_str())),
            _ => None,
        };
        let columns = ColumnFields::new(|field| content.get_field(field).cloned());
        Some(SlidingSyncEvent {
            event_id: Arc::from(event.event_id()),
            event_type: Arc::from(event.type_()),
            state_key: Arc::from(state_key),
            sender: Arc::from(event.sender()),
            membership,
            columns,
        })
    }

    fn key(&self) -> StateKey {
        StateKey(Arc::clone(&self.event_type), Arc::clone(&self.state_key))
    }
}

/// `SlidingSyncStateInsertValues`. Each field is `None` where Python leaves
/// the key out, and `Some(None)` where it sets it to `None`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SlidingSyncStateValues {
    pub room_type: Option<Option<Arc<str>>>,
    pub is_encrypted: Option<bool>,
    pub room_name: Option<Option<Arc<str>>>,
    pub tombstone_successor_room_id: Option<Option<Arc<str>>>,
}

/// `SlidingSyncMembershipSnapshotSharedInsertValues`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SnapshotSharedValues {
    pub state: SlidingSyncStateValues,
    pub has_known_state: Option<bool>,
}

/// `SlidingSyncMembershipInfo`: the values of one membership snapshot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlidingSyncMembershipInfo {
    pub user_id: Arc<str>,
    pub sender: Arc<str>,
    pub membership_event_id: Arc<str>,
    pub membership: Arc<str>,
}

/// `SlidingSyncTableChanges`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SlidingSyncTableChanges {
    pub room_id: Arc<str>,
    /// The bump stamp for a `sliding_sync_joined_rooms` row that does not
    /// exist yet and so has to be inserted in full.
    pub joined_room_bump_stamp_to_fully_insert: Option<i64>,
    /// The values to upsert into `sliding_sync_joined_rooms`.
    pub joined_room_updates: SlidingSyncStateValues,
    /// The values every snapshot in `to_insert_membership_snapshots` shares.
    pub membership_snapshot_shared_insert_values: SnapshotSharedValues,
    /// The snapshots to upsert, sorted by user ID.
    pub to_insert_membership_snapshots: Vec<SlidingSyncMembershipInfo>,
    /// The users whose snapshots to delete.
    pub to_delete_membership_snapshots: Vec<Arc<str>>,
}

/// The reads the step makes.
#[async_trait]
pub trait SlidingSyncReads: Sync {
    /// See [`PersistReader::has_sliding_sync_joined_room`].
    async fn has_sliding_sync_joined_room(&self, room_id: &str) -> anyhow::Result<bool>;

    /// The stream ordering of the room's last event of one of
    /// [`DEFAULT_BUMP_EVENT_TYPES`]. See
    /// [`PersistReader::last_event_pos_in_room`].
    async fn last_bump_stream_ordering(&self, room_id: &str) -> anyhow::Result<Option<i64>>;

    /// `get_partial_filtered_current_state_ids` for the relevant state.
    async fn relevant_current_state_ids(&self, room_id: &str) -> anyhow::Result<StateMap>;

    /// `get_events`: the state events found, redacted where they have been
    /// and leaving out the rejected ones.
    async fn state_events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<Vec<SlidingSyncEvent>>;
}

#[async_trait]
impl SlidingSyncReads for PersistReader<'_> {
    async fn has_sliding_sync_joined_room(&self, room_id: &str) -> anyhow::Result<bool> {
        PersistReader::has_sliding_sync_joined_room(self, room_id).await
    }

    async fn last_bump_stream_ordering(&self, room_id: &str) -> anyhow::Result<Option<i64>> {
        let last = self
            .last_event_pos_in_room(room_id, Some(&DEFAULT_BUMP_EVENT_TYPES))
            .await?;
        Ok(last.map(|(_, pos)| pos.stream))
    }

    async fn relevant_current_state_ids(&self, room_id: &str) -> anyhow::Result<StateMap> {
        let filter = StateFilter::from_types(RELEVANT_STATE_TYPES.map(|t| (t, Some(""))));
        (**self).current_state_ids(room_id, &filter).await
    }

    async fn state_events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<Vec<SlidingSyncEvent>> {
        let events = self
            .events(event_ids, RedactBehaviour::Redact, false, false)
            .await?;
        events
            .iter()
            .map(|event| {
                SlidingSyncEvent::new(event).ok_or_else(|| {
                    anyhow::anyhow!(
                        "current state event {} is not a state event",
                        event.event_id()
                    )
                })
            })
            .collect()
    }
}

/// `_calculate_sliding_sync_table_changes`: what the delta changes in the
/// `sliding_sync_joined_rooms` and `sliding_sync_membership_snapshots`
/// tables. `events` holds the chunk's state events, which save loading
/// them. Fails as Python's assertions do if a state event cannot be found,
/// or if a membership snapshot has no state to work from while the server
/// is still in the room. Also fails for a membership event whose
/// membership is not a string, which event auth rejects. Python passes
/// such a value on to the write transaction.
pub async fn calculate_sliding_sync_table_changes(
    reads: &dyn SlidingSyncReads,
    room_id: &str,
    server_name: &str,
    events: &[SlidingSyncEvent],
    delta: &DeltaState,
) -> anyhow::Result<SlidingSyncTableChanges> {
    let mut changes = SlidingSyncTableChanges {
        room_id: Arc::from(room_id),
        ..Default::default()
    };
    // No state changes when a partial state room resyncs its state.
    if delta.to_insert.is_empty() && delta.to_delete.is_empty() {
        return Ok(changes);
    }

    let event_map: HashMap<&str, &SlidingSyncEvent> = events
        .iter()
        .map(|event| (&*event.event_id, event))
        .collect();

    // A local user's membership leaves the current state only on a state
    // reset out of the room.
    changes.to_delete_membership_snapshots = delta
        .to_delete
        .iter()
        .filter(|key| &*key.0 == MEMBER && is_mine_id(&key.1, server_name))
        .map(|key| Arc::clone(&key.1))
        .collect();

    if !delta.to_insert.is_empty() {
        let mut user_by_membership_event: HashMap<Arc<str>, Arc<str>> = HashMap::new();
        for (key, event_id) in &delta.to_insert {
            if &*key.0 == MEMBER && is_mine_id(&key.1, server_name) {
                user_by_membership_event.insert(Arc::clone(event_id), Arc::clone(&key.1));
            }
        }

        // A state reset back to an earlier membership has no membership
        // event in the chunk.
        let membership_event_ids: Vec<Arc<str>> =
            user_by_membership_event.keys().cloned().collect();
        let membership_events = resolve_events(reads, &event_map, &membership_event_ids).await?;
        let mut snapshots = Vec::with_capacity(user_by_membership_event.len());
        for (event_id, user_id) in user_by_membership_event {
            let event = &membership_events[&event_id];
            let Some(membership) = &event.membership else {
                bail!("membership event {event_id} has no membership");
            };
            snapshots.push(SlidingSyncMembershipInfo {
                user_id,
                sender: Arc::clone(&event.sender),
                membership_event_id: event_id,
                membership: Arc::clone(membership),
            });
        }
        snapshots.sort_by(|a, b| a.user_id.cmp(&b.user_id));

        if !snapshots.is_empty() {
            // The current state as the delta leaves it.
            let mut state_ids = reads.relevant_current_state_ids(room_id).await?;
            for key in &delta.to_delete {
                state_ids.remove(key);
            }
            for (key, event_id) in &delta.to_insert {
                if is_relevant(key) {
                    state_ids.insert(key.clone(), Arc::clone(event_id));
                }
            }
            let state = state_events(reads, &event_map, &state_ids).await?;
            if state.is_empty() {
                // The server left the room and cleared its current state.
                // Another local user, invited or knocking, then left too.
                // The snapshot keeps the state values it had.
                ensure!(
                    delta.no_longer_in_room,
                    "no current state for the membership snapshots of {room_id}"
                );
            } else {
                changes.membership_snapshot_shared_insert_values = SnapshotSharedValues {
                    state: insert_values_from_state_map(&state)?,
                    has_known_state: Some(true),
                };
            }
        }
        changes.to_insert_membership_snapshots = snapshots;
    }

    // Only the state columns of `sliding_sync_joined_rooms`. The write
    // transaction updates the stream ordering and the bump stamp.
    if !delta.no_longer_in_room {
        let mut state_ids = StateMap::new();
        // A missing row is inserted in full, so that a row that exists can
        // be relied on.
        if !reads.has_sliding_sync_joined_room(room_id).await? {
            // A remote room that was just joined can have a backfilled
            // last bump event, whose negative stream ordering cannot be
            // compared. The API uses the membership's position instead.
            changes.joined_room_bump_stamp_to_fully_insert = reads
                .last_bump_stream_ordering(room_id)
                .await?
                .filter(|stream| *stream > 0);
            state_ids = reads.relevant_current_state_ids(room_id).await?;
        }
        for (key, event_id) in &delta.to_insert {
            if is_relevant(key) {
                state_ids.insert(key.clone(), Arc::clone(event_id));
            }
        }
        let state = state_events(reads, &event_map, &state_ids).await?;
        let mut updates = insert_values_from_state_map(&state)?;
        for key in &delta.to_delete {
            if !key.1.is_empty() {
                continue;
            }
            match &*key.0 {
                CREATE => updates.room_type = Some(None),
                ENCRYPTION => updates.is_encrypted = Some(false),
                NAME => updates.room_name = Some(None),
                _ => {}
            }
        }
        changes.joined_room_updates = updates;
    }

    Ok(changes)
}

/// The events `event_ids` name, from the chunk where it has them and from
/// the database otherwise, by event ID. Fails if one cannot be found.
async fn resolve_events(
    reads: &dyn SlidingSyncReads,
    event_map: &HashMap<&str, &SlidingSyncEvent>,
    event_ids: &[Arc<str>],
) -> anyhow::Result<HashMap<Arc<str>, SlidingSyncEvent>> {
    let mut found = HashMap::with_capacity(event_ids.len());
    let mut missing = Vec::new();
    for event_id in event_ids {
        match event_map.get(&**event_id) {
            Some(event) => {
                found.insert(Arc::clone(event_id), (*event).clone());
            }
            None => missing.push(Arc::clone(event_id)),
        }
    }
    if !missing.is_empty() {
        let loaded = reads.state_events(&missing).await?;
        let loaded_ids: HashSet<&str> = loaded.iter().map(|event| &*event.event_id).collect();
        let not_found: Vec<&str> = missing
            .iter()
            .map(|id| &**id)
            .filter(|id| !loaded_ids.contains(id))
            .collect();
        ensure!(not_found.is_empty(), "events not found: {not_found:?}");
        for event in loaded {
            found.insert(Arc::clone(&event.event_id), event);
        }
    }
    Ok(found)
}

/// The events of a state map, keyed as Python keys them: by the map's key
/// for an event in the chunk, and by the event's own type and state key
/// for one loaded from the database.
async fn state_events(
    reads: &dyn SlidingSyncReads,
    event_map: &HashMap<&str, &SlidingSyncEvent>,
    state_ids: &StateMap,
) -> anyhow::Result<HashMap<StateKey, SlidingSyncEvent>> {
    let mut state = HashMap::with_capacity(state_ids.len());
    let mut missing = Vec::new();
    for (key, event_id) in state_ids {
        match event_map.get(&**event_id) {
            Some(event) => {
                state.insert(key.clone(), (*event).clone());
            }
            None => missing.push(Arc::clone(event_id)),
        }
    }
    if !missing.is_empty() {
        let loaded = resolve_events(reads, &HashMap::new(), &missing).await?;
        for event in loaded.into_values() {
            state.insert(event.key(), event);
        }
    }
    Ok(state)
}

/// Whether a column value can be stored: a string without a null byte,
/// which Postgres does not allow in text columns, or `None`. The value to
/// store, if so.
fn text_column(value: Option<&Value>) -> Option<Option<Arc<str>>> {
    match value {
        None | Some(Value::Null) => Some(None),
        Some(Value::String(s)) if !s.contains('\0') => Some(Some(Arc::from(s.as_str()))),
        Some(_) => None,
    }
}

/// `_get_sliding_sync_insert_values_from_state_map`.
pub fn insert_values_from_state_map(
    state: &HashMap<StateKey, SlidingSyncEvent>,
) -> anyhow::Result<SlidingSyncStateValues> {
    let mut values = SlidingSyncStateValues::default();
    for (key, event) in state {
        let columns = &event.columns;
        match (&*key.0, &*key.1) {
            (CREATE, "") => {
                if let Some(room_type) = text_column(columns.room_type.as_ref()) {
                    values.room_type = Some(room_type);
                }
            }
            (ENCRYPTION, "") => {
                values.is_encrypted = Some(!matches!(columns.algorithm, None | Some(Value::Null)));
            }
            (NAME, "") => {
                if let Some(name) = text_column(columns.name.as_ref()) {
                    values.room_name = Some(name);
                }
            }
            (TOMBSTONE, "") => {
                if let Some(successor) = text_column(columns.replacement_room.as_ref()) {
                    values.tombstone_successor_room_id = Some(successor);
                }
            }
            _ => bail!("unexpected state {key:?} {}", event.event_id),
        }
    }
    Ok(values)
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use serde_json::json;

    use super::*;

    const SERVER: &str = "test";

    fn event(
        event_id: &str,
        event_type: &str,
        state_key: &str,
        content: Value,
    ) -> SlidingSyncEvent {
        let membership = content
            .get("membership")
            .and_then(Value::as_str)
            .map(Arc::from);
        let columns = ColumnFields::new(|field| content.get(field).cloned());
        SlidingSyncEvent {
            event_id: Arc::from(event_id),
            event_type: Arc::from(event_type),
            state_key: Arc::from(state_key),
            sender: Arc::from("@alice:test"),
            membership,
            columns,
        }
    }

    #[derive(Default)]
    struct FakeReads {
        joined_room_row: bool,
        last_bump: Option<i64>,
        current_state: StateMap,
        stored: Vec<SlidingSyncEvent>,
        loaded: Mutex<Vec<Vec<Arc<str>>>>,
    }

    #[async_trait]
    impl SlidingSyncReads for FakeReads {
        async fn has_sliding_sync_joined_room(&self, _room_id: &str) -> anyhow::Result<bool> {
            Ok(self.joined_room_row)
        }

        async fn last_bump_stream_ordering(&self, _room_id: &str) -> anyhow::Result<Option<i64>> {
            Ok(self.last_bump)
        }

        async fn relevant_current_state_ids(&self, _room_id: &str) -> anyhow::Result<StateMap> {
            Ok(self.current_state.clone())
        }

        async fn state_events(
            &self,
            event_ids: &[Arc<str>],
        ) -> anyhow::Result<Vec<SlidingSyncEvent>> {
            self.loaded.lock().unwrap().push(event_ids.to_vec());
            Ok(self
                .stored
                .iter()
                .filter(|event| event_ids.contains(&event.event_id))
                .cloned()
                .collect())
        }
    }

    fn calculate(
        reads: &FakeReads,
        events: &[SlidingSyncEvent],
        delta: &DeltaState,
    ) -> anyhow::Result<SlidingSyncTableChanges> {
        futures::executor::block_on(calculate_sliding_sync_table_changes(
            reads,
            "!room:test",
            SERVER,
            events,
            delta,
        ))
    }

    fn key(event_type: &str, state_key: &str) -> StateKey {
        StateKey::new(event_type, state_key)
    }

    fn insert(entries: &[(&str, &str, &str)]) -> StateMap {
        entries
            .iter()
            .map(|(t, k, id)| (key(t, k), Arc::from(*id)))
            .collect()
    }

    #[test]
    fn column_values() {
        let state: HashMap<StateKey, SlidingSyncEvent> = [
            event("$c", CREATE, "", json!({"type": "m.space"})),
            event("$e", ENCRYPTION, "", json!({"algorithm": null})),
            event("$n", NAME, "", json!({"name": "a\u{0}b"})),
            event("$t", TOMBSTONE, "", json!({"replacement_room": 3})),
        ]
        .into_iter()
        .map(|event| (event.key(), event))
        .collect();
        let values = insert_values_from_state_map(&state).unwrap();
        assert_eq!(
            values,
            SlidingSyncStateValues {
                room_type: Some(Some(Arc::from("m.space"))),
                is_encrypted: Some(false),
                room_name: None,
                tombstone_successor_room_id: None,
            }
        );

        // The key's type picks the field, as in Python.
        let mut wrong = HashMap::new();
        wrong.insert(key(NAME, ""), event("$c", CREATE, "", json!({"name": "n"})));
        assert_eq!(
            insert_values_from_state_map(&wrong).unwrap().room_name,
            Some(Some(Arc::from("n")))
        );
    }

    #[test]
    fn empty_delta_changes_nothing() {
        let reads = FakeReads::default();
        let changes = calculate(&reads, &[], &DeltaState::default()).unwrap();
        assert_eq!(
            changes,
            SlidingSyncTableChanges {
                room_id: Arc::from("!room:test"),
                ..Default::default()
            }
        );
    }

    #[test]
    fn local_join_with_state_from_the_database() {
        let reads = FakeReads {
            last_bump: Some(7),
            current_state: insert(&[(CREATE, "", "$create"), (NAME, "", "$old_name")]),
            stored: vec![
                event("$create", CREATE, "", json!({})),
                event("$old_name", NAME, "", json!({"name": "old"})),
            ],
            ..Default::default()
        };
        let chunk = vec![
            event("$join", MEMBER, "@bob:test", json!({"membership": "join"})),
            event("$name", NAME, "", json!({"name": "new"})),
        ];
        let delta = DeltaState {
            to_insert: insert(&[
                (MEMBER, "@bob:test", "$join"),
                (MEMBER, "@carol:remote", "$remote"),
                (NAME, "", "$name"),
            ]),
            ..Default::default()
        };
        let changes = calculate(&reads, &chunk, &delta).unwrap();
        let expected_state = SlidingSyncStateValues {
            room_type: Some(None),
            room_name: Some(Some(Arc::from("new"))),
            ..Default::default()
        };
        assert_eq!(
            changes.to_insert_membership_snapshots,
            vec![SlidingSyncMembershipInfo {
                user_id: Arc::from("@bob:test"),
                sender: Arc::from("@alice:test"),
                membership_event_id: Arc::from("$join"),
                membership: Arc::from("join"),
            }]
        );
        assert_eq!(
            changes.membership_snapshot_shared_insert_values,
            SnapshotSharedValues {
                state: expected_state.clone(),
                has_known_state: Some(true),
            }
        );
        assert_eq!(changes.joined_room_bump_stamp_to_fully_insert, Some(7));
        assert_eq!(changes.joined_room_updates, expected_state);
        // Only the create event is loaded: the old name is replaced.
        assert_eq!(
            *reads.loaded.lock().unwrap(),
            vec![
                vec![Arc::<str>::from("$create")],
                vec![Arc::from("$create")]
            ]
        );
    }

    #[test]
    fn existing_row_only_takes_the_delta() {
        let reads = FakeReads {
            joined_room_row: true,
            last_bump: Some(7),
            current_state: insert(&[(CREATE, "", "$create")]),
            ..Default::default()
        };
        let chunk = vec![event("$enc", ENCRYPTION, "", json!({"algorithm": "x"}))];
        let delta = DeltaState {
            to_insert: insert(&[(ENCRYPTION, "", "$enc")]),
            to_delete: vec![key(NAME, ""), key(TOMBSTONE, ""), key(MEMBER, "@bob:test")],
            no_longer_in_room: false,
        };
        let changes = calculate(&reads, &chunk, &delta).unwrap();
        assert_eq!(changes.joined_room_bump_stamp_to_fully_insert, None);
        assert_eq!(
            changes.joined_room_updates,
            SlidingSyncStateValues {
                is_encrypted: Some(true),
                room_name: Some(None),
                ..Default::default()
            }
        );
        assert_eq!(
            changes.to_delete_membership_snapshots,
            vec![Arc::<str>::from("@bob:test")]
        );
        assert!(reads.loaded.lock().unwrap().is_empty());
    }

    #[test]
    fn backfilled_bump_event_is_not_a_stamp() {
        let reads = FakeReads {
            last_bump: Some(-3),
            ..Default::default()
        };
        let delta = DeltaState {
            to_delete: vec![key(NAME, "")],
            ..Default::default()
        };
        let changes = calculate(&reads, &[], &delta).unwrap();
        assert_eq!(changes.joined_room_bump_stamp_to_fully_insert, None);
    }

    #[test]
    fn leaving_after_the_server_left_keeps_the_snapshot_state() {
        let reads = FakeReads::default();
        let chunk = vec![event(
            "$leave",
            MEMBER,
            "@bob:test",
            json!({"membership": "leave"}),
        )];
        let mut delta = DeltaState {
            to_insert: insert(&[(MEMBER, "@bob:test", "$leave")]),
            no_longer_in_room: true,
            ..Default::default()
        };
        let changes = calculate(&reads, &chunk, &delta).unwrap();
        assert_eq!(changes.to_insert_membership_snapshots.len(), 1);
        assert_eq!(
            changes.membership_snapshot_shared_insert_values,
            SnapshotSharedValues::default()
        );
        assert_eq!(
            changes.joined_room_updates,
            SlidingSyncStateValues::default()
        );

        delta.no_longer_in_room = false;
        assert!(calculate(&reads, &chunk, &delta).is_err());
    }

    #[test]
    fn missing_event_fails() {
        let reads = FakeReads::default();
        let delta = DeltaState {
            to_insert: insert(&[(MEMBER, "@bob:test", "$gone")]),
            ..Default::default()
        };
        let err = calculate(&reads, &[], &delta).unwrap_err();
        assert!(err.to_string().contains("$gone"), "{err}");
    }
}
