use std::convert::Infallible;

use lazy_static::lazy_static;
use pyo3::prelude::*;
use pyo3_log::ResetHandle;
use tikv_jemallocator::Jemalloc;

pub mod acl;
pub mod canonical_json;
pub mod duration;
pub mod errors;
pub mod events;
pub mod http;
pub mod http_client;
pub mod identifier;
pub mod matrix_const;
pub mod msc4388_rendezvous;
pub mod push;
pub mod rendezvous;
pub mod room_versions;
pub mod segmenter;

use libc::{c_char, c_int, c_void, size_t};

#[global_allocator]
static GLOBAL: Jemalloc = Jemalloc;

lazy_static! {
    static ref LOGGING_HANDLE: ResetHandle = pyo3_log::init();
}

/// Returns the hash of all the rust source files at the time it was compiled.
///
/// Used by python to detect if the rust library is outdated.
#[pyfunction]
fn get_rust_file_digest() -> &'static str {
    env!("SYNAPSE_RUST_DIGEST")
}

/// Returns the `rustc` version used when this native module was built.
///
/// This value is embedded at build time, so it can be exported as a prometheus metrics.
#[pyfunction]
pub fn get_rustc_version() -> &'static str {
    env!("SYNAPSE_RUSTC_VERSION")
}

/// Formats the sum of two numbers as string.
#[pyfunction]
#[pyo3(text_signature = "(a, b, /)")]
fn sum_as_string(a: usize, b: usize) -> PyResult<String> {
    Ok((a + b).to_string())
}

/// Reset the cached logging configuration of pyo3-log to pick up any changes
/// in the Python logging configuration.
///
#[pyfunction]
fn reset_logging_config() {
    LOGGING_HANDLE.reset();
}

/// The entry point for defining the Python module.
#[pymodule]
fn synapse_rust(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sum_as_string, m)?)?;
    m.add_function(wrap_pyfunction!(get_rust_file_digest, m)?)?;
    m.add_function(wrap_pyfunction!(get_rustc_version, m)?)?;
    m.add_function(wrap_pyfunction!(reset_logging_config, m)?)?;

    acl::register_module(py, m)?;
    push::register_module(py, m)?;
    events::register_module(py, m)?;
    http_client::register_module(py, m)?;
    rendezvous::register_module(py, m)?;
    msc4388_rendezvous::register_module(py, m)?;
    segmenter::register_module(py, m)?;
    room_versions::register_module(py, m)?;

    Ok(())
}

pub trait UnwrapInfallible<T> {
    fn unwrap_infallible(self) -> T;
}

impl<T> UnwrapInfallible<T> for Result<T, Infallible> {
    fn unwrap_infallible(self) -> T {
        match self {
            Ok(val) => val,
            Err(never) => match never {},
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn malloc(size: size_t) -> *mut c_void {
    tikv_jemalloc_sys::malloc(size)
}

#[no_mangle]
pub unsafe extern "C" fn calloc(number: size_t, size: size_t) -> *mut c_void {
    tikv_jemalloc_sys::calloc(number, size)
}

#[no_mangle]
pub unsafe extern "C" fn realloc(ptr: *mut c_void, size: size_t) -> *mut c_void {
    tikv_jemalloc_sys::realloc(ptr, size)
}

#[no_mangle]
pub unsafe extern "C" fn free(ptr: *mut c_void) {
    tikv_jemalloc_sys::free(ptr)
}

#[no_mangle]
pub unsafe extern "C" fn mallctl(
    name: *const c_char,
    oldp: *mut c_void,
    oldlenp: *mut size_t,
    newp: *mut c_void,
    newlen: size_t,
) -> c_int {
    tikv_jemalloc_sys::mallctl(name, oldp, oldlenp, newp, newlen)
}
