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

//! Per-destination queues: Python's `PerDestinationQueue` and its
//! transmission loop.
//!
//! A [`DestinationQueue`] holds what is waiting to go to one destination. The
//! first thing queued starts its transmission loop as a tokio task, which
//! sends transactions until there is nothing left and then ends, as the
//! Python loop does.
//!
//! The loop needs the database for to-device messages, device list updates
//! and the stream positions, and runs catch-up and the sticky event backlog.
//! Those stay in Python for now, behind [`QueueHooks`]. A transaction takes
//! two trips to the reactor: one for its device EDUs and one to record that
//! it was sent. A loop that runs out of things to send has asked for device
//! EDUs once more to find that out, as Python's does. Everything else,
//! building and sending the transaction included, runs on tokio.

use std::{
    any::Any,
    sync::{
        atomic::{AtomicBool, AtomicIsize, AtomicU64, Ordering},
        Arc, Mutex, MutexGuard,
    },
};

use async_trait::async_trait;
use pyo3::prelude::*;
use serde_json::{json, Map, Value};
use tokio::{runtime::Handle, task::AbortHandle};

use super::{
    client::{limiter::NotRetrying, ClientError, RequestStats},
    transaction::Transaction,
};
use crate::{deferred::AwaitableObserver, events::Event};

mod ordered;
pub mod python;

#[cfg(test)]
mod tests;

pub use ordered::OrderedMap;

/// If we back off from a destination for longer than this, we drop what we
/// cannot afford to keep and catch up later. In milliseconds.
pub const CATCHUP_RETRY_INTERVAL_MS: u64 = 60 * 60 * 1000;
/// Presence states per presence EDU.
pub const MAX_PRESENCE_STATES_PER_EDU: usize = 50;
/// PDUs per transaction: the spec's limit.
pub const MAX_PDUS_PER_TRANSACTION: usize = 50;
/// EDUs per transaction: `MAX_EDUS_PER_TRANSACTION`.
pub const MAX_EDUS_PER_TRANSACTION: usize = 100;
/// Room kept for device list updates when there are many to-device
/// messages: `NUMBER_OF_RESERVED_EDUS_PER_TRANSACTION`.
pub const NUMBER_OF_RESERVED_EDUS_PER_TRANSACTION: usize = 10;
/// Receipt EDUs per transaction.
const MAX_RECEIPT_EDUS_PER_TRANSACTION: usize = 5;

/// The logger of Python's `PerDestinationQueue`.
const QUEUE_LOG: &str = "synapse.federation.sender.per_destination_queue";

/// An EDU: `synapse.federation.units.Edu`, less the origin and destination.
#[derive(Clone, Debug, PartialEq)]
pub struct Edu {
    pub edu_type: String,
    pub content: Value,
}

impl Edu {
    /// `Edu.get_dict`.
    pub fn into_value(self) -> Value {
        json!({"edu_type": self.edu_type, "content": self.content})
    }
}

/// A user's presence, as much of `UserPresenceState` as goes over
/// federation.
#[derive(Clone, Debug, PartialEq)]
pub struct PresenceState {
    pub user_id: String,
    pub state: String,
    pub last_active_ts: i64,
    pub status_msg: Option<String>,
    pub currently_active: bool,
}

impl PresenceState {
    /// `format_user_presence_state(state, now)`.
    pub fn format(&self, now_ms: i64) -> Value {
        let mut content = Map::new();
        content.insert("presence".to_owned(), Value::from(self.state.as_str()));
        content.insert("user_id".to_owned(), Value::from(self.user_id.as_str()));
        if self.last_active_ts != 0 {
            content.insert(
                "last_active_ago".to_owned(),
                Value::from(now_ms - self.last_active_ts),
            );
        }
        if let Some(status_msg) = self.status_msg.as_deref().filter(|msg| !msg.is_empty()) {
            content.insert("status_msg".to_owned(), Value::from(status_msg));
        }
        if self.state == "online" {
            content.insert(
                "currently_active".to_owned(),
                Value::from(self.currently_active),
            );
        }
        Value::Object(content)
    }
}

/// A read receipt: `synapse.types.ReadReceipt`.
#[derive(Clone, Debug)]
pub struct Receipt {
    pub room_id: String,
    pub receipt_type: String,
    pub user_id: String,
    pub thread_id: Option<String>,
    pub event_ids: Vec<String>,
    pub data: Map<String, Value>,
}

impl Receipt {
    /// The receipt as the EDU carries it.
    fn serialize(&self) -> Value {
        let mut data = self.data.clone();
        if let Some(thread_id) = &self.thread_id {
            data.insert("thread_id".to_owned(), Value::from(thread_id.as_str()));
        }
        json!({"event_ids": self.event_ids, "data": data})
    }
}

/// Where a PDU's JSON comes from.
pub enum PduBody {
    /// The event. Its JSON is read on tokio, without the GIL.
    Event(Py<Event>),
    /// The JSON itself, for tests.
    #[cfg(test)]
    Json(Value),
}

/// A PDU waiting to be sent.
pub struct QueuedPdu {
    pub event_id: String,
    pub stream_ordering: Option<i64>,
    pub origin_server_ts: i64,
    pub body: PduBody,
}

impl QueuedPdu {
    /// The PDU as `get_pdu_json` gives it.
    fn to_value(&self) -> serde_json::Result<Value> {
        match &self.body {
            PduBody::Event(event) => event.get().to_pdu_value(),
            #[cfg(test)]
            PduBody::Json(value) => Ok(value.clone()),
        }
    }
}

/// Why the loop stopped: the exceptions Python's loop tells apart.
#[derive(Debug)]
pub enum LoopError {
    /// `NotRetryingDestination`.
    NotRetrying(NotRetrying),
    /// `FederationDeniedError`, with its message.
    Denied(String),
    /// `HttpResponseException`.
    Http { code: u16, message: String },
    /// `RequestSendFailed`, with its message.
    SendFailed(String),
    /// Anything else, with its message and, from Python, the traceback.
    Other(String),
}

impl LoopError {
    pub fn from_client(err: ClientError, destination: &str) -> Self {
        match err {
            ClientError::Denied => {
                LoopError::Denied(format!("Federation denied with {destination}."))
            }
            ClientError::NotRetrying(not_retrying) => LoopError::NotRetrying(not_retrying),
            ClientError::Http(http) => LoopError::Http {
                code: http.code,
                message: format!("{}: {}", http.code, http.phrase),
            },
            err @ ClientError::SendFailed { .. } => {
                LoopError::SendFailed(format!("Failed to send request: {err}"))
            }
            err => LoopError::Other(err.to_string()),
        }
    }
}

/// What the device EDU query is asked for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DeviceEdusRequest {
    pub last_device_stream_id: i64,
    pub last_device_list_stream_id: i64,
    /// How many to-device messages may go in.
    pub to_device_limit: usize,
    /// How many EDUs may go in, of both kinds.
    pub edu_limit: usize,
}

/// The device EDUs for a transaction.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct DeviceEdus {
    /// The contents of the to-device EDUs.
    pub to_device: Vec<Value>,
    /// How far through the to-device stream these go.
    pub to_device_stream_id: i64,
    pub device_list: Vec<Edu>,
    /// How far through the device list stream these go.
    pub device_list_stream_id: i64,
}

/// A position in the sticky events stream. Python's, which Rust only hands
/// back.
pub type StickyStreamPosition = Arc<dyn Any + Send + Sync>;

/// Backlogged sticky events from one room.
pub struct StickyBatch {
    pub room_id: String,
    pub position: StickyStreamPosition,
    pub pdus: Vec<Arc<QueuedPdu>>,
}

/// A transaction that went out.
pub struct Sent {
    pub pdus: Vec<Arc<QueuedPdu>>,
    pub edu_types: Vec<String>,
    /// The PDUs the remote accepted.
    pub acknowledged: Vec<String>,
    pub stats: RequestStats,
}

/// What a finished transaction moved forward. Only reported once the
/// transaction was sent, or had nothing to send.
pub struct Completion {
    pub to_device_stream_id: Option<i64>,
    pub device_list_stream_id: Option<i64>,
    /// The stream ordering of the last PDU sent.
    pub last_stream_ordering: Option<i64>,
    pub sticky: Option<(String, StickyStreamPosition)>,
    /// `None` for a sticky batch that visibility filtering emptied.
    pub sent: Option<Sent>,
}

/// Hears when the loop waits on the reactor: see [`LoopActivity`].
pub type Observer = Arc<dyn AwaitableObserver>;

/// The parts of the loop that run in Python for now. Each is given the
/// calling loop's [`Observer`].
#[async_trait]
pub trait QueueHooks: Send + Sync + 'static {
    /// `_catch_up_transmission_loop`.
    async fn catch_up(&self, observer: &Observer) -> Result<(), LoopError>;

    /// The to-device messages and device list updates to send.
    async fn device_edus(
        &self,
        observer: &Observer,
        request: DeviceEdusRequest,
    ) -> Result<DeviceEdus, LoopError>;

    /// `StickyEventBacklogTracker.prepare_transaction`: `None` once the
    /// backlog is empty.
    async fn sticky_backlog(&self, observer: &Observer) -> Result<Option<StickyBatch>, LoopError>;

    /// Record a finished transaction: the stream positions, the module
    /// callback and the metrics.
    async fn complete(&self, observer: &Observer, completion: Completion) -> Result<(), LoopError>;
}

/// The answer to a sent transaction.
pub struct SendOutcome {
    pub acknowledged: Vec<String>,
    pub stats: RequestStats,
}

/// Where transactions go.
#[async_trait]
pub trait TransactionSink: Send + Sync + 'static {
    fn now_ms(&self) -> u64;

    /// `get_retry_limiter`'s check: whether we are backing off from the
    /// destination.
    async fn backoff(&self, destination: &str) -> Result<Option<NotRetrying>, LoopError>;

    fn build(&self, destination: &str, pdus: Vec<Value>, edus: Vec<Value>) -> Transaction;

    async fn send(
        &self,
        transaction: Transaction,
        observer: &Observer,
    ) -> Result<SendOutcome, LoopError>;
}

/// How many transmission loops are doing work, as opposed to waiting on the
/// reactor. Tests wait for this to reach zero.
#[derive(Default)]
pub struct Activity {
    busy: AtomicIsize,
}

impl Activity {
    /// Whether every loop has finished or is waiting on the reactor.
    pub fn idle(&self) -> bool {
        self.busy.load(Ordering::SeqCst) <= 0
    }
}

/// One loop's part in [`Activity`]. Python calls tell it when they start
/// and finish, on the reactor thread. Each loop has its own, so a loop that
/// ends cannot clear the flag of the next one.
pub struct LoopActivity {
    activity: Arc<Activity>,
    /// The loop is waiting on a Python call that has started.
    parked: AtomicBool,
}

impl LoopActivity {
    pub fn new(activity: Arc<Activity>) -> Arc<Self> {
        Arc::new(LoopActivity {
            activity,
            parked: AtomicBool::new(false),
        })
    }

    fn loop_started(&self) {
        self.activity.busy.fetch_add(1, Ordering::SeqCst);
    }

    fn loop_ended(&self) {
        // A loop aborted while parked is no longer counted.
        if !self.parked.swap(false, Ordering::SeqCst) {
            self.activity.busy.fetch_sub(1, Ordering::SeqCst);
        }
    }
}

impl AwaitableObserver for LoopActivity {
    fn started(&self) {
        self.parked.store(true, Ordering::SeqCst);
        self.activity.busy.fetch_sub(1, Ordering::SeqCst);
    }

    fn finished(&self) {
        if self.parked.swap(false, Ordering::SeqCst) {
            self.activity.busy.fetch_add(1, Ordering::SeqCst);
        }
    }
}

/// The PDUs waiting, in the order they were queued.
type PendingPdus = OrderedMap<String, Arc<QueuedPdu>>;

/// A receipt EDU's content: room ID → receipt type → user ID → receipt.
type ReceiptContent = Map<String, Value>;

/// Everything the queue holds. Python's attributes of the same names.
struct QueueState {
    transmission_loop_running: bool,
    /// Which loop is running, so an old loop ending cannot mark a newer one
    /// as stopped.
    loop_id: u64,
    loop_task: Option<AbortHandle>,
    transmission_loop_enabled: bool,
    new_data_to_send: bool,

    catching_up: bool,
    catchup_last_skipped: i64,
    last_successful_stream_ordering: Option<i64>,

    pending_pdus: PendingPdus,
    /// Never used in practice: see matrix-org/synapse#7549.
    pending_edus: Vec<Edu>,
    /// EDUs that replace each other by key, such as typing.
    pending_edus_keyed: OrderedMap<(String, String), Edu>,
    pending_presence: OrderedMap<String, PresenceState>,
    pending_receipt_edus: Vec<ReceiptContent>,

    last_device_stream_id: i64,
    last_device_list_stream_id: i64,

    sticky_backlog_turn: bool,
    /// Whether there may be backlogged sticky events.
    sticky_backlogged: bool,
}

/// A transaction ready to send: `_PreparedTransaction`.
struct Prepared {
    pdus: Vec<Arc<QueuedPdu>>,
    edus: Vec<Edu>,
    to_device_stream_id: Option<i64>,
    device_list_stream_id: Option<i64>,
    last_stream_ordering: Option<i64>,
    sticky: Option<(String, StickyStreamPosition)>,
}

impl Prepared {
    fn has_anything_to_send(&self) -> bool {
        !self.pdus.is_empty() || !self.edus.is_empty()
    }
}

/// What the queue was built with.
pub struct QueueConfig {
    pub destination: String,
    /// Whether this instance sends to the destination. If not, nothing is
    /// sent, as in Python.
    pub should_send_on_this_instance: bool,
    /// MSC4354: whether there is a sticky event backlog to send.
    pub msc4354_enabled: bool,
}

/// The queue for one destination: `PerDestinationQueue`.
pub struct DestinationQueue {
    config: QueueConfig,
    state: Mutex<QueueState>,
    hooks: Arc<dyn QueueHooks>,
    sink: Arc<dyn TransactionSink>,
    activity: Arc<Activity>,
    tokio: Handle,
}

/// Loop ids, unique across queues.
static NEXT_LOOP_ID: AtomicU64 = AtomicU64::new(1);

impl DestinationQueue {
    pub fn new(
        config: QueueConfig,
        hooks: Arc<dyn QueueHooks>,
        sink: Arc<dyn TransactionSink>,
        activity: Arc<Activity>,
        tokio: Handle,
    ) -> Arc<Self> {
        if !config.should_send_on_this_instance {
            log::error!(
                target: QUEUE_LOG,
                "Create a per destination queue for {} on wrong worker",
                config.destination
            );
        }
        Arc::new(DestinationQueue {
            config,
            state: Mutex::new(QueueState {
                transmission_loop_running: false,
                loop_id: 0,
                loop_task: None,
                transmission_loop_enabled: true,
                new_data_to_send: false,
                // We start out catching up, so we can catch up at startup.
                catching_up: true,
                catchup_last_skipped: 0,
                last_successful_stream_ordering: None,
                pending_pdus: OrderedMap::default(),
                pending_edus: Vec::new(),
                pending_edus_keyed: OrderedMap::default(),
                pending_presence: OrderedMap::default(),
                pending_receipt_edus: Vec::new(),
                last_device_stream_id: 0,
                last_device_list_stream_id: 0,
                sticky_backlog_turn: false,
                sticky_backlogged: true,
            }),
            hooks,
            sink,
            activity,
            tokio,
        })
    }

    pub fn destination(&self) -> &str {
        &self.config.destination
    }

    fn state(&self) -> MutexGuard<'_, QueueState> {
        self.state.lock().expect("poisoned")
    }

    /// Stop sending. The running loop, if any, is aborted where it stands.
    pub fn shutdown(&self) {
        let task = {
            let mut state = self.state();
            state.transmission_loop_enabled = false;
            state.loop_task.take()
        };
        if let Some(task) = task {
            task.abort();
        }
    }

    pub fn pending_pdu_count(&self) -> usize {
        self.state().pending_pdus.len()
    }

    pub fn pending_edu_count(&self) -> usize {
        let state = self.state();
        state.pending_edus.len() + state.pending_presence.len() + state.pending_edus_keyed.len()
    }

    pub fn transmission_loop_running(&self) -> bool {
        self.state().transmission_loop_running
    }

    pub fn new_data_to_send(&self) -> bool {
        self.state().new_data_to_send
    }

    /// Queue a PDU and start the loop. While catching up, the PDU is left
    /// for catch-up to find in the database, and only noted as skipped.
    pub fn send_pdu(self: &Arc<Self>, pdu: Arc<QueuedPdu>) {
        {
            let mut state = self.state();
            if !state.catching_up || state.last_successful_stream_ordering.is_none() {
                state.pending_pdus.insert(pdu.event_id.clone(), pdu);
            } else {
                state.catchup_last_skipped = pdu.stream_ordering.unwrap_or(0);
            }
        }
        self.attempt_new_transaction();
    }

    /// Queue presence updates, replacing any queued for the same users.
    pub fn send_presence(self: &Arc<Self>, states: Vec<PresenceState>, start_loop: bool) {
        {
            let mut state = self.state();
            for presence in states {
                state
                    .pending_presence
                    .insert(presence.user_id.clone(), presence);
            }
            state.new_data_to_send = true;
        }
        if start_loop {
            self.attempt_new_transaction();
        }
    }

    /// Queue a read receipt, without starting the loop.
    ///
    /// Each EDU holds one receipt per (room, receipt type, user, thread).
    /// The receipt replaces one in the first EDU that has the same tuple, or
    /// goes into the first EDU without the user, or into a new EDU.
    pub fn queue_read_receipt(&self, receipt: &Receipt) {
        let serialized = receipt.serialize();
        let mut state = self.state();

        let mut placed = false;
        for edu in &mut state.pending_receipt_edus {
            let room = edu
                .entry(receipt.room_id.clone())
                .or_insert_with(|| Value::Object(Map::new()));
            let Some(room) = room.as_object_mut() else {
                continue;
            };
            let by_user = room
                .entry(receipt.receipt_type.clone())
                .or_insert_with(|| Value::Object(Map::new()));
            let Some(by_user) = by_user.as_object_mut() else {
                continue;
            };
            // This reads `thread_id` from the top level of the queued
            // receipt, as Python does. It is never there (it is under
            // `data`), so a receipt with a thread never replaces one.
            let fits = match by_user.get(&receipt.user_id) {
                None => true,
                Some(existing) => {
                    existing.get("thread_id").and_then(Value::as_str)
                        == receipt.thread_id.as_deref()
                }
            };
            if fits {
                by_user.insert(receipt.user_id.clone(), serialized.clone());
                placed = true;
                break;
            }
        }
        if !placed {
            let mut by_user = Map::new();
            by_user.insert(receipt.user_id.clone(), serialized);
            let mut room = Map::new();
            room.insert(receipt.receipt_type.clone(), Value::Object(by_user));
            let mut edu = Map::new();
            edu.insert(receipt.room_id.clone(), Value::Object(room));
            state.pending_receipt_edus.push(edu);
        }

        state.new_data_to_send = true;
    }

    /// Queue an EDU that replaces any queued with the same type and key.
    pub fn send_keyed_edu(self: &Arc<Self>, edu: Edu, key: String) {
        self.state()
            .pending_edus_keyed
            .insert((edu.edu_type.clone(), key), edu);
        self.attempt_new_transaction();
    }

    pub fn send_edu(self: &Arc<Self>, edu: Edu) {
        self.state().pending_edus.push(edu);
        self.attempt_new_transaction();
    }

    /// Note new data without starting the loop. A running loop picks it up.
    pub fn mark_new_data(&self) {
        self.state().new_data_to_send = true;
    }

    /// Start the loop, unless it is running.
    pub fn attempt_new_transaction(self: &Arc<Self>) {
        let mut state = self.state();
        // A running loop rechecks for things to send before it ends.
        state.new_data_to_send = true;

        if state.transmission_loop_running {
            log::debug!(
                target: QUEUE_LOG,
                "TX [{}] Transaction already in progress",
                self.destination()
            );
            return;
        }
        if !self.config.should_send_on_this_instance {
            log::error!(
                target: QUEUE_LOG,
                "Trying to start a transaction to {} on wrong worker",
                self.destination()
            );
            return;
        }
        if !state.transmission_loop_enabled {
            log::warn!(
                target: QUEUE_LOG,
                "Shutdown has been requested. Not sending transaction"
            );
            return;
        }

        log::debug!(
            target: QUEUE_LOG,
            "TX [{}] Starting transaction loop",
            self.destination()
        );
        let loop_id = NEXT_LOOP_ID.fetch_add(1, Ordering::Relaxed);
        state.transmission_loop_running = true;
        state.loop_id = loop_id;
        let activity = LoopActivity::new(Arc::clone(&self.activity));
        activity.loop_started();
        // The guard takes the lock when it drops, which it does at once if
        // the runtime has shut down.
        drop(state);

        // The guard is made here, so it drops even if the task never runs.
        let guard = LoopGuard {
            queue: Arc::clone(self),
            loop_id,
            activity: Arc::clone(&activity),
        };
        let this = Arc::clone(self);
        let task = self.tokio.spawn(async move {
            let _guard = guard;
            this.transmission_loop(activity).await;
        });

        let mut state = self.state();
        if state.loop_id == loop_id && state.transmission_loop_running {
            state.loop_task = Some(task.abort_handle());
        }
    }

    /// `_transaction_transmission_loop`.
    async fn transmission_loop(&self, activity: Arc<LoopActivity>) {
        let observer: Observer = activity;
        let mut in_flight = Vec::new();
        if let Err(err) = self.run_transmission_loop(&observer, &mut in_flight).await {
            self.handle_error(err, &in_flight);
        }
    }

    async fn run_transmission_loop(
        &self,
        observer: &Observer,
        in_flight: &mut Vec<Arc<QueuedPdu>>,
    ) -> Result<(), LoopError> {
        // Fail fast if we are backing off. The client checks again.
        if let Some(not_retrying) = self.sink.backoff(self.destination()).await? {
            return Err(LoopError::NotRetrying(not_retrying));
        }

        if self.state().catching_up {
            self.hooks.catch_up(observer).await?;
            if self.state().catching_up {
                // Not caught up yet.
                return Ok(());
            }
        }

        loop {
            {
                let mut state = self.state();
                if !state.transmission_loop_enabled {
                    return Ok(());
                }
                state.new_data_to_send = false;
            }
            in_flight.clear();

            let Some(prepared) = self.prepare_transaction(observer).await? else {
                log::debug!(target: QUEUE_LOG, "TX [{}] Nothing to send", self.destination());
                let mut state = self.state();
                if state.new_data_to_send {
                    // Something was queued while we looked. A queue call
                    // that saw the loop running did not start another.
                    continue;
                }
                state.transmission_loop_running = false;
                state.loop_task = None;
                return Ok(());
            };

            if !prepared.has_anything_to_send() {
                // Filtering emptied a sticky event batch. Preparing it still
                // moved the backlog on, which must be recorded.
                self.complete_transaction(observer, prepared, None).await?;
                continue;
            }

            in_flight.extend(prepared.pdus.iter().cloned());
            let sent = self.send_transaction(observer, &prepared).await?;
            self.complete_transaction(observer, prepared, Some(sent))
                .await?;
        }
    }

    /// Build and send the transaction.
    async fn send_transaction(
        &self,
        observer: &Observer,
        prepared: &Prepared,
    ) -> Result<Sent, LoopError> {
        let destination = self.destination();
        if !prepared.pdus.is_empty() {
            log::debug!(
                target: QUEUE_LOG,
                "TX [{destination}] len(pending_pdus_by_dest[dest]) = {}",
                prepared.pdus.len()
            );
        }

        let pdus = prepared
            .pdus
            .iter()
            .map(|pdu| pdu.to_value())
            .collect::<Result<Vec<_>, _>>()
            .map_err(|err| LoopError::Other(format!("Failed to serialize event: {err}")))?;
        let edus = prepared.edus.iter().cloned().map(Edu::into_value).collect();
        let transaction = self.sink.build(destination, pdus, edus);

        let outcome = self.sink.send(transaction, observer).await?;
        Ok(Sent {
            pdus: prepared.pdus.clone(),
            edu_types: prepared
                .edus
                .iter()
                .map(|edu| edu.edu_type.clone())
                .collect(),
            acknowledged: outcome.acknowledged,
            stats: outcome.stats,
        })
    }

    /// The exception handlers of `_transaction_transmission_loop`.
    fn handle_error(&self, err: LoopError, in_flight: &[Arc<QueuedPdu>]) {
        let destination = self.destination();
        let log_failed_pdus = || {
            for pdu in in_flight {
                log::info!(
                    target: QUEUE_LOG,
                    "Failed to send event {} to {destination}",
                    pdu.event_id
                );
            }
        };
        match err {
            LoopError::NotRetrying(not_retrying) => {
                log::debug!(
                    target: QUEUE_LOG,
                    "TX [{destination}] not ready for retry yet (next retry at {} ms) - dropping transaction for now",
                    not_retrying.retry_last_ts + not_retrying.retry_interval
                );
                if not_retrying.retry_interval > CATCHUP_RETRY_INTERVAL_MS {
                    // We will not retry for over an hour. Drop the EDUs we
                    // can afford to lose (typing, receipts and presence),
                    // or they pile up. To-device messages and device lists
                    // are in the database. Catching up drops the PDUs.
                    let mut state = self.state();
                    state.pending_edus.clear();
                    state.pending_edus_keyed.clear();
                    state.pending_presence.clear();
                    state.pending_receipt_edus.clear();
                    start_catching_up(&mut state);
                }
            }
            LoopError::Denied(message) => log::info!(target: QUEUE_LOG, "{message}"),
            LoopError::Http { code, message } => log::warn!(
                target: QUEUE_LOG,
                "TX [{destination}] Received {code} response to transaction: {message}"
            ),
            LoopError::SendFailed(message) => {
                log::warn!(
                    target: QUEUE_LOG,
                    "TX [{destination}] Failed to send transaction: {message}"
                );
                log_failed_pdus();
            }
            LoopError::Other(message) => {
                log::error!(
                    target: QUEUE_LOG,
                    "TX [{destination}] Failed to send transaction\n{message}"
                );
                log_failed_pdus();
            }
        }
    }

    /// `_prepare_transaction`: take turns between the sticky event backlog
    /// and the real-time queue, so neither starves the other.
    async fn prepare_transaction(
        &self,
        observer: &Observer,
    ) -> Result<Option<Prepared>, LoopError> {
        let sticky_turn = std::mem::take(&mut self.state().sticky_backlog_turn);
        if sticky_turn {
            if let Some(prepared) = self.prepare_sticky_transaction(observer).await? {
                return Ok(Some(prepared));
            }
            return self.prepare_main_queue_transaction(observer).await;
        }

        let prepared = self.prepare_main_queue_transaction(observer).await?;
        let backlogged = {
            let mut state = self.state();
            state.sticky_backlog_turn = self.config.msc4354_enabled && state.sticky_backlogged;
            state.sticky_backlog_turn
        };
        if prepared.is_some() {
            return Ok(prepared);
        }
        if backlogged {
            return self.prepare_sticky_transaction(observer).await;
        }
        Ok(None)
    }

    /// `StickyEventBacklogTracker.prepare_transaction`.
    async fn prepare_sticky_transaction(
        &self,
        observer: &Observer,
    ) -> Result<Option<Prepared>, LoopError> {
        if !self.config.msc4354_enabled || !self.state().sticky_backlogged {
            return Ok(None);
        }

        let Some(batch) = self.hooks.sticky_backlog(observer).await? else {
            log::info!(
                target: QUEUE_LOG,
                "Caught up on federation sticky event backlog for destination '{}'",
                self.destination()
            );
            self.state().sticky_backlogged = false;
            return Ok(None);
        };

        Ok(Some(Prepared {
            pdus: batch.pdus,
            // No EDUs go with backlogged sticky events.
            edus: Vec::new(),
            to_device_stream_id: None,
            device_list_stream_id: None,
            last_stream_ordering: None,
            sticky: Some((batch.room_id, batch.position)),
        }))
    }

    /// `_prepare_main_queue_transaction`.
    ///
    /// EDUs go in this order, up to 100: one presence EDU, up to five
    /// receipt EDUs, to-device messages (leaving ten places for device list
    /// updates), device list updates, then anything else.
    async fn prepare_main_queue_transaction(
        &self,
        observer: &Observer,
    ) -> Result<Option<Prepared>, LoopError> {
        let now_ms = self.sink.now_ms() as i64;
        let mut edus = Vec::new();
        let request = {
            let mut state = self.state();

            if !state.pending_presence.is_empty() {
                let mut push = Vec::new();
                while push.len() < MAX_PRESENCE_STATES_PER_EDU {
                    let Some((_, presence)) = state.pending_presence.pop_front() else {
                        break;
                    };
                    push.push(presence.format(now_ms));
                }
                edus.push(Edu {
                    edu_type: "m.presence".to_owned(),
                    content: json!({ "push": push }),
                });
            }

            let receipts = state
                .pending_receipt_edus
                .len()
                .min(MAX_RECEIPT_EDUS_PER_TRANSACTION);
            for content in state.pending_receipt_edus.drain(..receipts) {
                edus.push(Edu {
                    edu_type: "m.receipt".to_owned(),
                    content: Value::Object(content),
                });
            }

            let edu_limit = MAX_EDUS_PER_TRANSACTION - edus.len();
            DeviceEdusRequest {
                last_device_stream_id: state.last_device_stream_id,
                last_device_list_stream_id: state.last_device_list_stream_id,
                to_device_limit: edu_limit - NUMBER_OF_RESERVED_EDUS_PER_TRANSACTION,
                edu_limit,
            }
        };

        let device = self.hooks.device_edus(observer, request).await?;

        let mut state = self.state();
        let mut edu_limit = request.edu_limit;

        // With nothing to send in a stream, we can move on in it now.
        // Otherwise only once the transaction is sent.
        let to_device_stream_id = if device.to_device.is_empty() {
            state.last_device_stream_id = device.to_device_stream_id;
            None
        } else {
            Some(device.to_device_stream_id)
        };
        edu_limit = edu_limit.saturating_sub(device.to_device.len());
        edus.extend(device.to_device.into_iter().map(|content| Edu {
            edu_type: "m.direct_to_device".to_owned(),
            content,
        }));

        let device_list_stream_id = if device.device_list.is_empty() {
            state.last_device_list_stream_id = device.device_list_stream_id;
            None
        } else {
            Some(device.device_list_stream_id)
        };
        edu_limit = edu_limit.saturating_sub(device.device_list.len());
        edus.extend(device.device_list);

        let others = state.pending_edus.len().min(edu_limit);
        edus.extend(state.pending_edus.drain(..others));
        edu_limit -= others;
        while edu_limit > 0 {
            let Some((_, edu)) = state.pending_edus_keyed.pop_back() else {
                break;
            };
            edus.push(edu);
            edu_limit -= 1;
        }

        let pdus: Vec<Arc<QueuedPdu>> = state
            .pending_pdus
            .values()
            .take(MAX_PDUS_PER_TRANSACTION)
            .cloned()
            .collect();

        if pdus.is_empty() && edus.is_empty() {
            // Nothing to send, and nothing to record: the device stream
            // positions have moved on above.
            return Ok(None);
        }

        let last_stream_ordering = pdus.last().and_then(|pdu| pdu.stream_ordering);
        Ok(Some(Prepared {
            pdus,
            edus,
            to_device_stream_id,
            device_list_stream_id,
            last_stream_ordering,
            sticky: None,
        }))
    }

    /// `_complete_transaction`: the transaction went out, so drop its PDUs
    /// and record how far we got.
    async fn complete_transaction(
        &self,
        observer: &Observer,
        prepared: Prepared,
        sent: Option<Sent>,
    ) -> Result<(), LoopError> {
        {
            let mut state = self.state();
            for pdu in &prepared.pdus {
                state.pending_pdus.remove(pdu.event_id.as_str());
            }
        }

        // Python skips a zero position, as it tests them for truth.
        let to_device_stream_id = prepared.to_device_stream_id.filter(|id| *id != 0);
        let device_list_stream_id = prepared.device_list_stream_id.filter(|id| *id != 0);
        if let Some(device_list_stream_id) = device_list_stream_id {
            log::info!(
                target: QUEUE_LOG,
                "Marking as sent '{}' {device_list_stream_id}",
                self.destination()
            );
        }

        self.hooks
            .complete(
                observer,
                Completion {
                    to_device_stream_id,
                    device_list_stream_id,
                    last_stream_ordering: prepared.last_stream_ordering.filter(|so| *so != 0),
                    sticky: prepared.sticky,
                    sent,
                },
            )
            .await?;

        let mut state = self.state();
        if let Some(id) = to_device_stream_id {
            state.last_device_stream_id = id;
        }
        if let Some(id) = device_list_stream_id {
            state.last_device_list_stream_id = id;
        }
        Ok(())
    }

    // The catch-up state, which the Python catch-up loop reads and writes.

    pub fn catching_up(&self) -> bool {
        self.state().catching_up
    }

    pub fn set_catching_up(&self, catching_up: bool) {
        self.state().catching_up = catching_up;
    }

    pub fn catchup_last_skipped(&self) -> i64 {
        self.state().catchup_last_skipped
    }

    pub fn set_catchup_last_skipped(&self, stream_ordering: i64) {
        self.state().catchup_last_skipped = stream_ordering;
    }

    pub fn last_successful_stream_ordering(&self) -> Option<i64> {
        self.state().last_successful_stream_ordering
    }

    pub fn set_last_successful_stream_ordering(&self, stream_ordering: Option<i64>) {
        self.state().last_successful_stream_ordering = stream_ordering;
    }

    pub fn transmission_loop_enabled(&self) -> bool {
        self.state().transmission_loop_enabled
    }

    /// `_start_catching_up`: enter catch-up and drop the queued PDUs.
    pub fn start_catching_up(&self) {
        start_catching_up(&mut self.state());
    }

    /// Whether there may be backlogged sticky events: `is_backlogged`.
    pub fn sticky_backlogged(&self) -> bool {
        self.config.msc4354_enabled && self.state().sticky_backlogged
    }

    pub fn set_sticky_backlogged(&self, backlogged: bool) {
        self.state().sticky_backlogged = backlogged;
    }

    /// The event IDs of the queued PDUs, oldest first. For tests.
    pub fn pending_pdu_ids(&self) -> Vec<String> {
        self.state()
            .pending_pdus
            .values()
            .map(|pdu| pdu.event_id.clone())
            .collect()
    }
}

fn start_catching_up(state: &mut QueueState) {
    state.catching_up = true;
    state.pending_pdus.clear();
}

/// Marks the loop as stopped when its task ends, however it ends.
struct LoopGuard {
    queue: Arc<DestinationQueue>,
    loop_id: u64,
    activity: Arc<LoopActivity>,
}

impl Drop for LoopGuard {
    fn drop(&mut self) {
        {
            let mut state = self.queue.state();
            if state.loop_id == self.loop_id {
                state.transmission_loop_running = false;
                state.loop_task = None;
            }
        }
        self.activity.loop_ended();
    }
}
