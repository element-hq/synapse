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

//! The loop's database work through the native Postgres driver.
//!
//! When the main database uses the native driver, [`NativeHooks`] reads
//! the device EDUs and records finished transactions with the
//! [`FederationStore`], on tokio. A transaction then needs no trip to the
//! reactor, unless it finishes a sticky event batch or a module wants to
//! hear about delivered events. Catch-up and the sticky event backlog stay
//! Python, through [`PyHooks`].

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use pyo3::{exceptions::PyRuntimeError, intern, prelude::*};

use super::{
    changes::StreamChanges, metrics::QueueMetrics, python::PyHooks, Completion, DeviceEdus,
    DeviceEdusRequest, LoopError, Observer, QueueHooks, StickyBatch,
};
use crate::{
    database::postgres::pool::{ConnectionPool, PyConnectionPool},
    federation::gil::with_gil,
    storage::federation_store::FederationStore,
};

/// The logger of `synapse.logging.issue9533_logger`.
const ISSUE_9533_LOG: &str = "synapse.9533_debug";

/// What the queues of a sender share when they use the native store.
pub struct Native {
    pub store: FederationStore,
    pub pool: NativePool,
    /// `stream_tokens()`: the current positions of the to-device and device
    /// list streams, which only Python's ID generators know.
    pub stream_tokens: Py<PyAny>,
    /// Which destinations have new to-device messages.
    pub to_device: StreamChanges,
    /// Which destinations have new device list updates.
    pub device_lists: StreamChanges,
}

impl Native {
    /// The positions of the to-device and device list streams. Takes the
    /// GIL, but not a trip to the reactor: the ID generators are read where
    /// they stand.
    fn stream_tokens(&self) -> Result<(i64, i64), LoopError> {
        with_gil("queue_stream_tokens", |py| {
            self.stream_tokens.bind(py).call0()?.extract()
        })
        .map_err(|err: PyErr| LoopError::Other(format!("Failed to read the stream tokens: {err}")))
    }
}

/// The native pool, as the Python `RustConnectionPool` holds it. That
/// replaces its pool when it is closed and started again, so a closed pool
/// is read again from Python.
pub struct NativePool {
    /// The Python `RustConnectionPool`.
    python: Py<PyAny>,
    cached: Mutex<Option<ConnectionPool>>,
}

impl NativePool {
    pub fn new(python: Py<PyAny>) -> Self {
        NativePool {
            python,
            cached: Mutex::new(None),
        }
    }

    pub fn get(&self) -> Result<ConnectionPool, LoopError> {
        let mut cached = self.cached.lock().expect("poisoned");
        if let Some(pool) = cached.as_ref().filter(|pool| !pool.is_closed()) {
            return Ok(pool.clone());
        }
        let pool = with_gil("queue_db_pool", |py| -> PyResult<ConnectionPool> {
            let pool = self.python.bind(py).getattr(intern!(py, "_pool"))?;
            if pool.is_none() {
                return Err(PyRuntimeError::new_err("the database pool is closed"));
            }
            Ok(pool.cast::<PyConnectionPool>()?.get().pool.clone())
        })
        .map_err(|err| LoopError::Other(err.to_string()))?;
        *cached = Some(pool.clone());
        Ok(pool)
    }
}

/// The hooks with the database work done natively.
pub(super) struct NativeHooks {
    pub(super) native: Arc<Native>,
    /// For what stays Python.
    pub(super) python: PyHooks,
    pub(super) metrics: Arc<QueueMetrics>,
    pub(super) destination: String,
    /// Whether a module wants to hear about delivered events, when the
    /// queue was made.
    pub(super) notify_delivered: bool,
}

fn db_error(err: anyhow::Error) -> LoopError {
    LoopError::Other(format!("{err:#}"))
}

#[async_trait]
impl QueueHooks for NativeHooks {
    async fn catch_up(&self, observer: &Observer) -> Result<(), LoopError> {
        self.python.catch_up(observer).await
    }

    /// `get_new_device_msgs_for_remote` and `get_device_updates_by_remote`,
    /// with the change trackers in place of their stream change caches.
    async fn device_edus(
        &self,
        _observer: &Observer,
        request: DeviceEdusRequest,
    ) -> Result<DeviceEdus, LoopError> {
        let destination = self.destination.as_str();
        let to_device_changed = self
            .native
            .to_device
            .has_entity_changed(destination, request.last_device_stream_id);
        let device_lists_changed = self
            .native
            .device_lists
            .has_entity_changed(destination, request.last_device_list_stream_id);
        if !to_device_changed && !device_lists_changed {
            // Python moves both positions on to the current tokens here. We
            // would need the GIL to read them, and stay put instead.
            return Ok(DeviceEdus {
                to_device: Vec::new(),
                to_device_stream_id: request.last_device_stream_id,
                device_list: Vec::new(),
                device_list_stream_id: request.last_device_list_stream_id,
            });
        }

        let (to_device_token, device_list_token) = self.native.stream_tokens()?;
        let pool = self.native.pool.get()?;
        let store = &self.native.store;

        let (to_device, to_device_stream_id) = if to_device_changed {
            store
                .get_new_device_msgs_for_remote(
                    &pool,
                    destination,
                    request.last_device_stream_id,
                    to_device_token,
                    request.to_device_limit,
                )
                .await
                .map_err(db_error)?
        } else {
            (Vec::new(), to_device_token)
        };
        if !to_device.is_empty() {
            log::debug!(
                target: ISSUE_9533_LOG,
                "Sending {} to-device messages to {destination}, up to stream id {to_device_stream_id}",
                to_device.len()
            );
        }

        let device_list_limit = request.edu_limit.saturating_sub(to_device.len());
        let (device_list_stream_id, device_list) = if device_lists_changed {
            store
                .get_device_updates_by_remote(
                    &pool,
                    destination,
                    request.last_device_list_stream_id,
                    device_list_token,
                    device_list_limit,
                )
                .await
                .map_err(db_error)?
        } else {
            (device_list_token, Vec::new())
        };
        if device_list.len() > device_list_limit {
            return Err(LoopError::Other(
                "get_device_updates_by_remote returned too many EDUs".to_owned(),
            ));
        }

        Ok(DeviceEdus {
            to_device,
            to_device_stream_id,
            device_list,
            device_list_stream_id,
        })
    }

    async fn sticky_backlog(&self, observer: &Observer) -> Result<Option<StickyBatch>, LoopError> {
        self.python.sticky_backlog(observer).await
    }

    /// The database writes of `_rust_complete`, natively. Python is only
    /// called for a sticky event batch or a module that wants to hear about
    /// the delivered events.
    async fn complete(&self, observer: &Observer, completion: Completion) -> Result<(), LoopError> {
        let destination = self.destination.as_str();
        if let Some(sent) = &completion.sent {
            self.metrics.record_sent(destination, sent);
        }

        let writes = completion.to_device_stream_id.is_some()
            || completion.device_list_stream_id.is_some()
            || completion.last_stream_ordering.is_some();
        if writes {
            let pool = self.native.pool.get()?;
            let store = &self.native.store;
            if let Some(stream_id) = completion.to_device_stream_id {
                store
                    .delete_device_msgs_for_remote(&pool, destination, stream_id)
                    .await
                    .map_err(db_error)?;
            }
            if let Some(stream_id) = completion.device_list_stream_id {
                store
                    .mark_as_sent_devices_by_remote(&pool, destination, stream_id)
                    .await
                    .map_err(db_error)?;
            }
            if let Some(stream_ordering) = completion.last_stream_ordering {
                store
                    .set_destination_last_successful_stream_ordering(
                        &pool,
                        destination,
                        stream_ordering,
                    )
                    .await
                    .map_err(db_error)?;
            }
        }

        // The module hears only of acknowledged PDUs, and nothing if none.
        let notify = self.notify_delivered
            && completion.sent.as_ref().is_some_and(|sent| {
                sent.pdus
                    .iter()
                    .any(|pdu| sent.acknowledged.contains(&pdu.event_id))
            });
        if completion.sticky.is_none() && !notify {
            return Ok(());
        }
        self.python
            .call_complete(
                observer,
                Completion {
                    to_device_stream_id: None,
                    device_list_stream_id: None,
                    last_stream_ordering: None,
                    sticky: completion.sticky,
                    sent: if notify { completion.sent } else { None },
                },
            )
            .await
    }
}
