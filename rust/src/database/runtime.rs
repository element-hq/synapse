//! Shared tokio runtime used by the async-backed database drivers (currently
//! just the Postgres backend). The runtime is created lazily on first use and
//! lives for the lifetime of the process.

use once_cell::sync::Lazy;
use tokio::runtime::Runtime;

static RUNTIME: Lazy<Runtime> = Lazy::new(|| {
    tokio::runtime::Builder::new_multi_thread()
        // A small fixed pool is enough: these threads only drive the I/O-bound
        // connection tasks and the futures blocked on them (the actual blocking
        // wait happens on the calling Python thread, see `helpers::block_on`),
        // so we don't need one worker per connection.
        .worker_threads(2)
        .enable_all()
        .thread_name("synapse-db")
        .build()
        .expect("failed to build tokio runtime for synapse database backend")
});

pub fn runtime() -> &'static Runtime {
    &RUNTIME
}
