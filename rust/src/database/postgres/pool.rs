//! A `deadpool`-managed pool of `tokio_postgres` connections for the native
//! Rust Postgres backend.
//!
//! We use the generic `deadpool::managed` pool with our own [`ConnectionManager`]
//! rather than the `deadpool-postgres` crate, so that creating a connection
//! reuses our own DSN handling (libpq's default host, see
//! [`super::fixup_default_host`]) and drives the connection task on the shared
//! runtime.
//!
//! The pooled item is a plain [`tokio_postgres::Client`]. Rust-native code takes
//! one from the pool and uses it with the standard `tokio_postgres` async query
//! functions; the Python-facing `Connection`/`Cursor` shim borrows from the
//! *same* pool, so both share a single set of connections rather than running
//! two pools that could exhaust the server's connection limit between them.

use deadpool::managed::{Manager, Metrics, Object, Pool, PoolError, RecycleError, RecycleResult};
use log::warn;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio_postgres::{Client, Config, NoTls};

use crate::database::postgres::connection::Connection;
use crate::database::postgres::errors::{pg_err_to_py, OperationalError};
use crate::database::postgres::fixup_default_host;
use crate::database::postgres::helpers::BlockingPostgres;
use crate::database::runtime::runtime;

/// Per-connection session settings applied once when a pooled connection is
/// opened (the native equivalent of the engine's `on_new_connection`).
///
/// Note we deliberately do *not* set `bytea_output`: unlike psycopg2,
/// [`tokio_postgres`] talks the binary protocol for prepared statements, so the
/// text-format `bytea_output` GUC is irrelevant to how we decode `bytea`.
#[derive(Clone)]
pub struct SessionConfig {
    /// When false, `synchronous_commit` is turned off for the session (don't
    /// wait for the server to fsync before returning from a commit).
    pub synchronous_commit: bool,
    /// If set, `statement_timeout` (in milliseconds) — statements running longer
    /// are aborted and turned into errors.
    pub statement_timeout_ms: Option<i32>,
}

impl Default for SessionConfig {
    fn default() -> Self {
        // Match the engine's defaults: synchronous commit on, no statement
        // timeout unless configured.
        Self {
            synchronous_commit: true,
            statement_timeout_ms: None,
        }
    }
}

impl SessionConfig {
    /// The `SET` statements to run on a freshly-opened connection.
    fn setup_sql(&self) -> String {
        // Match the engine's `default_isolation_level` (REPEATABLE READ) as the
        // session default, so a plain `BEGIN` gets the same isolation psycopg2
        // gave us.
        let mut sql = String::from(
            "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ;",
        );
        if !self.synchronous_commit {
            sql.push_str(" SET synchronous_commit TO OFF;");
        }
        if let Some(ms) = self.statement_timeout_ms {
            // `ms` is an integer we control (from config), so inlining is safe.
            sql.push_str(&format!(" SET statement_timeout TO {ms};"));
        }
        sql
    }
}

/// Creates and recycles [`tokio_postgres`] connections for a [`ConnectionPool`].
pub struct ConnectionManager {
    /// The resolved connection config, parsed once from the DSN (with libpq's
    /// default host filled in if the DSN omitted one).
    config: Config,
    /// Session settings applied to each connection when it is opened.
    session: SessionConfig,
}

impl ConnectionManager {
    /// Build a manager from a libpq-style DSN and session settings.
    pub fn from_dsn(dsn: &str, session: SessionConfig) -> Result<Self, anyhow::Error> {
        Ok(Self {
            config: fixup_default_host(dsn)?,
            session,
        })
    }
}

impl Manager for ConnectionManager {
    type Type = Client;
    type Error = tokio_postgres::Error;

    async fn create(&self) -> Result<Client, Self::Error> {
        // Establish the connection, then drive its long-lived connection task
        // (which pumps the socket) on the shared runtime. The task ends on its
        // own when the `Client` is dropped, i.e. when the pool discards this
        // connection.
        let (client, connection) = self.config.connect(NoTls).await?;

        runtime().spawn(async move {
            if let Err(e) = connection.await {
                warn!("postgres connection error: {e}");
            }
        });

        // Apply per-connection session setup once, up front (the native
        // equivalent of the engine's `on_new_connection`).
        client.batch_execute(&self.session.setup_sql()).await?;

        Ok(client)
    }

    async fn recycle(&self, client: &mut Client, _: &Metrics) -> RecycleResult<Self::Error> {
        // Cheap liveness check before handing a pooled connection back out: if
        // the connection task has ended (server closed the socket, fatal error,
        // …) the client reports closed, so tell deadpool to drop it and create
        // a fresh one rather than returning a dead connection.
        if client.is_closed() {
            return Err(RecycleError::message("connection closed"));
        }
        Ok(())
    }
}

/// A pool of [`tokio_postgres`] connections.
pub type ConnectionPool = Pool<ConnectionManager>;

/// A connection checked out of a [`ConnectionPool`]. Dereferences to the
/// underlying [`tokio_postgres::Client`]; returned to the pool when dropped.
pub type PooledConnection = Object<ConnectionManager>;

/// Build a [`ConnectionPool`] from a libpq-style DSN, capped at `max_size`
/// connections, using the default [`SessionConfig`].
pub fn create_pool(dsn: &str, max_size: usize) -> Result<ConnectionPool, anyhow::Error> {
    create_pool_with_session(dsn, max_size, SessionConfig::default())
}

/// Build a [`ConnectionPool`] with explicit per-connection [`SessionConfig`].
pub fn create_pool_with_session(
    dsn: &str,
    max_size: usize,
    session: SessionConfig,
) -> Result<ConnectionPool, anyhow::Error> {
    let manager = ConnectionManager::from_dsn(dsn, session)?;
    Ok(Pool::builder(manager).max_size(max_size).build()?)
}

/// The Python-facing connection pool.
///
/// This is the single entry point Python uses to obtain a [`Connection`]:
/// build a pool from a DSN once, then check connections out of it with
/// [`PyConnectionPool::connect`]. Each checkout hands back a [`Connection`]
/// borrowed from the pool that returns itself for reuse when closed or dropped.
#[pyclass(name = "ConnectionPool", frozen)]
pub struct PyConnectionPool {
    pool: ConnectionPool,
}

#[pymethods]
impl PyConnectionPool {
    /// Build a pool from a libpq-style DSN, capped at `max_size` connections.
    ///
    /// This only parses the DSN; connections are opened lazily on the first
    /// (and each subsequent) [`connect`](Self::connect) that needs a new one.
    /// Each connection gets the same per-connection session setup: the default
    /// (REPEATABLE READ) isolation level, plus `synchronous_commit` /
    /// `statement_timeout` if configured here.
    #[new]
    #[pyo3(signature = (dsn, max_size = 10, *, synchronous_commit = true, statement_timeout_ms = None))]
    fn new(
        dsn: &str,
        max_size: usize,
        synchronous_commit: bool,
        statement_timeout_ms: Option<i32>,
    ) -> PyResult<Self> {
        let session = SessionConfig {
            synchronous_commit,
            statement_timeout_ms,
        };
        let pool = create_pool_with_session(dsn, max_size, session).map_err(|e| {
            PyRuntimeError::new_err(format!("failed to build connection pool: {e}"))
        })?;
        Ok(Self { pool })
    }

    /// Check a connection out of the pool.
    ///
    /// Blocks (releasing the GIL) until a connection is available, opening a new
    /// one if the pool is below `max_size`. A failure to establish the
    /// connection surfaces through the same DBAPI2 exception hierarchy as a
    /// query error, so callers can treat it like psycopg2's `connect`.
    fn connect(&self, py: Python<'_>) -> PyResult<Connection> {
        let conn = self.pool.get().block_on(py).map_err(pool_err_to_py)?;
        Ok(Connection::new(conn))
    }

    /// Close the pool, closing every idle connection.
    ///
    /// After this, [`connect`](Self::connect) fails; a connection still checked
    /// out is closed when it is returned. Idempotent. This lets the owning
    /// Python code drop the pool's server connections deterministically rather
    /// than waiting for garbage collection.
    fn close(&self) {
        self.pool.close();
    }
}

/// Map a `deadpool` checkout failure onto the DBAPI2 exception hierarchy.
fn pool_err_to_py(err: PoolError<tokio_postgres::Error>) -> PyErr {
    match err {
        // The backend failed to establish the connection: reuse the exact
        // mapping (and `pgcode` tagging) a query error gets.
        PoolError::Backend(e) => pg_err_to_py(&e),
        // Timed out waiting for a slot, pool closed, no runtime, or a
        // post-create hook failure: all connection-level problems, which
        // Synapse treats as retryable operational errors.
        other => OperationalError::new_err(format!("failed to acquire connection: {other}")),
    }
}

#[cfg(test)]
mod tests {
    //! These tests need a live Postgres, so they only run when
    //! `SYNAPSE_TEST_POSTGRES_DSN` is set (e.g. to
    //! `host=postgres user=postgres password=postgres`); otherwise they no-op.
    //! The state-machine / value / error logic is unit-tested elsewhere against
    //! fakes — this is specifically the pooling behaviour against a real server.

    use super::*;
    use crate::database::runtime::runtime;

    fn test_dsn() -> Option<String> {
        std::env::var("SYNAPSE_TEST_POSTGRES_DSN").ok()
    }

    #[test]
    fn pool_acquires_runs_a_query_and_reuses_the_connection() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        let pool = create_pool(&dsn, 2).unwrap();

        runtime().block_on(async {
            // Acquire a connection and run a query with the standard async API.
            let client = pool.get().await.unwrap();
            let row = client.query_one("SELECT 1::int", &[]).await.unwrap();
            assert_eq!(row.get::<_, i32>(0), 1);

            // Returning it to the pool (drop) and acquiring again reuses it.
            drop(client);
            assert_eq!(pool.status().size, 1);

            let client = pool.get().await.unwrap();
            let row = client.query_one("SELECT 2::int", &[]).await.unwrap();
            assert_eq!(row.get::<_, i32>(0), 2);
            assert_eq!(pool.status().size, 1);
        });
    }

    #[test]
    fn pool_hands_out_up_to_max_size_connections_concurrently() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        let pool = create_pool(&dsn, 2).unwrap();

        runtime().block_on(async {
            // Two live checkouts at once => two distinct connections created.
            let a = pool.get().await.unwrap();
            let b = pool.get().await.unwrap();
            assert_eq!(pool.status().size, 2);

            // Both usable independently.
            assert_eq!(
                a.query_one("SELECT 10::int", &[])
                    .await
                    .unwrap()
                    .get::<_, i32>(0),
                10
            );
            assert_eq!(
                b.query_one("SELECT 20::int", &[])
                    .await
                    .unwrap()
                    .get::<_, i32>(0),
                20
            );
        });
    }

    #[test]
    fn create_applies_session_setup_to_each_connection() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        let session = SessionConfig {
            synchronous_commit: false,
            statement_timeout_ms: Some(1234),
        };
        let pool = create_pool_with_session(&dsn, 1, session).unwrap();

        runtime().block_on(async {
            let client = pool.get().await.unwrap();

            // The session default isolation matches the engine's default, and
            // the configured `synchronous_commit` / `statement_timeout` are set.
            let show = |name: &'static str| {
                let client = &client;
                async move {
                    client
                        .query_one(&format!("SHOW {name}"), &[])
                        .await
                        .unwrap()
                        .get::<_, String>(0)
                }
            };
            assert_eq!(
                show("default_transaction_isolation").await,
                "repeatable read"
            );
            assert_eq!(show("synchronous_commit").await, "off");
            assert_eq!(show("statement_timeout").await, "1234ms");
        });
    }
}
