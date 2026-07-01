//! The DBAPI2 exception hierarchy for the Postgres backend, and the mapping
//! from a [`tokio_postgres`] error onto it.
//!
//! Synapse's transaction driver (`synapse.storage.database.new_transaction`)
//! and the Postgres engine branch on the *type* of the exception a database
//! call raises, and on its `pgcode` (the 5-character SQLSTATE). To be a drop-in
//! for psycopg2 we therefore need to raise exceptions of the right class and
//! carry a `pgcode`.
//!
//! Rather than reproduce psycopg2's full PEP-249 hierarchy (ten classes) and
//! its complete SQLSTATE→class table, we expose only the distinctions Synapse
//! actually acts on:
//!
//! * [`Error`] — the base every database error derives from. Synapse catches it
//!   when a rollback itself fails.
//! * [`DatabaseError`] — a server-side error. Synapse catches this and calls
//!   `is_deadlock`, which reads `pgcode` to spot serialization/deadlock failures
//!   (`40001`/`40P01`) and retry them.
//! * [`OperationalError`] — a transient/connection-level failure ("the database
//!   disappeared mid-transaction"). Synapse catches this and retries.
//! * [`IntegrityError`] — a constraint violation. Synapse catches this to retry
//!   upserts and to handle insert races.
//! * [`ProgrammingError`] — a SQL-level mistake (syntax error, duplicate table
//!   or index, …; SQLSTATE class `42`). Caught by the search store's GIN-index
//!   migration to ignore "already exists".
//!
//! Every other Postgres error (data errors, …) surfaces as a plain
//! [`DatabaseError`]. Nothing in Synapse catches the remaining psycopg2 classes
//! (`DataError`, `InternalError`, …), so collapsing them is invisible at
//! runtime. If full [`DBAPI2Module`] conformance is needed later (when a Rust
//! engine is wired up) the remaining PEP-249 names can be added as aliases of
//! [`DatabaseError`].
//!
//! [`DBAPI2Module`]: (see `synapse/storage/types.py`)

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::{create_exception, types::PyModule};

create_exception!(
    postgres,
    Error,
    PyException,
    "Base class for every error raised by the Rust Postgres backend (PEP-249 `Error`)."
);
create_exception!(
    postgres,
    DatabaseError,
    Error,
    "A server-side database error. Carries the SQLSTATE as `pgcode`."
);
create_exception!(
    postgres,
    OperationalError,
    DatabaseError,
    "A transient/connection-level failure that is worth retrying."
);
create_exception!(
    postgres,
    IntegrityError,
    DatabaseError,
    "A constraint violation (e.g. a unique or foreign-key violation)."
);
create_exception!(
    postgres,
    ProgrammingError,
    DatabaseError,
    "A SQL-level mistake (syntax error, duplicate table/index, undefined column, ...)."
);

/// Build the Python exception for a Postgres failure, tagging it with `pgcode`.
///
/// `code` is the SQLSTATE (`None` for an error that never got a server
/// response). When present, the class is chosen from the SQLSTATE *class* (its
/// first two characters):
///
/// * `23` (integrity constraint violation) → [`IntegrityError`]
/// * `08`/`53`/`57`/`58` (connection, resource, operator-intervention,
///   system errors) → [`OperationalError`]
/// * `42` (syntax error or access rule violation, e.g. `42P07` duplicate
///   table/index) → [`ProgrammingError`], matching psycopg2's mapping
/// * everything else → [`DatabaseError`]
///
/// Note deadlock/serialization failures (`40001`/`40P01`) deliberately fall
/// into the [`DatabaseError`] bucket, not [`OperationalError`]: Synapse retries
/// them via `is_deadlock`, which only needs a `DatabaseError` with the right
/// `pgcode`.
///
/// A codeless error is one that never reached, or never heard back from, the
/// server. We can't inspect its kind, but `closed` (from
/// [`tokio_postgres::Error::is_closed`]) tells us whether it was a lost
/// connection: if so it's [`OperationalError`] and worth retrying; otherwise
/// it's a client-side problem (a bad parameter, a failed connect) that
/// shouldn't be retried, so it becomes a plain [`DatabaseError`].
fn new_err_for(py: Python<'_>, code: Option<&str>, closed: bool, msg: &str) -> PyErr {
    let err = match code.map(sqlstate_class) {
        Some("23") => IntegrityError::new_err(msg.to_string()),
        Some("08" | "53" | "57" | "58") => OperationalError::new_err(msg.to_string()),
        Some("42") => ProgrammingError::new_err(msg.to_string()),
        // A server error we don't single out, e.g. a deadlock (`40*`) or a
        // data error (`22*`), …
        Some(_) => DatabaseError::new_err(msg.to_string()),
        // No SQLSTATE: a lost connection is operational (retry); any other
        // codeless error is client-side and propagates as a `DatabaseError`.
        None if closed => OperationalError::new_err(msg.to_string()),
        None => DatabaseError::new_err(msg.to_string()),
    };

    // Attach `pgcode` (the SQLSTATE string, or Python `None`) so engine code
    // such as `is_deadlock` can read `error.pgcode` exactly as it does for
    // psycopg2. Setting an attribute on a fresh exception instance does not
    // fail in practice, so a failure here is not worth propagating.
    let _ = err.value(py).setattr("pgcode", code);

    err
}

/// The SQLSTATE *class*: the first two characters of a 5-character code.
fn sqlstate_class(code: &str) -> &str {
    &code[..2.min(code.len())]
}

/// Map a [`tokio_postgres`] error into the appropriate Python exception.
///
/// Takes the error by reference so it can be called both from the `block_on`
/// helpers (which own the error) and from the cursor's row-stream draining
/// (which only has a borrow).
///
/// For a server-reported error the message includes the SQLSTATE, the server's
/// message and, when present, its DETAIL and HINT — `tokio_postgres`'s own
/// `Display` is just "db error", which buries the reason (psycopg2 surfaces
/// the full server message).
pub(crate) fn pg_err_to_py(e: &tokio_postgres::Error) -> PyErr {
    let code = e.code().map(|c| c.code());
    let msg = match e.as_db_error() {
        Some(db_err) => {
            let mut msg = format!(
                "postgres error {}: {}",
                db_err.code().code(),
                db_err.message()
            );
            if let Some(detail) = db_err.detail() {
                msg.push_str(&format!(" DETAIL: {detail}"));
            }
            if let Some(hint) = db_err.hint() {
                msg.push_str(&format!(" HINT: {hint}"));
            }
            msg
        }
        None => format!("postgres error: {e}"),
    };
    Python::attach(|py| new_err_for(py, code, e.is_closed(), &msg))
}

/// Add the exception classes to the `postgres` submodule so the module conforms
/// to (the load-bearing part of) Synapse's `DBAPI2Module` protocol.
pub(crate) fn register_exceptions(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("Error", py.get_type::<Error>())?;
    m.add("DatabaseError", py.get_type::<DatabaseError>())?;
    m.add("OperationalError", py.get_type::<OperationalError>())?;
    m.add("IntegrityError", py.get_type::<IntegrityError>())?;
    m.add("ProgrammingError", py.get_type::<ProgrammingError>())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    //! These tests don't touch Postgres: `new_err_for` takes the SQLSTATE as a
    //! plain string, so the whole classification and `pgcode` tagging can be
    //! exercised without a live database (a `tokio_postgres::Error` can't be
    //! constructed by hand anyway).

    use pyo3::PyTypeInfo;

    use super::*;

    /// Assert `code` maps to exception type `T`, is a `DatabaseError` and an
    /// `Error`, and carries the expected `pgcode`.
    fn assert_maps<T: PyTypeInfo>(code: &str) {
        Python::attach(|py| {
            let err = new_err_for(py, Some(code), false, "boom");
            let value = err.value(py);
            assert!(
                value.is_instance_of::<T>(),
                "{code} did not map to the expected type"
            );
            assert!(value.is_instance_of::<DatabaseError>());
            assert!(value.is_instance_of::<Error>());

            let pgcode: String = value.getattr("pgcode").unwrap().extract().unwrap();
            assert_eq!(pgcode, code);
        });
    }

    #[test]
    fn integrity_class_maps_to_integrity_error() {
        Python::initialize();
        // Unique violation, foreign-key violation, not-null violation.
        assert_maps::<IntegrityError>("23505");
        assert_maps::<IntegrityError>("23503");
    }

    #[test]
    fn connection_and_resource_classes_map_to_operational_error() {
        Python::initialize();
        assert_maps::<OperationalError>("08006"); // connection failure
        assert_maps::<OperationalError>("53100"); // disk full
        assert_maps::<OperationalError>("57014"); // query canceled
        assert_maps::<OperationalError>("58000"); // system error
    }

    #[test]
    fn programming_class_maps_to_programming_error() {
        Python::initialize();
        // Syntax error, duplicate table/index (what the GIN-index migration
        // catches to ignore "already exists").
        assert_maps::<ProgrammingError>("42601");
        assert_maps::<ProgrammingError>("42P07");
    }

    #[test]
    fn other_server_errors_map_to_plain_database_error() {
        Python::initialize();
        // A data error is a `DatabaseError` but none of the specific classes.
        Python::attach(|py| {
            let value = new_err_for(py, Some("22012"), false, "boom");
            let value = value.value(py);
            assert!(value.is_instance_of::<DatabaseError>());
            assert!(!value.is_instance_of::<OperationalError>());
            assert!(!value.is_instance_of::<IntegrityError>());
            assert!(!value.is_instance_of::<ProgrammingError>());
        });
    }

    #[test]
    fn deadlock_and_serialization_are_retryable_database_errors() {
        Python::initialize();
        // The behaviour Synapse's `is_deadlock` relies on: a `DatabaseError`
        // (so the `isinstance` check passes) carrying the right `pgcode`.
        for code in ["40001", "40P01"] {
            Python::attach(|py| {
                let err = new_err_for(py, Some(code), false, "boom");
                let value = err.value(py);
                assert!(value.is_instance_of::<DatabaseError>());
                let pgcode: String = value.getattr("pgcode").unwrap().extract().unwrap();
                assert_eq!(pgcode, code);
            });
        }
    }

    #[test]
    fn closed_connection_error_is_operational() {
        Python::initialize();
        Python::attach(|py| {
            // A lost connection (codeless, `is_closed()`) is operational, so
            // Synapse retries it.
            let err = new_err_for(py, None, true, "connection closed");
            let value = err.value(py);
            assert!(value.is_instance_of::<OperationalError>());
            assert!(value.is_instance_of::<DatabaseError>());
            // `pgcode` is present but `None`, so `error.pgcode in (...)` is a
            // safe membership test rather than an `AttributeError`.
            assert!(value.getattr("pgcode").unwrap().is_none());
        });
    }

    #[test]
    fn other_codeless_error_is_a_plain_database_error() {
        Python::initialize();
        Python::attach(|py| {
            // A client-side error that isn't a lost connection (a bad
            // parameter, a failed connect) is *not* retryable, so it is a plain
            // `DatabaseError`, not `OperationalError`.
            let err = new_err_for(py, None, false, "error serializing parameter");
            let value = err.value(py);
            assert!(value.is_instance_of::<DatabaseError>());
            assert!(!value.is_instance_of::<OperationalError>());
            assert!(value.getattr("pgcode").unwrap().is_none());
        });
    }
}
