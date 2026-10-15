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

use std::{
    collections::VecDeque,
    sync::{OnceLock, Weak},
    time::Duration,
};

use serde_json::json;
use tokio::sync::Notify;

use super::*;

const DEST: &str = "remote.example";
const NOW_MS: u64 = 1_000_000;

/// What the fake sink does with the next transaction.
enum Answer {
    /// Accept every PDU.
    Accept,
    Fail(fn() -> LoopError),
    /// Wait for the notify, then accept.
    WaitThenAccept(Arc<Notify>),
}

#[derive(Default)]
struct FakeSink {
    answers: Mutex<VecDeque<Answer>>,
    sent: Mutex<Vec<Transaction>>,
    backoff: Mutex<Option<NotRetrying>>,
    next_txn_id: AtomicU64,
}

impl FakeSink {
    fn sent(&self) -> Vec<Transaction> {
        self.sent.lock().unwrap().clone()
    }

    fn answer(&self, answer: Answer) {
        self.answers.lock().unwrap().push_back(answer);
    }
}

#[async_trait]
impl TransactionSink for FakeSink {
    fn now_ms(&self) -> u64 {
        NOW_MS
    }

    async fn backoff(&self, _destination: &str) -> Result<Option<NotRetrying>, LoopError> {
        Ok(*self.backoff.lock().unwrap())
    }

    fn build(&self, destination: &str, pdus: Vec<Value>, edus: Vec<Value>) -> Transaction {
        Transaction {
            transaction_id: self.next_txn_id.fetch_add(1, Ordering::SeqCst).to_string(),
            origin: "origin.example".to_owned(),
            destination: destination.to_owned(),
            origin_server_ts: NOW_MS,
            pdus,
            edus,
        }
    }

    async fn send(
        &self,
        transaction: Transaction,
        _observer: &Observer,
    ) -> Result<SendOutcome, LoopError> {
        let answer = self
            .answers
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or(Answer::Accept);
        self.sent.lock().unwrap().push(transaction.clone());
        match answer {
            Answer::Accept => {}
            Answer::Fail(err) => return Err(err()),
            Answer::WaitThenAccept(notify) => notify.notified().await,
        }
        let acknowledged = transaction
            .pdus
            .iter()
            .filter_map(|pdu| pdu["event_id"].as_str().map(str::to_owned))
            .collect();
        Ok(SendOutcome {
            acknowledged,
            stats: RequestStats::default(),
        })
    }
}

/// A completion, without the parts that are not comparable.
#[derive(Debug, PartialEq)]
struct Completed {
    to_device_stream_id: Option<i64>,
    device_list_stream_id: Option<i64>,
    last_stream_ordering: Option<i64>,
    sticky_room: Option<String>,
    sent_pdus: Option<Vec<String>>,
    edu_types: Option<Vec<String>>,
}

#[derive(Default)]
struct FakeHooks {
    queue: OnceLock<Weak<DestinationQueue>>,
    device_edus: Mutex<VecDeque<DeviceEdus>>,
    device_requests: Mutex<Vec<DeviceEdusRequest>>,
    sticky: Mutex<VecDeque<Option<StickyBatch>>>,
    completed: Mutex<Vec<Completed>>,
    catch_ups: AtomicU64,
}

impl FakeHooks {
    fn completed(&self) -> Vec<Completed> {
        std::mem::take(&mut *self.completed.lock().unwrap())
    }
}

#[async_trait]
impl QueueHooks for FakeHooks {
    async fn catch_up(&self, _observer: &Observer) -> Result<(), LoopError> {
        self.catch_ups.fetch_add(1, Ordering::SeqCst);
        // As Python's, with nothing in the database to catch up on.
        let queue = self.queue.get().and_then(Weak::upgrade).unwrap();
        if queue.last_successful_stream_ordering().is_none() {
            queue.set_catching_up(false);
        }
        Ok(())
    }

    async fn device_edus(
        &self,
        _observer: &Observer,
        request: DeviceEdusRequest,
    ) -> Result<DeviceEdus, LoopError> {
        self.device_requests.lock().unwrap().push(request);
        Ok(self
            .device_edus
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or(DeviceEdus {
                to_device_stream_id: request.last_device_stream_id,
                device_list_stream_id: request.last_device_list_stream_id,
                ..DeviceEdus::default()
            }))
    }

    async fn sticky_backlog(&self, _observer: &Observer) -> Result<Option<StickyBatch>, LoopError> {
        Ok(self.sticky.lock().unwrap().pop_front().flatten())
    }

    async fn complete(
        &self,
        _observer: &Observer,
        completion: Completion,
    ) -> Result<(), LoopError> {
        self.completed.lock().unwrap().push(Completed {
            to_device_stream_id: completion.to_device_stream_id,
            device_list_stream_id: completion.device_list_stream_id,
            last_stream_ordering: completion.last_stream_ordering,
            sticky_room: completion.sticky.map(|(room_id, _)| room_id),
            sent_pdus: completion
                .sent
                .as_ref()
                .map(|sent| sent.acknowledged.clone()),
            edu_types: completion.sent.map(|sent| sent.edu_types),
        });
        Ok(())
    }
}

struct Harness {
    queue: Arc<DestinationQueue>,
    hooks: Arc<FakeHooks>,
    sink: Arc<FakeSink>,
    activity: Arc<Activity>,
}

fn harness_with(msc4354_enabled: bool) -> Harness {
    let hooks = Arc::new(FakeHooks::default());
    let sink = Arc::new(FakeSink::default());
    let activity = Arc::new(Activity::default());
    let queue = DestinationQueue::new(
        QueueConfig {
            destination: DEST.to_owned(),
            should_send_on_this_instance: true,
            msc4354_enabled,
        },
        hooks.clone(),
        sink.clone(),
        Arc::clone(&activity),
        Handle::current(),
    );
    hooks.queue.set(Arc::downgrade(&queue)).unwrap();
    Harness {
        queue,
        hooks,
        sink,
        activity,
    }
}

fn harness() -> Harness {
    harness_with(false)
}

impl Harness {
    /// Wait for the loop to end.
    async fn settle(&self) {
        for _ in 0..1000 {
            if !self.queue.transmission_loop_running() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        panic!("the loop did not end");
    }
}

fn pdu(event_id: &str, stream_ordering: i64) -> Arc<QueuedPdu> {
    Arc::new(QueuedPdu {
        event_id: event_id.to_owned(),
        stream_ordering: Some(stream_ordering),
        origin_server_ts: 1,
        body: PduBody::Json(json!({"event_id": event_id, "depth": 1})),
    })
}

fn edu_types(transaction: &Transaction) -> Vec<&str> {
    transaction
        .edus
        .iter()
        .map(|edu| edu["edu_type"].as_str().unwrap())
        .collect()
}

fn pdu_ids(transaction: &Transaction) -> Vec<&str> {
    transaction
        .pdus
        .iter()
        .map(|pdu| pdu["event_id"].as_str().unwrap())
        .collect()
}

fn presence(user_id: &str) -> PresenceState {
    PresenceState {
        user_id: user_id.to_owned(),
        state: "online".to_owned(),
        last_active_ts: 0,
        status_msg: None,
        currently_active: true,
    }
}

fn receipt(user_id: &str, event_id: &str, thread_id: Option<&str>) -> Receipt {
    Receipt {
        room_id: "!room".to_owned(),
        receipt_type: "m.read".to_owned(),
        user_id: user_id.to_owned(),
        thread_id: thread_id.map(str::to_owned),
        event_ids: vec![event_id.to_owned()],
        data: Map::new(),
    }
}

#[tokio::test]
async fn sends_a_queued_pdu() {
    let h = harness();
    h.queue.send_pdu(pdu("$a", 5));
    h.settle().await;

    let sent = h.sink.sent();
    assert_eq!(sent.len(), 1);
    assert_eq!(pdu_ids(&sent[0]), ["$a"]);
    // Nothing to catch up on, so the first loop leaves catch-up.
    assert_eq!(h.hooks.catch_ups.load(Ordering::SeqCst), 1);
    assert!(!h.queue.catching_up());

    assert_eq!(
        h.hooks.completed(),
        [Completed {
            to_device_stream_id: None,
            device_list_stream_id: None,
            last_stream_ordering: Some(5),
            sticky_room: None,
            sent_pdus: Some(vec!["$a".to_owned()]),
            edu_types: Some(vec![]),
        }]
    );
    assert_eq!(h.queue.pending_pdu_count(), 0);
    assert!(h.activity.idle());
}

#[tokio::test]
async fn fifty_pdus_per_transaction() {
    let h = harness();
    h.queue.set_catching_up(false);
    {
        // Queue them all before the loop runs.
        let mut state = h.queue.state();
        for i in 0..120 {
            let pdu = pdu(&format!("${i}"), i + 1);
            state.pending_pdus.insert(pdu.event_id.clone(), pdu);
        }
    }
    h.queue.attempt_new_transaction();
    h.settle().await;

    let sizes: Vec<_> = h.sink.sent().iter().map(|t| t.pdus.len()).collect();
    assert_eq!(sizes, [50, 50, 20]);
    assert_eq!(pdu_ids(&h.sink.sent()[1])[0], "$50");
}

#[tokio::test]
async fn edus_in_order_and_within_limits() {
    let h = harness();
    h.queue.set_catching_up(false);
    h.hooks.device_edus.lock().unwrap().push_back(DeviceEdus {
        to_device: vec![json!({"message_id": "m1"})],
        to_device_stream_id: 7,
        device_list: vec![Edu {
            edu_type: "m.device_list_update".to_owned(),
            content: json!({}),
        }],
        device_list_stream_id: 9,
    });

    let users: Vec<_> = (0..60).map(|i| presence(&format!("@u{i}:x"))).collect();
    h.queue.send_presence(users, false);
    for i in 0..7 {
        // Each receipt has a thread, so each needs its own EDU.
        h.queue
            .queue_read_receipt(&receipt("@u:x", &format!("$e{i}"), Some("t")));
    }
    h.queue.send_edu(Edu {
        edu_type: "m.other".to_owned(),
        content: json!({}),
    });
    h.settle().await;

    let sent = h.sink.sent();
    assert_eq!(
        edu_types(&sent[0]),
        [
            "m.presence",
            "m.receipt",
            "m.receipt",
            "m.receipt",
            "m.receipt",
            "m.receipt",
            "m.direct_to_device",
            "m.device_list_update",
            "m.other",
        ]
    );
    assert_eq!(
        sent[0].edus[0]["content"]["push"].as_array().unwrap().len(),
        50
    );
    // The rest go in the next transaction.
    assert_eq!(
        edu_types(&sent[1]),
        ["m.presence", "m.receipt", "m.receipt"]
    );
    assert_eq!(
        sent[1].edus[0]["content"]["push"].as_array().unwrap().len(),
        10
    );

    // One presence and five receipt EDUs leave 94 places, ten of them kept
    // for device list updates.
    let requests = h.hooks.device_requests.lock().unwrap().clone();
    assert_eq!(
        requests[0],
        DeviceEdusRequest {
            last_device_stream_id: 0,
            last_device_list_stream_id: 0,
            to_device_limit: 84,
            edu_limit: 94,
        }
    );
    // The positions move on once the transaction is sent.
    assert_eq!(requests[1].last_device_stream_id, 7);
    assert_eq!(requests[1].last_device_list_stream_id, 9);

    let completed = h.hooks.completed();
    assert_eq!(completed[0].to_device_stream_id, Some(7));
    assert_eq!(completed[0].device_list_stream_id, Some(9));
}

#[tokio::test]
async fn positions_move_on_when_nothing_to_send() {
    let h = harness();
    h.queue.set_catching_up(false);
    h.hooks.device_edus.lock().unwrap().push_back(DeviceEdus {
        to_device_stream_id: 4,
        device_list_stream_id: 6,
        ..DeviceEdus::default()
    });
    h.queue.attempt_new_transaction();
    h.settle().await;

    assert!(h.sink.sent().is_empty());
    let state = h.queue.state();
    assert_eq!(state.last_device_stream_id, 4);
    assert_eq!(state.last_device_list_stream_id, 6);
}

#[test]
fn receipts_merge_as_python_does() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let _guard = rt.enter();
    let h = harness();

    h.queue.queue_read_receipt(&receipt("@a:x", "$1", None));
    // The same user without a thread replaces the receipt.
    h.queue.queue_read_receipt(&receipt("@a:x", "$2", None));
    // Another user goes in the same EDU.
    h.queue.queue_read_receipt(&receipt("@b:x", "$3", None));
    // A thread receipt for a user already there needs a new EDU.
    h.queue
        .queue_read_receipt(&receipt("@a:x", "$4", Some("t")));

    let state = h.queue.state();
    assert_eq!(state.pending_receipt_edus.len(), 2);
    assert_eq!(
        Value::Object(state.pending_receipt_edus[0].clone()),
        json!({"!room": {"m.read": {
            "@a:x": {"event_ids": ["$2"], "data": {}},
            "@b:x": {"event_ids": ["$3"], "data": {}},
        }}})
    );
    assert_eq!(
        Value::Object(state.pending_receipt_edus[1].clone()),
        json!({"!room": {"m.read": {
            "@a:x": {"event_ids": ["$4"], "data": {"thread_id": "t"}},
        }}})
    );
    assert!(state.new_data_to_send);
}

#[tokio::test]
async fn keyed_edus_replace_and_go_newest_first() {
    let h = harness();
    h.queue.set_catching_up(false);
    let typing = |room: &str, typing: bool| Edu {
        edu_type: "m.typing".to_owned(),
        content: json!({"room_id": room, "typing": typing}),
    };
    {
        let mut state = h.queue.state();
        state
            .pending_edus_keyed
            .insert(("m.typing".to_owned(), "a".to_owned()), typing("!a", true));
        state
            .pending_edus_keyed
            .insert(("m.typing".to_owned(), "b".to_owned()), typing("!b", true));
    }
    h.queue.send_keyed_edu(typing("!a", false), "a".to_owned());
    h.settle().await;

    let sent = h.sink.sent();
    assert_eq!(
        sent[0].edus,
        [
            typing("!b", true).into_value(),
            typing("!a", false).into_value()
        ]
    );
}

#[tokio::test]
async fn a_failed_send_keeps_the_pdus() {
    let h = harness();
    h.queue.set_catching_up(false);
    h.sink
        .answer(Answer::Fail(|| LoopError::SendFailed("boom".to_owned())));
    h.queue.send_pdu(pdu("$a", 1));
    h.settle().await;

    assert_eq!(h.sink.sent().len(), 1);
    assert!(h.hooks.completed().is_empty());
    assert_eq!(h.queue.pending_pdu_ids(), ["$a"]);

    // The next attempt sends it.
    h.queue.attempt_new_transaction();
    h.settle().await;
    assert_eq!(h.sink.sent().len(), 2);
    assert_eq!(h.queue.pending_pdu_count(), 0);
}

#[tokio::test]
async fn long_backoff_drops_edus_and_catches_up() {
    let h = harness();
    h.queue.set_catching_up(false);
    h.queue.send_presence(vec![presence("@u:x")], false);
    h.queue.queue_read_receipt(&receipt("@u:x", "$r", None));
    {
        let mut state = h.queue.state();
        let pdu = pdu("$a", 1);
        state.pending_pdus.insert(pdu.event_id.clone(), pdu);
    }
    *h.sink.backoff.lock().unwrap() = Some(NotRetrying {
        retry_last_ts: NOW_MS,
        retry_interval: CATCHUP_RETRY_INTERVAL_MS + 1,
    });
    h.queue.attempt_new_transaction();
    h.settle().await;

    assert!(h.sink.sent().is_empty());
    assert!(h.queue.catching_up());
    assert_eq!(h.queue.pending_pdu_count(), 0);
    assert_eq!(h.queue.pending_edu_count(), 0);
    assert!(h.queue.state().pending_receipt_edus.is_empty());
}

#[tokio::test]
async fn short_backoff_keeps_everything() {
    let h = harness();
    h.queue.set_catching_up(false);
    h.queue.send_presence(vec![presence("@u:x")], false);
    *h.sink.backoff.lock().unwrap() = Some(NotRetrying {
        retry_last_ts: NOW_MS,
        retry_interval: 60_000,
    });
    h.queue.send_pdu(pdu("$a", 1));
    h.settle().await;

    assert!(h.sink.sent().is_empty());
    assert!(!h.queue.catching_up());
    assert_eq!(h.queue.pending_pdu_count(), 1);
    assert_eq!(h.queue.pending_edu_count(), 1);
}

#[tokio::test]
async fn pdus_skipped_while_catching_up() {
    let h = harness();
    h.queue.set_last_successful_stream_ordering(Some(3));
    // Keep the loop from running, so the queue stays catching up.
    h.queue.state().transmission_loop_enabled = false;
    h.queue.send_pdu(pdu("$a", 8));

    assert_eq!(h.queue.pending_pdu_count(), 0);
    assert_eq!(h.queue.catchup_last_skipped(), 8);
}

#[tokio::test]
async fn data_queued_mid_send_is_not_lost() {
    let h = harness();
    h.queue.set_catching_up(false);
    let release = Arc::new(Notify::new());
    h.sink.answer(Answer::WaitThenAccept(Arc::clone(&release)));

    h.queue.send_pdu(pdu("$a", 1));
    // Wait for the first send to start.
    for _ in 0..1000 {
        if !h.sink.sent().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(1)).await;
    }
    // The loop is running, so this only marks new data.
    h.queue.send_pdu(pdu("$b", 2));
    assert!(h.queue.transmission_loop_running());
    release.notify_one();
    h.settle().await;

    let sent = h.sink.sent();
    assert_eq!(sent.len(), 2);
    assert_eq!(pdu_ids(&sent[1]), ["$b"]);
}

#[tokio::test]
async fn sticky_backlog_takes_turns() {
    let h = harness_with(true);
    h.queue.set_catching_up(false);
    let batch = |room: &str, event_id: &str| {
        Some(StickyBatch {
            room_id: room.to_owned(),
            position: Arc::new(0i64),
            pdus: vec![pdu(event_id, 100)],
        })
    };
    h.hooks
        .sticky
        .lock()
        .unwrap()
        .extend([batch("!s1", "$s1"), batch("!s2", "$s2"), None]);
    {
        let mut state = h.queue.state();
        for i in 0..120 {
            let pdu = pdu(&format!("${i}"), i + 1);
            state.pending_pdus.insert(pdu.event_id.clone(), pdu);
        }
    }
    h.queue.attempt_new_transaction();
    h.settle().await;

    let firsts: Vec<_> = h
        .sink
        .sent()
        .iter()
        .map(|t| pdu_ids(t)[0].to_owned())
        .collect();
    assert_eq!(firsts, ["$0", "$s1", "$50", "$s2", "$100"]);
    assert!(!h.queue.sticky_backlogged());
    let sticky: Vec<_> = h
        .hooks
        .completed()
        .into_iter()
        .filter_map(|c| c.sticky_room)
        .collect();
    assert_eq!(sticky, ["!s1", "!s2"]);
}

#[tokio::test]
async fn an_emptied_sticky_batch_is_still_completed() {
    let h = harness_with(true);
    h.queue.set_catching_up(false);
    h.hooks.sticky.lock().unwrap().extend([
        Some(StickyBatch {
            room_id: "!s".to_owned(),
            position: Arc::new(0i64),
            pdus: Vec::new(),
        }),
        None,
    ]);
    h.queue.attempt_new_transaction();
    h.settle().await;

    assert!(h.sink.sent().is_empty());
    assert_eq!(
        h.hooks.completed(),
        [Completed {
            to_device_stream_id: None,
            device_list_stream_id: None,
            last_stream_ordering: None,
            sticky_room: Some("!s".to_owned()),
            sent_pdus: None,
            edu_types: None,
        }]
    );
}

#[tokio::test]
async fn shutdown_stops_the_loop() {
    let h = harness();
    h.queue.set_catching_up(false);
    let release = Arc::new(Notify::new());
    h.sink.answer(Answer::WaitThenAccept(release));
    h.queue.send_pdu(pdu("$a", 1));
    for _ in 0..1000 {
        if !h.sink.sent().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(1)).await;
    }

    h.queue.shutdown();
    h.settle().await;
    assert!(h.activity.idle());

    // A shut down queue starts nothing.
    h.queue.send_pdu(pdu("$b", 2));
    assert!(!h.queue.transmission_loop_running());
}

#[test]
fn presence_format_matches_python() {
    let state = PresenceState {
        user_id: "@u:x".to_owned(),
        state: "online".to_owned(),
        last_active_ts: 400,
        status_msg: Some("hi".to_owned()),
        currently_active: false,
    };
    assert_eq!(
        state.format(1000),
        json!({
            "presence": "online",
            "user_id": "@u:x",
            "last_active_ago": 600,
            "status_msg": "hi",
            "currently_active": false,
        })
    );

    // Unset fields are left out, and only "online" says whether the user is
    // active.
    let state = PresenceState {
        state: "unavailable".to_owned(),
        last_active_ts: 0,
        status_msg: Some(String::new()),
        ..state
    };
    assert_eq!(
        state.format(1000),
        json!({"presence": "unavailable", "user_id": "@u:x"})
    );
}

#[test]
fn receipts_with_threads_share_an_edu() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let _guard = rt.enter();
    let h = harness();

    h.queue.queue_read_receipt(&receipt("alice", "$e", None));
    h.queue
        .queue_read_receipt(&receipt("alice", "$e", Some("thread")));
    h.queue.queue_read_receipt(&receipt("bob", "$e", None));
    h.queue
        .queue_read_receipt(&receipt("bob", "$e", Some("diff-thread")));

    let state = h.queue.state();
    let users: Vec<Vec<&String>> = state
        .pending_receipt_edus
        .iter()
        .map(|edu| edu["!room"]["m.read"].as_object().unwrap().keys().collect())
        .collect();
    assert_eq!(users, [["alice", "bob"], ["alice", "bob"]]);
}

#[test]
fn a_loop_ending_does_not_touch_the_next() {
    // The old loop ends after the next one has parked on Python.
    let activity = Arc::new(Activity::default());
    let old = LoopActivity::new(Arc::clone(&activity));
    old.loop_started();
    let next = LoopActivity::new(Arc::clone(&activity));
    next.loop_started();
    next.started();
    old.loop_ended();
    assert!(activity.idle());

    next.finished();
    assert!(!activity.idle());
    next.loop_ended();
    assert!(activity.idle());
}
