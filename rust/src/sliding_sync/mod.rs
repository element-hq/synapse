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

//! Simplified sliding sync (MSC4186) in Rust, reading the database through
//! the native Postgres driver.
//!
//! [`SlidingSyncHandler`] is the Python entry point, used by
//! `synapse.handlers.sliding_sync.SlidingSyncHandler` when
//! `rust_sliding_sync` is on. It shares the v2 sync handler's
//! [`SyncStore`], so the two read through the same caches.
//!
//! The handler's entry points follow the v2 handler's shape: a `start_*`
//! method reads what it needs from Python and returns a future, which the
//! public method runs off the GIL on the runtime and returns as a deferred
//! ([`SlidingSyncHandler::defer`]), and a `_blocking` twin runs to completion
//! for tests ([`SlidingSyncHandler::block_on`]).

use std::collections::HashMap;
use std::sync::Arc;

use futures::future::BoxFuture;
use pyo3::{exceptions::PyRuntimeError, intern, prelude::*, types::PyModule};
use request::SlidingSyncRequest;
use result::{RoomInput, RoomsResult};

use crate::database::postgres::pool::{ConnectionPool, PyConnectionPool};
use crate::deferred::{create_deferred_with, OnCancel};
use crate::runtime::RustRuntime;
use crate::storage::sync_store::{anyhow_to_py, SyncDbStats, SyncStore};

pub mod config;
pub mod connection;
pub mod encode;
pub mod extensions;
pub mod request;
pub mod required_state;
pub mod result;
pub mod room;
pub mod room_entry;
pub mod room_lists;

/// Computes sliding sync responses with the Rust implementation, reading
/// the database through the native Postgres driver.
#[pyclass(frozen)]
pub struct SlidingSyncHandler {
    /// The Python `RustConnectionPool`. Its `_pool` is read per call, as
    /// `close()`/`start()` replace it.
    db_pool: Py<PyAny>,
    runtime: RustRuntime,
    /// The queries, over the Rust store's caches. The v2 sync handler's.
    store: Arc<SyncStore>,
}

impl SlidingSyncHandler {
    pub fn new(db_pool: Py<PyAny>, runtime: RustRuntime, store: Arc<SyncStore>) -> Self {
        SlidingSyncHandler {
            db_pool,
            runtime,
            store,
        }
    }

    pub fn store(&self) -> &Arc<SyncStore> {
        &self.store
    }

    /// The connection pool to read through, or an error if it is closed.
    pub fn pool(&self, py: Python<'_>) -> PyResult<ConnectionPool> {
        let pool = self.db_pool.bind(py).getattr(intern!(py, "_pool"))?;
        if pool.is_none() {
            return Err(PyRuntimeError::new_err("the database pool is closed"));
        }
        Ok(pool.cast::<PyConnectionPool>()?.get().pool.clone())
    }

    /// Run `future` on the runtime and return a deferred for its result.
    /// Cancelling the deferred stops the computation. The servlet is not
    /// `@cancellable`, so today that only happens on shutdown.
    pub fn defer<'py, O>(
        &self,
        py: Python<'py>,
        future: BoxFuture<'static, PyResult<O>>,
    ) -> PyResult<Bound<'py, PyAny>>
    where
        for<'a> O: IntoPyObject<'a> + Send + 'static,
    {
        create_deferred_with(py, &self.runtime, future, OnCancel::Abort)
    }

    /// Run `future` to completion on the runtime, releasing the GIL. For the
    /// `_blocking` entry points.
    pub fn block_on<T: Send>(
        &self,
        py: Python<'_>,
        future: BoxFuture<'static, PyResult<T>>,
    ) -> PyResult<T> {
        let handle = self.runtime.tokio_handle()?;
        py.detach(|| handle.block_on(future))
    }

    /// Start computing the entries of `rooms`: read them from Python, and
    /// return the future that computes them off the GIL.
    fn start_room_entries(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        rooms: Vec<Bound<'_, RoomInput>>,
        instance_ids: HashMap<String, i64>,
    ) -> PyResult<BoxFuture<'static, PyResult<RoomsResult>>> {
        let pool = self.pool(py)?;
        let request = Arc::clone(&request.get().data);
        let rooms: Vec<_> = rooms.iter().map(|room| room.get().data.clone()).collect();
        let store = Arc::clone(&self.store);
        let now_ms = self.runtime.clock().now_millis() as i64;
        Ok(Box::pin(async move {
            let reader = store.sliding_sync_reader(pool);
            let outcomes =
                room_entry::generate_room_entries(&reader, &request, &instance_ids, now_ms, &rooms)
                    .await
                    .map_err(anyhow_to_py)?;
            let stats = SlidingSyncStats::from_db(&reader.stats());
            Ok(RoomsResult::new(outcomes, request.from.is_some(), stats))
        }))
    }
}

#[pymethods]
impl SlidingSyncHandler {
    /// `get_room_sync_data` for each of `rooms`, twenty at a time: a
    /// deferred resolving to a `RoomsResult`. `instance_ids` are
    /// `get_id_for_instance` for the writers the rooms' tokens may name.
    #[pyo3(signature = (request, rooms, *, instance_ids))]
    fn generate_room_entries<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'py, SlidingSyncRequest>,
        rooms: Vec<Bound<'py, RoomInput>>,
        instance_ids: HashMap<String, i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_room_entries(py, request, rooms, instance_ids)?;
        self.defer(py, future)
    }

    /// As [`Self::generate_room_entries`], but blocks.
    #[pyo3(signature = (request, rooms, *, instance_ids))]
    fn generate_room_entries_blocking(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        rooms: Vec<Bound<'_, RoomInput>>,
        instance_ids: HashMap<String, i64>,
    ) -> PyResult<RoomsResult> {
        let future = self.start_room_entries(py, request, rooms, instance_ids)?;
        self.block_on(py, future)
    }
}

/// What the Rust implementation did for one request, for the
/// `synapse_sliding_sync_rust_*` histograms. Python observes it once per
/// request.
#[pyclass(frozen, get_all, skip_from_py_object)]
#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct SlidingSyncStats {
    /// Time spent waiting on statements, in seconds.
    pub db_time: f64,
    /// Time spent waiting for a connection from the pool, in seconds.
    pub checkout_time: f64,
    /// Transactions begun.
    pub transactions: u64,
    /// Statements executed.
    pub queries: u64,
    /// Rooms whose entry Rust computed.
    pub rooms_computed: u64,
    /// Rooms whose lazy-loaded members were looked up in the connection
    /// state.
    pub lazy_member_lookups: u64,
}

impl SlidingSyncStats {
    /// The database half of the stats, from a reader's.
    pub fn from_db(db: &SyncDbStats) -> Self {
        SlidingSyncStats {
            db_time: db.db_time.as_secs_f64(),
            checkout_time: db.checkout_time.as_secs_f64(),
            transactions: db.transactions,
            queries: db.queries,
            ..Default::default()
        }
    }
}

#[pymethods]
impl SlidingSyncStats {
    #[new]
    #[pyo3(signature = (*, db_time=0.0, checkout_time=0.0, transactions=0, queries=0, rooms_computed=0, lazy_member_lookups=0))]
    fn py_new(
        db_time: f64,
        checkout_time: f64,
        transactions: u64,
        queries: u64,
        rooms_computed: u64,
        lazy_member_lookups: u64,
    ) -> Self {
        SlidingSyncStats {
            db_time,
            checkout_time,
            transactions,
            queries,
            rooms_computed,
            lazy_member_lookups,
        }
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "sliding_sync")?;
    child_module.add_class::<SlidingSyncHandler>()?;
    child_module.add_class::<SlidingSyncStats>()?;
    child_module.add_class::<request::SlidingSyncRequest>()?;
    child_module.add_class::<RoomInput>()?;
    child_module.add_class::<result::RoomResult>()?;
    child_module.add_class::<RoomsResult>()?;
    child_module.add_function(wrap_pyfunction!(encode::encode_rooms, &child_module)?)?;
    child_module.add_class::<crate::storage::sliding_sync_store::PySlidingSyncStoreForTest>()?;
    config::register_test_functions(&child_module)?;
    connection::register_test_functions(&child_module)?;
    extensions::register_test_functions(&child_module)?;
    required_state::register_test_functions(&child_module)?;
    room::register_test_functions(&child_module)?;
    room_lists::register_test_functions(&child_module)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import sliding_sync` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.sliding_sync", child_module)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    #[test]
    fn stats_from_db() {
        let db = SyncDbStats {
            checkouts: 3,
            checkout_time: Duration::from_millis(5),
            transactions: 2,
            queries: 7,
            db_time: Duration::from_millis(250),
            ..Default::default()
        };
        let stats = SlidingSyncStats::from_db(&db);
        assert_eq!(stats.transactions, 2);
        assert_eq!(stats.queries, 7);
        assert!((stats.db_time - 0.25).abs() < 1e-9);
        assert!((stats.checkout_time - 0.005).abs() < 1e-9);
        assert_eq!(stats.rooms_computed, 0);
    }
}
