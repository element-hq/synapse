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

use std::future::Future;

use futures::future::BoxFuture;

pub mod python_db_pool;
pub mod rust_db_pool;

/// A database connection pool.
///
/// Code is written against this trait so the same store can run against either
/// the Python-backed pool (in Synapse, see [`python_db_pool`]) or a native
/// `tokio-postgres` pool (in `synapse-rust-apps`, see [`rust_db_pool`]). The pool
/// type is fixed within any given binary, so callers are generic over the pool
/// (e.g. `Store<P: DatabasePool>`) rather than using dynamic dispatch.
///
/// `Send + Sync` so it can be stored in a `#[pyclass]` and shared across threads.
pub trait DatabasePool: Send + Sync {
    /// Starts a transaction on the database and runs the given function,
    /// returning its result.
    ///
    /// `func` may be called multiple times under certain failure modes (like
    /// serialization and deadlock errors), so it is `Fn` rather than `FnOnce`.
    fn run_interaction<R, F>(
        &self,
        name: &'static str,
        func: F,
    ) -> impl Future<Output = anyhow::Result<R>> + Send
    where
        R: Send + 'static,
        F: for<'txn> Fn(&'txn mut dyn Transaction) -> BoxFuture<'txn, anyhow::Result<R>>
            + Send
            + Sync
            + 'static;
}

/// A [`tokio_postgres::Transaction`] looking thing that we can use on the Rust side to
/// interact with the database
#[async_trait::async_trait]
pub trait Transaction: Send {
    async fn query(&mut self, sql: &str, args: &[&str])
        -> Result<Vec<Box<dyn Row>>, anyhow::Error>;
}

/// A single backend-agnostic value within a [`Row`].
///
/// Each pool maps the values its database driver hands back into this common
/// set, so callers can work with one representation regardless of engine.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    /// A SQL `NULL`.
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
}

/// A row of data returned from the database by a query.
///
/// Modelled after [`tokio_postgres::Row`]: values are pulled out by their numeric
/// index with [`get`](Self::get) / [`try_get`](Self::try_get). Each database pool
/// implements this trait for its own native row type — the Python pool inspects
/// the Python type of each cell, while the `tokio-postgres` pool reuses that
/// crate's own `FromSql` machinery — converting cells into the engine-agnostic
/// [`Value`] returned by [`Row::value`].
pub trait Row: std::fmt::Debug + Send {
    /// Returns the number of values in the row.
    fn len(&self) -> usize;

    /// Returns whether the row contains no values.
    fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Returns the backend-agnostic [`Value`] at `index`, converting from the
    /// pool's native representation.
    ///
    /// Errors if `index` is out of bounds.
    fn value(&self, index: usize) -> Result<Value, anyhow::Error>;
}

impl dyn Row {
    /// Deserializes a value from the row, specified by its numeric index,
    /// returning an error if the index is out of bounds or the value cannot be
    /// converted into `T`.
    pub fn try_get<T: FromValue>(&self, index: usize) -> Result<T, anyhow::Error> {
        T::from_value(self.value(index)?)
    }
}

/// Converts a backend-agnostic [`Value`] into a concrete Rust type, analogous to
/// `tokio-postgres`'s `FromSql`.
pub trait FromValue: Sized {
    fn from_value(value: Value) -> Result<Self, anyhow::Error>;
}

impl FromValue for bool {
    fn from_value(value: Value) -> Result<Self, anyhow::Error> {
        match value {
            Value::Bool(b) => Ok(b),
            // SQLite has no native boolean type and stores them as integers.
            Value::Int(i) => Ok(i != 0),
            other => anyhow::bail!("cannot read {other:?} as bool"),
        }
    }
}

impl FromValue for i64 {
    fn from_value(value: Value) -> Result<Self, anyhow::Error> {
        match value {
            Value::Int(i) => Ok(i),
            Value::Bool(b) => Ok(b as i64),
            other => anyhow::bail!("cannot read {other:?} as i64"),
        }
    }
}

impl FromValue for f64 {
    fn from_value(value: Value) -> Result<Self, anyhow::Error> {
        match value {
            Value::Float(f) => Ok(f),
            Value::Int(i) => Ok(i as f64),
            other => anyhow::bail!("cannot read {other:?} as f64"),
        }
    }
}

impl FromValue for String {
    fn from_value(value: Value) -> Result<Self, anyhow::Error> {
        match value {
            Value::Text(s) => Ok(s),
            other => anyhow::bail!("cannot read {other:?} as String"),
        }
    }
}

impl FromValue for Vec<u8> {
    fn from_value(value: Value) -> Result<Self, anyhow::Error> {
        match value {
            Value::Bytes(b) => Ok(b),
            other => anyhow::bail!("cannot read {other:?} as bytes"),
        }
    }
}

impl<T: FromValue> FromValue for Option<T> {
    fn from_value(value: Value) -> Result<Self, anyhow::Error> {
        match value {
            Value::Null => Ok(None),
            other => Ok(Some(T::from_value(other)?)),
        }
    }
}
