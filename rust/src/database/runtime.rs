//! Shared tokio runtime used by the async-backed database drivers (currently
//! just the Postgres backend). The runtime is created lazily on first use and
//! lives for the lifetime of the process.

use once_cell::sync::Lazy;
use tokio::runtime::Runtime;

static RUNTIME: Lazy<Runtime> = Lazy::new(|| {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .thread_name("synapse-db")
        .build()
        .expect("failed to build tokio runtime for synapse database backend")
});

pub fn runtime() -> &'static Runtime {
    &RUNTIME
}
