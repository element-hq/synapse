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
//! hear about delivered events. Catch-up runs here too, reading events
//! through the shared event cache. The sticky event backlog stays Python,
//! through [`PyHooks`].

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use pyo3::{exceptions::PyRuntimeError, intern, prelude::*};

use super::{
    changes::StreamChanges, metrics::QueueMetrics, python::PyHooks, Completion,
    DestinationQueue, DeviceEdus, DeviceEdusRequest, LoopError, Observer, PduBody, QueueHooks,
    QueuedPdu, StickyBatch, QUEUE_LOG,
};
use crate::{
    database::postgres::pool::{ConnectionPool, PyConnectionPool},
    events::Event,
    federation::gil::with_gil,
    storage::{
        events::RedactBehaviour,
        federation_store::FederationStore,
        sync_store::{SyncReader, SyncStore},
    },
    visibility::{filter_events_for_server, ServerFilter, ServerFiltered},
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
    /// The queries over the shared event and state caches, which the sync
    /// handler uses too.
    pub sync: Arc<SyncStore>,
    /// This server's name.
    pub server_name: String,
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

/// The stream ordering of an event read from the database, which has one.
fn stream_ordering_of(event: &Event) -> Result<i64, LoopError> {
    event
        .internal_metadata()
        .stream_ordering()
        .map(|so| so.get())
        .ok_or_else(|| LoopError::Other(format!("Event {} has no stream ordering", event.event_id())))
}

fn queued_pdu(event: Event) -> Result<Arc<QueuedPdu>, LoopError> {
    Ok(Arc::new(QueuedPdu {
        event_id: event.event_id().to_owned(),
        stream_ordering: Some(stream_ordering_of(&event)?),
        origin_server_ts: event.origin_server_ts(),
        body: PduBody::Native(event),
    }))
}

impl NativeHooks {
    /// The PDUs to catch `pdu`'s room up with: the room's latest forward
    /// extremities that the destination may see, or `pdu` itself.
    async fn room_catch_up_pdus(
        &self,
        reader: &SyncReader<'_>,
        pool: &ConnectionPool,
        pdu: Event,
        last_successful_stream_ordering: i64,
    ) -> Result<Vec<Event>, LoopError> {
        // The PDU from the database is the newest PDU in the room from this
        // server that we failed to send. Other servers may have sent lots
        // since, and we would rather tell the destination about the latest
        // events in the room only.
        let extremities = self
            .native
            .store
            .get_prev_events_for_room(pool, pdu.room_id())
            .await
            .map_err(db_error)?;
        if extremities.iter().any(|id| id == pdu.event_id()) {
            return Ok(vec![pdu]);
        }
        if reader
            .is_partial_state_room(pdu.room_id())
            .await
            .map_err(db_error)?
        {
            // We cannot be sure which events the destination may see with
            // partial state, so retry the newest PDU it is missing from us.
            return Ok(vec![pdu]);
        }

        let ids: Vec<Arc<str>> = extremities.iter().map(|id| Arc::from(id.as_str())).collect();
        let extremities = reader
            .events(&ids, RedactBehaviour::Redact, false, false)
            .await
            .map_err(db_error)?;
        let mut new_pdus = Vec::with_capacity(extremities.len());
        for event in extremities {
            // Leave out events from before the destination went offline.
            if stream_ordering_of(&event)? >= last_successful_stream_ordering {
                new_pdus.push(event);
            }
        }

        // Leave out events the destination may not see, such as when it has
        // left the room.
        let filter = ServerFilter {
            target_server_name: &self.destination,
            local_server_name: &self.native.server_name,
            redact: false,
            filter_out_erased_senders: true,
            filter_out_remote_partial_state_events: true,
        };
        match filter_events_for_server(reader, &filter, new_pdus)
            .await
            .map_err(db_error)?
        {
            ServerFiltered::Events(events) if !events.is_empty() => Ok(events),
            // With every extremity filtered out, send the original event, so
            // the destination gets at least some of what it missed. Python
            // would wait for the full state where Rust gives up, and we do
            // the same as for a room with partial state then.
            ServerFiltered::Events(_) | ServerFiltered::NeedsFullState => Ok(vec![pdu]),
        }
    }
}

#[async_trait]
impl QueueHooks for NativeHooks {
    /// `_catch_up_transmission_loop`, natively.
    async fn catch_up(&self, queue: &DestinationQueue, observer: &Observer) -> Result<(), LoopError> {
        let destination = self.destination.as_str();
        let pool = self.native.pool.get()?;
        let store = &self.native.store;
        let reader = self.native.sync.reader(pool.clone());

        let first_catch_up_check = queue.last_successful_stream_ordering().is_none();
        if first_catch_up_check {
            let last = store
                .get_destination_last_successful_stream_ordering(&pool, destination)
                .await
                .map_err(db_error)?;
            queue.set_last_successful_stream_ordering(last);
        }
        let Some(mut last_successful) = queue.last_successful_stream_ordering() else {
            // We have never sent this destination a PDU, so we do not know
            // what it is missing.
            queue.set_catching_up(false);
            return Ok(());
        };

        while queue.transmission_loop_enabled() {
            let event_ids = store
                .get_catch_up_room_event_ids(&pool, destination, last_successful)
                .await
                .map_err(db_error)?;
            if event_ids.is_empty() {
                // Check that no new event was skipped while we caught up.
                let skipped = queue.catchup_last_skipped();
                if skipped != 0 && skipped > last_successful {
                    // Which is also how a room purged while we loop ends up,
                    // so reset the flag rather than loop hot.
                    queue.set_catchup_last_skipped(0);
                    continue;
                }
                queue.set_catching_up(false);
                break;
            }

            if first_catch_up_check {
                // PDUs queued before we knew we had to catch up go.
                queue.start_catching_up();
            }

            let ids: Vec<Arc<str>> = event_ids.iter().map(|id| Arc::from(id.as_str())).collect();
            let pdus = reader
                .events(&ids, RedactBehaviour::Redact, false, false)
                .await
                .map_err(db_error)?;
            if pdus.is_empty() {
                return Err(LoopError::Other(format!(
                    "No events retrieved when we asked for {event_ids:?}. This should not happen."
                )));
            }
            log::info!(
                target: QUEUE_LOG,
                "Catching up destination {destination} with {} PDUs",
                pdus.len()
            );

            // One room per transaction, so the destination gets small
            // amounts of work. There is one PDU per room.
            for pdu in pdus {
                let stream_ordering = stream_ordering_of(&pdu)?;
                let room_id = pdu.room_id().to_owned();
                let room_pdus = self
                    .room_catch_up_pdus(&reader, &pool, pdu, last_successful)
                    .await?;
                if !queue.transmission_loop_enabled() {
                    return Ok(());
                }

                log::info!(target: QUEUE_LOG, "Catching up rooms to {destination}: '{room_id}'");
                let sent_stream_orderings = room_pdus
                    .iter()
                    .map(stream_ordering_of)
                    .collect::<Result<Vec<_>, _>>()?;
                let room_pdus = room_pdus
                    .into_iter()
                    .map(queued_pdu)
                    .collect::<Result<Vec<_>, _>>()?;
                let sent = queue.send_pdus(observer, room_pdus).await?;
                self.complete(
                    observer,
                    Completion {
                        to_device_stream_id: None,
                        device_list_stream_id: None,
                        last_stream_ordering: None,
                        sticky: None,
                        sent: Some(sent),
                    },
                )
                .await?;

                // Sticky events in the gap we skip over become a backlog.
                store
                    .mark_backlogged_sticky_events_after_catchup_transaction(
                        &pool,
                        destination,
                        last_successful,
                        stream_ordering,
                        &sent_stream_orderings,
                    )
                    .await
                    .map_err(db_error)?;
                queue.set_sticky_backlogged(true);

                // Our place in the queue of missed PDUs is the original PDU,
                // not those we sent.
                last_successful = stream_ordering;
                queue.set_last_successful_stream_ordering(Some(stream_ordering));
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
        Ok(())
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
            // Python moves an unchanged stream on to the token. Here the token
            // is read after the check, so a change between the two would be
            // skipped. Stay put instead.
            (Vec::new(), request.last_device_stream_id)
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
            (request.last_device_list_stream_id, Vec::new())
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
    /// the delivered events. As in Python, the module hears first, so a failed
    /// write does not keep the news from it.
    async fn complete(&self, observer: &Observer, completion: Completion) -> Result<(), LoopError> {
        let destination = self.destination.as_str();
        if let Some(sent) = &completion.sent {
            self.metrics.record_sent(destination, sent);
        }

        // The module hears only of acknowledged PDUs, and nothing if none.
        let notify = self.notify_delivered
            && completion.sent.as_ref().is_some_and(|sent| {
                sent.pdus
                    .iter()
                    .any(|pdu| sent.acknowledged.contains(&pdu.event_id))
            });
        if notify {
            self.python
                .call_complete(
                    observer,
                    Completion {
                        to_device_stream_id: None,
                        device_list_stream_id: None,
                        last_stream_ordering: None,
                        sticky: None,
                        sent: completion.sent,
                    },
                )
                .await?;
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

        // The sticky batch finishes last, as in Python.
        if completion.sticky.is_none() {
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
                    sent: None,
                },
            )
            .await
    }
}
