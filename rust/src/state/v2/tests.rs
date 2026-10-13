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

//! Ports of the graphs in `tests/state/test_v2.py`.

use std::{cell::Cell, collections::BTreeMap};

use serde_json::json;

use super::*;
use crate::event_auth::{auth_types_for, tests::make_event};
use crate::state::store::MemoryStateResStore;

const ALICE: &str = "@alice:example.com";
const BOB: &str = "@bob:example.com";
const CHARLIE: &str = "@charlie:example.com";
const EVELYN: &str = "@evelyn:example.com";
const ZARA: &str = "@zara:example.com";

const ROOM_ID: &str = "!test:example.com";

const CREATE: &str = "m.room.create";
const MEMBER: &str = "m.room.member";
const POWER_LEVELS: &str = "m.room.power_levels";
const JOIN_RULES: &str = "m.room.join_rules";
const TOPIC: &str = "m.room.topic";
const MESSAGE: &str = "m.room.message";

const V2_ROOM: &RoomVersion = &RoomVersion::V11;
const V21_ROOM: &RoomVersion = &RoomVersion::HYDRA_V11;

/// A fake event, referred to by a node ID. The event ID is
/// `$<node_id>:example.com`.
#[derive(Clone)]
struct FakeEvent {
    id: &'static str,
    sender: &'static str,
    event_type: &'static str,
    state_key: Option<&'static str>,
    content: Value,
}

fn fake(
    id: &'static str,
    sender: &'static str,
    event_type: &'static str,
    state_key: Option<&'static str>,
    content: Value,
) -> FakeEvent {
    FakeEvent {
        id,
        sender,
        event_type,
        state_key,
        content,
    }
}

fn event_id(node_id: &str) -> Arc<str> {
    Arc::from(format!("${node_id}:example.com"))
}

impl FakeEvent {
    /// Build a room v1 event, as `FakeEvent.to_event` does.
    fn to_event(
        &self,
        auth_events: &[Arc<str>],
        prev_events: &[Arc<str>],
        ts: i64,
    ) -> StateResEvent {
        let mut event = json!({
            "auth_events": auth_events.iter().map(|a| json!([&**a, {}])).collect::<Vec<_>>(),
            "prev_events": prev_events.iter().map(|p| json!([&**p, {}])).collect::<Vec<_>>(),
            "event_id": &*event_id(self.id),
            "sender": self.sender,
            "type": self.event_type,
            "content": self.content,
            "origin_server_ts": ts,
            "room_id": ROOM_ID,
        });
        if let Some(state_key) = self.state_key {
            event["state_key"] = json!(state_key);
        }
        StateResEvent::from_rust(make_event(&RoomVersion::V1, event)).unwrap()
    }

    fn auth_types(&self) -> HashSet<(&str, &str)> {
        let content: BTreeMap<Box<str>, Value> =
            serde_json::from_value(self.content.clone()).unwrap();
        // The borrow of `content` is local, so copy the keys out.
        auth_types_for(
            &RoomVersion::V6,
            self.event_type,
            self.sender,
            self.state_key,
            &content,
        )
        .unwrap()
        .into_iter()
        .map(|(t, k)| -> (&str, &str) { (leak(t), leak(k)) })
        .collect()
    }
}

/// Test-only: extend a string's lifetime.
fn leak(s: &str) -> &'static str {
    Box::leak(s.to_owned().into_boxed_str())
}

fn join() -> Value {
    json!({"membership": "join"})
}

fn initial_events() -> Vec<FakeEvent> {
    vec![
        fake("CREATE", ALICE, CREATE, Some(""), json!({"creator": ALICE})),
        fake("IMA", ALICE, MEMBER, Some(ALICE), join()),
        fake(
            "IPOWER",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100}}),
        ),
        fake(
            "IJR",
            ALICE,
            JOIN_RULES,
            Some(""),
            json!({"join_rule": "public"}),
        ),
        fake("IMB", BOB, MEMBER, Some(BOB), join()),
        fake("IMC", CHARLIE, MEMBER, Some(CHARLIE), join()),
        fake("IMZ", ZARA, MEMBER, Some(ZARA), join()),
        fake("START", ZARA, MESSAGE, None, json!({})),
        fake("END", ZARA, MESSAGE, None, json!({})),
    ]
}

const INITIAL_EDGES: &[&str] = &[
    "START", "IMZ", "IMC", "IMB", "IJR", "IPOWER", "IMA", "CREATE",
];

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap()
}

fn key(event_type: &str, state_key: &str) -> StateKey {
    StateKey::new(event_type, state_key)
}

/// The events of a test graph, and the state after each node.
struct Graph {
    event_map: EventMap,
    state_at: HashMap<&'static str, StateMap>,
}

impl Graph {
    /// Port of `build_event_graph`: `INITIAL_EVENTS` plus `events`, with
    /// `edges` as chains of prev events (`[A, B, C]` is A->B and B->C).
    fn build(events: &[FakeEvent], edges: &[&[&'static str]]) -> Graph {
        let mut graph: HashMap<Arc<str>, HashSet<Arc<str>>> = HashMap::new();
        let mut fakes: HashMap<&str, FakeEvent> = HashMap::new();
        for ev in initial_events().into_iter().chain(events.iter().cloned()) {
            graph.insert(Arc::from(ev.id), HashSet::new());
            fakes.insert(ev.id, ev);
        }
        for chain in std::iter::once(INITIAL_EDGES).chain(edges.iter().copied()) {
            for pair in chain.windows(2) {
                graph.get_mut(pair[0]).unwrap().insert(Arc::from(pair[1]));
            }
        }

        let order =
            lexicographical_topological_sort(graph.clone(), |node| Ok(Arc::clone(node))).unwrap();

        let ts = Cell::new(0);
        let mut event_map = EventMap::new();
        let mut state_at: HashMap<&'static str, StateMap> = HashMap::new();
        let rt = runtime();

        for node in order {
            let fake_event = &fakes[&*node];
            let prev_nodes: Vec<&'static str> =
                graph[&node].iter().map(|n| fakes[&**n].id).collect();

            let state_before = match prev_nodes.len() {
                0 => StateMap::new(),
                1 => state_at[prev_nodes[0]].clone(),
                _ => {
                    let sets: Vec<StateMap> =
                        prev_nodes.iter().map(|n| state_at[n].clone()).collect();
                    let store = MemoryStateResStore::new(event_map.clone());
                    rt.block_on(resolve(
                        &store,
                        ROOM_ID,
                        &RoomVersion::V2,
                        &sets,
                        event_map.clone(),
                        None,
                    ))
                    .unwrap()
                    .merged()
                }
            };

            let mut state_after = state_before.clone();
            if let Some(state_key) = fake_event.state_key {
                state_after.insert(
                    key(fake_event.event_type, state_key),
                    event_id(fake_event.id),
                );
            }

            let auth_events: Vec<Arc<str>> = fake_event
                .auth_types()
                .into_iter()
                .filter_map(|k| lookup(&state_before, k).cloned())
                .collect();
            let prev_events: Vec<Arc<str>> = prev_nodes.iter().map(|n| event_id(n)).collect();

            let event = fake_event.to_event(&auth_events, &prev_events, ts.get());
            ts.set(ts.get() + 1);

            state_at.insert(fake_event.id, state_after);
            event_map.insert(event_id(fake_event.id), event);
        }

        Graph {
            event_map,
            state_at,
        }
    }

    fn event(&self, node_id: &str) -> &StateResEvent {
        &self.event_map[&event_id(node_id)]
    }

    /// The state at START with the given events applied.
    fn state(&self, node_ids: &[&str]) -> StateMap {
        let mut state = self.state_at["START"].clone();
        for node_id in node_ids {
            let event = self.event(node_id);
            state.insert(
                key(event.type_(), event.state_key().unwrap()),
                event_id(node_id),
            );
        }
        state
    }

    fn resolve_with_cache(
        &self,
        room_version: &'static RoomVersion,
        state_sets: &[StateMap],
        cache: &ConflictCache,
    ) -> StateMap {
        let store = MemoryStateResStore::new(self.event_map.clone());
        runtime()
            .block_on(resolve(
                &store,
                ROOM_ID,
                room_version,
                state_sets,
                EventMap::new(),
                Some(cache),
            ))
            .unwrap()
            .merged()
    }
}

/// Port of `do_check`: the state at END, excluding keys unchanged since
/// START, must be `expected_state_ids`.
fn do_check(events: &[FakeEvent], edges: &[&[&'static str]], expected_state_ids: &[&str]) {
    let graph = Graph::build(events, edges);

    let mut expected = StateMap::new();
    for node_id in expected_state_ids {
        let event = graph.event(node_id);
        expected.insert(
            key(event.type_(), event.state_key().unwrap()),
            event_id(node_id),
        );
    }

    let start = &graph.state_at["START"];
    let end: StateMap = graph.state_at["END"]
        .iter()
        .filter(|(k, v)| expected.contains_key(*k) || start.get(*k) != Some(*v))
        .map(|(k, v)| (k.clone(), Arc::clone(v)))
        .collect();

    assert_eq!(expected, end);
}

#[test]
fn ban_vs_pl() {
    let events = [
        fake(
            "PA",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake("MA", ALICE, MEMBER, Some(ALICE), join()),
        fake("MB", ALICE, MEMBER, Some(BOB), json!({"membership": "ban"})),
        fake(
            "PB",
            BOB,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
    ];
    let edges: &[&[&str]] = &[&["END", "MB", "MA", "PA", "START"], &["END", "PB", "PA"]];
    do_check(&events, edges, &["PA", "MA", "MB"]);
}

#[test]
fn join_rule_evasion() {
    let events = [
        fake(
            "JR",
            ALICE,
            JOIN_RULES,
            Some(""),
            json!({"join_rules": "private"}),
        ),
        fake("ME", EVELYN, MEMBER, Some(EVELYN), join()),
    ];
    let edges: &[&[&str]] = &[&["END", "JR", "START"], &["END", "ME", "START"]];
    do_check(&events, edges, &["JR"]);
}

#[test]
fn offtopic_pl() {
    let events = [
        fake(
            "PA",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake(
            "PB",
            BOB,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50, CHARLIE: 50}}),
        ),
        fake(
            "PC",
            CHARLIE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50, CHARLIE: 0}}),
        ),
    ];
    let edges: &[&[&str]] = &[&["END", "PC", "PB", "PA", "START"], &["END", "PA"]];
    do_check(&events, edges, &["PC"]);
}

#[test]
fn topic_basic() {
    let events = [
        fake("T1", ALICE, TOPIC, Some(""), json!({})),
        fake(
            "PA1",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake("T2", ALICE, TOPIC, Some(""), json!({})),
        fake(
            "PA2",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 0}}),
        ),
        fake(
            "PB",
            BOB,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake("T3", BOB, TOPIC, Some(""), json!({})),
    ];
    let edges: &[&[&str]] = &[
        &["END", "PA2", "T2", "PA1", "T1", "START"],
        &["END", "T3", "PB", "PA1"],
    ];
    do_check(&events, edges, &["PA2", "T2"]);
}

#[test]
fn topic_reset() {
    let events = [
        fake("T1", ALICE, TOPIC, Some(""), json!({})),
        fake(
            "PA",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake("T2", BOB, TOPIC, Some(""), json!({})),
        fake("MB", ALICE, MEMBER, Some(BOB), json!({"membership": "ban"})),
    ];
    let edges: &[&[&str]] = &[&["END", "MB", "T2", "PA", "T1", "START"], &["END", "T1"]];
    do_check(&events, edges, &["T1", "MB", "PA"]);
}

#[test]
fn topic() {
    let events = [
        fake("T1", ALICE, TOPIC, Some(""), json!({})),
        fake(
            "PA1",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake("T2", ALICE, TOPIC, Some(""), json!({})),
        fake(
            "PA2",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 0}}),
        ),
        fake(
            "PB",
            BOB,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake("T3", BOB, TOPIC, Some(""), json!({})),
        fake("MZ1", ZARA, MESSAGE, None, json!({})),
        fake("T4", ALICE, TOPIC, Some(""), json!({})),
    ];
    let edges: &[&[&str]] = &[
        &["END", "T4", "MZ1", "PA2", "T2", "PA1", "T1", "START"],
        &["END", "MZ1", "T3", "PB", "PA1"],
    ];
    do_check(&events, edges, &["T4", "PA2"]);
}

#[test]
fn mainline_sort() {
    let events = [
        fake("T1", ALICE, TOPIC, Some(""), json!({})),
        fake(
            "PA1",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake("T2", ALICE, TOPIC, Some(""), json!({})),
        fake(
            "PA2",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}, "events": {POWER_LEVELS: 100}}),
        ),
        fake(
            "PB",
            BOB,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake("T3", BOB, TOPIC, Some(""), json!({})),
        fake("T4", ALICE, TOPIC, Some(""), json!({})),
    ];
    let edges: &[&[&str]] = &[
        &["END", "T3", "PA2", "T2", "PA1", "T1", "START"],
        &["END", "T4", "PB", "PA1"],
    ];
    // T3 wins because the other topics point at older power levels. Without
    // mainline ordering we'd pick T4, as it was sent after T3.
    do_check(&events, edges, &["T3", "PA2"]);
}

#[test]
fn lexicographical_sort() {
    let graph: HashMap<Arc<str>, HashSet<Arc<str>>> = [
        ("l", vec!["o"]),
        ("m", vec!["n", "o"]),
        ("n", vec!["o"]),
        ("o", vec![]),
        ("p", vec!["o"]),
    ]
    .into_iter()
    .map(|(k, v)| (Arc::from(k), v.into_iter().map(Arc::from).collect()))
    .collect();

    let sorted = lexicographical_topological_sort(graph, |n| Ok(Arc::clone(n))).unwrap();
    let sorted: Vec<&str> = sorted.iter().map(|s| &**s).collect();
    assert_eq!(sorted, ["o", "l", "n", "m", "p"]);
}

#[test]
fn event_map_none() {
    // Port of `SimpleParamStateTestCase.test_event_map_none`: Bob and Charlie
    // join at the same time.
    let create =
        fake("CREATE", ALICE, CREATE, Some(""), json!({"creator": ALICE})).to_event(&[], &[], 0);
    let alice = fake("IMA", ALICE, MEMBER, Some(ALICE), join()).to_event(
        &[event_id("CREATE")],
        &[event_id("CREATE")],
        1,
    );
    let join_rules = fake(
        "IJR",
        ALICE,
        JOIN_RULES,
        Some(""),
        json!({"join_rule": "public"}),
    )
    .to_event(
        &[event_id("CREATE"), event_id("IMA")],
        &[event_id("IMA")],
        2,
    );
    let bob = fake("IMB", BOB, MEMBER, Some(BOB), join()).to_event(
        &[event_id("CREATE"), event_id("IJR")],
        &[event_id("IJR")],
        3,
    );
    let charlie = fake("IMC", CHARLIE, MEMBER, Some(CHARLIE), join()).to_event(
        &[event_id("CREATE"), event_id("IJR")],
        &[event_id("IJR")],
        4,
    );

    let state_of = |events: &[&StateResEvent]| -> StateMap {
        events
            .iter()
            .map(|e| {
                (
                    key(e.type_(), e.state_key().unwrap()),
                    Arc::from(e.event_id()),
                )
            })
            .collect()
    };
    let at_bob = state_of(&[&create, &alice, &join_rules, &bob]);
    let at_charlie = state_of(&[&create, &alice, &join_rules, &charlie]);
    let expected = state_of(&[&create, &alice, &join_rules, &bob, &charlie]);

    let event_map: EventMap = [create, alice, join_rules, bob, charlie]
        .into_iter()
        .map(|e| (Arc::from(e.event_id()), e))
        .collect();
    let store = MemoryStateResStore::new(event_map);
    let state = runtime()
        .block_on(resolve(
            &store,
            ROOM_ID,
            &RoomVersion::V2,
            &[at_bob, at_charlie],
            EventMap::new(),
            None,
        ))
        .unwrap()
        .merged();
    assert_eq!(state, expected);
}

// The conflict cache tests. All the events branch off START.
//
// PA gives Bob PL 50. T1 and T2 are topic changes by Bob, which only pass
// auth if PA is in the state. ZJ1 and ZJ2 are Zara re-joining on two
// branches, and INV1 and INV2 are invites she sends to Evelyn on each branch,
// which pull the joins into the auth chain difference.

fn cache_graph() -> Graph {
    let events = [
        fake(
            "PA",
            ALICE,
            POWER_LEVELS,
            Some(""),
            json!({"users": {ALICE: 100, BOB: 50}}),
        ),
        fake("T1", BOB, TOPIC, Some(""), json!({})),
        fake("T2", BOB, TOPIC, Some(""), json!({})),
        fake("ZJ1", ZARA, MEMBER, Some(ZARA), join()),
        fake("ZJ2", ZARA, MEMBER, Some(ZARA), join()),
        fake(
            "INV1",
            ZARA,
            MEMBER,
            Some(EVELYN),
            json!({"membership": "invite"}),
        ),
        fake(
            "INV2",
            ZARA,
            MEMBER,
            Some(EVELYN),
            json!({"membership": "invite"}),
        ),
    ];
    let edges: &[&[&str]] = &[
        &["PA", "START"],
        &["T1", "START"],
        &["T2", "START"],
        &["INV1", "ZJ1", "START"],
        &["INV2", "ZJ2", "START"],
    ];
    Graph::build(&events, edges)
}

fn new_cache() -> ConflictCache {
    ConflictCache::new(100_000, Duration::from_secs(3600))
}

#[test]
fn conflict_cache_shared_across_unconflicted_state() {
    // Two resolutions with the same conflicted set but different unconflicted
    // state share a cache entry, and each result keeps its own unconflicted
    // state. The topics aren't authed against Zara's membership, so under v2
    // it isn't part of the cache key.
    for room_version in [V2_ROOM, V21_ROOM] {
        let graph = cache_graph();
        let cache = new_cache();
        let zara_key = key(MEMBER, ZARA);

        let with_zara = [graph.state(&["PA", "T1"]), graph.state(&["PA", "T2"])];
        let without_zara: Vec<StateMap> = with_zara
            .iter()
            .map(|state| {
                state
                    .iter()
                    .filter(|(k, _)| **k != zara_key)
                    .map(|(k, v)| (k.clone(), Arc::clone(v)))
                    .collect()
            })
            .collect();

        let first = graph.resolve_with_cache(room_version, &with_zara, &cache);
        let second = graph.resolve_with_cache(room_version, &without_zara, &cache);
        assert_eq!(cache.len(), 1, "expected a cache hit");

        assert!(first.contains_key(&zara_key));
        assert!(!second.contains_key(&zara_key));

        let mut first_without = first.clone();
        first_without.remove(&zara_key);
        assert_eq!(first_without, second);
    }
}

#[test]
fn conflict_cache_keys_on_base_state() {
    // Under v2 the key includes the unconflicted state the auth checks depend
    // on, so changing the power levels is a miss with a different result.
    let graph = cache_graph();
    let cache = new_cache();

    let powerless = [graph.state(&["T1"]), graph.state(&["T2"])];
    let powered = [graph.state(&["PA", "T1"]), graph.state(&["PA", "T2"])];

    let under_ipower = graph.resolve_with_cache(V2_ROOM, &powerless, &cache);
    let under_pa = graph.resolve_with_cache(V2_ROOM, &powered, &cache);
    assert_eq!(cache.len(), 2, "expected a cache miss");

    // Bob's topics fail auth under IPOWER and pass under PA.
    let topic_key = key(TOPIC, "");
    assert!(!under_ipower.contains_key(&topic_key));
    assert!(under_pa.contains_key(&topic_key));
}

#[test]
fn conflict_cache_key_repartitioned() {
    // A cached result is correct when the same conflicted set is split
    // differently between conflicted and unconflicted keys. In the first call
    // Zara's membership is unconflicted, but ZJ1 and ZJ2 are in the auth
    // chain difference. In the second it is conflicted.
    let graph = cache_graph();
    let cache = new_cache();
    let zara_key = key(MEMBER, ZARA);

    let agreed = [graph.state(&["INV1"]), graph.state(&["INV2"])];
    let conflicting = [graph.state(&["ZJ1", "INV1"]), graph.state(&["ZJ2", "INV2"])];

    let agreed_result = graph.resolve_with_cache(V21_ROOM, &agreed, &cache);
    let warm = graph.resolve_with_cache(V21_ROOM, &conflicting, &cache);
    assert_eq!(cache.len(), 1, "expected a cache hit");

    // The first call's unconflicted state takes precedence.
    assert_eq!(agreed_result[&zara_key], graph.state(&[])[&zara_key]);

    // The second call gets the winner from the cached resolution, the same
    // one it computes from cold.
    cache.clear();
    let cold = graph.resolve_with_cache(V21_ROOM, &conflicting, &cache);
    assert_ne!(warm[&zara_key], agreed_result[&zara_key]);
    assert_eq!(warm, cold);
}

/// Members with an empty state key, chained through their auth events, for
/// the auth chain difference tests.
fn chain_event(node_id: &'static str, auth: &[&StateResEvent]) -> StateResEvent {
    let auth: Vec<Arc<str>> = auth.iter().map(|e| Arc::from(e.event_id())).collect();
    fake(node_id, ALICE, MEMBER, Some(""), json!({})).to_event(&auth, &[], 0)
}

fn auth_difference(
    persisted: &[&StateResEvent],
    unpersisted: &[&StateResEvent],
    state_sets: &[&[(&str, &StateResEvent)]],
) -> HashSet<Arc<str>> {
    let to_map = |events: &[&StateResEvent]| -> EventMap {
        events
            .iter()
            .map(|e| (Arc::from(e.event_id()), (*e).clone()))
            .collect()
    };
    let store = MemoryStateResStore::new(to_map(persisted));
    let state_sets: Vec<StateMap> = state_sets
        .iter()
        .map(|set| {
            set.iter()
                .map(|(t, e)| (key(t, ""), Arc::from(e.event_id())))
                .collect()
        })
        .collect();
    runtime()
        .block_on(get_auth_chain_difference(
            ROOM_ID,
            &state_sets,
            &to_map(unpersisted),
            &store,
            None,
        ))
        .unwrap()
}

fn ids(events: &[&StateResEvent]) -> HashSet<Arc<str>> {
    events.iter().map(|e| Arc::from(e.event_id())).collect()
}

#[test]
fn auth_difference_simple() {
    //  Unpersisted | Persisted
    //           C -|-> B -> A
    let a = chain_event("A", &[]);
    let b = chain_event("B", &[&a]);
    let c = chain_event("C", &[&b]);

    let difference = auth_difference(&[&a, &b], &[&c], &[&[("a", &a), ("b", &b)], &[("c", &c)]]);
    assert_eq!(difference, ids(&[&c]));
}

#[test]
fn auth_difference_multiple_unpersisted_chain() {
    //  Unpersisted | Persisted
    //      D -> C -|-> B -> A
    let a = chain_event("A", &[]);
    let b = chain_event("B", &[&a]);
    let c = chain_event("C", &[&b]);
    let d = chain_event("D", &[&c]);

    let difference = auth_difference(
        &[&a, &b],
        &[&c, &d],
        &[&[("a", &a), ("b", &b)], &[("c", &c), ("d", &d)]],
    );
    assert_eq!(difference, ids(&[&d, &c]));
}

#[test]
fn auth_difference_unpersisted_events_different_sets() {
    //  Unpersisted | Persisted
    //     D --> C -|-> B -> A
    //     E ----^ -|---^
    let a = chain_event("A", &[]);
    let b = chain_event("B", &[&a]);
    let c = chain_event("C", &[&b]);
    let d = chain_event("D", &[&c]);
    let e = chain_event("E", &[&c, &b]);

    let difference = auth_difference(
        &[&a, &b],
        &[&c, &d, &e],
        &[&[("a", &a), ("b", &b), ("e", &e)], &[("c", &c), ("d", &d)]],
    );
    assert_eq!(difference, ids(&[&d, &e]));
}

#[test]
fn power_level_for_sender() {
    // The definition of `creator` depends on the room version.
    for room_version in [&RoomVersion::V10, &RoomVersion::V11] {
        let mut create_content = json!({"room_version": room_version.identifier});
        if !room_version.implicit_room_creator {
            create_content["creator"] = json!(ALICE);
        }
        let make =
            |partial: Value| StateResEvent::from_rust(make_event(room_version, partial)).unwrap();

        let create = make(json!({"room_id": ROOM_ID, "sender": ALICE, "type": CREATE,
                                 "state_key": "", "content": create_content}));
        let member = make(json!({"room_id": ROOM_ID, "sender": ALICE, "type": MEMBER,
                                 "state_key": ALICE, "content": join(),
                                 "auth_events": [create.event_id()],
                                 "prev_events": [create.event_id()]}));
        let pl = make(
            json!({"room_id": ROOM_ID, "sender": ALICE, "type": POWER_LEVELS,
                             "state_key": "",
                             "content": {"users": {ALICE: 100, BOB: 50}, "users_default": 10},
                             "auth_events": [create.event_id(), member.event_id()],
                             "prev_events": [member.event_id()]}),
        );

        let topic = |sender: &str| {
            make(
                json!({"room_id": ROOM_ID, "sender": sender, "type": TOPIC, "state_key": "",
                        "content": {"topic": "Test"},
                        "auth_events": [create.event_id(), member.event_id(), pl.event_id()],
                        "prev_events": [pl.event_id()]}),
            )
        };

        let level_of = |events: &[&StateResEvent], target: &StateResEvent| {
            let mut map: EventMap = events
                .iter()
                .map(|e| (Arc::from(e.event_id()), (*e).clone()))
                .collect();
            map.insert(Arc::from(target.event_id()), target.clone());
            let core = Core {
                room_id: ROOM_ID,
                room_version,
                events: &map,
            };
            core.get_power_level_for_sender(target.event_id()).unwrap()
        };

        for (user, want) in [(ALICE, 100), (BOB, 50), (CHARLIE, 10)] {
            let got = level_of(&[&create, &member, &pl], &topic(user));
            assert_eq!(got, want, "wrong pl for {user} on v{room_version}");
        }

        // The creator alone without a PL is 100, everyone else 0.
        for (user, want) in [(ALICE, 100), (BOB, 0), (CHARLIE, 0)] {
            let got = level_of(&[&create], &topic(user));
            assert_eq!(
                got, want,
                "wrong pl for {user} with no PL event on v{room_version}"
            );
        }
    }
}

#[test]
fn conflict_cache_key_matches_python() {
    // Computed with `synapse.state.v2._conflict_cache_key("!r:x", RoomVersions.V10,
    // {"$b", "$a", "$é"}, {("t", ""): "$c"}).hex()`.
    let conflicted: HashSet<Arc<str>> =
        ["$b", "$a", "$\u{e9}"].into_iter().map(Arc::from).collect();
    let base: StateMap = [(key("t", ""), Arc::from("$c"))].into_iter().collect();
    let digest = conflict_cache_key("!r:x", &RoomVersion::V10, &conflicted, &base);
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(hex, PYTHON_CACHE_KEY);
}

const PYTHON_CACHE_KEY: &str = "1bec55929258a437d466c54109510427ae0450b73ba5b2cbb3fb1755e4e8fa7b";
