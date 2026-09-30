from synapse.server import HomeServer

class RustRuntime:
    """The per-homeserver state for the Rust side of Synapse.

    Holds the tokio thread pool (started lazily on first use, shut down with the
    Homeserver). Rust classes that need them take this as a constructor
    argument. Get it from `hs.get_rust_runtime()`.
    """

    def __init__(
        self,
        hs: HomeServer,
        worker_threads: int = 4,
    ) -> None: ...
    def time_msec(self) -> int:
        """The current time in milliseconds since the Unix epoch, as the Rust
        side sees it. Nothing keeps this in sync with `Clock.time_msec()`. They
        agree because both read the system clock, `clock_gettime(CLOCK_REALTIME)`
        on Linux. See `rust/src/clock.rs`."""

    def set_virtual_time_msec(self, millis: int) -> None:
        """Pin the Rust clock to the given time. Only for tests, which run
        against a virtual reactor clock."""
