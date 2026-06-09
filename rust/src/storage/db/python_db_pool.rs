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

//! We have these three main classes:
//!  - Database pool [`PythonDatabasePoolWrapper`] which creates
//!  - connections [`LoggingDatabaseConnectionWrapper`] which creates
//!  - transactions [`LoggingTransactionWrapper`]

use pyo3::{intern, prelude::*, types::PyCFunction, types::PyList};

use crate::storage::db::{DatabaseConnection, DatabasePool, Row, Transaction};

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

/// Wrapper for a `DatabasePool` from the Python side of Synapse.
pub struct PythonDatabasePoolWrapper {
    /// The underlying `DatabasePool`
    database_pool_py: Py<PyAny>,
}

impl<'a, 'py> FromPyObject<'a, 'py> for PythonDatabasePoolWrapper {
    type Error = PyErr;

    /// Extract from a Python `DatabasePool` passed as an argument.
    fn extract(database_pool_py: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        Ok(Self {
            database_pool_py: database_pool_py.to_owned().unbind(),
        })
    }
}

#[async_trait::async_trait]
impl DatabasePool for PythonDatabasePoolWrapper {
    async fn get_connection(&self) -> Result<Box<dyn DatabaseConnection>, anyhow::Error> {
        let callback_func =
            PyCFunction::new_closure(py, None, None, move |args, _| -> PyResult<Py<PyAny>> {
                // TODO: Error if already called

                let py = args.py();
                // We found our `LoggingDatabaseConnection`
                let conn_py = args.get_item(0)?;
                tx.send(conn_py.unbind());
            });

        let execute_fn = self
            .database_pool_py
            .getattr(intern!(self.database_pool_py.py(), "runWithConnection"))?;
        execute_fn.call1((callback_func,))?;

        Ok(Box::new(LoggingDatabaseConnectionWrapper {
            database_pool_py: self.database_pool_py,
            logging_database_connection_py: connection,
        }))
    }
}

/// Wrapper for a `LoggingDatabaseConnection` from the Python side of Synapse.
pub struct LoggingDatabaseConnectionWrapper {
    /// The underlying `DatabasePool`
    ///
    /// We purposely avoid `Bound<'py, PyAny>` so it can be stored and moved freely
    /// across threads (like to extract it from the `runWithConnection(...)` callback).
    /// You will need to acquire your own `py` and bind it using
    /// `database_pool_py.bind(py)` to do anything useful.
    database_pool_py: Py<PyAny>,
    /// The underlying `LoggingDatabaseConnection`
    logging_database_connection_py: Py<PyAny>,
}

#[async_trait::async_trait]
impl DatabaseConnection for LoggingDatabaseConnectionWrapper {
    async fn get_transaction(
        &self,
        description: &str,
    ) -> Result<Box<dyn Transaction>, anyhow::Error> {
        // Synapse's `DatabasePool.new_transaction` has built-in retry functionality and
        // can call this function multiple times under certain failure modes. Normally,
        // everything in the transaction happens in the callback but since we have a
        // little bit of a different API surface, we instead extract the transaction for
        // us to use outside.
        //
        // Re-using `runInteraction`, means we get all of the logging, metrics, etc for
        // free.
        let callback_func =
            PyCFunction::new_closure(py, None, None, move |args, _| -> PyResult<Py<PyAny>> {
                // TODO: Error if already called

                let py = args.py();
                let txn: LoggingTransactionWrapper = args.get_item(0)?;
                tx.send(txn);

                // Wait until we see the signal that we're `done_with_txn_rx`
            });

        let execute_fn = self
            .database_pool_py
            .getattr(intern!(self.database_pool_py.py(), "new_transaction"))?;
        execute_fn.call1((
            self.logging_database_connection_py,
            description,
            [],
            [],
            [],
            callback_func,
        ))?;

        Ok(Box::new(txn))
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
pub struct LoggingTransactionWrapper {
    /// The underlying `LoggingTransaction`
    ///
    /// We purposely avoid `Bound<'py, PyAny>` so it can be stored and moved freely
    /// across threads (like to extract it from the `new_transaction(...)` callback).
    /// You will need to acquire your own `py` and bind it using
    /// `logging_transaction_py.bind(py)` to do anything useful.
    logging_transaction_py: Py<PyAny>,

    /// Disambiguate which underlying database engine we're working with
    pub database_engine: DatabaseEngine,
}

impl<'a, 'py> FromPyObject<'a, 'py> for LoggingTransactionWrapper {
    type Error = PyErr;

    /// Extract from a Python `LoggingTransaction` passed as an argument.
    fn extract(logging_transaction_py: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let database_engine = detect_engine(&logging_transaction_py.to_owned())?;
        Ok(Self {
            logging_transaction_py: logging_transaction_py.to_owned().unbind(),
            database_engine,
        })
    }
}

pub trait ValidDatabaseFieldType {}
pub trait ValidDatabaseReturnType {}
impl ValidDatabaseFieldType for String {}
impl ValidDatabaseFieldType for usize {}
impl<T: ValidDatabaseFieldType> ValidDatabaseFieldType for Option<T> {}
impl<T0: ValidDatabaseFieldType> ValidDatabaseReturnType for (T0,) {}
impl<T0: ValidDatabaseFieldType, T1: ValidDatabaseFieldType> ValidDatabaseReturnType for (T0, T1) {}
impl<T0: ValidDatabaseFieldType, T1: ValidDatabaseFieldType, T2: ValidDatabaseFieldType>
    ValidDatabaseReturnType for (T0, T1, T2)
{
}
impl<
        T0: ValidDatabaseFieldType,
        T1: ValidDatabaseFieldType,
        T2: ValidDatabaseFieldType,
        T3: ValidDatabaseFieldType,
    > ValidDatabaseReturnType for (T0, T1, T2, T3)
{
}

impl LoggingTransactionWrapper {
    pub fn execute<'py>(
        &mut self,
        py: Python<'py>,
        sql: &str,
        args: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let execute_fn = self
            .logging_transaction_py
            .bind(py)
            .getattr(intern!(py, "execute"))?;
        execute_fn.call1((sql, args))?;
        Ok(())
    }

    pub fn fetchall<'py, T: FromPyObjectOwned<'py> + ValidDatabaseReturnType>(
        &mut self,
        py: Python<'py>,
    ) -> anyhow::Result<Vec<T>> {
        let fetch_fn = self
            .logging_transaction_py
            .bind(py)
            .getattr(intern!(py, "fetchall"))?;
        Ok(fetch_fn.call0()?.extract()?)
    }
}

#[async_trait::async_trait]
impl Transaction for LoggingTransactionWrapper {
    async fn query(&self, sql: &str, args: &[&str]) -> Result<Vec<Row>, anyhow::Error> {
        Python::attach(|py| -> PyResult<Vec<Row>> {
            // Convert the Rust `&[&str]` of SQL parameters into a Python sequence so it
            // can be passed through to the Python-side `execute`.
            let args = PyList::new(py, args)?;
            // Run the query
            self.execute(py, sql, args.as_any())?;
            // Get the results
            let rows = self.fetchall(py)?;

            Ok(rows)
        })
        .map_err(anyhow::Error::from)
    }

    async fn commit(&self) -> Result<(), anyhow::Error> {
        // In Synapse, `commit` is part of `LoggingDatabaseConnection` and will be
        // called as part of the `new_transaction(...)` machinery we used to create the
        // transaction in the first place.
        //
        // We just need to send the proper signal which will finish the txn callback and
        // have it run.
        done_with_txn_tx.send(())
        // TODO: How can we guarantee that `commit` was run? Perhaps we have to wait for
        // `new_transaction(...)` to complete successfully
    }
}
