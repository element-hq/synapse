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

use pyo3::prelude::*;

/// Wrapper for a `LoggingTransaction` from the Python side of Synapse.
pub struct LoggingTransactionWrapper<'py> {
    /// The underlying LoggingTransaction
    raw: &'py PyAny,

    database_engine: DatabaseEngine,
}

impl<'source> FromPyObject<'source> for LoggingTransactionWrapper<'source> {
    fn extract(ob: &'source PyAny) -> PyResult<Self> {
        let database_engine = match ob
            .getattr("database_engine")?
            .get_type()
            .name()
            .expect("DB engine should have a type name")
        {
            "PostgresEngine" => DatabaseEngine::Postgres,
            "Sqlite3Engine" => DatabaseEngine::Sqlite,
            other => panic!("Unknown engine {other:?}"),
        };
        Ok(Self {
            raw: ob,
            database_engine,
        })
    }
}

impl<'py> LoggingTransactionWrapper<'py> {
    pub fn execute<T: IntoPy<PyObject>>(&mut self, sql: &str, args: T) -> PyResult<()> {
        let execute_fn = self.raw.getattr(intern!(self.raw.py(), "execute"))?;
        execute_fn.call1((sql, args))?;
        Ok(())
    }

    pub fn execute_values<T: IntoPy<PyObject>, R: FromPyObject<'py> + ValidDatabaseReturnType>(
        &mut self,
        sql: &str,
        args: T,
    ) -> PyResult<Vec<R>> {
        let execute_fn = self.raw.getattr(intern!(self.raw.py(), "execute_values"))?;
        Ok(execute_fn.call1((sql, args))?.extract()?)
    }

    pub fn fetchall<T: FromPyObject<'py> + ValidDatabaseReturnType>(
        &mut self,
    ) -> anyhow::Result<Vec<T>> {
        let fetch_fn = self.raw.getattr(intern!(self.raw.py(), "fetchall"))?;
        Ok(fetch_fn.call0()?.extract()?)
    }
}

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
