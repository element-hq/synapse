//! Resolve libpq's default host for a DSN that omits `host=`.
//!
//! [`tokio_postgres`] applies its *own* default host (`localhost`) when a DSN
//! omits `host=`, whereas Synapse has always used libpq's default — the
//! compiled-in default socket directory (which distributions such as Debian
//! patch) and environment variables like `PGHOST`. So when a DSN has no host we
//! ask the real libpq what it would do and copy that across.
//!
//! We call libpq through [`pq_sys`], which ships pre-generated bindings and
//! links the system libpq itself. Unlike a bindgen-based binding crate that
//! generates its FFI at build time, this needs no libclang to build — only a C
//! linker and a discoverable libpq (`pg_config` / `pkg-config`).

use std::ffi::{c_char, CStr, CString};

use anyhow::{bail, Error};
use pq_sys::{ConnStatusType, PGconn, PQconnectStart, PQerrorMessage, PQfinish, PQhost, PQstatus};

/// Owns a `PGconn` and calls `PQfinish` on drop.
struct Connection(*mut PGconn);

impl Drop for Connection {
    fn drop(&mut self) {
        // SAFETY: `self.0` was returned by `PQconnectStart` and is finished
        // exactly once, here. `PQfinish` accepts a connection that never
        // completed its (non-blocking) connect.
        unsafe { PQfinish(self.0) }
    }
}

/// Ask libpq which host an otherwise-empty connection string would connect to.
///
/// Resolves libpq's defaults *without opening a socket*: `PQconnectStart`
/// begins a non-blocking connection but we never poll it (no `PQconnectPoll`),
/// then drop it immediately — so this only parses options and applies defaults.
pub fn default_host() -> Result<String, Error> {
    // An empty conninfo means "use every default".
    let conninfo = CString::new("")?;

    // SAFETY: `conninfo` is a valid NUL-terminated string that outlives the
    // call; `PQconnectStart` copies what it needs.
    let conn = unsafe { PQconnectStart(conninfo.as_ptr()) };
    if conn.is_null() {
        bail!("libpq PQconnectStart returned null (out of memory)");
    }
    let conn = Connection(conn);

    // SAFETY: `conn.0` is non-null and valid for the rest of this scope.
    if unsafe { PQstatus(conn.0) } == ConnStatusType::CONNECTION_BAD {
        // SAFETY: `PQerrorMessage` returns a valid (possibly empty) C string
        // owned by the connection.
        let msg = unsafe { cstr_lossy(PQerrorMessage(conn.0)) };
        bail!("libpq could not parse default connection options: {msg}");
    }

    // SAFETY: `conn.0` is valid; `PQhost` returns a pointer owned by the
    // connection (valid until `PQfinish`), or null.
    let host_ptr = unsafe { PQhost(conn.0) };
    if host_ptr.is_null() {
        bail!("libpq PQhost returned null");
    }

    // SAFETY: non-null, checked just above.
    let host = unsafe { cstr_lossy(host_ptr) };
    if host.is_empty() {
        bail!("libpq reported an empty default host");
    }

    Ok(host)
}

/// Copy a libpq-owned C string into an owned `String` (lossy on non-UTF-8).
///
/// # Safety
/// `ptr` must be non-null and point to a valid NUL-terminated string.
unsafe fn cstr_lossy(ptr: *const c_char) -> String {
    CStr::from_ptr(ptr).to_string_lossy().into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_a_nonempty_default_host() {
        // We can't assert the exact value (it's host/distro/env dependent),
        // but libpq must always resolve *some* non-empty default host — the
        // whole reason we defer to it rather than hard-coding one.
        let host = default_host().expect("libpq should resolve a default host");
        assert!(!host.is_empty());
    }
}
