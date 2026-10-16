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

//! The Python face of event persistence: the [`Persister`] that
//! `RustHandlers.persister` hands out, and helpers for tests.

use std::sync::Arc;

use http::StatusCode;
use pyo3::{exceptions::PyRuntimeError, intern, prelude::*, types::PyDict};

use super::{
    context::PersistContext, extremities::MissingPrevStateEvents, metrics::PersistMetrics,
};
use crate::{
    errors::SynapseError,
    events::Event,
    homeserver::HomeServer,
    runtime::RustRuntime,
    storage::{
        native_pool::NativePool,
        store::Store,
        stream_ids::{PyStreamIdTracker, StreamIdTracker},
        sync_store::{SyncStore, SyncStoreConfig},
    },
    types::state_filter::state_map_to_py,
};

/// Persists events in Rust, on the events writer, over the native driver.
///
/// Holds what every level needs: the pool, the shared caches, the stream ID
/// trackers of the events, backfill and cache streams (shared with their
/// Python `MultiWriterIdGenerator`s) and the metrics. The entry points come
/// with the levels that use them.
#[pyclass(frozen)]
pub struct Persister {
    #[allow(dead_code)] // Read by the levels to come.
    pub(super) inner: Arc<PersisterInner>,
}

#[allow(dead_code)] // Read by the levels to come.
pub(super) struct PersisterInner {
    pub pool: NativePool,
    pub runtime: RustRuntime,
    pub store: Arc<Store>,
    /// The sync store over the same caches, which the
    /// [`super::reader::PersistReader`] builds on.
    pub sync: Arc<SyncStore>,
    pub events_stream: Arc<StreamIdTracker>,
    pub backfill_stream: Arc<StreamIdTracker>,
    pub cache_stream: Arc<StreamIdTracker>,
    pub server_name: String,
    pub instance_name: String,
    pub metrics: PersistMetrics,
}

impl Persister {
    /// Build the persister for `homeserver`, whose main database uses the
    /// native driver through `connection_pool` (a `RustConnectionPool`).
    /// `None` if this instance does not write events.
    pub fn for_homeserver(
        py: Python<'_>,
        homeserver: &HomeServer,
        connection_pool: Py<PyAny>,
        runtime: RustRuntime,
        store: Arc<Store>,
    ) -> PyResult<Option<Persister>> {
        let instance_name = homeserver.instance_name(py)?;
        if !homeserver.event_writers(py)?.contains(&instance_name) {
            return Ok(None);
        }

        let main = homeserver.main_datastore(py)?;
        let tracker = |attr: &Bound<'_, pyo3::types::PyString>| -> PyResult<Arc<StreamIdTracker>> {
            let id_gen = main.getattr(attr)?;
            if id_gen.is_none() {
                return Err(PyRuntimeError::new_err(format!(
                    "the main datastore has no {attr}"
                )));
            }
            Ok(id_gen
                .getattr(intern!(py, "_tracker"))?
                .cast::<PyStreamIdTracker>()?
                .get()
                .tracker())
        };

        Ok(Some(Persister {
            inner: Arc::new(PersisterInner {
                pool: NativePool::new(connection_pool, "persist_db_pool"),
                runtime,
                sync: Arc::new(SyncStore::new(
                    Arc::clone(&store.caches),
                    SyncStoreConfig::from_config(&homeserver.config(py)?),
                )),
                store,
                events_stream: tracker(intern!(py, "_stream_id_gen"))?,
                backfill_stream: tracker(intern!(py, "_backfill_id_gen"))?,
                cache_stream: tracker(intern!(py, "_cache_id_gen"))?,
                server_name: homeserver.hostname(py)?,
                instance_name,
                metrics: PersistMetrics::default(),
            }),
        }))
    }
}

#[pymethods]
impl Persister {
    /// What persisting has counted since the last call, for
    /// `flush_rust_persist_metrics`.
    fn take_metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.inner.metrics.take().to_py(py)
    }
}

/// Convert an error from persisting to the Python exception the Python
/// path would raise: a Python exception as it was raised, a 500
/// `SynapseError` for missing prev state events, as
/// `_calculate_new_state_dag_extremities` raises, and a `RuntimeError`
/// otherwise.
pub fn persist_error_to_py(e: anyhow::Error) -> PyErr {
    let e = match e.downcast::<PyErr>() {
        Ok(err) => return err,
        Err(e) => e,
    };
    if let Some(missing) = e.downcast_ref::<MissingPrevStateEvents>() {
        return SynapseError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            missing.to_string(),
            "M_UNKNOWN",
            None,
            None,
        );
    }
    PyRuntimeError::new_err(format!("{e:#}"))
}

/// `(internal_metadata, json)`: the two `event_json` columns as Rust would
/// write them for `event`, for tests to compare with Python's.
#[pyfunction]
pub fn persistence_json_for_test(event: &Bound<'_, Event>) -> PyResult<(String, String)> {
    let event = event.get();
    let internal_metadata = event.internal_metadata().to_persistence_value()?;
    let json = event.to_persistence_value()?;
    Ok((internal_metadata.to_string(), json.to_string()))
}

/// The [`PersistContext`] of `context` as a dict, for tests to check the
/// conversion.
#[pyfunction]
pub fn persist_context_for_test<'py>(
    py: Python<'py>,
    context: PersistContext,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("rejected", context.rejected)?;
    dict.set_item("state_group", context.state_group)?;
    dict.set_item("state_group_before_event", context.state_group_before_event)?;
    dict.set_item(
        "state_delta_due_to_event",
        context
            .state_delta_due_to_event
            .as_ref()
            .map(|delta| state_map_to_py(py, delta))
            .transpose()?,
    )?;
    let deltas = PyDict::new(py);
    for (key, delta) in &context.state_group_deltas {
        deltas.set_item(key, state_map_to_py(py, delta)?)?;
    }
    dict.set_item("state_group_deltas", deltas)?;
    dict.set_item("partial_state", context.partial_state)?;
    dict.set_item("app_service", context.app_service)?;
    Ok(dict)
}
