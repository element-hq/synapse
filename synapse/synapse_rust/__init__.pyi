from synapse.types import ISynapseReactor

def sum_as_string(a: int, b: int) -> str: ...
def get_rust_file_digest() -> str: ...
def reset_logging_config() -> None: ...
def get_rustc_version() -> str: ...

class RustRuntime:
    """The per-homeserver state for the Rust side of Synapse.

    Holds the tokio thread pool (started lazily on first use, shut down by a
    reactor shutdown trigger) and a handle to the reactor. Rust classes that
    need either take this as a constructor argument; get it from
    `hs.get_rust_runtime()`.
    """

    def __init__(
        self,
        reactor: ISynapseReactor,
        worker_threads: int = 4,
    ) -> None: ...
