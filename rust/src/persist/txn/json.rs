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

//! JSON as Python's `json_encoder` writes it: compact, with every character
//! outside printable ASCII escaped (`ensure_ascii`). Keys come in the order
//! the value holds them, so a value with Python's key order gives Python's
//! text.
//!
//! Numbers are written as the value holds them. Python writes a float in
//! its own way, so number text from federation can differ (`1.50` is
//! stored as `1.5` by Python). The value is the same.

use std::io;

use serde::Serialize;
use serde_json::ser::{CompactFormatter, Formatter, Serializer};

/// A compact formatter that escapes non-ASCII characters and DEL as
/// `\uXXXX`, in UTF-16 code units as Python does. serde escapes the other
/// control characters before they reach it.
struct AsciiFormatter;

impl Formatter for AsciiFormatter {
    fn write_string_fragment<W>(&mut self, writer: &mut W, fragment: &str) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut start = 0;
        for (i, c) in fragment.char_indices() {
            if c.is_ascii() && c != '\x7f' {
                continue;
            }
            writer.write_all(&fragment.as_bytes()[start..i])?;
            let mut units = [0u16; 2];
            for unit in c.encode_utf16(&mut units) {
                write!(writer, "\\u{unit:04x}")?;
            }
            start = i + c.len_utf8();
        }
        writer.write_all(&fragment.as_bytes()[start..])
    }

    // The rest as the compact formatter writes it.
    fn begin_array_value<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        CompactFormatter.begin_array_value(writer, first)
    }

    fn begin_object_key<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        CompactFormatter.begin_object_key(writer, first)
    }

    fn begin_object_value<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        CompactFormatter.begin_object_value(writer)
    }
}

/// Encode `value` as Python's `json_encoder.encode` does.
pub fn encode<T: Serialize + ?Sized>(value: &T) -> serde_json::Result<String> {
    let mut out = Vec::new();
    value.serialize(&mut Serializer::with_formatter(&mut out, AsciiFormatter))?;
    Ok(String::from_utf8(out).expect("the formatter writes ASCII"))
}

/// Encode `pairs` as an object, with the keys in the order given.
pub fn encode_object<K: Serialize, V: Serialize>(pairs: &[(K, V)]) -> serde_json::Result<String> {
    struct Object<'a, K, V>(&'a [(K, V)]);

    impl<K: Serialize, V: Serialize> Serialize for Object<'_, K, V> {
        fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            serializer.collect_map(self.0.iter().map(|(k, v)| (k, v)))
        }
    }

    encode(&Object(pairs))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn matches_python() {
        // `json_encoder.encode({"a": ["é", "👍", "\x01", "\x7f~", "\"\\"], "b": 1.5})`
        let value = json!({"a": ["é", "👍", "\u{1}", "\u{7f}~", "\"\\"], "b": 1.5});
        assert_eq!(
            encode(&value).unwrap(),
            r#"{"a":["\u00e9","\ud83d\udc4d","\u0001","\u007f~","\"\\"],"b":1.5}"#
        );
    }

    #[test]
    fn object_keeps_key_order() {
        assert_eq!(
            encode_object(&[("z", json!(1)), ("a", json!("é"))]).unwrap(),
            r#"{"z":1,"a":"\u00e9"}"#
        );
    }
}
