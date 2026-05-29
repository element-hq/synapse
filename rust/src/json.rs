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

/// A wrapper type that represents a value that may be missing.
///
/// We can't necessarily use `Option<T>` for this, as we want to distinguish
/// between a missing value and a value that is present but null (e.g.
/// `{"field": null}` vs `{}`). Serde by default treats missing fields as
/// `None`, so we need a custom type to capture this distinction.
///
/// A plain `AllowMissing<T>` is used for fields that are either present and of
/// type `T`, or absent. An `AllowMissing<Option<T>>` is used for fields that
/// are of type `T`, null, or absent.
///
/// Note, to use this type correctly, the field **MUST** be annotated with:
///
/// ```rust
/// #[serde(
///     default,
///     with = "crate::json::allow_missing",
///     skip_serializing_if = "AllowMissing::is_absent"
/// )]
/// ```
///
#[derive(Default, Debug, Clone)]
pub enum AllowMissing<T> {
    Some(T),
    #[default]
    Absent,
}

impl<T> AllowMissing<T> {
    /// Returns `true` if the value is present, even if it is null.
    pub fn is_some(&self) -> bool {
        matches!(self, AllowMissing::Some(_))
    }

    /// Returns `true` if the value is absent.
    pub fn is_absent(&self) -> bool {
        matches!(self, AllowMissing::Absent)
    }

    /// Converts to `Option<T::Target>`.
    ///
    /// Useful for converting e.g. `AllowMissing<String>` to `Option<&str>`.
    pub fn as_deref_opt(&self) -> Option<&T::Target>
    where
        T: std::ops::Deref,
    {
        match self {
            AllowMissing::Some(inner) => Some(inner.deref()),
            AllowMissing::Absent => None,
        }
    }

    /// Converts to `Option<&T>`.
    pub fn as_ref_opt(&self) -> Option<&T> {
        match self {
            AllowMissing::Some(inner) => Some(inner),
            AllowMissing::Absent => None,
        }
    }
}

/// A module that provides the serialization and deserialization logic for
/// `AllowMissing<T>`.
pub mod allow_missing {
    use serde::ser::Error as _;

    use super::AllowMissing;

    pub fn deserialize<'de, T, D>(deserializer: D) -> Result<AllowMissing<T>, D::Error>
    where
        T: serde::Deserialize<'de>,
        D: serde::Deserializer<'de>,
    {
        Ok(AllowMissing::Some(T::deserialize(deserializer)?))
    }

    pub fn serialize<T, S>(value: &AllowMissing<T>, serializer: S) -> Result<S::Ok, S::Error>
    where
        T: serde::Serialize,
        S: serde::Serializer,
    {
        match value {
            AllowMissing::Some(inner) => inner.serialize(serializer),
            // We should never attempt to serialize an `AllowMissing::Absent`, as we
            // should have skipped it with `skip_serializing_if`.
            AllowMissing::Absent => Err(S::Error::custom("cannot serialize AllowMissing::Absent")),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::assert_matches;

    use serde::{Deserialize, Serialize};

    use super::*;

    #[derive(Serialize, Deserialize)]
    struct TestStruct {
        #[serde(
            default,
            with = "crate::json::allow_missing",
            skip_serializing_if = "AllowMissing::is_absent"
        )]
        value: AllowMissing<i32>,
    }

    #[test]
    fn test_deserialize() {
        let json = r#"{"value":42}"#;
        let deserialized: TestStruct = serde_json::from_str(json).unwrap();
        assert!(deserialized.value.is_some());
        assert_matches!(deserialized.value, AllowMissing::Some(42));

        let json = r#"{}"#;
        let deserialized: TestStruct = serde_json::from_str(json).unwrap();
        assert!(deserialized.value.is_absent());
        assert_matches!(deserialized.value, AllowMissing::Absent);
    }

    #[test]
    fn test_serialize() {
        let value = TestStruct {
            value: AllowMissing::Some(42),
        };
        let serialized = serde_json::to_string(&value).unwrap();
        assert_eq!(serialized, r#"{"value":42}"#);

        let value = TestStruct {
            value: AllowMissing::Absent,
        };
        let serialized = serde_json::to_string(&value).unwrap();
        assert_eq!(serialized, r#"{}"#);
    }

    /// Test that we get an error if we attempt to serialize an
    /// `AllowMissing::Absent` without the skip_serializing_if annotation.
    #[test]
    fn test_serialize_absent_error() {
        #[derive(Serialize)]
        struct TestStructWithoutSkip {
            #[serde(default, with = "crate::json::allow_missing")]
            value: AllowMissing<i32>,
        }

        let value = TestStructWithoutSkip {
            value: AllowMissing::Absent,
        };

        let err = serde_json::to_string(&value).unwrap_err();
        assert_eq!(err.to_string(), "cannot serialize AllowMissing::Absent");
    }

    #[derive(Serialize, Deserialize)]
    struct TestStructOption {
        #[serde(
            default,
            with = "crate::json::allow_missing",
            skip_serializing_if = "AllowMissing::is_absent"
        )]
        value: AllowMissing<Option<i32>>,
    }

    #[test]
    fn test_serialize_option() {
        let value = TestStructOption {
            value: AllowMissing::Some(Some(42)),
        };
        let serialized = serde_json::to_string(&value).unwrap();
        assert_eq!(serialized, r#"{"value":42}"#);

        let value = TestStructOption {
            value: AllowMissing::Some(None),
        };
        let serialized = serde_json::to_string(&value).unwrap();
        assert_eq!(serialized, r#"{"value":null}"#);

        let value = TestStructOption {
            value: AllowMissing::Absent,
        };
        let serialized = serde_json::to_string(&value).unwrap();
        assert_eq!(serialized, r#"{}"#);
    }

    #[test]
    fn test_deserialize_option() {
        let json = r#"{"value":42}"#;
        let deserialized: TestStructOption = serde_json::from_str(json).unwrap();
        assert!(deserialized.value.is_some());
        assert_matches!(deserialized.value, AllowMissing::Some(Some(42)));

        let json = r#"{"value":null}"#;
        let deserialized: TestStructOption = serde_json::from_str(json).unwrap();
        assert!(deserialized.value.is_some());
        assert_matches!(deserialized.value, AllowMissing::Some(None));

        let json = r#"{}"#;
        let deserialized: TestStructOption = serde_json::from_str(json).unwrap();
        assert!(deserialized.value.is_absent());
        assert_matches!(deserialized.value, AllowMissing::Absent);
    }
}
