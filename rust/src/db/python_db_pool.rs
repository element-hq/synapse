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

/// Wrapper for a `DatabasePool` from the Python side of Synapse.
pub struct DatabasePool<'py> {
    /// The underlying `DatabasePool`
    raw: Bound<'py, PyAny>,
}

impl<'py> DatabasePool<'py> {
    pub fn get_transaction(&mut self, description: &str) -> LoggingTransactionWrapper {
        todo!("TODO");
        // let execute_fn = self.raw.getattr(intern!(self.raw.py(), "runInteraction"))?;
        // execute_fn.call1((sql, args))?;
        // Ok(())
    }
}

/// Wrapper for a `LoggingTransaction` from the Python side of Synapse.
pub struct LoggingTransactionWrapper<'py> {
    /// The underlying `LoggingTransaction`
    raw: Bound<'py, PyAny>,

    /// Dissambiguate which underyling database engine we're working with
    database_engine: DatabaseEngine,
}

impl<'py> FromPyObject<'_, 'py> for LoggingTransactionWrapper<'py> {
    type Error = PyErr;

    /// From Python `LoggingTransaction`
    fn extract(logging_transaction_python_object: Borrowed<'_, 'py, PyAny>) -> PyResult<Self> {
        let database_engine = match logging_transaction_python_object
            .getattr("database_engine")
            .expect("Expected the Python object you passed to be `LoggingTransaction` which should have `database_engine` attr")
            .get_type()
            .name()
            .expect("Expected `LoggingTransaction.database_engine` to have a type name")
            .to_str()
            .expect("Expected to be able to convert the `LoggingTransaction.database_engine` type name to a string")
        {
            "PostgresEngine" => DatabaseEngine::Postgres,
            "Sqlite3Engine" => DatabaseEngine::Sqlite,
            other => unimplemented!("Unknown database engine {other:?}. This is a Synapse programming error."),
        };
        Ok(Self {
            raw: logging_transaction_python_object.cast()?.to_owned(),
            database_engine,
        })
    }
}

impl<'py> LoggingTransactionWrapper<'py> {
    pub fn execute(&mut self, sql: &str, args: &'py Bound<'py, PyAny>) -> PyResult<()> {
        let execute_fn = self.raw.getattr(intern!(self.raw.py(), "execute"))?;
        execute_fn.call1((sql, args))?;
        Ok(())
    }
}
