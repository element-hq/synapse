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

//! Encode Jaeger spans as the `Agent.emitBatch` thrift message, in the compact
//! protocol, which is what the Jaeger agent accepts on UDP port 6831.
//!
//! Write-only and limited to the structs in `jaeger.thrift` and `agent.thrift`.
//! The output matches what jaeger-client's Python reporter sends byte for byte,
//! which the golden test below checks.
//!
//! Compact protocol summary: integers are zigzag varints, a field header packs
//! the field ID's delta from the previous field into the high nibble when it
//! is 1 to 15, booleans live in the field header's type nibble, and lists of
//! up to 14 elements pack the size into the header byte.

use std::borrow::Cow;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// The compact protocol's type codes.
mod ctype {
    pub const BOOL_TRUE: u8 = 1;
    pub const BOOL_FALSE: u8 = 2;
    pub const I32: u8 = 5;
    pub const I64: u8 = 6;
    pub const DOUBLE: u8 = 7;
    pub const BINARY: u8 = 8;
    pub const LIST: u8 = 9;
    pub const STRUCT: u8 = 12;
}

const PROTOCOL_ID: u8 = 0x82;
const VERSION: u8 = 1;
const MESSAGE_TYPE_ONEWAY: u8 = 4;

/// The value of a span tag or log field. Jaeger's `TagType` enum, with the
/// value in the matching field.
#[derive(Clone, Debug, PartialEq)]
pub enum TagValue {
    Str(Cow<'static, str>),
    Double(f64),
    Bool(bool),
    Long(i64),
    Binary(Vec<u8>),
}

/// A span tag or a log field.
#[derive(Clone, Debug, PartialEq)]
pub struct Tag {
    pub key: Cow<'static, str>,
    pub value: TagValue,
}

impl Tag {
    pub fn new(key: impl Into<Cow<'static, str>>, value: TagValue) -> Self {
        Tag {
            key: key.into(),
            value,
        }
    }

    pub fn string(key: impl Into<Cow<'static, str>>, value: impl Into<Cow<'static, str>>) -> Self {
        Tag::new(key, TagValue::Str(value.into()))
    }
}

/// A timestamped set of fields on a span, Python's `span.log_kv`.
#[derive(Clone, Debug, PartialEq)]
pub struct Log {
    pub timestamp: SystemTime,
    pub fields: Vec<Tag>,
}

/// How a span relates to the span it references.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RefType {
    ChildOf,
    FollowsFrom,
}

/// A reference from a span to another span, possibly in another trace.
#[derive(Clone, Debug, PartialEq)]
pub struct SpanRef {
    pub ref_type: RefType,
    pub trace_id: u128,
    pub span_id: u64,
}

/// A finished span, ready to report.
#[derive(Clone, Debug, PartialEq)]
pub struct FinishedSpan {
    pub trace_id: u128,
    pub span_id: u64,
    /// The parent's span ID, or 0 for a root span.
    pub parent_id: u64,
    pub flags: u8,
    pub operation: Cow<'static, str>,
    pub start: SystemTime,
    pub duration: Duration,
    pub tags: Vec<Tag>,
    pub logs: Vec<Log>,
    pub references: Vec<SpanRef>,
}

/// The reporting process: its service name and process-wide tags. Sent with
/// every batch.
#[derive(Clone, Debug, PartialEq)]
pub struct Process {
    pub service_name: String,
    pub tags: Vec<Tag>,
}

/// Microseconds since the epoch, as Jaeger's i64 timestamps. Times before the
/// epoch come out as 0.
fn micros_since_epoch(time: SystemTime) -> i64 {
    time.duration_since(UNIX_EPOCH)
        .map(duration_micros)
        .unwrap_or(0)
}

fn duration_micros(duration: Duration) -> i64 {
    i64::try_from(duration.as_micros()).unwrap_or(i64::MAX)
}

/// A write-only compact protocol encoder.
#[derive(Default)]
struct CompactWriter {
    buf: Vec<u8>,
    /// The ID of the last field written in the current struct, for the delta
    /// in the next field header.
    last_field_id: i16,
    /// `last_field_id` of each enclosing struct.
    outer_field_ids: Vec<i16>,
}

impl CompactWriter {
    fn with_buffer(buf: Vec<u8>) -> Self {
        CompactWriter {
            buf,
            ..Default::default()
        }
    }

    fn varint(&mut self, mut value: u64) {
        while value >= 0x80 {
            self.buf.push((value as u8 & 0x7f) | 0x80);
            value >>= 7;
        }
        self.buf.push(value as u8);
    }

    fn zigzag_i32(&mut self, value: i32) {
        self.varint(((value << 1) ^ (value >> 31)) as u32 as u64);
    }

    fn zigzag_i64(&mut self, value: i64) {
        self.varint(((value << 1) ^ (value >> 63)) as u64);
    }

    fn struct_begin(&mut self) {
        self.outer_field_ids.push(self.last_field_id);
        self.last_field_id = 0;
    }

    fn struct_end(&mut self) {
        // The field stop.
        self.buf.push(0);
        self.last_field_id = self.outer_field_ids.pop().unwrap_or(0);
    }

    fn field_header(&mut self, field_type: u8, id: i16) {
        let delta = id - self.last_field_id;
        if (1..=15).contains(&delta) {
            self.buf.push(((delta as u8) << 4) | field_type);
        } else {
            self.buf.push(field_type);
            self.zigzag_i32(id.into());
        }
        self.last_field_id = id;
    }

    fn list_header(&mut self, element_type: u8, size: usize) {
        if size <= 14 {
            self.buf.push(((size as u8) << 4) | element_type);
        } else {
            self.buf.push(0xf0 | element_type);
            self.varint(size as u64);
        }
    }

    fn bytes(&mut self, value: &[u8]) {
        self.varint(value.len() as u64);
        self.buf.extend_from_slice(value);
    }

    fn i32_field(&mut self, id: i16, value: i32) {
        self.field_header(ctype::I32, id);
        self.zigzag_i32(value);
    }

    fn i64_field(&mut self, id: i16, value: i64) {
        self.field_header(ctype::I64, id);
        self.zigzag_i64(value);
    }

    fn double_field(&mut self, id: i16, value: f64) {
        self.field_header(ctype::DOUBLE, id);
        self.buf.extend_from_slice(&value.to_le_bytes());
    }

    fn bool_field(&mut self, id: i16, value: bool) {
        let field_type = if value {
            ctype::BOOL_TRUE
        } else {
            ctype::BOOL_FALSE
        };
        self.field_header(field_type, id);
    }

    fn bytes_field(&mut self, id: i16, value: &[u8]) {
        self.field_header(ctype::BINARY, id);
        self.bytes(value);
    }

    /// A list of structs, each written by `write`.
    fn struct_list_field<T>(&mut self, id: i16, items: &[T], write: impl Fn(&mut Self, &T)) {
        self.field_header(ctype::LIST, id);
        self.list_header(ctype::STRUCT, items.len());
        for item in items {
            write(self, item);
        }
    }

    fn tag(&mut self, tag: &Tag) {
        self.struct_begin();
        self.bytes_field(1, tag.key.as_bytes());
        let (tag_type, write_value): (i32, &dyn Fn(&mut Self)) = match &tag.value {
            TagValue::Str(value) => (0, &|w| w.bytes_field(3, value.as_bytes())),
            TagValue::Double(value) => (1, &|w| w.double_field(4, *value)),
            TagValue::Bool(value) => (2, &|w| w.bool_field(5, *value)),
            TagValue::Long(value) => (3, &|w| w.i64_field(6, *value)),
            TagValue::Binary(value) => (4, &|w| w.bytes_field(7, value)),
        };
        self.i32_field(2, tag_type);
        write_value(self);
        self.struct_end();
    }

    fn log(&mut self, log: &Log) {
        self.struct_begin();
        self.i64_field(1, micros_since_epoch(log.timestamp));
        self.struct_list_field(2, &log.fields, Self::tag);
        self.struct_end();
    }

    fn span_ref(&mut self, span_ref: &SpanRef) {
        self.struct_begin();
        let ref_type = match span_ref.ref_type {
            RefType::ChildOf => 0,
            RefType::FollowsFrom => 1,
        };
        self.i32_field(1, ref_type);
        self.i64_field(2, trace_id_low(span_ref.trace_id));
        self.i64_field(3, trace_id_high(span_ref.trace_id));
        self.i64_field(4, span_ref.span_id as i64);
        self.struct_end();
    }

    fn span(&mut self, span: &FinishedSpan) {
        self.struct_begin();
        self.i64_field(1, trace_id_low(span.trace_id));
        self.i64_field(2, trace_id_high(span.trace_id));
        // Python hands thrift signed ids, so ids of 2^63 and above go out
        // negative. A bit cast does the same.
        self.i64_field(3, span.span_id as i64);
        self.i64_field(4, span.parent_id as i64);
        self.bytes_field(5, span.operation.as_bytes());
        // jaeger-client leaves out an empty reference list, but writes empty
        // tag and log lists.
        if !span.references.is_empty() {
            self.struct_list_field(6, &span.references, Self::span_ref);
        }
        self.i32_field(7, span.flags.into());
        self.i64_field(8, micros_since_epoch(span.start));
        self.i64_field(9, duration_micros(span.duration));
        self.struct_list_field(10, &span.tags, Self::tag);
        self.struct_list_field(11, &span.logs, Self::log);
        self.struct_end();
    }

    fn process(&mut self, process: &Process) {
        self.struct_begin();
        self.bytes_field(1, process.service_name.as_bytes());
        self.struct_list_field(2, &process.tags, Self::tag);
        self.struct_end();
    }
}

fn trace_id_low(trace_id: u128) -> i64 {
    trace_id as u64 as i64
}

fn trace_id_high(trace_id: u128) -> i64 {
    (trace_id >> 64) as u64 as i64
}

/// Encode one span as an element of the batch's span list. Spans are encoded
/// as they finish so that the reporter knows each one's size, then joined by
/// [`encode_emit_batch`].
pub fn encode_span(span: &FinishedSpan) -> Vec<u8> {
    let mut writer = CompactWriter::default();
    writer.span(span);
    writer.buf
}

/// The `emitBatch` message for the spans, as `encode_span` encoded them, ready
/// to send as one UDP datagram.
///
/// `seq_id` numbers the message. jaeger-client counts up from 1. The agent
/// ignores it.
pub fn encode_emit_batch(process: &Process, encoded_spans: &[Vec<u8>], seq_id: u32) -> Vec<u8> {
    let spans_len: usize = encoded_spans.iter().map(Vec::len).sum();
    let mut writer = CompactWriter::with_buffer(Vec::with_capacity(spans_len + 256));

    writer.buf.push(PROTOCOL_ID);
    writer.buf.push(VERSION | (MESSAGE_TYPE_ONEWAY << 5));
    // Not zigzag encoded, unlike other integers.
    writer.varint(seq_id.into());
    writer.bytes(b"emitBatch");

    // emitBatch_args { 1: Batch batch }
    writer.struct_begin();
    writer.field_header(ctype::STRUCT, 1);

    // Batch { 1: Process process, 2: list<Span> spans }
    writer.struct_begin();
    writer.field_header(ctype::STRUCT, 1);
    writer.process(process);
    writer.field_header(ctype::LIST, 2);
    writer.list_header(ctype::STRUCT, encoded_spans.len());
    for span in encoded_spans {
        writer.buf.extend_from_slice(span);
    }
    writer.struct_end();

    writer.struct_end();
    writer.buf
}

/// An upper bound on the size of an `emitBatch` message holding `span_count`
/// spans whose encodings total `spans_len` bytes, without building it. Exact
/// but for the sequence ID's varint, which can be up to four bytes shorter.
pub fn emit_batch_len(encoded_process_len: usize, span_count: usize, spans_len: usize) -> usize {
    // Message header: protocol ID, version and type, sequence ID (at most
    // five bytes), the method name with its one-byte length.
    let header = 2 + 5 + 1 + "emitBatch".len();
    // Field headers for the args' batch and the batch's process, the spans
    // field header, two struct stops.
    let framing = 1 + 1 + 1 + 2;
    let list_header = if span_count <= 14 {
        1
    } else {
        1 + varint_len(span_count as u64)
    };
    header + framing + encoded_process_len + list_header + spans_len
}

/// The encoded size of the process struct, for [`emit_batch_len`].
pub fn encoded_process_len(process: &Process) -> usize {
    let mut writer = CompactWriter::default();
    writer.process(process);
    writer.buf.len()
}

fn varint_len(mut value: u64) -> usize {
    let mut len = 1;
    while value >= 0x80 {
        value >>= 7;
        len += 1;
    }
    len
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at_micros(micros: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_micros(micros)
    }

    /// The batch `scripts-dev/generate_jaeger_thrift_fixture.py` builds.
    fn fixture_batch() -> (Process, Vec<FinishedSpan>) {
        let process = Process {
            service_name: "test.server master".to_owned(),
            tags: vec![
                Tag::string("hostname", "host-1"),
                Tag::string("jaeger.version", "Python-4.8.0"),
            ],
        };

        let full = FinishedSpan {
            trace_id: (0x0123456789ABCDEF << 64) | 0xFEDCBA9876543210,
            span_id: 0x8000000000000001,
            parent_id: 0xFFFFFFFFFFFFFFFF,
            flags: 1,
            operation: "db.query".into(),
            start: at_micros(1_760_000_000_123_456),
            duration: Duration::from_micros(789),
            tags: vec![
                Tag::string("db.statement", "SELECT 1"),
                Tag::new("ratio", TagValue::Double(-1.5)),
                Tag::new("sampled", TagValue::Bool(true)),
                Tag::new("error", TagValue::Bool(false)),
                Tag::new("db.queued", TagValue::Long(-300)),
                Tag::new("raw", TagValue::Binary(b"raw-bytes".to_vec())),
            ],
            logs: vec![Log {
                timestamp: at_micros(1_760_000_000_123_999),
                fields: vec![Tag::string("message", "commit")],
            }],
            references: vec![SpanRef {
                ref_type: RefType::FollowsFrom,
                trace_id: 12345,
                span_id: 67890,
            }],
        };

        let many_tags = FinishedSpan {
            trace_id: 1,
            span_id: 2,
            parent_id: 0,
            flags: 3,
            operation: "many tags".into(),
            start: at_micros(1_760_000_000_000_000),
            duration: Duration::ZERO,
            tags: (0..20)
                .map(|i| Tag::string(format!("k{i}"), "v".repeat(i)))
                .collect(),
            logs: vec![],
            references: vec![],
        };

        let minimal = (0..15).map(|i| FinishedSpan {
            trace_id: 7,
            span_id: 100 + i,
            parent_id: 7,
            flags: 1,
            operation: "persist.write_chunk".into(),
            start: at_micros(1_760_000_000_000_000 + i),
            duration: Duration::from_micros(1_000_000 * i),
            tags: vec![],
            logs: vec![],
            references: vec![],
        });

        let mut spans = vec![full, many_tags];
        spans.extend(minimal);
        (process, spans)
    }

    /// Generated by `scripts-dev/generate_jaeger_thrift_fixture.py` with
    /// jaeger-client 4.8.0 and thrift 0.24.0.
    const GOLDEN: &[u8] = include_bytes!("testdata/emit_batch.bin");

    #[test]
    fn golden_emit_batch() {
        let (process, spans) = fixture_batch();
        let encoded: Vec<Vec<u8>> = spans.iter().map(encode_span).collect();
        let message = encode_emit_batch(&process, &encoded, 1);

        assert_eq!(message, GOLDEN);
    }

    #[test]
    fn emit_batch_len_bounds_the_message() {
        let (process, spans) = fixture_batch();
        let process_len = encoded_process_len(&process);

        for count in [0, 1, 14, 15, spans.len()] {
            let encoded: Vec<Vec<u8>> = spans[..count].iter().map(encode_span).collect();
            let spans_len = encoded.iter().map(Vec::len).sum();
            for seq_id in [1, u32::MAX] {
                let message = encode_emit_batch(&process, &encoded, seq_id);
                let bound = emit_batch_len(process_len, count, spans_len);
                assert!(message.len() <= bound, "{} > {bound}", message.len());
                assert!(bound - message.len() <= 4);
            }
        }
    }

    #[test]
    fn varint_and_zigzag() {
        let mut writer = CompactWriter::default();
        writer.varint(300);
        writer.zigzag_i32(-1);
        writer.zigzag_i64(i64::MIN);
        assert_eq!(
            writer.buf,
            [
                0xac, 0x02, // 300
                0x01, // zigzag(-1) = 1
                0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x01, // u64::MAX
            ]
        );
    }

    #[test]
    fn field_header_without_delta() {
        let mut writer = CompactWriter::default();
        writer.struct_begin();
        writer.i32_field(20, 1);
        // Going backwards also needs the full form.
        writer.i32_field(2, 1);
        writer.struct_end();
        assert_eq!(writer.buf, [ctype::I32, 40, 2, ctype::I32, 4, 2, 0]);
    }
}
