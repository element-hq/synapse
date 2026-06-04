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

use pyo3::{intern, prelude::*};

use crate::storage::db::{DatabasePool, Transaction};

/// The database engines we support in the Python side of Synapse
#[derive(Copy, Clone, Debug)]
pub enum DatabaseEngine {
    Sqlite,
    Postgres,
}

impl DatabaseEngine {
    //[inline]
    pub fn supports_using_any_list(&self) -> bool {
        match self {
            DatabaseEngine::Sqlite => false,
            DatabaseEngine::Postgres => true,
        }
    }
}

pub struct PythonDatabasePool {}

impl DatabasePool for PythonDatabasePool {
    pub fn get_transaction(&self, description: &str) -> dyn Transaction {
        todo!("...");
        // let execute_fn = self.raw.getattr(intern!(self.raw.py(), "runInteraction"))?;
        // execute_fn.call1((sql, args))?;
        // Ok(())
    }
}

fn detect_engine(txn_py: &Bound<'_, PyAny>) -> PyResult<DatabaseEngine> {
    let name = txn_py
        .getattr("database_engine")
        .expect("`LoggingTransaction` must have `database_engine` attr")
        .get_type()
        .name()
        .expect("`database_engine` type must have a name")
        .to_str()
        .expect("`database_engine` type name must be valid UTF-8")
        .to_owned();

    Ok(match name.as_str() {
        "PostgresEngine" => DatabaseEngine::Postgres,
        "Sqlite3Engine" => DatabaseEngine::Sqlite,
        other => unimplemented!(
            "Unknown database engine {other:?}. This is a Synapse programming error."
        ),
    })
}

/// Wrapper for a `LoggingTransaction` from the Python side of Synapse.
///
/// Holds no `'py` lifetime so it can be stored and moved freely across threads.
/// Use [`execute`](Self::execute) (or other methods) while holding the GIL.
pub struct LoggingTransactionWrapper {
    /// The underlying `LoggingTransaction`
    raw: Py<PyAny>,

    /// Disambiguate which underlying database engine we're working with
    pub database_engine: DatabaseEngine,
}

impl<'a, 'py> FromPyObject<'a, 'py> for LoggingTransactionWrapper {
    type Error = PyErr;

    /// Extract from a Python `LoggingTransaction` passed as an argument.
    ///
    /// The resulting wrapper has `done_tx = None`; Python owns the transaction lifetime.
    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let database_engine = detect_engine(&obj.to_owned())?;
        Ok(Self {
            raw: obj.to_owned().unbind(),
            database_engine,
        })
    }
}

impl LoggingTransactionWrapper {
    pub fn execute<'py>(
        &mut self,
        py: Python<'py>,
        sql: &str,
        args: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let execute_fn = self.raw.bind(py).getattr(intern!(py, "execute"))?;
        execute_fn.call1((sql, args))?;
        Ok(())
    }
}

impl Transaction for LoggingTransactionWrapper {
    fn query(&self, sql: &str, args: &[&str]) -> () {
        self.execute(sql, args);
    }
}
