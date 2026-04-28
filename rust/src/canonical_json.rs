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
 * Originally licensed under the Apache License, Version 2.0:
 * <http://www.apache.org/licenses/LICENSE-2.0>.
 *
 * [This file includes modifications made by Element Creations Ltd]
 */

//! Serialize a Rust data structure into canonical JSON data.
//!
//! See the [Canonical
//! JSON](https://matrix.org/docs/spec/appendices#canonical-json) docs for more
//! information.

use std::{
    collections::BTreeMap,
    convert::TryFrom,
    io::{self, Write},
};

use serde::ser::SerializeMap;
use serde::{
    ser::{Error as _, SerializeStruct},
    Serialize,
};
use serde_json::{
    ser::{Formatter, Serializer},
    value::RawValue,
    Value,
};

/// The minimum integer that can be used in canonical JSON.
pub const MIN_VALID_INTEGER: i64 = -(2i64.pow(53)) + 1;

/// The maximum integer that can be used in canonical JSON.
pub const MAX_VALID_INTEGER: i64 = (2i64.pow(53)) - 1;

/// Options to control how strict JSON canonicalization is.
#[derive(Clone, Debug)]
pub struct CanonicalizationOptions {
    /// Configure the serializer to strictly enforce the canonical JSON allowable number range.
    /// Allows JSON for room versions v5 or less when `false`.
    enforce_int_range: bool,
}

impl CanonicalizationOptions {
    /// Creates an instance of [CanonicalizationOptions] with permissive JSON enforcement settings.
    pub fn relaxed() -> Self {
        Self {
            enforce_int_range: false,
        }
    }

    /// Creates an instance of [CanonicalizationOptions] with strict JSON enforcement settings.
    pub fn strict() -> Self {
        Self {
            enforce_int_range: true,
        }
    }
}

/// Serialize the given data structure as a canonical JSON byte vector.
///
/// See the [Canonical
/// JSON](https://matrix.org/docs/spec/appendices#canonical-json) docs for more
/// information.
///
/// Note: serializing [`RawValue`] is not supported, as it may contain JSON that
/// is not canonical.
///
/// # Errors
///
/// Serialization can fail if `T`'s implementation of `Serialize` decides to
/// fail, if `T` contains a map with non-string keys, or if `T` contains numbers
/// that are not integers in the range `[-2**53 + 1, 2**53 - 1]`.
pub fn to_vec_canonical<T>(
    value: &T,
    options: CanonicalizationOptions,
) -> Result<Vec<u8>, serde_json::Error>
where
    T: Serialize + ?Sized,
{
    let mut vec = Vec::new();
    let mut ser = CanonicalSerializer::new(&mut vec, options);
    value.serialize(&mut ser)?;

    Ok(vec)
}

/// Serialize the given data structure as a canonical JSON string.
///
/// See the [Canonical
/// JSON](https://matrix.org/docs/spec/appendices#canonical-json) docs for more
/// information.
///
/// Note: serializing [`RawValue`] is not supported, as it may contain JSON that
/// is not canonical.
///
/// # Errors
///
/// Serialization can fail if `T`'s implementation of `Serialize` decides to
/// fail, if `T` contains a map with non-string keys, or if `T` contains numbers
/// that are not integers in the range `[-2**53 + 1, 2**53 - 1]`.
pub fn to_string_canonical<T>(
    value: &T,
    options: CanonicalizationOptions,
) -> Result<String, serde_json::Error>
where
    T: Serialize + ?Sized,
{
    let vec = to_vec_canonical(value, options)?;

    // We'll always get valid UTF-8 out
    let json_string = String::from_utf8(vec).expect("valid utf8");

    Ok(json_string)
}

/// A helper function that asserts that an integer is in the valid range.
fn assert_integer_in_range<I>(v: I) -> Result<(), serde_json::Error>
where
    i64: TryFrom<I>,
{
    let res = i64::try_from(v);
    match res {
        Ok(MIN_VALID_INTEGER..=MAX_VALID_INTEGER) => Ok(()),
        Ok(_) | Err(_) => Err(serde_json::Error::custom("integer out of range")),
    }
}

/// A JSON formatter that ensures all strings are encoded as per the [Canonical
/// JSON](https://matrix.org/docs/spec/appendices#canonical-json) spec.
pub struct CanonicalFormatter;

impl Formatter for CanonicalFormatter {
    fn write_string_fragment<W>(&mut self, writer: &mut W, fragment: &str) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        // `fragment` only contains characters that are not escaped, and don't
        // need to be escaped, so they can be written directly to the writer.
        writer.write_all(fragment.as_bytes())
    }

    fn write_char_escape<W>(
        &mut self,
        writer: &mut W,
        char_escape: serde_json::ser::CharEscape,
    ) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        use serde_json::ser::CharEscape::*;

        let s = match char_escape {
            Quote => b"\\\"" as &[u8],
            ReverseSolidus => b"\\\\",
            Solidus => b"/", // Note: this doesn't need to be escaped (and appears unused in serde_json).
            Backspace => b"\\b",
            FormFeed => b"\\f",
            LineFeed => b"\\n",
            CarriageReturn => b"\\r",
            Tab => b"\\t",
            AsciiControl(byte) => {
                static HEX_DIGITS: [u8; 16] = *b"0123456789abcdef";
                let bytes = &[
                    b'\\',
                    b'u',
                    b'0',
                    b'0',
                    HEX_DIGITS[(byte >> 4) as usize],
                    HEX_DIGITS[(byte & 0xF) as usize],
                ];
                return writer.write_all(bytes);
            }
        };

        writer.write_all(s)
    }
}

/// A JSON serializer that outputs [Canonical
/// JSON](https://matrix.org/docs/spec/appendices#canonical-json).
pub struct CanonicalSerializer<W> {
    inner: Serializer<W, CanonicalFormatter>,
    options: CanonicalizationOptions,
}

impl<W> CanonicalSerializer<W>
where
    W: Write,
{
    /// Create a new serializer that writes the canonical JSON bytes to the
    /// given writer.
    pub fn new(writer: W, options: CanonicalizationOptions) -> Self {
        Self {
            inner: Serializer::with_formatter(writer, CanonicalFormatter),
            options,
        }
    }
}

// We implement the serializer by proxying all calls to the standard
// `serde_json` serializer, except where we a) buffer up maps and structs so that we can
// sort them, and b) ensure that all numbers are integers in the valid range.
impl<'a, W> serde::Serializer for &'a mut CanonicalSerializer<W>
where
    W: Write,
{
    type Ok = <&'a mut Serializer<W, CanonicalFormatter> as serde::Serializer>::Ok;

    type Error = <&'a mut Serializer<W, CanonicalFormatter> as serde::Serializer>::Error;

    type SerializeSeq =
        <&'a mut Serializer<W, CanonicalFormatter> as serde::Serializer>::SerializeSeq;

    type SerializeTuple =
        <&'a mut Serializer<W, CanonicalFormatter> as serde::Serializer>::SerializeTuple;

    type SerializeTupleStruct =
        <&'a mut Serializer<W, CanonicalFormatter> as serde::Serializer>::SerializeTupleStruct;

    type SerializeTupleVariant =
        <&'a mut Serializer<W, CanonicalFormatter> as serde::Serializer>::SerializeTupleVariant;

    type SerializeMap = CanonicalSerializeMap<'a, W>;

    type SerializeStruct = CanonicalSerializeMap<'a, W>;

    type SerializeStructVariant =
        <&'a mut Serializer<W, CanonicalFormatter> as serde::Serializer>::SerializeStructVariant;

    fn serialize_bool(self, v: bool) -> Result<Self::Ok, Self::Error> {
        self.inner.serialize_bool(v)
    }

    fn serialize_i8(self, v: i8) -> Result<Self::Ok, Self::Error> {
        assert_integer_in_range(v)?;

        self.inner.serialize_i8(v)
    }

    fn serialize_i16(self, v: i16) -> Result<Self::Ok, Self::Error> {
        assert_integer_in_range(v)?;

        self.inner.serialize_i16(v)
    }

    fn serialize_i32(self, v: i32) -> Result<Self::Ok, Self::Error> {
        assert_integer_in_range(v)?;

        self.inner.serialize_i32(v)
    }

    fn serialize_i64(self, v: i64) -> Result<Self::Ok, Self::Error> {
        if self.options.enforce_int_range {
            assert_integer_in_range(v)?;
        }

        self.inner.serialize_i64(v)
    }

    fn serialize_i128(self, v: i128) -> Result<Self::Ok, Self::Error> {
        if self.options.enforce_int_range {
            assert_integer_in_range(v)?;
        }

        self.inner.serialize_i128(v)
    }

    fn serialize_u8(self, v: u8) -> Result<Self::Ok, Self::Error> {
        assert_integer_in_range(v)?;

        self.inner.serialize_u8(v)
    }

    fn serialize_u16(self, v: u16) -> Result<Self::Ok, Self::Error> {
        self.inner.serialize_u16(v)
    }

    fn serialize_u32(self, v: u32) -> Result<Self::Ok, Self::Error> {
        assert_integer_in_range(v)?;

        self.inner.serialize_u32(v)
    }

    fn serialize_u64(self, v: u64) -> Result<Self::Ok, Self::Error> {
        if self.options.enforce_int_range {
            assert_integer_in_range(v)?;
        }

        self.inner.serialize_u64(v)
    }

    fn serialize_u128(self, v: u128) -> Result<Self::Ok, Self::Error> {
        if self.options.enforce_int_range {
            assert_integer_in_range(v)?;
        }

        self.inner.serialize_u128(v)
    }

    fn serialize_f32(self, _: f32) -> Result<Self::Ok, Self::Error> {
        Err(serde_json::Error::custom(
            "non-integer numbers are not allowed",
        ))
    }

    fn serialize_f64(self, _: f64) -> Result<Self::Ok, Self::Error> {
        Err(serde_json::Error::custom(
            "non-integer numbers are not allowed",
        ))
    }

    fn serialize_char(self, v: char) -> Result<Self::Ok, Self::Error> {
        self.inner.serialize_char(v)
    }

    fn serialize_str(self, v: &str) -> Result<Self::Ok, Self::Error> {
        self.inner.serialize_str(v)
    }

    fn serialize_bytes(self, v: &[u8]) -> Result<Self::Ok, Self::Error> {
        self.inner.serialize_bytes(v)
    }

    fn serialize_none(self) -> Result<Self::Ok, Self::Error> {
        self.inner.serialize_none()
    }

    fn serialize_some<T>(self, value: &T) -> Result<Self::Ok, Self::Error>
    where
        T: serde::Serialize + ?Sized,
    {
        self.inner.serialize_some(value)
    }

    fn serialize_unit(self) -> Result<Self::Ok, Self::Error> {
        self.inner.serialize_unit()
    }

    fn serialize_unit_struct(self, name: &'static str) -> Result<Self::Ok, Self::Error> {
        self.inner.serialize_unit_struct(name)
    }

    fn serialize_unit_variant(
        self,
        name: &'static str,
        variant_index: u32,
        variant: &'static str,
    ) -> Result<Self::Ok, Self::Error> {
        self.inner
            .serialize_unit_variant(name, variant_index, variant)
    }

    fn serialize_newtype_struct<T>(
        self,
        name: &'static str,
        value: &T,
    ) -> Result<Self::Ok, Self::Error>
    where
        T: serde::Serialize + ?Sized,
    {
        self.inner.serialize_newtype_struct(name, value)
    }

    fn serialize_newtype_variant<T>(
        self,
        name: &'static str,
        variant_index: u32,
        variant: &'static str,
        value: &T,
    ) -> Result<Self::Ok, Self::Error>
    where
        T: serde::Serialize + ?Sized,
    {
        self.inner
            .serialize_newtype_variant(name, variant_index, variant, value)
    }

    fn serialize_seq(self, len: Option<usize>) -> Result<Self::SerializeSeq, Self::Error> {
        self.inner.serialize_seq(len)
    }

    fn serialize_tuple(self, len: usize) -> Result<Self::SerializeTuple, Self::Error> {
        self.inner.serialize_tuple(len)
    }

    fn serialize_tuple_struct(
        self,
        name: &'static str,
        len: usize,
    ) -> Result<Self::SerializeTupleStruct, Self::Error> {
        self.inner.serialize_tuple_struct(name, len)
    }

    fn serialize_tuple_variant(
        self,
        name: &'static str,
        variant_index: u32,
        variant: &'static str,
        len: usize,
    ) -> Result<Self::SerializeTupleVariant, Self::Error> {
        self.inner
            .serialize_tuple_variant(name, variant_index, variant, len)
    }

    fn serialize_map(self, _len: Option<usize>) -> Result<Self::SerializeMap, Self::Error> {
        Ok(CanonicalSerializeMap::new(
            &mut self.inner,
            self.options.clone(),
        ))
    }

    fn serialize_struct(
        self,
        name: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeStruct, Self::Error> {
        // We want to disallow `RawValue` as we don't know if its contents is
        // canonical JSON.
        //
        // Note: the `name` here comes from `serde_json::raw::TOKEN`, which
        // unfortunately isn't exported by the crate.
        if name == "$serde_json::private::RawValue" {
            return Err(Self::Error::custom("`RawValue` is not supported"));
        }
        Ok(CanonicalSerializeMap::new(
            &mut self.inner,
            self.options.clone(),
        ))
    }

    fn serialize_struct_variant(
        self,
        name: &'static str,
        variant_index: u32,
        variant: &'static str,
        len: usize,
    ) -> Result<Self::SerializeStructVariant, Self::Error> {
        self.inner
            .serialize_struct_variant(name, variant_index, variant, len)
    }

    fn collect_str<T>(self, value: &T) -> Result<Self::Ok, Self::Error>
    where
        T: std::fmt::Display + ?Sized,
    {
        self.inner.collect_str(value)
    }
}

/// A helper type for [`CanonicalSerializer`] that serializes JSON maps in
/// lexicographic order.
#[doc(hidden)]
pub struct CanonicalSerializeMap<'a, W> {
    // We buffer up the key and serialized value for each field we see.
    // The BTreeMap will then serialize in lexicographic order.
    map: BTreeMap<String, Box<RawValue>>,
    // A key which we're still waiting for a value for
    last_key: Option<String>,
    // The serializer to use to write the sorted map too.
    ser: &'a mut Serializer<W, CanonicalFormatter>,
    options: CanonicalizationOptions,
}

impl<'a, W> CanonicalSerializeMap<'a, W> {
    fn new(
        ser: &'a mut Serializer<W, CanonicalFormatter>,
        options: CanonicalizationOptions,
    ) -> Self {
        Self {
            map: BTreeMap::new(),
            last_key: None,
            ser,
            options,
        }
    }
}

impl<'a, W> SerializeMap for CanonicalSerializeMap<'a, W>
where
    W: Write,
{
    type Ok = ();

    type Error = serde_json::Error;

    fn serialize_key<T>(&mut self, key: &T) -> Result<(), Self::Error>
    where
        T: serde::Serialize + ?Sized,
    {
        if self.last_key.is_some() {
            // This can only happen if `serialize_key` is called multiple times
            // in a row without a `serialize_value` call in between. This
            // violates the contract of `SerializeMap`.
            return Err(Self::Error::custom(
                "serialize_key called multiple times in a row without serialize_value",
            ));
        }

        // Parse the `key` into a string.
        let key_string = if let Value::String(str) = serde_json::to_value(key)? {
            str
        } else {
            return Err(Self::Error::custom("key must be a string"));
        };

        self.last_key = Some(key_string);

        Ok(())
    }

    fn serialize_value<T>(&mut self, value: &T) -> Result<(), Self::Error>
    where
        T: serde::Serialize + ?Sized,
    {
        let key_string = if let Some(key_string) = self.last_key.take() {
            key_string
        } else {
            // `serde` should ensure that for every `serialize_key` there is a
            // `serialize_field` call, so `last_key` should never be None here.
            unreachable!()
        };

        // We serialize the value canonically, then store it as a `RawValue` in
        // the buffer map.
        let value_string = to_string_canonical(value, self.options.clone())?;

        self.map
            .insert(key_string, RawValue::from_string(value_string)?);

        Ok(())
    }

    fn end(self) -> Result<Self::Ok, Self::Error> {
        // No more entries in the map being serialized, so we can now serialize
        // our buffered map (which will be serialized in the correct order as
        // its a BTreeMap).
        self.map.serialize(self.ser)?;

        Ok(())
    }
}

impl<'a, W> SerializeStruct for CanonicalSerializeMap<'a, W>
where
    W: Write,
{
    type Ok = ();

    type Error = serde_json::Error;

    fn serialize_field<T>(&mut self, key: &'static str, value: &T) -> Result<(), Self::Error>
    where
        T: Serialize + ?Sized,
    {
        let key_string = key.to_string();

        // We serialize the value canonically, then store it as a `RawValue` in
        // the buffer map.
        let value_string = to_string_canonical(value, self.options.clone())?;

        self.map
            .insert(key_string, RawValue::from_string(value_string)?);

        Ok(())
    }

    fn end(self) -> Result<Self::Ok, Self::Error> {
        self.map.serialize(self.ser)?;

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use itertools::Itertools;
    use serde::Serializer;
    use serde_json::json;

    use super::*;

    #[test]
    fn empty() {
        let test = json!({});

        let json_string = to_string_canonical(&test, CanonicalizationOptions::strict()).unwrap();

        assert_eq!(json_string, r#"{}"#);
    }

    #[test]
    fn order_struct_fields() {
        #[derive(Serialize)]
        struct Test {
            b: u8,
            a: u8,
        }

        let test = Test { b: 1, a: 2 };

        let json_string = to_string_canonical(&test, CanonicalizationOptions::strict()).unwrap();

        assert_eq!(json_string, r#"{"a":2,"b":1}"#);
    }

    #[test]
    fn strings() {
        let test = json!({
            "a": "\u{1F37B}",
            "b": "\n",
            "c": "\x01",
        });

        let json_string = to_string_canonical(&test, CanonicalizationOptions::strict()).unwrap();

        assert_eq!(json_string, r#"{"a":"🍻","b":"\n","c":"\u0001"}"#);
    }

    #[test]
    fn escapes() {
        let mut buffer;
        let mut char_buffer = [0u8; 4];

        // Ensure that we encode every UTF-8 character correctly
        for c in '\0'..='\u{10FFFF}' {
            // Serialize the character and strip out the quotes to make comparison easier.
            let json_string = to_string_canonical(&c, CanonicalizationOptions::strict()).unwrap();
            let unquoted_json_string = &json_string[1..json_string.len() - 1];

            let expected = match c {
                // Some control characters have specific escape codes.
                '\x08' => r"\b",
                '\x09' => r"\t",
                '\x0A' => r"\n",
                '\x0C' => r"\f",
                '\x0D' => r"\r",
                '\x22' => r#"\""#,
                '\x5C' => r"\\",
                // Otherwise any character less than \x1F gets escaped as
                // `\u00xx`
                '\0'..='\x1F' => {
                    buffer = format!(r"\u00{:02x}", c as u32);
                    &buffer
                }
                // And everything else doesn't get escaped
                _ => c.encode_utf8(&mut char_buffer),
            };

            // The serialized character will be wrapped in quotes.
            assert_eq!(unquoted_json_string, expected);
        }
    }

    #[test]
    fn nested_map() {
        let test = json!({
            "a": {"b": 1}
        });

        let json_string = to_string_canonical(&test, CanonicalizationOptions::strict()).unwrap();

        assert_eq!(json_string, r#"{"a":{"b":1}}"#);
    }

    #[test]
    fn floats() {
        assert!(to_string_canonical(&100.0f32, CanonicalizationOptions::strict()).is_err());
        assert!(to_string_canonical(&100.0f64, CanonicalizationOptions::strict()).is_err());
    }

    #[test]
    fn integers() {
        assert_eq!(
            to_string_canonical(&100u8, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );
        assert_eq!(
            to_string_canonical(&100u16, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );
        assert_eq!(
            to_string_canonical(&100u32, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );
        assert_eq!(
            to_string_canonical(&100u64, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );
        assert_eq!(
            to_string_canonical(&100u128, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );

        assert_eq!(
            to_string_canonical(&100i8, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );
        assert_eq!(
            to_string_canonical(&100i16, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );
        assert_eq!(
            to_string_canonical(&100i32, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );
        assert_eq!(
            to_string_canonical(&100i64, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );
        assert_eq!(
            to_string_canonical(&100i128, CanonicalizationOptions::strict()).unwrap(),
            "100"
        );

        assert!(to_string_canonical(&2u64.pow(60), CanonicalizationOptions::strict()).is_err());
        assert!(to_string_canonical(&2u128.pow(60), CanonicalizationOptions::strict()).is_err());

        assert!(to_string_canonical(&2i64.pow(60), CanonicalizationOptions::strict()).is_err());
        assert!(to_string_canonical(&2i128.pow(60), CanonicalizationOptions::strict()).is_err());
        assert!(to_string_canonical(&-(2i64.pow(60)), CanonicalizationOptions::strict()).is_err());
        assert!(to_string_canonical(&-(2i128.pow(60)), CanonicalizationOptions::strict()).is_err());
    }

    #[test]
    fn backwards_compatibility() {
        assert_eq!(
            to_string_canonical(&u64::MAX, CanonicalizationOptions::relaxed()).unwrap(),
            format!("{}", u64::MAX)
        );
        assert_eq!(
            to_string_canonical(&u128::MAX, CanonicalizationOptions::relaxed()).unwrap(),
            format!("{}", u128::MAX)
        );
        assert_eq!(
            to_string_canonical(&i128::MAX, CanonicalizationOptions::relaxed()).unwrap(),
            format!("{}", i128::MAX)
        );
        assert_eq!(
            to_string_canonical(&-i128::MAX, CanonicalizationOptions::relaxed()).unwrap(),
            format!("{}", -i128::MAX)
        );
    }

    #[test]
    fn hashmap_order() {
        let mut test = HashMap::new();
        test.insert("e", 1);
        test.insert("d", 1);
        test.insert("c", 1);
        test.insert("b", 1);
        test.insert("a", 1);
        test.insert("AA", 1);

        let json_string = to_string_canonical(&test, CanonicalizationOptions::strict()).unwrap();

        assert_eq!(json_string, r#"{"AA":1,"a":1,"b":1,"c":1,"d":1,"e":1}"#);
    }

    #[test]
    fn raw_value() {
        let raw_value = RawValue::from_string("{}".to_string()).unwrap();

        assert!(to_string_canonical(&raw_value, CanonicalizationOptions::strict()).is_err());
    }

    #[test]
    fn map_with_duplicate_keys() {
        let mut output = Vec::new();
        let mut serializer =
            CanonicalSerializer::new(&mut output, CanonicalizationOptions::strict());
        let mut map_serializer = serializer.serialize_map(None).unwrap();

        map_serializer.serialize_entry("a", &1).unwrap();
        map_serializer.serialize_entry("a", &2).unwrap();

        // Also try with different representations of the same key (e.g. `\t` and `\u{0009}`).
        map_serializer.serialize_entry("\t", &2).unwrap();
        map_serializer.serialize_entry("\u{0009}", &2).unwrap();

        SerializeMap::end(map_serializer).unwrap();

        assert_eq!(String::from_utf8(output).unwrap(), r#"{"\t":2,"a":2}"#);
    }

    #[test]
    fn map_with_out_of_order_keys() {
        let mut output = Vec::new();
        let mut serializer =
            CanonicalSerializer::new(&mut output, CanonicalizationOptions::strict());
        let mut map_serializer = serializer.serialize_map(None).unwrap();

        // An ordered list of keys to insert, and the expected way they should be serialized.
        let ascii_order = [
            ('\0', r"\u0000"),
            ('\t', r"\t"),
            (' ', r" "),
            ('!', r"!"),
            ('"', r#"\""#),
            ('&', r"&"),
            ('A', r"A"),
            ('\\', r"\\"),
            ('a', r"a"),
            ('🍻', r"🍻"),
        ];

        // Double check that the keys are in the expected order.
        assert!(ascii_order.is_sorted_by_key(|(c, _)| u32::from(*c)));

        // Serialize the keys in the reverse order.
        for (c, _) in ascii_order.iter().rev() {
            map_serializer.serialize_entry(c.into(), &1).unwrap();
        }
        SerializeMap::end(map_serializer).unwrap();

        // The expected JSON should have the keys in the correct order, and the
        // correct escaping.
        let expected_json_inner = ascii_order
            .iter()
            .map(|(_, escaped)| format!(r#""{escaped}":1"#))
            .join(",");
        let expected_json = r"{".to_owned() + &expected_json_inner + r"}";

        assert_eq!(String::from_utf8(output).unwrap(), expected_json);
    }
}
