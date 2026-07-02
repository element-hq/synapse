//! A `deadpool`-managed pool of `tokio_postgres` connections for the native
//! Rust Postgres backend.
//!
//! We use the generic `deadpool::managed` pool with our own [`ConnectionManager`]
//! rather than the `deadpool-postgres` crate, so that creating a connection
//! reuses the same DSN handling (libpq's default host, see
//! [`super::fixup_default_host`]) and connection-task spawning as
//! [`super::connect`].
//!
//! The pooled item is a plain [`tokio_postgres::Client`]. Rust-native code takes
//! one from the pool and uses it with the standard `tokio_postgres` async query
//! functions; the Python-facing `Connection`/`Cursor` shim borrows from the
//! *same* pool, so both share a single set of connections rather than running
//! two pools that could exhaust the server's connection limit between them.

use deadpool::managed::{Manager, Metrics, Object, Pool, RecycleError, RecycleResult};
use log::warn;
use tokio_postgres::{Client, Config, NoTls};

use crate::database::postgres::fixup_default_host;
use crate::database::runtime::runtime;

/// Creates and recycles [`tokio_postgres`] connections for a [`ConnectionPool`].
pub struct ConnectionManager {
    /// The resolved connection config, parsed once from the DSN (with libpq's
    /// default host filled in if the DSN omitted one).
    config: Config,
}

impl ConnectionManager {
    /// Build a manager from a libpq-style DSN.
    pub fn from_dsn(dsn: &str) -> Result<Self, anyhow::Error> {
        Ok(Self {
            config: fixup_default_host(dsn)?,
        })
    }
}

impl Manager for ConnectionManager {
    type Type = Client;
    type Error = tokio_postgres::Error;

    async fn create(&self) -> Result<Client, Self::Error> {
        // As in `super::connect`: establish the connection, then drive its
        // long-lived connection task (which pumps the socket) on the shared
        // runtime. The task ends on its own when the `Client` is dropped, i.e.
        // when the pool discards this connection.
        let (client, connection) = self.config.connect(NoTls).await?;

        runtime().spawn(async move {
            if let Err(e) = connection.await {
                warn!("postgres connection error: {e}");
            }
        });

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
/// connections.
pub fn create_pool(dsn: &str, max_size: usize) -> Result<ConnectionPool, anyhow::Error> {
    let manager = ConnectionManager::from_dsn(dsn)?;
    Ok(Pool::builder(manager).max_size(max_size).build()?)
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
}
