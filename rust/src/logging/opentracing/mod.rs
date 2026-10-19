/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 */

//! Rust side of Synapse's opentracing (Jaeger) support.
//!
//! Python creates spans with `jaeger_client` and keeps the active one on the
//! current logcontext's `scope`. Rust work spawned from Python needs that span
//! as its parent, so [`LogContextHandle::capture`] reads the span's context
//! once, on the calling thread, and the handle carries it into the tokio task.
//!
//! Tracing is off unless a homeserver calls `RustRuntime.enable_tracing`.
//! While it is off, a capture costs one relaxed atomic load.

pub mod layer;
pub mod reporter;
pub mod thrift;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use pyo3::{intern, IntoPyObjectExt};
use reporter::Reporter;
use thrift::{FinishedSpan, Process, Tag};

use crate::logging::context::{LogContextHandle, LoggingContext};
use crate::runtime::RustRuntime;

/// Jaeger's flag bit for a sampled trace.
pub const FLAG_SAMPLED: u8 = 1;
/// Jaeger's flag bit for a trace forced on by a debug ID.
pub const FLAG_DEBUG: u8 = 2;

/// Set by the first homeserver that enables tracing. Never cleared: the
/// tracer, like Python's global opentracing tracer, is per process.
static TRACING_ENABLED: AtomicBool = AtomicBool::new(false);

/// Whether any homeserver in this process has enabled tracing.
pub fn tracing_enabled() -> bool {
    TRACING_ENABLED.load(Ordering::Relaxed)
}

pub(crate) fn set_tracing_enabled(enabled: bool) {
    TRACING_ENABLED.store(enabled, Ordering::Relaxed);
}

/// The identity of a span, as Jaeger propagates it between processes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SpanContext {
    /// 64 bits unless the tracer is configured for 128-bit trace IDs.
    pub trace_id: u128,
    pub span_id: u64,
    /// Jaeger's flag bits, [`FLAG_SAMPLED`] and [`FLAG_DEBUG`].
    pub flags: u8,
    /// Baggage items. `None` when there are none, which is nearly always.
    pub baggage: Option<Arc<Vec<(String, String)>>>,
}

impl SpanContext {
    /// Whether spans in this trace are recorded and reported.
    pub fn is_sampled(&self) -> bool {
        self.flags & FLAG_SAMPLED != 0
    }
}

/// Read the context of the span active on `context`, the span Python would use
/// as the parent of a new one.
///
/// Returns `None` when no span is active, and when the span does not look like
/// a Jaeger span (the no-op tracer's span contexts have no trace ID).
pub(crate) fn read_active_span(
    py: Python<'_>,
    context: &Py<LoggingContext>,
) -> Option<SpanContext> {
    // Take the scope out before calling into Python: the getattrs below can
    // run arbitrary code, which must not find the context borrowed. A context
    // already borrowed mutably (in its `__init__`, say) has no span yet.
    let scope = context.bind(py).try_borrow().ok()?.scope(py)?;
    match span_context_from_scope(scope.bind(py)) {
        Ok(span) => span,
        Err(err) => {
            log::debug!("Could not read the active span's context: {err}");
            None
        }
    }
}

fn span_context_from_scope(scope: &Bound<'_, PyAny>) -> PyResult<Option<SpanContext>> {
    let py = scope.py();
    let context = scope
        .getattr(intern!(py, "span"))?
        .getattr(intern!(py, "context"))?;

    let Ok(trace_id) = context.getattr(intern!(py, "trace_id")) else {
        return Ok(None);
    };
    if trace_id.is_none() {
        return Ok(None);
    }

    let trace_id: u128 = trace_id.extract()?;
    let span_id: u64 = context.getattr(intern!(py, "span_id"))?.extract()?;
    let flags: u8 = context.getattr(intern!(py, "flags"))?.extract()?;

    let baggage = context.getattr(intern!(py, "baggage"))?;
    let baggage = match baggage.cast::<PyDict>() {
        Ok(dict) if !dict.is_empty() => Some(Arc::new(
            dict.iter()
                .map(|(key, value)| Ok((key.str()?.to_string(), value.str()?.to_string())))
                .collect::<PyResult<Vec<_>>>()?,
        )),
        _ => None,
    };

    Ok(Some(SpanContext {
        trace_id,
        span_id,
        flags,
        baggage,
    }))
}

/// The process-wide settings for reporting spans to the Jaeger agent, copied
/// from the Python tracer so that Rust spans show up under the same service.
#[derive(Clone, Debug, PartialEq)]
pub struct TracerConfig {
    pub service_name: String,
    /// The Jaeger process tags: the configured ones plus jaeger-client's own
    /// (`hostname`, `ip`, `jaeger.version`).
    pub process_tags: Vec<(String, String)>,
    pub reporting_host: String,
    pub reporting_port: u16,
    pub sampler: SamplerConfig,
}

/// A homeserver's tracing: its settings and the reporter sending its spans.
pub struct Tracer {
    config: TracerConfig,
    reporter: Arc<Reporter>,
}

impl Tracer {
    /// Start reporting to the agent `config` names.
    pub fn start(config: TracerConfig) -> std::io::Result<Tracer> {
        let process = Process {
            service_name: config.service_name.clone(),
            tags: config
                .process_tags
                .iter()
                .map(|(key, value)| Tag::string(key.clone(), value.clone()))
                .collect(),
        };
        let reporter = Arc::new(Reporter::start(
            process,
            &config.reporting_host,
            config.reporting_port,
        )?);

        let sink: Arc<dyn layer::SpanSink> = reporter.clone();
        let attached = layer::attach_global(layer::LayerTarget::new(&sink, config.sampler.clone()));
        if !attached {
            log::warn!(
                "Another homeserver in this process already reports spans from Rust code; \
                 it reports this homeserver's too"
            );
        }

        Ok(Tracer { config, reporter })
    }

    pub fn config(&self) -> &TracerConfig {
        &self.config
    }

    /// Queue a finished span for the agent. Never blocks.
    pub fn report(&self, span: FinishedSpan) {
        self.reporter.report(span);
    }

    pub fn reporter(&self) -> &Reporter {
        &self.reporter
    }

    /// Stop recording spans for this homeserver, then send the queued ones
    /// and stop the reporter, waiting up to `timeout`.
    pub fn shutdown(&self, timeout: Duration) {
        let sink: Arc<dyn layer::SpanSink> = self.reporter.clone();
        layer::detach_global(&sink);
        self.reporter.shutdown(timeout);
    }
}

/// How to decide whether to sample a trace that starts in Rust. Spans with a
/// parent never consult this: they copy the parent's flags.
#[derive(Clone, Debug, PartialEq)]
pub enum SamplerConfig {
    Const(bool),
    /// Sample with this probability, between 0 and 1.
    Probabilistic(f64),
    /// Rate limiting and remote sampling are not implemented in Rust, so a
    /// trace never starts in Rust under them.
    Never,
}

impl SamplerConfig {
    /// Build from the `(type, param)` pair that `synapse.logging.opentracing`
    /// derives from jaeger-client's sampler.
    pub fn from_type_and_param(sampler_type: &str, param: f64) -> Self {
        match sampler_type {
            "const" => SamplerConfig::Const(param != 0.0),
            "probabilistic" => SamplerConfig::Probabilistic(param.clamp(0.0, 1.0)),
            _ => SamplerConfig::Never,
        }
    }
}

/// The span context a handle captured here would carry, as
/// `(trace_id, span_id, flags, baggage)`, or `None`. For tests.
#[pyfunction]
fn captured_span_for_test(py: Python<'_>) -> PyResult<Option<Bound<'_, PyTuple>>> {
    span_to_py(py, LogContextHandle::capture(py).span())
}

/// The parent span a task run by `RustRuntime::block_on_scoped` sees, in the
/// same form as [`captured_span_for_test`]. For tests.
#[pyfunction]
fn span_in_blocking_task_for_test<'py>(
    py: Python<'py>,
    runtime: &Bound<'py, RustRuntime>,
) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let span = runtime.get().block_on_scoped(py, async {
        LogContextHandle::task_current().and_then(|handle| handle.span().cloned())
    })?;
    span_to_py(py, span.as_ref())
}

fn span_to_py<'py>(
    py: Python<'py>,
    span: Option<&SpanContext>,
) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let Some(span) = span else {
        return Ok(None);
    };

    let baggage = PyDict::new(py);
    for (key, value) in span.baggage.iter().flat_map(|baggage| baggage.iter()) {
        baggage.set_item(key, value)?;
    }

    Ok(Some(PyTuple::new(
        py,
        [
            span.trace_id.into_bound_py_any(py)?,
            span.span_id.into_bound_py_any(py)?,
            span.flags.into_bound_py_any(py)?,
            baggage.into_any(),
        ],
    )?))
}

/// Run a `tracing` span named `operation`, with a `db.queued = 3` tag and a
/// `commit` log, in a task on the runtime, blocking until it finishes. For
/// tests of the Python-to-Rust parent handoff.
#[pyfunction]
fn span_in_task_for_test(
    py: Python<'_>,
    runtime: &Bound<'_, RustRuntime>,
    operation: String,
) -> PyResult<()> {
    use tracing::Instrument;

    runtime.get().block_on_scoped(py, async move {
        let span = tracing::info_span!("test", otel.name = operation, db.queued = 3_i64);
        async {
            tokio::task::yield_now().await;
            tracing::info!(message = "commit");
        }
        .instrument(span)
        .await
    })
}

/// Run a `tracing` span named `operation` on the calling thread under
/// [`layer::with_py_parent`]. For tests.
#[pyfunction]
fn span_on_thread_for_test(py: Python<'_>, operation: String) {
    layer::with_py_parent(py, || {
        let _span = tracing::info_span!("test", otel.name = operation).entered();
    });
}

/// Turn span capture on or off for this process. For tests, which have no
/// homeserver with tracing enabled.
#[pyfunction]
fn set_tracing_enabled_for_test(enabled: bool) {
    set_tracing_enabled(enabled);
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "opentracing")?;
    child_module.add_function(wrap_pyfunction!(captured_span_for_test, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(
        span_in_blocking_task_for_test,
        &child_module
    )?)?;
    child_module.add_function(wrap_pyfunction!(span_in_task_for_test, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(span_on_thread_for_test, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(
        set_tracing_enabled_for_test,
        &child_module
    )?)?;

    m.add_submodule(&child_module)?;

    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.opentracing", child_module)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sampler_from_type_and_param() {
        assert_eq!(
            SamplerConfig::from_type_and_param("const", 1.0),
            SamplerConfig::Const(true)
        );
        assert_eq!(
            SamplerConfig::from_type_and_param("const", 0.0),
            SamplerConfig::Const(false)
        );
        assert_eq!(
            SamplerConfig::from_type_and_param("probabilistic", 2.0),
            SamplerConfig::Probabilistic(1.0)
        );
        assert_eq!(
            SamplerConfig::from_type_and_param("ratelimiting", 5.0),
            SamplerConfig::Never
        );
    }
}
