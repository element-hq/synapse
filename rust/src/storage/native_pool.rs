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

//! The native driver's pool, for Rust code that runs on tokio and must not
//! take the GIL for each query.

use std::sync::Mutex;

use pyo3::{exceptions::PyRuntimeError, intern, prelude::*};

use crate::{
    database::postgres::pool::{ConnectionPool, PyConnectionPool},
    gil::with_gil,
};

/// The native pool, as the Python `RustConnectionPool` holds it. That
/// replaces its pool when it is closed and started again, so a closed pool
/// is read again from Python, under the GIL site `site`.
pub struct NativePool {
    /// The Python `RustConnectionPool`.
    python: Py<PyAny>,
    site: &'static str,
    cached: Mutex<Option<ConnectionPool>>,
}

impl NativePool {
    pub fn new(python: Py<PyAny>, site: &'static str) -> Self {
        NativePool {
            python,
            site,
            cached: Mutex::new(None),
        }
    }

    pub fn get(&self) -> PyResult<ConnectionPool> {
        if let Some(pool) = self
            .cached
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
            .filter(|pool| !pool.is_closed())
        {
            return Ok(pool.clone());
        }
        // The lock is not held while this takes the GIL. A thread that holds
        // the GIL and waits for the lock would otherwise deadlock with us.
        let pool = with_gil(self.site, |py| -> PyResult<ConnectionPool> {
            let pool = self.python.bind(py).getattr(intern!(py, "_pool"))?;
            if pool.is_none() {
                return Err(PyRuntimeError::new_err("the database pool is closed"));
            }
            Ok(pool.cast::<PyConnectionPool>()?.get().pool.clone())
        })?;
        *self.cached.lock().unwrap_or_else(|e| e.into_inner()) = Some(pool.clone());
        Ok(pool)
    }
}
