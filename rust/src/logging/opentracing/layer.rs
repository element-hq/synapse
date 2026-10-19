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

//! Turn `tracing` spans into Jaeger spans.
//!
//! Rust code creates spans with the `tracing` crate's macros
//! (`tracing::info_span!` and friends). [`JaegerLayer`] gives each span
//! Jaeger IDs and, if the trace is sampled, reports it when it closes.
//!
//! A span's parent is, in order:
//! 1. its `tracing` parent: an explicit `parent:` on the macro, or the span
//!    current where it was created;
//! 2. for a span with no explicit parent, the span Python had active when the
//!    tokio task was spawned, carried by the task's [`LogContextHandle`];
//! 3. for a span with no explicit parent, the span set for this thread by
//!    [`with_py_parent`];
//! 4. none: the span starts a trace, and the configured sampler decides
//!    whether it is sampled.
//!
//! Python's span is only found by a span created inside the task. A span
//! created on the calling thread and attached with `.instrument()` before
//! the task is spawned does not see it.
//!
//! A span with a parent copies the parent's trace ID, flags and baggage.
//! Sampling is decided once per trace, never per span.
//!
//! A span ends when it is last exited, as an instrumented future does when
//! it completes, or when it closes if it was never entered. The `tracing`
//! registry only closes a span once all its children have closed, so a
//! span's report waits for its children. Work that outlives the span that
//! started it, such as a background task, should start its own trace with
//! `parent: None` and [`follows_from`] the span that started it.
//!
//! Only spans and events from Synapse's own crate are recorded. Dependencies
//! such as hyper and h2 emit `tracing` spans of their own, which would
//! otherwise flood the trace.
//!
//! The layer is installed as the process-wide `tracing` subscriber the first
//! time a homeserver enables tracing, and reports to that homeserver's
//! reporter until it shuts down. Production runs one homeserver per process.
//! Tests run several: there, the first homeserver with tracing enabled and
//! not yet shut down gets the spans. While no reporter is attached, spans are
//! disabled at creation and cost almost nothing.
//!
//! Fields recorded on a span become tags, except `otel.name`, which renames
//! the span (`tracing` span names must be static). An event inside a span
//! becomes a log on it, with the event's fields, so
//! `tracing::info!(message = "commit")` is Python's
//! `log_kv({"message": "commit"})`.

use std::borrow::Cow;
use std::cell::RefCell;
use std::fmt;
use std::sync::{Arc, OnceLock, RwLock, Weak};
use std::time::{Instant, SystemTime};

use log::warn;
use pyo3::prelude::*;
use rand::RngExt;
use tracing::field::{Field, Visit};
use tracing::span::{Attributes, Id, Record};
use tracing::subscriber::Interest;
use tracing::{Event, Metadata, Subscriber};
use tracing_subscriber::layer::{Context, SubscriberExt};
use tracing_subscriber::registry::LookupSpan;
use tracing_subscriber::{Layer, Registry};

use super::thrift::{FinishedSpan, Log, RefType, SpanRef, Tag, TagValue};
use super::{read_active_span, tracing_enabled, SamplerConfig, SpanContext, FLAG_SAMPLED};
use crate::logging::context::{current_context, LogContextHandle};

/// The longest string tag value kept, in bytes. jaeger-client's default
/// `max_tag_value_length`.
pub const MAX_TAG_VALUE_LENGTH: usize = 1024;

/// The span field that sets the span's operation name.
const NAME_FIELD: &str = "otel.name";

/// Where finished spans go: the UDP reporter, or a test's buffer.
pub trait SpanSink: Send + Sync {
    fn report(&self, span: FinishedSpan);
}

impl SpanSink for super::reporter::Reporter {
    fn report(&self, span: FinishedSpan) {
        super::reporter::Reporter::report(self, span);
    }
}

/// What a [`JaegerLayer`] reports to, and how it samples new traces.
///
/// Holds the sink weakly: a homeserver dropped without shutting down
/// detaches itself, and the next one to enable tracing can attach.
#[derive(Clone)]
pub struct LayerTarget {
    sink: Weak<dyn SpanSink>,
    sampler: SamplerConfig,
}

impl LayerTarget {
    pub fn new(sink: &Arc<dyn SpanSink>, sampler: SamplerConfig) -> Self {
        LayerTarget {
            sink: Arc::downgrade(sink),
            sampler,
        }
    }

    fn is_live(&self) -> bool {
        self.sink.strong_count() > 0
    }

    fn is(&self, sink: &Arc<dyn SpanSink>) -> bool {
        std::ptr::addr_eq(self.sink.as_ptr(), Arc::as_ptr(sink))
    }
}

/// A live target: the sink, and how to sample new traces.
struct LiveTarget {
    sink: Arc<dyn SpanSink>,
    sampler: SamplerConfig,
}

/// A `tracing` layer that reports spans to Jaeger. See the module docs.
#[derive(Clone, Default)]
pub struct JaegerLayer {
    /// `None` until tracing is enabled. While `None`, spans still get IDs
    /// but nothing is sampled.
    target: Arc<RwLock<Option<LayerTarget>>>,
}

impl JaegerLayer {
    pub fn new(target: LayerTarget) -> Self {
        JaegerLayer {
            target: Arc::new(RwLock::new(Some(target))),
        }
    }

    /// Point the layer at `target` unless it already has a live one.
    /// Returns whether it took this one.
    pub fn set_target_if_unset(&self, target: LayerTarget) -> bool {
        let Ok(mut current) = self.target.write() else {
            return false;
        };
        if current.as_ref().is_some_and(LayerTarget::is_live) {
            return false;
        }
        *current = Some(target);
        true
    }

    /// Detach `sink` if it is the current target. Returns whether it was.
    pub fn clear_target_if(&self, sink: &Arc<dyn SpanSink>) -> bool {
        let Ok(mut current) = self.target.write() else {
            return false;
        };
        let ours = current.as_ref().is_some_and(|target| target.is(sink));
        if ours {
            *current = None;
        }
        ours
    }

    fn target(&self) -> Option<LiveTarget> {
        let current = self.target.read().ok()?;
        let target = current.as_ref()?;
        Some(LiveTarget {
            sink: target.sink.upgrade()?,
            sampler: target.sampler.clone(),
        })
    }

    fn has_target(&self) -> bool {
        self.target
            .read()
            .is_ok_and(|target| target.as_ref().is_some_and(LayerTarget::is_live))
    }

    /// Decide whether a new trace starting in Rust is sampled. If it is,
    /// returns the `sampler.type` and `sampler.param` tags jaeger-client puts
    /// on a sampled root span.
    fn sample_root(&self) -> Option<Vec<Tag>> {
        let sampler = self.target()?.sampler;
        let (sampler_type, param) = match sampler {
            SamplerConfig::Const(true) => ("const", TagValue::Bool(true)),
            SamplerConfig::Probabilistic(rate) if rand::rng().random::<f64>() < rate => {
                ("probabilistic", TagValue::Double(rate))
            }
            _ => return None,
        };
        Some(vec![
            Tag::string("sampler.type", sampler_type),
            Tag::new("sampler.param", param),
        ])
    }
}

/// Whether `metadata` belongs to Synapse's crate rather than a dependency.
fn is_ours(metadata: &Metadata<'_>) -> bool {
    let target = metadata.target();
    target == "synapse" || target.starts_with("synapse::")
}

/// The Jaeger state of one `tracing` span, kept in the span's extensions.
struct SpanState {
    context: SpanContext,
    /// The parent's span ID, or 0 for a root span.
    parent_id: u64,
    /// `None` for an unsampled span, which records nothing.
    recording: Option<Recording>,
}

/// What a sampled span records until it closes.
struct Recording {
    operation: Cow<'static, str>,
    start: SystemTime,
    started: Instant,
    /// When the span was last exited.
    ended: Option<Instant>,
    tags: Vec<Tag>,
    logs: Vec<Log>,
    references: Vec<SpanRef>,
}

thread_local! {
    /// The parent set by [`with_py_parent`] for spans created on this thread.
    static PY_PARENT: RefCell<Option<SpanContext>> = const { RefCell::new(None) };
}

/// Run `f` with the span Python has active as the parent of `tracing` spans
/// created on this thread that have no other parent.
///
/// For blocking Rust work called from Python on the reactor thread (state
/// resolution, event auth), which has no task to carry the parent. Reads the
/// active span once, and only while tracing is enabled.
pub fn with_py_parent<R>(py: Python<'_>, f: impl FnOnce() -> R) -> R {
    if !tracing_enabled() {
        return f();
    }
    // Set even when Python has no active span, so that an outer call's
    // parent does not leak into this one.
    let parent = current_context(py).and_then(|context| read_active_span(py, &context));

    let previous = PY_PARENT.with(|slot| slot.replace(parent));
    // Restore even if `f` panics, so a later span on this thread does not
    // pick up a stale parent.
    struct Restore(Option<SpanContext>);
    impl Drop for Restore {
        fn drop(&mut self) {
            let previous = self.0.take();
            PY_PARENT.with(|slot| *slot.borrow_mut() = previous);
        }
    }
    let _restore = Restore(previous);
    f()
}

/// The parent for a span with no `tracing` parent.
fn python_parent() -> Option<SpanContext> {
    if let Some(handle) = LogContextHandle::task_current() {
        if let Some(span) = handle.span() {
            return Some(span.clone());
        }
    }
    PY_PARENT.with(|slot| slot.borrow().clone())
}

fn new_span_id() -> u64 {
    // Zero means "no parent" on the wire.
    loop {
        let id: u64 = rand::rng().random();
        if id != 0 {
            return id;
        }
    }
}

fn new_trace_id() -> u128 {
    // 64-bit, as jaeger-client generates by default.
    new_span_id().into()
}

impl<S> Layer<S> for JaegerLayer
where
    S: Subscriber + for<'a> LookupSpan<'a>,
{
    fn register_callsite(&self, metadata: &'static Metadata<'static>) -> Interest {
        if is_ours(metadata) {
            // `enabled` decides each time, depending on whether a reporter
            // is attached.
            Interest::sometimes()
        } else {
            Interest::never()
        }
    }

    fn enabled(&self, metadata: &Metadata<'_>, _ctx: Context<'_, S>) -> bool {
        is_ours(metadata) && self.has_target()
    }

    fn on_new_span(&self, attrs: &Attributes<'_>, id: &Id, ctx: Context<'_, S>) {
        let Some(span) = ctx.span(id) else {
            return;
        };

        // The registry has already resolved the explicit or contextual parent.
        let tracing_parent = span.parent().and_then(|parent| {
            parent
                .extensions()
                .get::<SpanState>()
                .map(|state| state.context.clone())
        });
        // An explicit parent, including `parent: None` and a disabled span,
        // is the caller's choice: only a contextual span falls back to
        // Python's.
        let parent = if attrs.is_contextual() {
            tracing_parent.or_else(python_parent)
        } else {
            tracing_parent
        };

        let mut sampler_tags = Vec::new();
        let (context, parent_id) = match parent {
            Some(parent) => (
                SpanContext {
                    trace_id: parent.trace_id,
                    span_id: new_span_id(),
                    flags: parent.flags,
                    baggage: parent.baggage,
                },
                parent.span_id,
            ),
            None => (
                SpanContext {
                    trace_id: new_trace_id(),
                    span_id: new_span_id(),
                    flags: match self.sample_root() {
                        Some(tags) => {
                            sampler_tags = tags;
                            FLAG_SAMPLED
                        }
                        None => 0,
                    },
                    baggage: None,
                },
                0,
            ),
        };

        let recording = (context.is_sampled() && self.has_target()).then(|| {
            let mut recording = Recording {
                operation: Cow::Borrowed(attrs.metadata().name()),
                start: SystemTime::now(),
                started: Instant::now(),
                ended: None,
                tags: sampler_tags,
                logs: Vec::new(),
                references: Vec::new(),
            };
            attrs.record(&mut SpanVisitor(&mut recording));
            recording
        });

        span.extensions_mut().insert(SpanState {
            context,
            parent_id,
            recording,
        });
    }

    fn on_record(&self, id: &Id, values: &Record<'_>, ctx: Context<'_, S>) {
        let Some(span) = ctx.span(id) else {
            return;
        };
        let mut extensions = span.extensions_mut();
        if let Some(recording) = extensions
            .get_mut::<SpanState>()
            .and_then(|state| state.recording.as_mut())
        {
            values.record(&mut SpanVisitor(recording));
        }
    }

    fn on_follows_from(&self, id: &Id, follows: &Id, ctx: Context<'_, S>) {
        let (Some(span), Some(follows)) = (ctx.span(id), ctx.span(follows)) else {
            return;
        };
        let Some(follows) = follows
            .extensions()
            .get::<SpanState>()
            .map(|state| state.context.clone())
        else {
            return;
        };
        add_follows_from(&mut span.extensions_mut(), &follows);
    }

    fn on_event(&self, event: &Event<'_>, ctx: Context<'_, S>) {
        let Some(span) = ctx.event_span(event) else {
            return;
        };
        let mut extensions = span.extensions_mut();
        let Some(recording) = extensions
            .get_mut::<SpanState>()
            .and_then(|state| state.recording.as_mut())
        else {
            return;
        };

        let mut fields = Vec::new();
        event.record(&mut FieldVisitor(&mut fields));
        recording.logs.push(Log {
            timestamp: SystemTime::now(),
            fields,
        });
    }

    fn on_exit(&self, id: &Id, ctx: Context<'_, S>) {
        let Some(span) = ctx.span(id) else {
            return;
        };
        let mut extensions = span.extensions_mut();
        if let Some(recording) = extensions
            .get_mut::<SpanState>()
            .and_then(|state| state.recording.as_mut())
        {
            recording.ended = Some(Instant::now());
        }
    }

    fn on_close(&self, id: Id, ctx: Context<'_, S>) {
        let Some(span) = ctx.span(&id) else {
            return;
        };
        let Some(state) = span.extensions_mut().remove::<SpanState>() else {
            return;
        };
        let Some(recording) = state.recording else {
            return;
        };
        let Some(target) = self.target() else {
            return;
        };

        target.sink.report(FinishedSpan {
            trace_id: state.context.trace_id,
            span_id: state.context.span_id,
            parent_id: state.parent_id,
            flags: state.context.flags,
            operation: recording.operation,
            start: recording.start,
            duration: recording
                .ended
                .unwrap_or_else(Instant::now)
                .saturating_duration_since(recording.started),
            tags: recording.tags,
            logs: recording.logs,
            references: recording.references,
        });
    }
}

fn add_follows_from(
    extensions: &mut tracing_subscriber::registry::ExtensionsMut<'_>,
    follows: &SpanContext,
) {
    if let Some(recording) = extensions
        .get_mut::<SpanState>()
        .and_then(|state| state.recording.as_mut())
    {
        recording.references.push(SpanRef {
            ref_type: RefType::FollowsFrom,
            trace_id: follows.trace_id,
            span_id: follows.span_id,
        });
    }
}

/// Records span fields as tags, and `otel.name` as the operation name.
struct SpanVisitor<'a>(&'a mut Recording);

impl SpanVisitor<'_> {
    fn add(&mut self, field: &Field, value: TagValue) {
        if field.name() == NAME_FIELD {
            if let TagValue::Str(name) = value {
                self.0.operation = name;
            }
            return;
        }
        push_tag(&mut self.0.tags, field, value);
    }
}

impl Visit for SpanVisitor<'_> {
    fn record_str(&mut self, field: &Field, value: &str) {
        self.add(field, TagValue::Str(truncate(value).into()));
    }

    fn record_bool(&mut self, field: &Field, value: bool) {
        self.add(field, TagValue::Bool(value));
    }

    fn record_i64(&mut self, field: &Field, value: i64) {
        self.add(field, TagValue::Long(value));
    }

    fn record_u64(&mut self, field: &Field, value: u64) {
        self.add(field, u64_value(value));
    }

    fn record_f64(&mut self, field: &Field, value: f64) {
        self.add(field, TagValue::Double(value));
    }

    fn record_debug(&mut self, field: &Field, value: &dyn fmt::Debug) {
        self.add(field, TagValue::Str(truncate(&format!("{value:?}")).into()));
    }
}

/// Records event fields as log fields.
struct FieldVisitor<'a>(&'a mut Vec<Tag>);

impl Visit for FieldVisitor<'_> {
    fn record_str(&mut self, field: &Field, value: &str) {
        push_tag(self.0, field, TagValue::Str(truncate(value).into()));
    }

    fn record_bool(&mut self, field: &Field, value: bool) {
        push_tag(self.0, field, TagValue::Bool(value));
    }

    fn record_i64(&mut self, field: &Field, value: i64) {
        push_tag(self.0, field, TagValue::Long(value));
    }

    fn record_u64(&mut self, field: &Field, value: u64) {
        push_tag(self.0, field, u64_value(value));
    }

    fn record_f64(&mut self, field: &Field, value: f64) {
        push_tag(self.0, field, TagValue::Double(value));
    }

    fn record_debug(&mut self, field: &Field, value: &dyn fmt::Debug) {
        push_tag(
            self.0,
            field,
            TagValue::Str(truncate(&format!("{value:?}")).into()),
        );
    }
}

/// Add a tag, replacing an earlier one with the same key (a field recorded
/// again with `Span::record`).
fn push_tag(tags: &mut Vec<Tag>, field: &Field, value: TagValue) {
    let key = field.name();
    if let Some(tag) = tags.iter_mut().find(|tag| tag.key == key) {
        tag.value = value;
    } else {
        tags.push(Tag::new(key, value));
    }
}

/// Thrift has no unsigned integers: a `u64` too big for an `i64` goes as a
/// string rather than wrapping negative.
fn u64_value(value: u64) -> TagValue {
    match i64::try_from(value) {
        Ok(value) => TagValue::Long(value),
        Err(_) => TagValue::Str(value.to_string().into()),
    }
}

/// `value` cut to at most [`MAX_TAG_VALUE_LENGTH`] bytes, on a character
/// boundary.
fn truncate(value: &str) -> String {
    if value.len() <= MAX_TAG_VALUE_LENGTH {
        return value.to_owned();
    }
    let mut end = MAX_TAG_VALUE_LENGTH;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    value[..end].to_owned()
}

/// The process-wide layer, installed by [`attach_global`].
/// `None` if another global subscriber was installed first.
static GLOBAL_LAYER: OnceLock<Option<JaegerLayer>> = OnceLock::new();

/// Report spans to `target`, installing the layer as the process-wide
/// subscriber on first use. Returns whether `target` is now the one in use:
/// another homeserver's reporter may already be attached.
pub fn attach_global(target: LayerTarget) -> bool {
    let layer = GLOBAL_LAYER.get_or_init(|| {
        let layer = JaegerLayer::default();
        let installed =
            tracing::subscriber::set_global_default(Registry::default().with(layer.clone()))
                .is_ok();
        installed.then_some(layer)
    });
    let Some(layer) = layer else {
        warn!("Another tracing subscriber is installed; spans from Rust code will not be reported");
        return false;
    };
    layer.set_target_if_unset(target)
}

/// Stop reporting to `sink` if the global layer reports to it.
pub fn detach_global(sink: &Arc<dyn SpanSink>) {
    if let Some(Some(layer)) = GLOBAL_LAYER.get() {
        layer.clear_target_if(sink);
    }
}

/// Run `f` on the Jaeger state of `span`, if it has any (a span created while
/// a [`JaegerLayer`] was the subscriber).
fn with_state<R>(span: &tracing::Span, f: impl FnOnce(&mut SpanState) -> R) -> Option<R> {
    span.with_subscriber(|(id, dispatch)| {
        let registry = dispatch.downcast_ref::<Registry>()?;
        let data = registry.span(id)?;
        let mut extensions = data.extensions_mut();
        extensions.get_mut::<SpanState>().map(f)
    })
    .flatten()
}

/// As [`with_state`], for reading.
fn read_state<R>(span: &tracing::Span, f: impl FnOnce(&SpanState) -> R) -> Option<R> {
    span.with_subscriber(|(id, dispatch)| {
        let registry = dispatch.downcast_ref::<Registry>()?;
        let data = registry.span(id)?;
        let extensions = data.extensions();
        extensions.get::<SpanState>().map(f)
    })
    .flatten()
}

/// The Jaeger context of `span`, for handing to code that propagates it.
pub fn span_context(span: &tracing::Span) -> Option<SpanContext> {
    read_state(span, |state| state.context.clone())
}

/// Whether `span` is sampled. For callers that would otherwise compute an
/// expensive tag (such as one-line SQL) for a span nobody will see.
pub fn is_sampled(span: &tracing::Span) -> bool {
    read_state(span, |state| state.recording.is_some()).unwrap_or(false)
}

/// The HTTP headers that carry `span`'s context to another server, as
/// jaeger-client's HTTP header codec writes them: `uber-trace-id`
/// (`{trace}:{span}:{parent}:{flags}` in hex), and an `uberctx-{key}`
/// header, percent-encoded, per baggage item. Empty if `span` has no Jaeger
/// context.
pub fn inject_headers(span: &tracing::Span) -> Vec<(String, String)> {
    read_state(span, |state| {
        let context = &state.context;
        let mut headers = vec![(
            "uber-trace-id".to_owned(),
            format!(
                "{:x}:{:x}:{:x}:{:x}",
                context.trace_id, context.span_id, state.parent_id, context.flags
            ),
        )];
        for (key, value) in context.baggage.iter().flat_map(|baggage| baggage.iter()) {
            headers.push((format!("uberctx-{key}"), percent_encode(value)));
        }
        headers
    })
    .unwrap_or_default()
}

/// Python's `urllib.parse.quote`: everything but ASCII letters, digits and
/// `_.-~/` is percent-encoded, as UTF-8 bytes.
fn percent_encode(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || b"_.-~/".contains(&byte) {
            encoded.push(byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

/// Record that `span` follows from `context`, a span that may be in another
/// trace, such as the Python span that queued the work.
pub fn follows_from(span: &tracing::Span, context: &SpanContext) {
    with_state(span, |state| {
        if let Some(recording) = state.recording.as_mut() {
            recording.references.push(SpanRef {
                ref_type: RefType::FollowsFrom,
                trace_id: context.trace_id,
                span_id: context.span_id,
            });
        }
    });
}

#[cfg(test)]
pub(crate) mod testing {
    //! Collecting spans in memory, for tests of instrumented code.

    use std::sync::Mutex;

    use tracing_subscriber::layer::SubscriberExt;

    use super::*;

    #[derive(Default)]
    pub(crate) struct MemorySink {
        spans: Mutex<Vec<FinishedSpan>>,
    }

    impl SpanSink for MemorySink {
        fn report(&self, span: FinishedSpan) {
            self.spans.lock().unwrap().push(span);
        }
    }

    impl MemorySink {
        pub(crate) fn take(&self) -> Vec<FinishedSpan> {
            std::mem::take(&mut *self.spans.lock().unwrap())
        }
    }

    /// Run `f` with a [`JaegerLayer`] as this thread's subscriber, returning
    /// the spans it reported in the order they closed.
    ///
    /// Spans must only be entered on this thread: run async code on a
    /// current-thread runtime. The registry releases the reference an enter
    /// takes through the thread's default subscriber, so a span entered on a
    /// tokio worker thread never closes. (In production the layer is the
    /// global default, which every thread sees.)
    pub(crate) fn traced<R>(
        sampler: SamplerConfig,
        f: impl FnOnce() -> R,
    ) -> (R, Vec<FinishedSpan>) {
        let sink = Arc::new(MemorySink::default());
        let dyn_sink: Arc<dyn SpanSink> = sink.clone();
        let layer = JaegerLayer::new(LayerTarget::new(&dyn_sink, sampler));
        let output = tracing::subscriber::with_default(Registry::default().with(layer), f);
        (output, sink.take())
    }

    /// The value of the tag `key` on `span`.
    pub(crate) fn tag<'a>(span: &'a FinishedSpan, key: &str) -> Option<&'a TagValue> {
        span.tags
            .iter()
            .find(|tag| tag.key == key)
            .map(|tag| &tag.value)
    }
}

#[cfg(test)]
mod tests {
    use tracing::Instrument;
    use tracing_subscriber::layer::SubscriberExt;

    use super::testing::{tag, MemorySink};
    use super::*;
    use crate::logging::context::testing::handle_with_span;

    /// [`testing::traced`] for closures that return nothing.
    fn traced(sampler: SamplerConfig, f: impl FnOnce()) -> Vec<FinishedSpan> {
        testing::traced(sampler, f).1
    }

    fn sampled_parent() -> SpanContext {
        SpanContext {
            trace_id: 0xabc << 64 | 0xdef,
            span_id: 77,
            flags: FLAG_SAMPLED,
            baggage: Some(Arc::new(vec![("k".to_owned(), "v".to_owned())])),
        }
    }

    #[test]
    fn root_span_follows_the_sampler() {
        let spans = traced(SamplerConfig::Const(true), || {
            let _span = tracing::info_span!("root", count = 3_i64).entered();
        });
        let [span] = &spans[..] else {
            panic!("expected one span, got {spans:?}")
        };
        assert_eq!(span.operation, "root");
        assert_eq!(span.parent_id, 0);
        assert_eq!(span.flags, FLAG_SAMPLED);
        assert_ne!(span.span_id, 0);
        assert_eq!(tag(span, "count"), Some(&TagValue::Long(3)));

        for sampler in [
            SamplerConfig::Const(false),
            SamplerConfig::Never,
            SamplerConfig::Probabilistic(0.0),
        ] {
            let spans = traced(sampler, || {
                let _span = tracing::info_span!("root").entered();
            });
            assert!(spans.is_empty());
        }
    }

    #[test]
    fn child_inherits_the_trace() {
        let spans = traced(SamplerConfig::Const(true), || {
            let _parent = tracing::info_span!("parent").entered();
            let _child = tracing::info_span!("child").entered();
        });
        let [child, parent] = &spans[..] else {
            panic!("expected two spans, got {spans:?}")
        };
        assert_eq!(child.operation, "child");
        assert_eq!(child.trace_id, parent.trace_id);
        assert_eq!(child.parent_id, parent.span_id);
        assert_ne!(child.span_id, parent.span_id);
    }

    #[test]
    fn explicit_parent_wins_over_the_current_span() {
        let spans = traced(SamplerConfig::Const(true), || {
            let chosen = tracing::info_span!("chosen");
            let _current = tracing::info_span!("current").entered();
            let _child = tracing::info_span!(parent: &chosen, "child").entered();
        });
        let child = spans.iter().find(|s| s.operation == "child").unwrap();
        let chosen = spans.iter().find(|s| s.operation == "chosen").unwrap();
        assert_eq!(child.parent_id, chosen.span_id);
    }

    #[test]
    fn task_parent_from_python() {
        let parent = sampled_parent();
        let spans = traced(SamplerConfig::Const(false), || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();
            runtime.block_on(handle_with_span(Some(parent.clone())).scope(async {
                let _span = tracing::info_span!("in task").entered();
            }));
        });
        // Sampled although the root sampler says no: the parent decided.
        let [span] = &spans[..] else {
            panic!("expected one span, got {spans:?}")
        };
        assert_eq!(span.trace_id, parent.trace_id);
        assert_eq!(span.parent_id, parent.span_id);
        assert_eq!(span.flags, parent.flags);
    }

    #[test]
    fn unsampled_python_parent_records_nothing() {
        let parent = SpanContext {
            flags: 0,
            ..sampled_parent()
        };
        let spans = traced(SamplerConfig::Const(true), || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();
            runtime.block_on(handle_with_span(Some(parent)).scope(async {
                let span = tracing::info_span!("in task");
                assert!(!is_sampled(&span));
                // The span still has IDs, for propagation.
                let context = span_context(&span).expect("expected a context");
                assert_eq!(context.flags, 0);
                let _entered = span.entered();
                let _child = tracing::info_span!("child").entered();
            }));
        });
        assert!(spans.is_empty());
    }

    #[test]
    fn thread_parent_from_python() {
        let parent = sampled_parent();
        let spans = traced(SamplerConfig::Const(false), || {
            PY_PARENT.with(|slot| *slot.borrow_mut() = Some(parent.clone()));
            let _span = tracing::info_span!("on thread").entered();
            PY_PARENT.with(|slot| *slot.borrow_mut() = None);
        });
        let [span] = &spans[..] else {
            panic!("expected one span, got {spans:?}")
        };
        assert_eq!(span.parent_id, parent.span_id);
    }

    #[test]
    fn spans_across_await_and_spawn() {
        let spans = traced(SamplerConfig::Const(true), || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_time()
                .build()
                .unwrap();
            runtime.block_on(
                async {
                    tokio::task::yield_now().await;
                    let child = tracing::info_span!("spawned");
                    tokio::spawn(
                        async {
                            tokio::task::yield_now().await;
                            tracing::info!(message = "in spawned task");
                        }
                        .instrument(child),
                    )
                    .await
                    .unwrap();
                }
                .instrument(tracing::info_span!("outer")),
            );
        });
        let spawned = spans.iter().find(|s| s.operation == "spawned").unwrap();
        let outer = spans.iter().find(|s| s.operation == "outer").unwrap();
        assert_eq!(spawned.parent_id, outer.span_id);
        assert_eq!(spawned.logs.len(), 1);
    }

    #[test]
    fn events_become_logs() {
        let spans = traced(SamplerConfig::Const(true), || {
            let _span = tracing::info_span!("txn").entered();
            tracing::info!(message = "commit", rows = 2_u64);
        });
        let [span] = &spans[..] else {
            panic!("expected one span, got {spans:?}")
        };
        let [log] = &span.logs[..] else {
            panic!("expected one log, got {:?}", span.logs)
        };
        assert_eq!(
            log.fields,
            vec![
                Tag::string("message", "commit"),
                Tag::new("rows", TagValue::Long(2)),
            ]
        );
    }

    #[test]
    fn fields_become_tags() {
        let long = "é".repeat(MAX_TAG_VALUE_LENGTH);
        let spans = traced(SamplerConfig::Const(true), || {
            let span = tracing::info_span!(
                "db.interaction",
                otel.name = "db.persist_events",
                long = long.as_str(),
                big = u64::MAX,
                ratio = 0.5,
                ok = true,
                debug = ?vec![1, 2],
                later = tracing::field::Empty,
            );
            span.record("later", 5_i64);
            span.record("ok", false);
        });
        let [span] = &spans[..] else {
            panic!("expected one span, got {spans:?}")
        };
        assert_eq!(span.operation, "db.persist_events");
        assert!(tag(span, NAME_FIELD).is_none());
        let Some(TagValue::Str(truncated)) = tag(span, "long") else {
            panic!("expected a string tag")
        };
        assert!(truncated.len() <= MAX_TAG_VALUE_LENGTH);
        assert!(truncated.len() >= MAX_TAG_VALUE_LENGTH - 1);
        assert_eq!(
            tag(span, "big"),
            Some(&TagValue::Str(u64::MAX.to_string().into()))
        );
        assert_eq!(tag(span, "ratio"), Some(&TagValue::Double(0.5)));
        assert_eq!(tag(span, "ok"), Some(&TagValue::Bool(false)));
        assert_eq!(tag(span, "debug"), Some(&TagValue::Str("[1, 2]".into())));
        assert_eq!(tag(span, "later"), Some(&TagValue::Long(5)));
    }

    #[test]
    fn dependency_spans_are_ignored() {
        let spans = traced(SamplerConfig::Const(true), || {
            let _theirs = tracing::info_span!(target: "hyper::client", "theirs").entered();
            let _ours = tracing::info_span!("ours").entered();
        });
        let [span] = &spans[..] else {
            panic!("expected one span, got {spans:?}")
        };
        assert_eq!(span.operation, "ours");
        assert_eq!(span.parent_id, 0);
    }

    #[test]
    fn propagation_helpers() {
        let parent = sampled_parent();
        let spans = traced(SamplerConfig::Const(true), || {
            let span = tracing::info_span!("send");
            assert!(is_sampled(&span));

            let context = span_context(&span).unwrap();
            assert_eq!(
                inject_headers(&span),
                vec![(
                    "uber-trace-id".to_owned(),
                    format!("{:x}:{:x}:0:1", context.trace_id, context.span_id)
                )]
            );

            follows_from(&span, &parent);
        });
        let [span] = &spans[..] else {
            panic!("expected one span, got {spans:?}")
        };
        assert_eq!(
            span.references,
            vec![SpanRef {
                ref_type: RefType::FollowsFrom,
                trace_id: parent.trace_id,
                span_id: parent.span_id,
            }]
        );

        // Outside a Jaeger subscriber there is nothing to read.
        let span = tracing::Span::none();
        assert!(!is_sampled(&span));
        assert!(inject_headers(&span).is_empty());
    }

    #[test]
    fn injected_headers_carry_baggage() {
        let parent = SpanContext {
            baggage: Some(Arc::new(vec![(
                "synapse-force-tracing".to_owned(),
                "a b/é".to_owned(),
            )])),
            ..sampled_parent()
        };
        traced(SamplerConfig::Const(false), || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();
            runtime.block_on(handle_with_span(Some(parent.clone())).scope(async {
                let span = tracing::info_span!("send");
                let headers = inject_headers(&span);
                assert_eq!(headers.len(), 2);
                assert_eq!(
                    headers[1],
                    (
                        "uberctx-synapse-force-tracing".to_owned(),
                        "a%20b/%C3%A9".to_owned()
                    )
                );
            }));
        });
    }

    #[test]
    fn explicit_root_ignores_the_python_parent() {
        let parent = sampled_parent();
        let spans = traced(SamplerConfig::Const(true), || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();
            runtime.block_on(handle_with_span(Some(parent.clone())).scope(async {
                let span = tracing::info_span!(parent: None, "detached");
                follows_from(&span, &parent);
            }));
        });
        let [span] = &spans[..] else {
            panic!("expected one span, got {spans:?}")
        };
        assert_ne!(span.trace_id, parent.trace_id);
        assert_eq!(span.parent_id, 0);
        assert_eq!(span.references.len(), 1);
        assert_eq!(
            tag(span, "sampler.type"),
            Some(&TagValue::Str("const".into()))
        );
    }

    #[test]
    fn duration_ends_at_the_last_exit() {
        let spans = traced(SamplerConfig::Const(true), || {
            let parent = tracing::info_span!("parent");
            let child = parent.in_scope(|| tracing::info_span!("child"));
            drop(parent);
            // The child keeps the parent open: it is only reported when the
            // child closes, but with the time it was in use.
            std::thread::sleep(std::time::Duration::from_millis(50));
            drop(child);
        });
        let parent = spans.iter().find(|s| s.operation == "parent").unwrap();
        let child = spans.iter().find(|s| s.operation == "child").unwrap();
        assert!(parent.duration < std::time::Duration::from_millis(50));
        // Never entered: it lasts until it closes.
        assert!(child.duration >= std::time::Duration::from_millis(50));
    }

    #[test]
    fn nested_with_py_parent_clears_the_outer_parent() {
        Python::initialize();
        crate::logging::opentracing::set_tracing_enabled(true);
        let parent = sampled_parent();
        let spans = traced(SamplerConfig::Const(false), || {
            PY_PARENT.with(|slot| *slot.borrow_mut() = Some(parent.clone()));
            // No logcontext is current, so Python has no span: the inner call
            // must not use the outer parent.
            Python::attach(|py| {
                with_py_parent(py, || {
                    let _span = tracing::info_span!("inner").entered();
                })
            });
            assert_eq!(PY_PARENT.with(|slot| slot.borrow().clone()), Some(parent));
            PY_PARENT.with(|slot| *slot.borrow_mut() = None);
        });
        crate::logging::opentracing::set_tracing_enabled(false);
        // The inner span became an unsampled root.
        assert!(spans.is_empty());
    }

    #[test]
    fn spans_are_disabled_without_a_target() {
        let sink: Arc<dyn SpanSink> = Arc::new(MemorySink::default());
        let layer = JaegerLayer::default();
        let subscriber = Registry::default().with(layer.clone());
        tracing::subscriber::with_default(subscriber, || {
            assert!(tracing::info_span!("before").is_disabled());

            let sampler = SamplerConfig::Const(true);
            assert!(layer.set_target_if_unset(LayerTarget::new(&sink, sampler.clone())));
            // A second target does not replace a live first one.
            let other: Arc<dyn SpanSink> = Arc::new(MemorySink::default());
            assert!(!layer.set_target_if_unset(LayerTarget::new(&other, sampler.clone())));
            assert!(!tracing::info_span!("attached").is_disabled());

            assert!(!layer.clear_target_if(&other));
            assert!(layer.clear_target_if(&sink));
            assert!(tracing::info_span!("after").is_disabled());

            // A dropped sink detaches itself, and another can take over.
            assert!(layer.set_target_if_unset(LayerTarget::new(&other, sampler.clone())));
            drop(other);
            assert!(tracing::info_span!("dropped").is_disabled());
            assert!(layer.set_target_if_unset(LayerTarget::new(&sink, sampler)));
        });
    }

    #[test]
    fn truncate_keeps_char_boundaries() {
        let value = format!("a{}", "é".repeat(MAX_TAG_VALUE_LENGTH));
        let truncated = truncate(&value);
        assert_eq!(truncated.len(), MAX_TAG_VALUE_LENGTH - 1);
        assert_eq!(truncate("short"), "short");
    }
}
