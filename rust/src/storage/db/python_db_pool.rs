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

//! We have these main classes:
//!  - Database pool [`PythonDatabasePoolWrapper`] which allows you to start a...
//!  - transaction [`LoggingTransactionWrapper`] and query the database

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
    fn run_interaction<'txn, R, F>(
        &'txn self,
        name: &'static str,
        func: F,
    ) -> impl Future<Output = anyhow::Result<R>> + 'txn
    where
        R: Send + Sync + 'static,
        F: for<'f> Fn(&'f mut dyn Transaction) -> BoxFuture<'f, anyhow::Result<R>> + Send + 'static,
    {
        Python::attach(|py| -> PyResult<Vec<Row>> {
            let callback_func =
                PyCFunction::new_closure(py, None, None, move |args, _| -> PyResult<Py<PyAny>> {
                    // We found our `LoggingTransactionWrapper`
                    let txn: LoggingTransactionWrapper = args.get_item(0)?;
                    func(txn);
                });

            let execute_fn = self
                .database_pool_py
                .bind(py)
                .getattr(intern!(py, "runInteraction"))?;
            let results = execute_fn.call1((callback_func,))?;
            results
        })
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
}
