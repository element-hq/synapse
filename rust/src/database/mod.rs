//! Rust database access.
//!
//! Two layers live here:
//!  - the DBAPI2-shaped `Connection`/`Cursor` shim (see the [`postgres`]
//!    submodule), which lets existing Python transaction functions run against a
//!    Rust connection unchanged, and
//!  - the backend-agnostic Rust-native query helpers ([`DbPool`]/[`DbConn`] plus
//!    [`value::DbValue`]), so simple queries can be written once and run against
//!    any backend.
//!
//! Currently the only backend is Postgres ([`tokio_postgres`]); SQLite is
//! expected to follow as a sibling variant of [`DbPool`]/[`DbConn`].

pub mod postgres;
pub mod runtime;
pub mod value;

use pyo3::prelude::*;
use pyo3::types::PyModule;

use crate::database::value::{DbRow, DbValue};

/// A backend-agnostic connection pool.
///
/// A thin enum over each backend's own pool rather than a trait: with a small,
/// closed set of backends this keeps the ergonomic `pool.get().await?` /
/// `conn.query(...).await?` surface without `dyn`/`async_trait` machinery.
pub enum DbPool {
    Postgres(postgres::pool::ConnectionPool),
}

impl DbPool {
    /// Check a connection out of the pool.
    pub async fn get(&self) -> Result<DbConn, anyhow::Error> {
        match self {
            DbPool::Postgres(pool) => Ok(DbConn::Postgres(pool.get().await?)),
        }
    }
}

/// A backend-agnostic connection checked out of a [`DbPool`].
///
/// Returned to the pool when dropped.
pub enum DbConn {
    Postgres(postgres::pool::PooledConnection),
}

impl DbConn {
    /// Run a query and return all resulting rows.
    ///
    /// Parameters are bound positionally to `?` placeholders (Synapse's existing
    /// convention); each backend rewrites them to its own placeholder syntax.
    /// Result cells come back as [`DbValue`]s, read out with
    /// [`value::DbRowExt::try_get`].
    pub async fn query(
        &mut self,
        sql: &str,
        params: &[DbValue],
    ) -> Result<Vec<DbRow>, anyhow::Error> {
        match self {
            DbConn::Postgres(conn) => postgres::query::query(conn, sql, params).await,
        }
    }
}

/// Register the `database` submodule (and its per-backend children) on the
/// top-level `synapse_rust` module.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child = PyModule::new(py, "database")?;

    postgres::register_module(py, &child)?;

    m.add_submodule(&child)?;

    // Mirror the convention used by other rust submodules so
    // `from synapse.synapse_rust import database` works.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.database", child)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    //! These exercise the end-to-end query helper against a live Postgres, so
    //! they only run when `SYNAPSE_TEST_POSTGRES_DSN` is set (e.g. to
    //! `host=postgres user=postgres password=postgres dbname=postgres`);
    //! otherwise they no-op. The placeholder rewriting and value mapping have
    //! their own server-free unit tests.

    use super::*;
    use crate::database::runtime::runtime;
    use crate::database::value::DbRowExt;

    fn test_dsn() -> Option<String> {
        std::env::var("SYNAPSE_TEST_POSTGRES_DSN").ok()
    }

    fn test_pool(dsn: &str) -> DbPool {
        DbPool::Postgres(postgres::pool::create_pool(dsn, 2).unwrap())
    }

    #[test]
    fn query_binds_placeholders_and_decodes_scalar_rows() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };
        let pool = test_pool(&dsn);

        runtime().block_on(async {
            let mut conn = pool.get().await.unwrap();
            let rows = conn
                .query(
                    "SELECT ?::int AS a, ?::text AS b, ?::bool AS c",
                    &[
                        DbValue::Int(7),
                        DbValue::Text("hi".into()),
                        DbValue::Bool(true),
                    ],
                )
                .await
                .unwrap();

            assert_eq!(rows.len(), 1);
            assert_eq!(rows[0].try_get::<i64>(0).unwrap(), 7);
            assert_eq!(rows[0].try_get::<String>(1).unwrap(), "hi");
            assert!(rows[0].try_get::<bool>(2).unwrap());
        });
    }

    #[test]
    fn query_decodes_nulls_bytes_and_multiple_rows() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };
        let pool = test_pool(&dsn);

        runtime().block_on(async {
            let mut conn = pool.get().await.unwrap();

            // A NULL cell reads back as `None`; bytea round-trips byte-for-byte.
            let rows = conn
                .query(
                    "SELECT NULL::text AS a, ?::bytea AS b",
                    &[DbValue::Bytes(vec![0, 255])],
                )
                .await
                .unwrap();
            assert_eq!(rows[0].try_get::<Option<String>>(0).unwrap(), None);
            assert_eq!(rows[0].try_get::<Vec<u8>>(1).unwrap(), vec![0u8, 255]);

            // Several rows come back in order.
            let rows = conn
                .query("SELECT g FROM generate_series(1, 3) AS g ORDER BY g", &[])
                .await
                .unwrap();
            let got: Vec<i64> = rows.iter().map(|r| r.try_get::<i64>(0).unwrap()).collect();
            assert_eq!(got, vec![1, 2, 3]);
        });
    }
}
