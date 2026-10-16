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

//! The queues as Python's `FederationSender` uses them.
//!
//! [`RustDestinationQueues`] makes a [`RustDestinationQueue`] for each
//! destination. The Python `RustPerDestinationQueue` wraps one and provides
//! the [`QueueHooks`] as coroutines: `_rust_catch_up`, `_rust_device_edus`,
//! `_rust_sticky_backlog` and `_rust_complete`. With the native database
//! driver, [`NativeHooks`] does the database work of the last two instead.

use std::{
    collections::HashSet,
    sync::{Arc, Mutex, OnceLock},
    time::Instant,
};

use async_trait::async_trait;
use pyo3::{
    intern,
    prelude::*,
    types::{PyDict, PyList, PyString, PyTuple},
};
use serde_json::Value;

use super::{
    changes::StreamChanges,
    metrics::QueueMetrics,
    native::{Native, NativeHooks},
    Activity, Completion, DestinationQueue, DeviceEdus, DeviceEdusRequest, Edu, LoopError,
    Observer, PduBody, PresenceState, QueueConfig, QueueHooks, QueuedPdu, Receipt, SendOutcome,
    StickyBatch, StickyStreamPosition, TransactionSink,
};
use crate::{
    deferred::run_python_awaitable_observed,
    events::Event,
    federation::{
        client::{limiter::NotRetrying, RequestStats},
        transaction::{
            acknowledged_pdus,
            python::{python_acknowledged, RustTransaction, RustTransactionManager},
            Transaction, TransactionResponse, TransactionSender,
        },
    },
    gil::{account, account_held, with_gil},
    homeserver::HomeServer,
    runtime::RustRuntime,
    storage::{
        federation_store::{FederationStore, FederationStoreConfig},
        native_pool::NativePool,
        sync_store::{SyncStore, SyncStoreConfig},
    },
};

/// Makes the queues, and tells tests when their loops are idle.
#[pyclass(frozen, name = "RustDestinationQueues")]
pub struct RustDestinationQueues {
    sink: Arc<ClientSink>,
    runtime: RustRuntime,
    activity: Arc<Activity>,
    metrics: Arc<QueueMetrics>,
    msc4354_enabled: bool,
    /// Set when the main database uses the native driver.
    native: OnceLock<Arc<Native>>,
}

#[pymethods]
impl RustDestinationQueues {
    /// `metrics_domains` is `federation_metrics_domains`.
    #[new]
    fn py_new(
        transactions: &Bound<'_, RustTransactionManager>,
        msc4354_enabled: bool,
        metrics_domains: HashSet<String>,
    ) -> Self {
        let transactions = transactions.get();
        let metrics = Arc::new(QueueMetrics::new(metrics_domains));
        RustDestinationQueues {
            sink: Arc::new(ClientSink {
                sender: Arc::clone(&transactions.sender),
                runtime: transactions.runtime.clone(),
                metrics: Arc::clone(&metrics),
                send_for_test: Mutex::new(None),
            }),
            runtime: transactions.runtime.clone(),
            activity: Arc::default(),
            metrics,
            msc4354_enabled,
            native: OnceLock::new(),
        }
    }

    /// Do the loops' database work through the native driver. `db_pool` is
    /// the `RustConnectionPool`. `stream_tokens()` returns the current
    /// to-device and device list stream positions. The change trackers know
    /// about the changes after `to_device_since` and `device_lists_since`,
    /// and Python must feed them every later change through
    /// `to_device_changed` and `device_list_changed`.
    ///
    /// Only the queues made after this use the native driver.
    #[allow(clippy::too_many_arguments)]
    fn use_native_store(
        &self,
        py: Python<'_>,
        homeserver: HomeServer,
        db_pool: Py<PyAny>,
        stream_tokens: Py<PyAny>,
        allow_device_name_lookup_over_federation: bool,
        to_device_since: i64,
        device_lists_since: i64,
    ) -> PyResult<()> {
        let config = homeserver.config(py)?;
        let server_name = homeserver.hostname(py)?;
        let store = homeserver.get_rust_store(py)?;
        let sync = SyncStore::new(
            Arc::clone(&store.caches),
            SyncStoreConfig::from_config(&config),
        );
        let native = Arc::new(Native {
            store: FederationStore::new(FederationStoreConfig {
                allow_device_name_lookup_over_federation,
                server_name: server_name.clone(),
            }),
            pool: NativePool::new(db_pool, "queue_db_pool"),
            stream_tokens,
            to_device: StreamChanges::new(to_device_since),
            device_lists: StreamChanges::new(device_lists_since),
            sync: Arc::new(sync),
            server_name,
        });
        self.native.set(native).map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("the native store is already in use")
        })
    }

    /// A destination has new to-device messages at `stream_pos`, or with no
    /// destination, any may have. A `StreamChangeCache` listener.
    #[pyo3(signature = (destination, stream_pos))]
    fn to_device_changed(&self, destination: Option<&str>, stream_pos: i64) {
        if let Some(native) = self.native.get() {
            stream_changed(&native.to_device, destination, stream_pos);
        }
    }

    /// As `to_device_changed`, for device list updates.
    #[pyo3(signature = (destination, stream_pos))]
    fn device_list_changed(&self, destination: Option<&str>, stream_pos: i64) {
        if let Some(native) = self.native.get() {
            stream_changed(&native.device_lists, destination, stream_pos);
        }
    }

    /// The counts since the last call, for Python's metrics:
    /// `(sent_transactions, sent_edus_by_type, put_requests,
    /// put_responses_by_code, last_sent_pdu_ts_by_destination)`.
    #[allow(clippy::type_complexity)]
    fn take_metrics(
        &self,
    ) -> (
        u64,
        std::collections::HashMap<String, u64>,
        u64,
        std::collections::HashMap<u16, u64>,
        std::collections::HashMap<String, i64>,
    ) {
        let counts = self.metrics.take();
        (
            counts.sent_transactions,
            counts.sent_edus_by_type,
            counts.requests,
            counts.responses,
            counts.last_sent_pdu_ts,
        )
    }

    /// The queue for `destination`. `facade` is the Python queue, which
    /// provides the hooks. The Rust queue holds it weakly.
    /// `notify_delivered` is whether a module wants to hear about delivered
    /// events.
    fn queue(
        &self,
        py: Python<'_>,
        destination: String,
        facade: &Bound<'_, PyAny>,
        should_send_on_this_instance: bool,
        notify_delivered: bool,
    ) -> PyResult<RustDestinationQueue> {
        let facade = py
            .import("weakref")?
            .getattr("ref")?
            .call1((facade,))?
            .unbind();
        let python = PyHooks {
            runtime: self.runtime.clone(),
            facade: Arc::new(facade),
            destination: destination.clone(),
            metrics: Arc::clone(&self.metrics),
        };
        let hooks: Arc<dyn QueueHooks> = match self.native.get() {
            Some(native) => Arc::new(NativeHooks {
                native: Arc::clone(native),
                python,
                metrics: Arc::clone(&self.metrics),
                destination: destination.clone(),
                notify_delivered,
            }),
            None => Arc::new(python),
        };
        let sink: Arc<dyn TransactionSink> = self.sink.clone();
        Ok(RustDestinationQueue {
            queue: DestinationQueue::new(
                QueueConfig {
                    destination,
                    should_send_on_this_instance,
                    msc4354_enabled: self.msc4354_enabled,
                },
                hooks,
                sink,
                Arc::clone(&self.activity),
                self.runtime.tokio_handle()?,
            ),
        })
    }

    /// Whether every loop has finished or is waiting on the reactor. For
    /// tests, which pump the reactor until it is.
    fn idle(&self) -> bool {
        self.activity.idle()
    }

    /// Send transactions through `send(transaction)`, a coroutine function
    /// taking a `RustTransaction` and returning the response dict, in place
    /// of the network. For tests.
    fn set_send_for_test(&self, send: Option<Py<PyAny>>) {
        *self.sink.send_for_test.lock().expect("poisoned") = send.map(Arc::new);
    }
}

/// One destination's queue. Each method is the `PerDestinationQueue` method
/// of the same name.
#[pyclass(frozen, name = "RustDestinationQueue")]
pub struct RustDestinationQueue {
    queue: Arc<DestinationQueue>,
}

#[pymethods]
impl RustDestinationQueue {
    fn shutdown(&self) {
        self.queue.shutdown();
    }

    fn pending_pdu_count(&self) -> usize {
        self.queue.pending_pdu_count()
    }

    fn pending_edu_count(&self) -> usize {
        self.queue.pending_edu_count()
    }

    #[getter]
    fn transmission_loop_running(&self) -> bool {
        self.queue.transmission_loop_running()
    }

    #[getter]
    fn new_data_to_send(&self) -> bool {
        self.queue.new_data_to_send()
    }

    fn send_pdu(&self, pdu: &Bound<'_, Event>) {
        self.queue.send_pdu(Arc::new(queued_pdu(pdu)));
    }

    #[pyo3(signature = (states, start_loop = true))]
    fn send_presence(&self, states: Vec<Bound<'_, PyAny>>, start_loop: bool) -> PyResult<()> {
        let states = states
            .iter()
            .map(presence_state)
            .collect::<PyResult<Vec<_>>>()?;
        self.queue.send_presence(states, start_loop);
        Ok(())
    }

    fn queue_read_receipt(&self, receipt: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = receipt.py();
        let data: Value = pythonize::depythonize(&receipt.getattr(intern!(py, "data"))?)?;
        let Value::Object(data) = data else {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "receipt data must be a dict",
            ));
        };
        self.queue.queue_read_receipt(&Receipt {
            room_id: receipt.getattr(intern!(py, "room_id"))?.extract()?,
            receipt_type: receipt.getattr(intern!(py, "receipt_type"))?.extract()?,
            user_id: receipt.getattr(intern!(py, "user_id"))?.extract()?,
            thread_id: receipt.getattr(intern!(py, "thread_id"))?.extract()?,
            event_ids: receipt.getattr(intern!(py, "event_ids"))?.extract()?,
            data,
        });
        Ok(())
    }

    /// `key` is the `repr` of Python's key, which is any hashable.
    fn send_keyed_edu(&self, edu: &Bound<'_, PyAny>, key: String) -> PyResult<()> {
        self.queue.send_keyed_edu(edu_from_py(edu)?, key);
        Ok(())
    }

    fn send_edu(&self, edu: &Bound<'_, PyAny>) -> PyResult<()> {
        self.queue.send_edu(edu_from_py(edu)?);
        Ok(())
    }

    fn mark_new_data(&self) {
        self.queue.mark_new_data();
    }

    fn attempt_new_transaction(&self) {
        self.queue.attempt_new_transaction();
    }

    // The catch-up state, for the Python catch-up loop.

    #[getter]
    fn catching_up(&self) -> bool {
        self.queue.catching_up()
    }

    #[setter]
    fn set_catching_up(&self, catching_up: bool) {
        self.queue.set_catching_up(catching_up);
    }

    #[getter]
    fn catchup_last_skipped(&self) -> i64 {
        self.queue.catchup_last_skipped()
    }

    #[setter]
    fn set_catchup_last_skipped(&self, stream_ordering: i64) {
        self.queue.set_catchup_last_skipped(stream_ordering);
    }

    #[getter]
    fn last_successful_stream_ordering(&self) -> Option<i64> {
        self.queue.last_successful_stream_ordering()
    }

    #[setter]
    fn set_last_successful_stream_ordering(&self, stream_ordering: Option<i64>) {
        self.queue
            .set_last_successful_stream_ordering(stream_ordering);
    }

    #[getter]
    fn transmission_loop_enabled(&self) -> bool {
        self.queue.transmission_loop_enabled()
    }

    fn start_catching_up(&self) {
        self.queue.start_catching_up();
    }

    #[getter]
    fn sticky_backlogged(&self) -> bool {
        self.queue.sticky_backlogged()
    }

    #[setter]
    fn set_sticky_backlogged(&self, backlogged: bool) {
        self.queue.set_sticky_backlogged(backlogged);
    }

    /// The event IDs of the queued PDUs, oldest first. For tests.
    fn pending_pdu_ids(&self) -> Vec<String> {
        self.queue.pending_pdu_ids()
    }
}

/// A PDU for the queue. Its JSON is read later, on tokio.
fn queued_pdu(pdu: &Bound<'_, Event>) -> QueuedPdu {
    let event = pdu.get();
    QueuedPdu {
        event_id: event.event_id().to_owned(),
        stream_ordering: event
            .internal_metadata()
            .stream_ordering()
            .map(|so| so.get()),
        origin_server_ts: event.origin_server_ts(),
        body: PduBody::Event(pdu.clone().unbind()),
    }
}

/// The parts of a `UserPresenceState` that go over federation.
fn presence_state(state: &Bound<'_, PyAny>) -> PyResult<PresenceState> {
    let py = state.py();
    Ok(PresenceState {
        user_id: state.getattr(intern!(py, "user_id"))?.extract()?,
        state: state.getattr(intern!(py, "state"))?.extract()?,
        last_active_ts: state.getattr(intern!(py, "last_active_ts"))?.extract()?,
        status_msg: state.getattr(intern!(py, "status_msg"))?.extract()?,
        currently_active: state
            .getattr(intern!(py, "currently_active"))?
            .is_truthy()?,
    })
}

/// An `Edu`'s type and content.
fn edu_from_py(edu: &Bound<'_, PyAny>) -> PyResult<Edu> {
    let py = edu.py();
    Ok(Edu {
        edu_type: edu.getattr(intern!(py, "edu_type"))?.extract()?,
        content: pythonize::depythonize(&edu.getattr(intern!(py, "content"))?)?,
    })
}

fn stream_changed(changes: &StreamChanges, destination: Option<&str>, stream_pos: i64) {
    match destination {
        Some(destination) => changes.entity_has_changed(destination, stream_pos),
        None => changes.all_entities_changed(stream_pos),
    }
}

/// The hooks, as coroutine methods of the Python queue.
pub(super) struct PyHooks {
    runtime: RustRuntime,
    /// A weak reference to the Python queue.
    facade: Arc<Py<PyAny>>,
    destination: String,
    metrics: Arc<QueueMetrics>,
}

impl PyHooks {
    /// Await `facade.<method>(*args(py))` on the reactor, and read its result
    /// with `convert` there. Both count as GIL time under `site`.
    async fn call<A, C, T>(
        &self,
        observer: &Observer,
        site: &'static str,
        method: &'static str,
        args: A,
        convert: C,
    ) -> Result<T, LoopError>
    where
        A: for<'py> Fn(Python<'py>) -> PyResult<Bound<'py, PyTuple>> + Send + Sync + 'static,
        C: for<'py> Fn(&Bound<'py, PyAny>) -> PyResult<T> + Send + Sync + 'static,
        T: Send + 'static,
    {
        let facade = Arc::clone(&self.facade);
        let result = run_python_awaitable_observed(
            &self.runtime,
            move |py| {
                account(site, py, |_| {
                    let facade = facade.bind(py).call0()?;
                    if facade.is_none() {
                        return Err(pyo3::exceptions::PyRuntimeError::new_err(
                            "the federation queue has gone",
                        ));
                    }
                    facade.call_method1(method, args(py)?)
                })
            },
            move |value| {
                let start = Instant::now();
                let converted = convert(value);
                account_held(site, start.elapsed());
                converted
            },
            Some(Arc::clone(observer)),
        )
        .await;
        result.map_err(|err| python_error(err, &self.destination))
    }
}

#[async_trait]
impl QueueHooks for PyHooks {
    async fn catch_up(
        &self,
        _queue: &DestinationQueue,
        observer: &Observer,
    ) -> Result<(), LoopError> {
        self.call(
            observer,
            "queue_catch_up",
            "_rust_catch_up",
            |py| Ok(PyTuple::empty(py)),
            |_| Ok(()),
        )
        .await
    }

    async fn device_edus(
        &self,
        observer: &Observer,
        request: DeviceEdusRequest,
    ) -> Result<DeviceEdus, LoopError> {
        self.call(
            observer,
            "queue_device_edus",
            "_rust_device_edus",
            move |py| {
                PyTuple::new(
                    py,
                    [
                        request.last_device_stream_id.into_pyobject(py)?,
                        request.last_device_list_stream_id.into_pyobject(py)?,
                        request.to_device_limit.into_pyobject(py)?,
                        request.edu_limit.into_pyobject(py)?,
                    ],
                )
            },
            |result| {
                let (to_device, to_device_stream_id, device_list, device_list_stream_id): (
                    Vec<Bound<'_, PyAny>>,
                    i64,
                    Vec<(String, Bound<'_, PyAny>)>,
                    i64,
                ) = result.extract()?;
                Ok(DeviceEdus {
                    to_device: to_device
                        .iter()
                        .map(|content| pythonize::depythonize(content))
                        .collect::<Result<_, _>>()?,
                    to_device_stream_id,
                    device_list: device_list
                        .iter()
                        .map(|(edu_type, content)| -> PyResult<Edu> {
                            Ok(Edu {
                                edu_type: edu_type.clone(),
                                content: pythonize::depythonize(content)?,
                            })
                        })
                        .collect::<PyResult<_>>()?,
                    device_list_stream_id,
                })
            },
        )
        .await
    }

    async fn sticky_backlog(
        &self,
        _queue: &DestinationQueue,
        observer: &Observer,
    ) -> Result<Option<StickyBatch>, LoopError> {
        self.call(
            observer,
            "queue_sticky_backlog",
            "_rust_sticky_backlog",
            |py| Ok(PyTuple::empty(py)),
            |result| {
                if result.is_none() {
                    return Ok(None);
                }
                let (room_id, position, events): (String, Py<PyAny>, Vec<Bound<'_, PyAny>>) =
                    result.extract()?;
                let pdus = events
                    .iter()
                    .map(|event| Ok(Arc::new(queued_pdu(event.cast::<Event>()?))))
                    .collect::<PyResult<_>>()?;
                let position: StickyStreamPosition = Arc::new(position);
                Ok(Some(StickyBatch {
                    room_id,
                    position,
                    pdus,
                }))
            },
        )
        .await
    }

    async fn complete(&self, observer: &Observer, completion: Completion) -> Result<(), LoopError> {
        if let Some(sent) = &completion.sent {
            self.metrics.record_sent(&self.destination, sent);
        }
        self.call_complete(observer, completion).await
    }
}

impl PyHooks {
    /// `_rust_complete`, without counting the transaction.
    pub(super) async fn call_complete(
        &self,
        observer: &Observer,
        completion: Completion,
    ) -> Result<(), LoopError> {
        let completion = Arc::new(completion);
        self.call(
            observer,
            "queue_complete",
            "_rust_complete",
            move |py| completion_args(py, &completion),
            |_| Ok(()),
        )
        .await
    }
}

/// The arguments of `_rust_complete`.
fn completion_args<'py>(py: Python<'py>, completion: &Completion) -> PyResult<Bound<'py, PyTuple>> {
    let sticky = match &completion.sticky {
        Some((room_id, position)) => {
            let position = position
                .downcast_ref::<Py<PyAny>>()
                .map(|position| position.clone_ref(py))
                .unwrap_or_else(|| py.None());
            (room_id.as_str(), position).into_pyobject(py)?.into_any()
        }
        None => py.None().into_bound(py),
    };
    let sent = match &completion.sent {
        Some(sent) => {
            let pdus = PyList::empty(py);
            for pdu in &sent.pdus {
                match &pdu.body {
                    PduBody::Event(event) => pdus.append(event.clone_ref(py))?,
                    PduBody::Native(event) => pdus.append(Py::new(py, event.shallow_clone())?)?,
                    #[cfg(test)]
                    PduBody::Json(_) => {}
                }
            }
            (pdus, &sent.acknowledged).into_pyobject(py)?.into_any()
        }
        None => py.None().into_bound(py),
    };
    PyTuple::new(
        py,
        [
            completion.to_device_stream_id.into_pyobject(py)?,
            completion.device_list_stream_id.into_pyobject(py)?,
            completion.last_stream_ordering.into_pyobject(py)?,
            sticky,
            sent,
        ],
    )
}

/// What a Python exception means to the loop. Takes the GIL, which only
/// error paths need.
fn python_error(err: PyErr, destination: &str) -> LoopError {
    with_gil("queue_error", |py| {
        classify_python_error(py, &err).unwrap_or_else(|classify_err| {
            LoopError::Other(format!(
                "{} (and classifying it failed: {classify_err}) for {destination}",
                format_exception(py, &err)
            ))
        })
    })
}

fn classify_python_error(py: Python<'_>, err: &PyErr) -> PyResult<LoopError> {
    let value = err.value(py);
    let errors = py.import("synapse.api.errors")?;
    let not_retrying = py
        .import("synapse.util.retryutils")?
        .getattr("NotRetryingDestination")?;

    Ok(if value.is_instance(&not_retrying)? {
        LoopError::NotRetrying(NotRetrying {
            retry_last_ts: value.getattr("retry_last_ts")?.extract()?,
            retry_interval: value.getattr("retry_interval")?.extract()?,
        })
    } else if value.is_instance(&errors.getattr("FederationDeniedError")?)? {
        LoopError::Denied(value.str()?.to_string())
    } else if value.is_instance(&errors.getattr("HttpResponseException")?)? {
        LoopError::Http {
            code: value.getattr("code")?.extract()?,
            message: value.str()?.to_string(),
        }
    } else if value.is_instance(&errors.getattr("RequestSendFailed")?)? {
        LoopError::SendFailed(value.str()?.to_string())
    } else {
        LoopError::Other(format_exception(py, err))
    })
}

/// The exception with its traceback, as `logger.exception` logs it.
fn format_exception(py: Python<'_>, err: &PyErr) -> String {
    let formatted = (|| -> PyResult<String> {
        let lines = py
            .import("traceback")?
            .call_method1("format_exception", (err.value(py),))?;
        Ok(PyString::new(py, "")
            .call_method1("join", (lines,))?
            .extract::<String>()?
            .trim_end()
            .to_owned())
    })();
    formatted.unwrap_or_else(|_| err.to_string())
}

/// Sends through the Rust transaction sender, or a Python stand-in in tests.
struct ClientSink {
    sender: Arc<TransactionSender>,
    runtime: RustRuntime,
    /// Counts the requests of a failed transaction. A sent one's are counted
    /// with its completion.
    metrics: Arc<QueueMetrics>,
    send_for_test: Mutex<Option<Arc<Py<PyAny>>>>,
}

impl ClientSink {
    /// Send through the Python stand-in.
    async fn send_for_test(
        &self,
        send: Arc<Py<PyAny>>,
        transaction: Transaction,
        observer: &Observer,
    ) -> Result<SendOutcome, LoopError> {
        let destination = transaction.destination.clone();
        let log_prefix = transaction.log_prefix();
        let transaction = Arc::new(transaction);
        let response = run_python_awaitable_observed(
            &self.runtime,
            move |py| {
                let built = RustTransaction {
                    inner: (*transaction).clone(),
                };
                send.bind(py).call1((built,))
            },
            |response| {
                Ok(match response.cast::<PyDict>() {
                    Ok(response) => pythonize::depythonize::<Value>(response)?,
                    // A mock that returns nothing.
                    Err(_) => Value::Null,
                })
            },
            Some(Arc::clone(observer)),
        )
        .await
        .map_err(|err| python_error(err, &destination))?;

        let acknowledged = match response {
            Value::Object(response) => acknowledged_pdus(&log_prefix, &response),
            _ => Vec::new(),
        };
        Ok(SendOutcome {
            acknowledged,
            stats: RequestStats::default(),
        })
    }
}

#[async_trait]
impl TransactionSink for ClientSink {
    fn now_ms(&self) -> u64 {
        self.sender.client().now_ms()
    }

    async fn backoff(&self, destination: &str) -> Result<Option<NotRetrying>, LoopError> {
        self.sender
            .client()
            .backoff(destination)
            .await
            .map_err(|err| LoopError::Other(err.to_string()))
    }

    fn build(&self, destination: &str, pdus: Vec<Value>, edus: Vec<Value>) -> Transaction {
        self.sender.build(destination, pdus, edus)
    }

    async fn send(
        &self,
        transaction: Transaction,
        observer: &Observer,
    ) -> Result<SendOutcome, LoopError> {
        let send_for_test = self.send_for_test.lock().expect("poisoned").clone();
        if let Some(send) = send_for_test {
            return self.send_for_test(send, transaction, observer).await;
        }

        let destination = transaction.destination.clone();
        let transaction_id = transaction.transaction_id.clone();
        let mut stats = RequestStats::default();
        let result = self.sender.send(transaction, Vec::new(), &mut stats).await;
        match result {
            Ok(TransactionResponse::Acknowledged(acknowledged)) => Ok(SendOutcome {
                acknowledged,
                stats,
            }),
            Ok(TransactionResponse::Unparsed(body)) => with_gil("queue_response_fallback", |py| {
                python_acknowledged(py, &destination, &transaction_id, &body).map_err(|err| {
                    // There will be no completion to count the requests.
                    self.metrics.record_requests(&stats);
                    classify_python_error(py, &err)
                        .unwrap_or_else(|_| LoopError::Other(format_exception(py, &err)))
                })
            })
            .map(|acknowledged| SendOutcome {
                acknowledged,
                stats,
            }),
            Err(err) => {
                // As above, there will be no completion.
                self.metrics.record_requests(&stats);
                Err(LoopError::from_client(err, &destination))
            }
        }
    }
}
