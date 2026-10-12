//! A `deadpool`-managed pool of `tokio_postgres` connections for the native
//! Rust Postgres backend.
//!
//! We use the generic `deadpool::managed` pool with our own [`ConnectionManager`]
//! rather than the `deadpool-postgres` crate, so that creating a connection
//! reuses our own DSN handling (libpq's default host, see
//! [`super::fixup_config_defaults`]) and drives the connection task on the shared
//! runtime.
//!
//! The pooled item is a [`PooledClient`]: a [`tokio_postgres::Client`] plus a
//! per-connection prepared-statement cache (see [`PooledClient::prepare_cached`]).
//! Rust-native code takes one from the pool and uses it with the standard
//! `tokio_postgres` async query functions (it derefs to the client); the
//! Python-facing `Connection`/`Cursor` shim borrows from the *same* pool, so
//! both share a single set of connections rather than running two pools that
//! could exhaust the server's connection limit between them.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

use deadpool::managed::{
    Manager, Metrics, Object, Pool, PoolError, RecycleError, RecycleResult, TimeoutType, Timeouts,
};
use deadpool::Runtime;
use log::warn;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::runtime::Handle;
use tokio_postgres::{Client, Config, Connection as PgConnection, NoTls, Socket, Statement};

use crate::database::postgres::connection::Connection;
use crate::database::postgres::errors::{pg_err_to_py, OperationalError};
use crate::database::postgres::helpers::BlockingPostgres;
use crate::database::postgres::resolve_connection_defaults;
use crate::database::postgres::tls::{self, TlsConnector, TlsParams};
use crate::runtime::RustRuntime;

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
    /// If set, `idle_in_transaction_session_timeout` (in milliseconds): a
    /// session left idle inside a transaction for longer is terminated, so a
    /// leaked connection cannot hold locks or block vacuum indefinitely.
    pub idle_in_transaction_session_timeout_ms: Option<i32>,
}

impl Default for SessionConfig {
    fn default() -> Self {
        // Match the engine's defaults: synchronous commit on, no timeouts
        // unless configured.
        Self {
            synchronous_commit: true,
            statement_timeout_ms: None,
            idle_in_transaction_session_timeout_ms: None,
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
        if let Some(ms) = self.idle_in_transaction_session_timeout_ms {
            sql.push_str(&format!(
                " SET idle_in_transaction_session_timeout TO {ms};"
            ));
        }
        sql
    }
}

/// Creates and recycles [`tokio_postgres`] connections for a [`ConnectionPool`].
pub struct ConnectionManager {
    /// The resolved connection config, parsed once from the DSN (with libpq's
    /// default host filled in if the DSN omitted one, and the SSL mode set from
    /// the `ssl*` params).
    config: Config,
    /// Handle to the extension's shared tokio runtime (see
    /// [`crate::runtime::RustRuntime`]). Each connection's long-lived connection task
    /// is spawned on it, and the `Connection` shim handed out for a pooled
    /// connection drives its blocking calls on it.
    handle: Handle,
    /// Session settings applied to each connection when it is opened.
    session: SessionConfig,
    /// How to (or whether to) negotiate TLS for each connection.
    tls: TlsConnector,
    /// If set, a connection is closed rather than reused once it has been
    /// checked out this many times. See [`PoolConfig::max_uses`].
    max_uses: Option<usize>,
}

impl ConnectionManager {
    /// Build a manager from a libpq-style DSN, session settings and TLS params,
    /// spawning connection tasks on the runtime behind `handle`.
    pub fn from_dsn(
        dsn: &str,
        handle: Handle,
        session: SessionConfig,
        tls_params: &TlsParams,
    ) -> Result<Self, anyhow::Error> {
        let (mut config, tls_params) = resolve_connection_defaults(dsn, tls_params)?;
        let (ssl_mode, connector) = tls::build(&tls_params)?;
        config.ssl_mode(ssl_mode);
        Ok(Self {
            config,
            handle,
            session,
            tls: connector,
            max_uses: None,
        })
    }
}

/// Drive a connection's background task (which pumps the socket) on the shared
/// runtime. The task ends on its own when the `Client` is dropped, i.e. when the
/// pool discards the connection. Generic over the stream so it works for both
/// the plaintext and TLS connectors.
fn spawn_connection_task<S>(handle: &Handle, connection: PgConnection<Socket, S>)
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    handle.spawn(async move {
        if let Err(e) = connection.await {
            warn!("postgres connection error: {e}");
        }
    });
}

/// A pooled [`Client`] plus its cache of prepared statements.
///
/// The cache lives *with* the pooled connection (not with a checkout): named
/// prepared statements are per-session server state, so they survive being
/// returned to the pool, and a repeated query on any later checkout of the same
/// connection skips the prepare round-trip. Dereferences to the [`Client`] so
/// existing call sites are unaffected.
pub struct PooledClient {
    client: Client,
    /// Prepared statements keyed by their SQL. Guarded by a plain [`Mutex`]:
    /// the critical sections are lookups/inserts (never awaits).
    statements: Mutex<HashMap<String, Statement>>,
}

/// The cache is cleared once it holds this many statements. Synapse's hot set
/// of distinct SQL strings is far smaller; the bound exists so one-off SQL
/// (e.g. `execute_values`'s literal-spliced statements) can't grow the map
/// without limit on a long-lived connection. A clear-all is crude but simple,
/// and the hot set re-warms in one round of queries.
const STATEMENT_CACHE_CAP: usize = 512;

impl PooledClient {
    /// Fetch the prepared [`Statement`] for `sql`, preparing and caching it on
    /// a miss. With `refresh`, any cached entry is discarded and re-prepared —
    /// used to recover when concurrent DDL invalidates a cached plan (SQLSTATE
    /// `0A000`, "cached plan must not change result type").
    pub async fn prepare_cached(
        &self,
        sql: &str,
        refresh: bool,
    ) -> Result<Statement, tokio_postgres::Error> {
        if refresh {
            self.statements.lock().expect("not poisoned").remove(sql);
        } else if let Some(statement) = self.statements.lock().expect("not poisoned").get(sql) {
            return Ok(statement.clone());
        }

        let statement = self.client.prepare(sql).await?;

        let mut statements = self.statements.lock().expect("not poisoned");
        if statements.len() >= STATEMENT_CACHE_CAP {
            statements.clear();
        }
        statements.insert(sql.to_owned(), statement.clone());
        Ok(statement)
    }

    /// Drop the cached statement for `sql`, if any. No I/O: used to shed a
    /// stale plan from inside an aborted transaction, where re-preparing is
    /// impossible until the transaction ends.
    pub fn invalidate(&self, sql: &str) {
        self.statements.lock().expect("not poisoned").remove(sql);
    }
}

impl std::ops::Deref for PooledClient {
    type Target = Client;

    fn deref(&self) -> &Client {
        &self.client
    }
}

impl Manager for ConnectionManager {
    type Type = PooledClient;
    type Error = tokio_postgres::Error;

    async fn create(&self) -> Result<PooledClient, Self::Error> {
        // Establish the connection, then drive its long-lived connection task
        // (which pumps the socket) on the shared runtime. The task ends on its
        // own when the `Client` is dropped, i.e. when the pool discards this
        // connection.
        let client = match &self.tls {
            TlsConnector::NoTls => {
                let (client, connection) = self.config.connect(NoTls).await?;
                spawn_connection_task(&self.handle, connection);
                client
            }
            TlsConnector::Rustls(connector) => {
                let (client, connection) = self.config.connect(connector.clone()).await?;
                spawn_connection_task(&self.handle, connection);
                client
            }
        };

        // Apply per-connection session setup once, up front (the native
        // equivalent of the engine's `on_new_connection`).
        client.batch_execute(&self.session.setup_sql()).await?;

        Ok(PooledClient {
            client,
            statements: Mutex::new(HashMap::new()),
        })
    }

    async fn recycle(
        &self,
        client: &mut PooledClient,
        metrics: &Metrics,
    ) -> RecycleResult<Self::Error> {
        // Cheap liveness check before handing a pooled connection back out: if
        // the connection task has ended (server closed the socket, fatal error,
        // …) the client reports closed, so tell deadpool to drop it and create
        // a fresh one rather than returning a dead connection.
        if client.is_closed() {
            return Err(RecycleError::message("connection closed"));
        }
        // Retire a connection that has served its share of checkouts (the
        // `txn_limit` config), bounding the server-side state a session
        // accumulates. `recycle_count` is the number of checkouts after the
        // first, so this runs when the `max_uses`th checkout would reuse it.
        if let Some(max_uses) = self.max_uses {
            if metrics.recycle_count + 1 >= max_uses {
                return Err(RecycleError::message("connection reached its use limit"));
            }
        }
        Ok(())
    }
}

/// A pool of [`tokio_postgres`] connections.
pub type ConnectionPool = Pool<ConnectionManager>;

/// A connection checked out of a [`ConnectionPool`]. Dereferences to the
/// underlying [`tokio_postgres::Client`]; returned to the pool when dropped.
pub type PooledConnection = Object<ConnectionManager>;

/// Everything that configures a [`ConnectionPool`] apart from the DSN and the
/// runtime it is driven on.
#[derive(Clone)]
pub struct PoolConfig {
    /// The most connections the pool will open.
    pub max_size: usize,
    /// Per-connection session setup, applied when a connection is opened.
    pub session: SessionConfig,
    /// How to (or whether to) negotiate TLS.
    pub tls: TlsParams,
    /// If set, a checkout ([`PyConnectionPool::connect`] / [`Pool::get`])
    /// blocks for at most this long before failing with a
    /// [`PoolError::Timeout`] rather than waiting indefinitely. Bounds both
    /// the wait-for-a-free-slot phase and the establish-a-new-connection
    /// phase, so a checkout can never hang forever (e.g. against an
    /// unreachable server).
    pub checkout_timeout: Option<Duration>,
    /// If set, a connection is closed and replaced once it has been checked
    /// out this many times, rather than reused forever. This is the pool-side
    /// form of Synapse's `txn_limit`, which bounds the server-side state
    /// (prepared statements, cached plans, memory) a long-lived session
    /// accumulates.
    pub max_uses: Option<usize>,
}

impl Default for PoolConfig {
    fn default() -> Self {
        Self {
            max_size: 10,
            session: SessionConfig::default(),
            tls: TlsParams::default(),
            checkout_timeout: None,
            max_uses: None,
        }
    }
}

/// Build a [`ConnectionPool`] from a libpq-style DSN. Connection tasks run on
/// the runtime behind `handle`.
pub fn create_pool(
    dsn: &str,
    handle: Handle,
    config: PoolConfig,
) -> Result<ConnectionPool, anyhow::Error> {
    let mut manager = ConnectionManager::from_dsn(dsn, handle, config.session, &config.tls)?;
    manager.max_uses = config.max_uses;
    let mut builder = Pool::builder(manager).max_size(config.max_size);
    if let Some(timeout) = config.checkout_timeout {
        // deadpool enforces timeouts by sleeping on a runtime (we drive `get()`
        // on our own tokio runtime, so `Runtime::Tokio1` is correct). `recycle`
        // is left unbounded: it's a cheap local `is_closed` check, not I/O.
        builder = builder.runtime(Runtime::Tokio1).timeouts(Timeouts {
            wait: Some(timeout),
            create: Some(timeout),
            recycle: None,
        });
    }
    Ok(builder.build()?)
}

#[pyclass(name = "ConnectionPool", frozen)]
pub struct PyConnectionPool {
    pool: ConnectionPool,
    /// Handle to the extension's shared tokio runtime: checkouts block on it,
    /// and each `Connection` handed out drives its calls on it.
    handle: Handle,
}

#[pymethods]
impl PyConnectionPool {
    /// Build a pool from a libpq-style DSN, capped at `max_size` connections.
    ///
    /// `runtime` is the homeserver's [`RustRuntime`]; its tokio runtime is
    /// started on first use, so this works during schema setup and in tests.
    /// Connection tasks are spawned on that runtime, and every `Connection` checked out drives
    /// its calls on it, whatever thread the call comes from.
    ///
    /// This only parses the DSN; connections are opened lazily on the first
    /// (and each subsequent) [`connect`](Self::connect) that needs a new one.
    /// Each connection gets the same per-connection session setup: the default
    /// (REPEATABLE READ) isolation level, plus `synchronous_commit`,
    /// `statement_timeout` and `idle_in_transaction_session_timeout` if
    /// configured here.
    #[new]
    #[pyo3(signature = (runtime, dsn, max_size = 10, *, synchronous_commit = true, statement_timeout_ms = None, idle_in_transaction_session_timeout_ms = None, checkout_timeout_ms = None, max_uses = None, sslmode = None, sslrootcert = None, sslcert = None, sslkey = None, sslpassword = None))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        runtime: &RustRuntime,
        dsn: &str,
        max_size: usize,
        synchronous_commit: bool,
        statement_timeout_ms: Option<i32>,
        idle_in_transaction_session_timeout_ms: Option<i32>,
        checkout_timeout_ms: Option<u64>,
        max_uses: Option<usize>,
        sslmode: Option<String>,
        sslrootcert: Option<String>,
        sslcert: Option<String>,
        sslkey: Option<String>,
        sslpassword: Option<String>,
    ) -> PyResult<Self> {
        let handle = runtime.tokio_handle()?;
        let config = PoolConfig {
            max_size,
            session: SessionConfig {
                synchronous_commit,
                statement_timeout_ms,
                idle_in_transaction_session_timeout_ms,
            },
            tls: TlsParams {
                sslmode,
                sslrootcert,
                sslcert,
                sslkey,
                sslpassword,
            },
            // A `checkout_timeout_ms` of `None` or `0` means no timeout (block
            // until a connection is available).
            checkout_timeout: checkout_timeout_ms
                .filter(|&ms| ms > 0)
                .map(Duration::from_millis),
            // `0` means no limit, like `txn_limit`.
            max_uses: max_uses.filter(|&n| n > 0),
        };
        let pool = create_pool(dsn, handle.clone(), config).map_err(|e| {
            PyRuntimeError::new_err(format!("failed to build connection pool: {e}"))
        })?;
        Ok(Self { pool, handle })
    }

    /// Check a connection out of the pool.
    ///
    /// Blocks (releasing the GIL) until a connection is available, opening a new
    /// one if the pool is below `max_size`. A failure to establish the
    /// connection surfaces through the same DBAPI2 exception hierarchy as a
    /// query error, so callers can treat it like psycopg2's `connect`.
    fn connect(&self, py: Python<'_>) -> PyResult<Connection> {
        let conn = self
            .pool
            .get()
            .block_on(py, &self.handle)
            .map_err(pool_err_to_py)?;
        Ok(Connection::new(conn, self.handle.clone()))
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

    /// The pool's occupancy, for metrics: `(max_size, size, available,
    /// waiting)`. `size` is the number of open connections, `available` how
    /// many of those are idle in the pool, and `waiting` how many checkouts
    /// are blocked waiting for one.
    fn status(&self) -> (usize, usize, usize, usize) {
        let status = self.pool.status();
        (
            status.max_size,
            status.size,
            status.available,
            status.waiting,
        )
    }
}

/// Map a `deadpool` checkout failure onto the DBAPI2 exception hierarchy.
fn pool_err_to_py(err: PoolError<tokio_postgres::Error>) -> PyErr {
    match err {
        // The backend failed to establish the connection: reuse the exact
        // mapping (and `pgcode` tagging) a query error gets.
        PoolError::Backend(e) => pg_err_to_py(&e),
        // Hit the configured checkout timeout. Report which phase so a slow
        // server (Create) is distinguishable from pool exhaustion (Wait).
        PoolError::Timeout(t) => {
            let phase = match t {
                TimeoutType::Wait => "waiting for a free connection slot",
                TimeoutType::Create => "establishing a new connection",
                TimeoutType::Recycle => "recycling a connection",
            };
            OperationalError::new_err(format!(
                "timed out checking out a database connection ({phase})"
            ))
        }
        // Pool closed, no runtime, or a post-create hook failure: all
        // connection-level problems, which Synapse treats as retryable
        // operational errors.
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

    use tokio::runtime::Runtime;

    use super::*;

    fn test_dsn() -> Option<String> {
        std::env::var("SYNAPSE_TEST_POSTGRES_DSN").ok()
    }

    /// A throwaway runtime standing in for the shared one: the pool takes the
    /// runtime to spawn on explicitly, so each test just passes this runtime's
    /// handle.
    fn test_runtime() -> Runtime {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap()
    }

    #[test]
    fn pool_acquires_runs_a_query_and_reuses_the_connection() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        let rt = test_runtime();
        let pool = create_pool(
            &dsn,
            rt.handle().clone(),
            PoolConfig {
                max_size: 2,
                ..PoolConfig::default()
            },
        )
        .unwrap();

        rt.block_on(async {
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

        let rt = test_runtime();
        let pool = create_pool(
            &dsn,
            rt.handle().clone(),
            PoolConfig {
                max_size: 2,
                ..PoolConfig::default()
            },
        )
        .unwrap();

        rt.block_on(async {
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
            idle_in_transaction_session_timeout_ms: Some(5678),
        };
        let rt = test_runtime();
        let pool = create_pool(
            &dsn,
            rt.handle().clone(),
            PoolConfig {
                max_size: 1,
                session,
                ..PoolConfig::default()
            },
        )
        .unwrap();

        rt.block_on(async {
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
            assert_eq!(show("idle_in_transaction_session_timeout").await, "5678ms");
        });
    }

    #[test]
    fn connection_is_replaced_after_max_uses() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        let rt = test_runtime();
        let pool = create_pool(
            &dsn,
            rt.handle().clone(),
            PoolConfig {
                max_size: 1,
                max_uses: Some(2),
                ..PoolConfig::default()
            },
        )
        .unwrap();

        async fn pid(pool: &ConnectionPool) -> i32 {
            let client = pool.get().await.unwrap();
            client
                .query_one("SELECT pg_backend_pid()", &[])
                .await
                .unwrap()
                .get::<_, i32>(0)
        }

        rt.block_on(async {
            // The first two checkouts share a server session; the third gets a
            // fresh one because the second checkout used the connection up.
            let first = pid(&pool).await;
            let second = pid(&pool).await;
            let third = pid(&pool).await;
            assert_eq!(first, second);
            assert_ne!(second, third);
        });
    }

    #[test]
    fn checkout_times_out_when_pool_is_exhausted() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        // A single-connection pool with a short checkout timeout.
        let rt = test_runtime();
        let pool = create_pool(
            &dsn,
            rt.handle().clone(),
            PoolConfig {
                max_size: 1,
                checkout_timeout: Some(Duration::from_millis(200)),
                ..PoolConfig::default()
            },
        )
        .unwrap();

        rt.block_on(async {
            // Hold the only connection, so a second checkout has to wait.
            let _held = pool.get().await.unwrap();

            let started = std::time::Instant::now();
            let result = pool.get().await;
            assert!(
                matches!(result, Err(PoolError::Timeout(TimeoutType::Wait))),
                "expected a wait timeout when the pool is exhausted"
            );
            // It should fail promptly (around the 200ms budget), not hang.
            assert!(
                started.elapsed() < Duration::from_secs(5),
                "checkout did not time out promptly"
            );
        });
    }
}
