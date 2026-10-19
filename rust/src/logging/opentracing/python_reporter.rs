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

//! Report Python's jaeger-client spans through the Rust reporter, so that one
//! UDP emitter sends both Python's and Rust's spans.
//!
//! `PythonSpanReporter` is the reporter `init_tracer` gives the Python
//! tracer. `report_span` converts a finished jaeger-client span into a
//! [`FinishedSpan`] with the GIL held, which is a handful of attribute
//! reads, and queues it on the homeserver's reporter. The encoding and the
//! send happen on the reporter thread, as for Rust's own spans.
//!
//! The conversion follows jaeger-client's `thrift.make_jaeger_batch`, so the
//! datagrams are what jaeger-client's own reporter sends.

use std::borrow::Cow;
use std::time::{Duration, UNIX_EPOCH};

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::{intern, IntoPyObjectExt};

use super::thrift::{FinishedSpan, Log, RefType, SpanRef, Tag, TagValue};
use crate::runtime::RustRuntime;

/// The reporter for Python's spans. See the module docs.
#[pyclass(frozen)]
pub struct PythonSpanReporter {
    runtime: RustRuntime,
}

#[pymethods]
impl PythonSpanReporter {
    #[new]
    fn py_new(runtime: &Bound<'_, RustRuntime>) -> Self {
        PythonSpanReporter {
            runtime: runtime.get().clone(),
        }
    }

    /// Queue a finished jaeger-client span. Never blocks: drops the span if
    /// the queue is full, or if tracing is not enabled for the homeserver.
    fn report_span(&self, span: &Bound<'_, PyAny>) -> PyResult<()> {
        let Some(tracer) = self.runtime.tracer() else {
            return Ok(());
        };
        tracer.report(finished_span_from_py(span)?);
        Ok(())
    }
}

/// jaeger-client's `timestamp_micros`, `int(ts * 1000000)`, for times since
/// the epoch: `as` truncates towards zero, as `int()` does. Negative values
/// (a clock that stepped back) become 0.
fn micros(seconds: f64) -> u64 {
    (seconds * 1_000_000.0) as u64
}

/// The low `bits` of the Python int `value`. jaeger-client masks the trace ID
/// to 128 bits; IDs from a remote context can be any length of hex.
fn masked<T: for<'a> FromPyObject<'a, 'a>>(value: &Bound<'_, PyAny>, bits: u32) -> PyResult<T> {
    let mask = (1u128 << (bits - 1)) | ((1u128 << (bits - 1)) - 1);
    value.bitand(mask)?.extract().map_err(Into::into)
}

/// Convert a finished jaeger-client `Span`.
pub(crate) fn finished_span_from_py(span: &Bound<'_, PyAny>) -> PyResult<FinishedSpan> {
    let py = span.py();
    let context = span.getattr(intern!(py, "context"))?;
    let trace_id: u128 = masked(&context.getattr(intern!(py, "trace_id"))?, 128)?;
    let span_id: u64 = masked(&context.getattr(intern!(py, "span_id"))?, 64)?;
    let parent_id = context.getattr(intern!(py, "parent_id"))?;
    let parent_id: u64 = if parent_id.is_none() {
        0
    } else {
        masked(&parent_id, 64)?
    };
    // Only the sampled and debug bits mean anything.
    let flags: u8 = masked(&context.getattr(intern!(py, "flags"))?, 8)?;
    let operation: String = span.getattr(intern!(py, "operation_name"))?.extract()?;

    let start_time: f64 = span.getattr(intern!(py, "start_time"))?.extract()?;
    let end_time: Option<f64> = span.getattr(intern!(py, "end_time"))?.extract()?;
    let end_time = end_time.unwrap_or(start_time);
    let start_micros = micros(start_time);
    let duration_micros = micros(end_time - start_time);

    let tags = tags_from_py(&span.getattr(intern!(py, "tags"))?)?;
    let logs = span
        .getattr(intern!(py, "logs"))?
        .cast_into::<PyList>()?
        .iter()
        .map(|log| {
            let timestamp: u64 = log.getattr(intern!(py, "timestamp"))?.extract()?;
            Ok(Log {
                timestamp: UNIX_EPOCH + Duration::from_micros(timestamp),
                fields: tags_from_py(&log.getattr(intern!(py, "fields"))?)?,
            })
        })
        .collect::<PyResult<Vec<_>>>()?;

    let references = span.getattr(intern!(py, "references"))?;
    let references = if references.is_none() {
        Vec::new()
    } else {
        references
            .try_iter()?
            .map(|reference| {
                let reference = reference?;
                let ref_type: String = reference.getattr(intern!(py, "type"))?.extract()?;
                let referenced = reference.getattr(intern!(py, "referenced_context"))?;
                Ok(SpanRef {
                    ref_type: if ref_type == "follows_from" {
                        RefType::FollowsFrom
                    } else {
                        RefType::ChildOf
                    },
                    trace_id: masked(&referenced.getattr(intern!(py, "trace_id"))?, 128)?,
                    span_id: masked(&referenced.getattr(intern!(py, "span_id"))?, 64)?,
                })
            })
            .collect::<PyResult<Vec<_>>>()?
    };

    Ok(FinishedSpan {
        trace_id,
        span_id,
        parent_id,
        flags,
        operation: Cow::Owned(operation),
        start: UNIX_EPOCH + Duration::from_micros(start_micros),
        duration: Duration::from_micros(duration_micros),
        tags,
        logs,
        references,
    })
}

/// Convert a list of jaeger thrift `Tag`s, which jaeger-client builds as
/// tags are set.
fn tags_from_py(tags: &Bound<'_, PyAny>) -> PyResult<Vec<Tag>> {
    let py = tags.py();
    tags.try_iter()?
        .map(|tag| {
            let tag = tag?;
            let key: String = tag.getattr(intern!(py, "key"))?.extract()?;
            let v_type: i32 = tag.getattr(intern!(py, "vType"))?.extract()?;
            let value = match v_type {
                1 => TagValue::Double(tag.getattr(intern!(py, "vDouble"))?.extract()?),
                2 => TagValue::Bool(tag.getattr(intern!(py, "vBool"))?.extract()?),
                3 => TagValue::Long(tag.getattr(intern!(py, "vLong"))?.extract()?),
                // Generated code writes `vBinary` as a string's UTF-8.
                4 => {
                    let value = tag.getattr(intern!(py, "vBinary"))?;
                    let bytes = match value.extract::<Vec<u8>>() {
                        Ok(bytes) if !value.is_instance_of::<pyo3::types::PyString>() => bytes,
                        _ => value.str()?.to_string().into_bytes(),
                    };
                    TagValue::Binary(bytes)
                }
                _ => TagValue::Str(
                    tag.getattr(intern!(py, "vStr"))?
                        .extract::<String>()?
                        .into(),
                ),
            };
            Ok(Tag::new(key, value))
        })
        .collect()
}

/// Encode `span`, a finished jaeger-client span, as one `emitBatch`
/// message with `process`, as the Rust reporter would send it. For tests
/// comparing with jaeger-client's own encoding.
#[pyfunction]
pub fn encode_python_span_for_test<'py>(
    py: Python<'py>,
    span: &Bound<'py, PyAny>,
    service_name: String,
    process_tags: Vec<(String, String)>,
) -> PyResult<Bound<'py, PyAny>> {
    let process = super::thrift::Process {
        service_name,
        tags: process_tags
            .into_iter()
            .map(|(key, value)| Tag::string(key, value))
            .collect(),
    };
    let encoded = super::thrift::encode_span(&finished_span_from_py(span)?);
    let message = super::thrift::encode_emit_batch(&process, &[encoded], 1);
    pyo3::types::PyBytes::new(py, &message).into_bound_py_any(py)
}
