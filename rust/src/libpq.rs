//! Resolve libpq's default host for a connection string that omits `host=`.
//!
//! Synapse has always connected to Postgres through libpq (via psycopg2), so a
//! connection string without `host=` gets libpq's default: the compiled-in
//! socket directory (which distributions such as Debian patch) and environment
//! variables like `PGHOST`. A Rust Postgres client such as `tokio-postgres`
//! applies its own default (`localhost`) instead. So when a connection string
//! has no host, the Rust driver asks the real libpq what it would do and copies
//! that across. This module does the asking.
//!
//! libpq is an *optional runtime* dependency: nothing here links against it at
//! build time. The shared library is loaded with `dlopen` the first time
//! [`default_host`] is called, so Synapse builds and runs without libpq
//! installed, and only a deployment that needs the default host (Rust Postgres
//! driver, DSN without a host) has to have the `libpq5` package. If the library
//! is missing, [`default_host`] returns an error saying so.
//!
//! The handful of libpq functions we need are declared by hand below rather
//! than pulled in through a bindings crate, as those all link libpq at build
//! time.

use std::ffi::{c_char, CStr, CString};

use anyhow::{bail, Context, Error};
use libloading::{Library, Symbol};
use once_cell::sync::OnceCell;

/// Opaque `PGconn` handle; only ever used behind a pointer.
#[repr(C)]
struct PGconn {
    _private: [u8; 0],
}

/// The libpq entry points we use, resolved from the loaded library.
///
/// The symbols borrow a leaked `&'static Library`: the library is loaded once
/// per process and never unloaded (unloading libpq is not something it
/// supports well, and there is no need to).
struct LibPq {
    connect_start: Symbol<'static, unsafe extern "C" fn(*const c_char) -> *mut PGconn>,
    error_message: Symbol<'static, unsafe extern "C" fn(*const PGconn) -> *mut c_char>,
    host: Symbol<'static, unsafe extern "C" fn(*const PGconn) -> *mut c_char>,
    finish: Symbol<'static, unsafe extern "C" fn(*mut PGconn)>,
}

static LIBPQ: OnceCell<LibPq> = OnceCell::new();

/// The library names to try, in order. The versioned soname comes first: on
/// Debian-style systems the unversioned `libpq.so` symlink only exists with the
/// `-dev` package installed, whereas `libpq.so.5` is what `libpq5` ships.
#[cfg(target_os = "linux")]
const LIBRARY_NAMES: &[&str] = &["libpq.so.5", "libpq.so"];
#[cfg(target_os = "macos")]
const LIBRARY_NAMES: &[&str] = &["libpq.5.dylib", "libpq.dylib"];
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
const LIBRARY_NAMES: &[&str] = &["libpq"];

impl LibPq {
    /// Load the system libpq (once) and resolve the symbols we need.
    fn get() -> Result<&'static LibPq, Error> {
        LIBPQ.get_or_try_init(Self::load)
    }

    fn load() -> Result<LibPq, Error> {
        let mut last_err = None;
        let mut library = None;
        for name in LIBRARY_NAMES {
            // SAFETY: loading libpq runs its initialisation routines, which
            // are well-behaved for a regular C shared library.
            match unsafe { Library::new(name) } {
                Ok(lib) => {
                    library = Some(lib);
                    break;
                }
                Err(e) => last_err = Some(e),
            }
        }
        let Some(library) = library else {
            let err = last_err.expect("LIBRARY_NAMES is non-empty");
            bail!(
                "could not load libpq ({err}); install the libpq shared library \
                 (e.g. the `libpq5` package) to use this feature"
            );
        };

        // Leak the library so the symbols can be `'static`; see the struct docs.
        let library: &'static Library = Box::leak(Box::new(library));

        // SAFETY: each symbol is given the signature it has in `libpq-fe.h`.
        unsafe {
            Ok(LibPq {
                connect_start: symbol(library, b"PQconnectStart\0")?,
                error_message: symbol(library, b"PQerrorMessage\0")?,
                host: symbol(library, b"PQhost\0")?,
                finish: symbol(library, b"PQfinish\0")?,
            })
        }
    }
}

/// Resolve one symbol from the library, with a readable error.
///
/// # Safety
/// `T` must be the function's actual signature.
unsafe fn symbol<T>(library: &'static Library, name: &[u8]) -> Result<Symbol<'static, T>, Error> {
    library
        .get(name)
        .with_context(|| format!("libpq lacks symbol {}", String::from_utf8_lossy(name)))
}

/// Owns a `PGconn` and calls `PQfinish` on drop.
struct Connection {
    libpq: &'static LibPq,
    conn: *mut PGconn,
}

impl Drop for Connection {
    fn drop(&mut self) {
        // SAFETY: `self.conn` was returned by `PQconnectStart` and is finished
        // exactly once, here. `PQfinish` accepts a connection that never
        // completed its (non-blocking) connect.
        unsafe { (self.libpq.finish)(self.conn) }
    }
}

/// Ask libpq which host an otherwise-empty connection string would connect to.
///
/// `PQconnectStart` with an empty conninfo parses libpq's defaults (compiled-in
/// socket directory, `PGHOST` and friends) and then begins a *non-blocking*
/// connection attempt to the first host. We never poll that attempt: we read
/// the host libpq settled on with `PQhost` and drop the connection straight
/// away. So this never completes a connection, although it does create a
/// socket, and if `PGHOST` names a hostname (rather than a socket directory or
/// IP address) libpq resolves it synchronously first, as every libpq connect
/// does.
///
/// The outcome of that connection attempt is deliberately ignored: `PQhost`
/// reports the resolved default whether or not a server is listening there,
/// and a server that isn't listening is for the real connection to report.
///
/// If `PGHOST` lists several hosts this returns one of them, not the list.
///
/// Fails if libpq is not installed (see the module docs).
pub fn default_host() -> Result<String, Error> {
    let libpq = LibPq::get()?;

    // An empty conninfo means "use every default".
    let conninfo = CString::new("")?;

    // SAFETY: `conninfo` is a valid NUL-terminated string that outlives the
    // call; `PQconnectStart` copies what it needs.
    let conn = unsafe { (libpq.connect_start)(conninfo.as_ptr()) };
    if conn.is_null() {
        bail!("libpq PQconnectStart returned null (out of memory)");
    }
    let conn = Connection { libpq, conn };

    // SAFETY: `conn.conn` is valid; `PQhost` returns a pointer owned by the
    // connection (valid until `PQfinish`), or null.
    let host_ptr = unsafe { (libpq.host)(conn.conn) };
    if host_ptr.is_null() {
        bail!("libpq PQhost returned null");
    }

    // SAFETY: non-null, checked just above.
    let host = unsafe { cstr_lossy(host_ptr) };
    if host.is_empty() {
        // libpq only leaves the host unset when it could not even parse its
        // options, in which case the error message says why.
        //
        // SAFETY: `PQerrorMessage` returns a valid (possibly empty) C string
        // owned by the connection.
        let msg = unsafe { cstr_lossy((libpq.error_message)(conn.conn)) };
        bail!("libpq did not resolve a default host: {msg}");
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

    /// Run `print_default_host` in a child process with a controlled libpq
    /// environment and return what it printed as the default host.
    ///
    /// libpq reads its defaults from the environment, and the test process's
    /// environment is shared with every other test, so we never mutate it.
    /// `PGSERVICE` could also supply a host, so it is cleared too.
    fn default_host_with_env(env: &[(&str, &str)]) -> String {
        let mut cmd = std::process::Command::new(std::env::current_exe().unwrap());
        cmd.args(["--exact", "libpq::tests::print_default_host", "--nocapture"]);
        for var in ["PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"] {
            cmd.env_remove(var);
        }
        cmd.envs(env.iter().copied());

        let out = cmd.output().expect("re-run test binary");
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let stdout = String::from_utf8_lossy(&out.stdout);
        let line = stdout
            .lines()
            .find_map(|l| l.strip_prefix("DEFAULT_HOST="))
            .unwrap_or_else(|| panic!("unexpected output: {stdout}"));
        line.to_owned()
    }

    /// Helper for the tests below; prints the default host for the parent test
    /// to read. Trivially passes when run on its own.
    #[test]
    fn print_default_host() {
        println!("DEFAULT_HOST={}", default_host().unwrap());
    }

    #[test]
    fn resolves_the_compiled_in_default() {
        // Needs libpq installed, which the CI runners and dev images have.
        //
        // With nothing in the environment libpq falls back to its compiled-in
        // socket directory, which on Unix is always an absolute path (and is
        // exactly the distro-specific value this module exists to recover). No
        // server is listening there in the test environment, which must not
        // matter.
        let host = default_host_with_env(&[]);
        assert!(!host.is_empty());
        #[cfg(unix)]
        assert!(
            host.starts_with('/'),
            "expected a socket directory, got {host:?}"
        );
    }

    #[test]
    fn honours_pghost() {
        // A socket directory rather than a hostname, so libpq does no DNS.
        let host = default_host_with_env(&[("PGHOST", "/nonexistent/synapse-test")]);
        assert_eq!(host, "/nonexistent/synapse-test");
    }
}
