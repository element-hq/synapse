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
        """Test utility only. Normal code should use
        `hs.get_clock().time_msec()`.

        The current time in milliseconds since the Unix epoch, as the Rust side
        sees it. This and `Clock.time_msec()` agree because both read the system
        clock, `clock_gettime(CLOCK_REALTIME)` on Linux.
        """

    def expire_caches(self, expiry_secs: float) -> int:
        """Evict entries not read for `expiry_secs` from every Rust cache of
        this homeserver, and return how many were evicted.

        Called from `synapse.util.caches.lrucache._expire_old_entries`, so that
        Rust and Python caches expire off one tick and one set of config
        options."""

    def evict_coldest(self, before_secs: float, max: int) -> tuple[int, float | None]:
        """Evict up to `max` entries last read before `before_secs` (seconds
        since the Unix epoch), coldest first across every Rust cache of this
        homeserver.

        Returns how many were evicted, and when the coldest entry left was last
        read, or None if the Rust caches are empty. Nothing in them gets any
        colder than that, so a caller can skip calling again until it wants to
        evict entries read after it."""

def set_virtual_time_msec(millis: int) -> None:
    """Pin the Rust clock to the given time. Only for tests, which run
    against a virtual reactor clock."""
