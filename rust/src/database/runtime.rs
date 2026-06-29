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
        //
        // This is also why a fixed pool of two can't deadlock: every blocking
        // wait (`Runtime::block_on`) runs on the *calling* Python thread, never
        // on a worker, so the workers are always free to drive the connection
        // tasks that those waits are waiting on. Moving a blocking wait onto a
        // worker (e.g. via `Handle::block_on` from inside the runtime) would
        // break that invariant.
        .worker_threads(2)
        .enable_all()
        .thread_name("synapse-db")
        .build()
        .expect("failed to build tokio runtime for synapse database backend")
});

pub fn runtime() -> &'static Runtime {
    &RUNTIME
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_runs_futures_and_is_shared() {
        // The runtime drives a future to completion...
        assert_eq!(runtime().block_on(async { 1 + 1 }), 2);

        // ...and is the same instance on every call (it's a process-global
        // `Lazy`), so a second `block_on` reuses it rather than spinning up a
        // fresh runtime.
        let first = runtime() as *const Runtime;
        let second = runtime() as *const Runtime;
        assert_eq!(first, second);
        assert_eq!(runtime().block_on(async { "ok" }), "ok");
    }
}
