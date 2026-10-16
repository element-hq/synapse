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

//! Which soft-failed sticky events (MSC4354) pass auth once the chunk's
//! state delta is applied: a port of `compute_sticky_events_to_un_soft_fail`
//! and `_get_soft_failed_sticky_events_to_recheck` from
//! `synapse/storage/databases/main/sticky_events.py`.
//!
//! The step runs before the write transaction and only reads. The reads go
//! through [`StickyReads`], so that the unit tests can fake them. The
//! caller runs it only when MSC4354 is enabled and the chunk has a delta.

use std::{
    collections::{BTreeSet, HashMap, HashSet},
    sync::Arc,
};

use async_trait::async_trait;
use log::info;

use super::{reader::PersistReader, state_delta::DeltaState};
use crate::{
    event_auth::{auth_types_for_event, build_auth_map, check_state_dependent_auth_rules_with_map},
    events::Event,
    state::{StateKey, StateMap},
    storage::events::RedactBehaviour,
    types::state_filter::StateFilter,
};

const MEMBER: &str = "m.room.member";

/// The state types whose change can change whether an event passes the
/// state-dependent auth rules.
const CRITICAL_AUTH_TYPES: [&str; 3] = ["m.room.join_rules", "m.room.power_levels", MEMBER];

/// What the step reads.
#[async_trait]
pub trait StickyReads: Sync {
    /// `_get_soft_failed_sticky_events_txn`.
    async fn soft_failed_sticky_events(
        &self,
        room_id: &str,
        senders: Option<&[Arc<str>]>,
        now_ms: i64,
    ) -> anyhow::Result<Vec<Arc<str>>>;

    /// `get_partial_filtered_current_state_ids`.
    async fn current_state_ids(
        &self,
        room_id: &str,
        filter: &StateFilter,
    ) -> anyhow::Result<StateMap>;

    /// `get_events_as_list` with `allow_rejected=False`: the events found,
    /// redacted if they have been.
    async fn events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<Vec<Event>>;
}

#[async_trait]
impl StickyReads for PersistReader<'_> {
    async fn soft_failed_sticky_events(
        &self,
        room_id: &str,
        senders: Option<&[Arc<str>]>,
        now_ms: i64,
    ) -> anyhow::Result<Vec<Arc<str>>> {
        PersistReader::soft_failed_sticky_events(self, room_id, senders, now_ms).await
    }

    async fn current_state_ids(
        &self,
        room_id: &str,
        filter: &StateFilter,
    ) -> anyhow::Result<StateMap> {
        (**self).current_state_ids(room_id, filter).await
    }

    async fn events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<Vec<Event>> {
        (**self)
            .events(event_ids, RedactBehaviour::Redact, false, false)
            .await
    }
}

/// `compute_sticky_events_to_un_soft_fail`: the soft-failed sticky events
/// in the room, outside the chunk, that pass the state-dependent auth rules
/// against the current state with `delta` applied. `events` holds the
/// chunk, whose events stand in for loading them as auth events.
///
/// An auth failure keeps the event soft failed. A malformed event, which
/// Python raises as something other than `AuthError`, fails the step.
pub async fn compute_sticky_events_to_un_soft_fail(
    reads: &dyn StickyReads,
    room_id: &str,
    events: &[Event],
    delta: &DeltaState,
    now_ms: i64,
) -> anyhow::Result<HashSet<Arc<str>>> {
    let to_check = soft_failed_sticky_events_to_recheck(reads, room_id, delta, now_ms).await?;
    // The chunk's events aren't in `sticky_events` yet, unless one is being
    // persisted again (de-outliering). Its soft failure won't have changed.
    let in_chunk: HashSet<&str> = events.iter().map(|event| event.event_id()).collect();
    let to_check: Vec<Arc<str>> = to_check
        .into_iter()
        .filter(|event_id| !in_chunk.contains(&**event_id))
        .collect();
    if to_check.is_empty() {
        return Ok(HashSet::new());
    }

    let to_check = reads.events(&to_check).await?;

    // The `(type, state_key)` pairs that auth the events to check.
    let mut needed: HashSet<(&str, &str)> = HashSet::new();
    for event in &to_check {
        needed.extend(auth_types_for_event(event.room_version(), event)?);
    }

    let filter = StateFilter::from_types(
        needed
            .iter()
            .map(|(event_type, state_key)| (*event_type, Some(*state_key))),
    );
    let mut auth_state = reads.current_state_ids(room_id, &filter).await?;
    // The delta isn't in the current state yet.
    for key in &delta.to_delete {
        auth_state.remove(key);
    }
    for (key, event_id) in &delta.to_insert {
        if needed.contains(&(&*key.0, &*key.1)) {
            auth_state.insert(key.clone(), Arc::clone(event_id));
        }
    }

    // Chunk events come first, then the loaded ones, as in Python.
    let chunk_by_id: HashMap<&str, &Event> = events
        .iter()
        .map(|event| (event.event_id(), event))
        .collect();
    let mut auth_events: Vec<&Event> = Vec::new();
    let mut to_fetch: Vec<Arc<str>> = Vec::new();
    for event_id in auth_state.values() {
        match chunk_by_id.get(&**event_id) {
            Some(event) => auth_events.push(event),
            None => to_fetch.push(Arc::clone(event_id)),
        }
    }
    let fetched = reads.events(&to_fetch).await?;
    auth_events.extend(&fetched);
    let auth_map = build_auth_map(auth_events)?;

    let mut passing = HashSet::new();
    for event in &to_check {
        match check_state_dependent_auth_rules_with_map(event, &auth_map) {
            Ok(()) => {
                passing.insert(Arc::from(event.event_id()));
            }
            // Still fails: stays soft failed.
            Err(failure) if failure.is_rejected() => {}
            Err(failure) => return Err(failure.into()),
        }
    }

    if !passing.is_empty() {
        info!(
            "{} soft-failed events now pass current state checks in room {room_id} : {}",
            passing.len(),
            shortstr(&passing),
        );
    }
    Ok(passing)
}

/// `_get_soft_failed_sticky_events_to_recheck`: the soft-failed sticky
/// events whose status `delta` may change.
async fn soft_failed_sticky_events_to_recheck(
    reads: &dyn StickyReads,
    room_id: &str,
    delta: &DeltaState,
    now_ms: i64,
) -> anyhow::Result<Vec<Arc<str>>> {
    // The current state is about to be wiped, so nothing can pass auth
    // against it.
    if delta.no_longer_in_room {
        return Ok(Vec::new());
    }

    let changed_keys = || delta.to_insert.keys().chain(&delta.to_delete);
    let critical_changed: HashSet<&str> = changed_keys()
        .map(|key: &StateKey| &*key.0)
        .filter(|event_type| CRITICAL_AUTH_TYPES.contains(event_type))
        .collect();
    if critical_changed.is_empty() {
        return Ok(Vec::new());
    }

    if critical_changed.len() == 1 && critical_changed.contains(MEMBER) {
        // Only memberships changed, so only the events of those members can
        // change. Sticky invite, kick or ban events would be the exception,
        // which the Client-Server API cannot create.
        let senders: Vec<Arc<str>> = changed_keys()
            .filter(|key| &*key.0 == MEMBER)
            .map(|key| Arc::clone(&key.1))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        return reads
            .soft_failed_sticky_events(room_id, Some(&senders), now_ms)
            .await;
    }

    // The join rules or power levels changed: recheck everything. Auth
    // isn't recursive, and needn't be: an event that becomes allowed and
    // lets others in is itself a change that triggers a recheck.
    reads.soft_failed_sticky_events(room_id, None, now_ms).await
}

/// Python's `shortstr` of the IDs, sorted: a list of up to five, then
/// `...`.
fn shortstr(ids: &HashSet<Arc<str>>) -> String {
    let mut sorted: Vec<&str> = ids.iter().map(|id| &**id).collect();
    sorted.sort_unstable();
    let items: Vec<String> = sorted.iter().take(5).map(|id| format!("'{id}'")).collect();
    let more = if sorted.len() > 5 { ", ..." } else { "" };
    format!("[{}{more}]", items.join(", "))
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use serde_json::{json, Value};

    use super::*;
    use crate::{event_auth::tests::make_event, room_versions::RoomVersion};

    const ROOM: &str = "!room:example.com";
    const ALICE: &str = "@alice:example.com";
    const BOB: &str = "@bob:example.com";
    const CAROL: &str = "@carol:example.com";
    const V1: &RoomVersion = &RoomVersion::V1;

    fn state(
        event_id: &str,
        event_type: &str,
        state_key: &str,
        sender: &str,
        content: Value,
    ) -> Event {
        make_event(
            V1,
            json!({"event_id": event_id, "type": event_type, "state_key": state_key,
                   "sender": sender, "content": content}),
        )
    }

    fn create() -> Event {
        state(
            "$create",
            "m.room.create",
            "",
            ALICE,
            json!({"creator": ALICE}),
        )
    }

    fn member(event_id: &str, user: &str, membership: &str) -> Event {
        state(
            event_id,
            MEMBER,
            user,
            user,
            json!({"membership": membership}),
        )
    }

    fn message(event_id: &str, sender: &str) -> Event {
        make_event(
            V1,
            json!({"event_id": event_id, "type": "m.room.message", "sender": sender,
                   "content": {"body": "sticky"}}),
        )
    }

    fn key(event_type: &str, state_key: &str) -> StateKey {
        StateKey::new(event_type, state_key)
    }

    /// A room where Alice created and joined, and Bob left.
    struct FakeReads {
        /// `(event ID, sender)` of the soft-failed sticky events.
        sticky: Vec<(&'static str, &'static str)>,
        current_state: Vec<Event>,
        stored: Vec<Event>,
        /// The `senders` of each sticky events read.
        sticky_reads: Mutex<Vec<Option<Vec<Arc<str>>>>>,
        /// The event IDs of each events read.
        loaded: Mutex<Vec<Vec<Arc<str>>>>,
    }

    impl FakeReads {
        fn new(sticky: Vec<(&'static str, &'static str)>) -> Self {
            let current_state = vec![
                create(),
                member("$alice", ALICE, "join"),
                member("$bob-leave", BOB, "leave"),
            ];
            let mut stored: Vec<Event> = current_state.iter().map(Event::shallow_clone).collect();
            stored.extend(
                sticky
                    .iter()
                    .map(|(event_id, sender)| message(event_id, sender)),
            );
            FakeReads {
                sticky,
                current_state,
                stored,
                sticky_reads: Mutex::default(),
                loaded: Mutex::default(),
            }
        }
    }

    #[async_trait]
    impl StickyReads for FakeReads {
        async fn soft_failed_sticky_events(
            &self,
            _room_id: &str,
            senders: Option<&[Arc<str>]>,
            _now_ms: i64,
        ) -> anyhow::Result<Vec<Arc<str>>> {
            self.sticky_reads
                .lock()
                .unwrap()
                .push(senders.map(<[_]>::to_vec));
            Ok(self
                .sticky
                .iter()
                .filter(|(_, sender)| {
                    senders.is_none_or(|senders| senders.iter().any(|s| &**s == *sender))
                })
                .map(|(event_id, _)| Arc::from(*event_id))
                .collect())
        }

        async fn current_state_ids(
            &self,
            _room_id: &str,
            filter: &StateFilter,
        ) -> anyhow::Result<StateMap> {
            Ok(self
                .current_state
                .iter()
                .map(|event| (key(event.type_(), event.state_key().unwrap()), event))
                .filter(|(key, _)| {
                    filter
                        .types()
                        .get(&key.0)
                        .is_some_and(|keys| keys.as_ref().is_none_or(|keys| keys.contains(&key.1)))
                })
                .map(|(key, event)| (key, Arc::from(event.event_id())))
                .collect())
        }

        async fn events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<Vec<Event>> {
            self.loaded.lock().unwrap().push(event_ids.to_vec());
            Ok(self
                .stored
                .iter()
                .filter(|event| event_ids.iter().any(|id| &**id == event.event_id()))
                .map(Event::shallow_clone)
                .collect())
        }
    }

    fn delta(to_insert: &[(StateKey, &str)], to_delete: &[StateKey]) -> DeltaState {
        DeltaState {
            to_delete: to_delete.to_vec(),
            to_insert: to_insert
                .iter()
                .map(|(key, event_id)| (key.clone(), Arc::from(*event_id)))
                .collect(),
            no_longer_in_room: false,
        }
    }

    fn compute(
        reads: &FakeReads,
        events: &[Event],
        delta: &DeltaState,
    ) -> anyhow::Result<BTreeSet<String>> {
        let passing = futures::executor::block_on(compute_sticky_events_to_un_soft_fail(
            reads, ROOM, events, delta, 0,
        ))?;
        Ok(passing.iter().map(|id| id.to_string()).collect())
    }

    fn ids(ids: &[&str]) -> BTreeSet<String> {
        ids.iter().map(|id| id.to_string()).collect()
    }

    #[test]
    fn leaving_the_room_rechecks_nothing() {
        let reads = FakeReads::new(vec![("$sticky", BOB)]);
        let mut delta = delta(&[(key(MEMBER, BOB), "$bob-join")], &[]);
        delta.no_longer_in_room = true;
        assert!(compute(&reads, &[], &delta).unwrap().is_empty());
        assert!(reads.sticky_reads.lock().unwrap().is_empty());
    }

    #[test]
    fn non_critical_changes_recheck_nothing() {
        let reads = FakeReads::new(vec![("$sticky", BOB)]);
        let delta = delta(
            &[(key("m.room.topic", ""), "$topic")],
            &[key("m.room.name", "")],
        );
        assert!(compute(&reads, &[], &delta).unwrap().is_empty());
        assert!(reads.sticky_reads.lock().unwrap().is_empty());
    }

    #[test]
    fn membership_changes_recheck_those_senders() {
        let reads = FakeReads::new(vec![("$bob-sticky", BOB), ("$carol-sticky", CAROL)]);
        let bob_join = member("$bob-join", BOB, "join");
        let delta = delta(
            &[
                (key(MEMBER, BOB), "$bob-join"),
                (key("m.room.topic", ""), "$topic"),
            ],
            &[key(MEMBER, CAROL)],
        );
        // Bob's join is in the chunk. Carol's membership goes, so her
        // event still fails.
        assert_eq!(
            compute(&reads, &[bob_join], &delta).unwrap(),
            ids(&["$bob-sticky"])
        );
        assert_eq!(
            *reads.sticky_reads.lock().unwrap(),
            vec![Some(vec![Arc::from(BOB), Arc::from(CAROL)])]
        );
        // The chunk's join isn't loaded.
        let loaded = reads.loaded.lock().unwrap();
        assert_eq!(loaded.len(), 2);
        assert!(!loaded[1].iter().any(|id| &**id == "$bob-join"));
    }

    #[test]
    fn power_level_changes_recheck_everyone() {
        let reads = FakeReads::new(vec![("$alice-sticky", ALICE), ("$bob-sticky", BOB)]);
        let pl = state(
            "$pl",
            "m.room.power_levels",
            "",
            ALICE,
            json!({"users": {ALICE: 100}}),
        );
        let delta = delta(
            &[
                (key("m.room.power_levels", ""), "$pl"),
                (key(MEMBER, CAROL), "$carol-join"),
            ],
            &[],
        );
        // Bob has left, so only Alice's event passes.
        assert_eq!(
            compute(&reads, &[pl], &delta).unwrap(),
            ids(&["$alice-sticky"])
        );
        assert_eq!(*reads.sticky_reads.lock().unwrap(), vec![None]);
    }

    #[test]
    fn join_rules_changes_recheck_everyone() {
        let reads = FakeReads::new(vec![("$alice-sticky", ALICE)]);
        let delta = delta(&[(key("m.room.join_rules", ""), "$jr")], &[]);
        let jr = state(
            "$jr",
            "m.room.join_rules",
            "",
            ALICE,
            json!({"join_rule": "public"}),
        );
        assert_eq!(
            compute(&reads, &[jr], &delta).unwrap(),
            ids(&["$alice-sticky"])
        );
        assert_eq!(*reads.sticky_reads.lock().unwrap(), vec![None]);
    }

    #[test]
    fn events_in_the_chunk_are_not_rechecked() {
        let reads = FakeReads::new(vec![("$alice-sticky", ALICE)]);
        let delta = delta(&[(key(MEMBER, ALICE), "$alice")], &[]);
        let again = message("$alice-sticky", ALICE);
        assert!(compute(&reads, &[again], &delta).unwrap().is_empty());
        assert!(reads.loaded.lock().unwrap().is_empty());
    }

    #[test]
    fn deleted_auth_state_fails() {
        let reads = FakeReads::new(vec![("$alice-sticky", ALICE)]);
        // Alice's membership leaves the state.
        let delta = delta(&[], &[key(MEMBER, ALICE)]);
        assert!(compute(&reads, &[], &delta).unwrap().is_empty());
    }

    #[test]
    fn unneeded_inserts_are_ignored() {
        // Alice's event doesn't need Bob's membership, so his new join
        // isn't loaded.
        let reads = FakeReads::new(vec![("$alice-sticky", ALICE)]);
        let delta = delta(
            &[
                (key("m.room.join_rules", ""), "$jr"),
                (key(MEMBER, BOB), "$bob-join"),
            ],
            &[],
        );
        assert_eq!(
            compute(&reads, &[], &delta).unwrap(),
            ids(&["$alice-sticky"])
        );
        let loaded = reads.loaded.lock().unwrap();
        assert!(!loaded.iter().flatten().any(|id| &**id == "$bob-join"));
    }

    #[test]
    fn malformed_auth_state_fails_the_step() {
        let mut reads = FakeReads::new(vec![("$alice-sticky", ALICE)]);
        // A create event whose sender has no domain.
        let bad_create = state(
            "$create",
            "m.room.create",
            "",
            "@nodomain",
            json!({"creator": ALICE}),
        );
        reads.current_state[0] = bad_create.shallow_clone();
        reads.stored[0] = bad_create;
        let delta = delta(&[(key("m.room.power_levels", ""), "$pl")], &[]);
        let err = compute(&reads, &[], &delta).unwrap_err();
        assert!(
            err.downcast_ref::<crate::event_auth::AuthFailure>()
                .is_some(),
            "{err:#}"
        );
    }

    #[test]
    fn missing_events_are_skipped() {
        // A rejected sticky event doesn't load.
        let mut reads = FakeReads::new(vec![("$alice-sticky", ALICE), ("$rejected", ALICE)]);
        reads.stored.pop();
        let delta = delta(&[(key("m.room.join_rules", ""), "$jr")], &[]);
        assert_eq!(
            compute(&reads, &[], &delta).unwrap(),
            ids(&["$alice-sticky"])
        );
    }

    #[test]
    fn shortstr_matches_python() {
        let few: HashSet<Arc<str>> = ["$b", "$a"].map(Arc::from).into();
        assert_eq!(shortstr(&few), "['$a', '$b']");
        let many: HashSet<Arc<str>> = ["$1", "$2", "$3", "$4", "$5", "$6"].map(Arc::from).into();
        assert_eq!(shortstr(&many), "['$1', '$2', '$3', '$4', '$5', ...]");
    }
}
