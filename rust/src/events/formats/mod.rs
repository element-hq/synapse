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

//! Over-the-wire representations of Matrix events, parameterised by event
//! format version (i.e. the structure we parse the event JSON into).
//!
//! # Design
//!
//! The shape of an event's JSON varies with the room version, but there are
//! many common fields across all of them — `type`, `sender`, `content`, etc.
//!
//! We model this with a single [`FormattedEvent`] container that is generic
//! over the format-specific tail `E`. Serde `#[serde(flatten)]` merges the
//! common and specific halves into a single JSON object over the wire, while
//! keeping them as distinct structs in Rust. This lets version-agnostic code
//! (field getters, the `unsigned` accessor, …) read [`EventCommonFields`]
//! directly, and only the small amount of version-aware logic (auth-event
//! derivation, room-ID lookup, validation) needs to match on the format.
//!
//! The default `E` parameter is the type-erased [`EventFormatEnum`], which can
//! contain any known format. The [`FormattedEvent::into_general`] method allows
//! converting from a specific format to the general enum.
//!
//! The `signatures` and `unsigned` fields are kept distinct from the
//! common/specific as they allow mutation. When copying an event they need to
//! be deep-copied, but the common/specific fields (which are immutable) can be
//! shared.
//!
//! # Format variants
//!
//! Different room versions have different over-the-wire formats, which is
//! tracked by [`crate::room_versions::RoomVersion::event_format`] field.
//!
//! Each format struct owns only its version-specific fields and any
//! validation/derivation logic; the rest lives in [`EventCommonFields`]. The
//! [`EventFormatEnum`] sum type erases the generic parameter when an `Event`
//! needs to be stored alongside others of unknown room version.
//!
//! Note that any fields not recognised by the format-specific struct or by the
//! common fields are captured into [`EventCommonFields::other_fields`] and
//! round-tripped losslessly. This is useful for capturing optional fields that
//! don't need to be parsed up front. Generally, optional fields should be
//! handled via `other_fields`, as this saves space when they are not present.
//!
//! # Serialization and deserialization
//!
//! Deserializing a Matrix Event from JSON is done by specifying the expected
//! format struct (e.g. [`FormattedEvent<EventFormatV4>`]), which enforces the
//! invariants of that format at parse time. This can then be converted into the
//! version-agnostic [`FormattedEvent`] with the
//! [`FormattedEvent::into_general`] method, which erases the format-specific
//! type but keeps the parsed fields intact.
//!
//! Serializing a [`FormattedEvent`] produces the correct Matrix Event JSON
//! shape for the format variant it contains.

use std::{collections::HashMap, sync::Arc};

use anyhow::Error;
use serde::{Deserialize, Serialize};

use crate::{
    events::{json_object::JsonObject, signatures::Signatures, unsigned::Unsigned},
    json::AllowMissing,
};

mod v1;
mod v2v3;
mod v4;
mod vmsc4242;

pub use v1::EventFormatV1;
pub use v2v3::EventFormatV2V3;
pub use v4::EventFormatV4;
pub use vmsc4242::EventFormatVMSC4242;

/// A parsed Matrix event in its over-the-wire layout.
///
/// `E` is the format-specific tail. Code that deserialises a known
/// room version picks a concrete `E` (e.g. `FormattedEvent<EventFormatV4>`);
/// the default `Arc<EventFormatEnum>` is used once the event has been
/// boxed into the version-agnostic [`Event`](crate::events::Event)
/// pyclass.
///
/// The `signatures` and `unsigned` fields are kept separate from the other
/// fields as they are mutable. Use [`FormattedEvent::deep_copy`] when an
/// independently-mutable copy is required. `common_fields` and
/// `specific_fields` are both `#[serde(flatten)]`ed so that the serialised JSON
/// is a single flat object matching the Matrix spec.
///
/// Note, deserialization of this struct must not be done from
/// [`serde_json::Value`] nor [`pythonize::depythonize`], due to a bug with
/// `#[serde(flatten)]` combined with the `arbitrary_precision` feature.
/// Instead, deserialize directly from a JSON string with
/// `serde_json::from_str`. See https://github.com/serde-rs/serde/issues/2230
/// for details.
#[derive(Serialize, Deserialize)]
pub struct FormattedEvent<E = Arc<EventFormatEnum>> {
    /// The event's signatures.
    ///
    /// Kept separate from common/specific fields as this this is a mutable
    /// field.
    #[serde(default)]
    pub signatures: Signatures,

    /// The event's unsigned data.
    ///
    /// Kept separate from common/specific fields as this this is a mutable
    /// field.
    #[serde(default)]
    pub unsigned: Unsigned,

    /// The format-specific fields of the event. This is an immutable field.
    #[serde(flatten)]
    pub specific_fields: E,

    /// The fields common to all event formats. This is an immutable field.
    #[serde(flatten)]
    pub common_fields: Arc<EventCommonFields>,
}

impl FormattedEvent {
    /// Creates a deep copy of this event, allowing the signatures and unsigned
    /// to be mutated without affecting the original.
    ///
    /// The common and specific fields are shared between the copy and the
    /// original, as they are immutable.
    pub fn deep_copy(&self) -> FormattedEvent {
        FormattedEvent {
            signatures: self.signatures.deep_copy(),
            unsigned: self.unsigned.deep_copy(),
            // These fields can safely be shared among all of the copies as they
            // are immutable (they're behind an Arc and so you can't get a
            // mutable reference and they have no interior mutability) and these
            // write protections extend into Python land as well (i.e. you can't
            // accidentally do the wrong thing and mutate)
            specific_fields: Arc::clone(&self.specific_fields),
            common_fields: Arc::clone(&self.common_fields),
        }
    }

    pub fn validate(&self) -> Result<(), Error> {
        match &*self.specific_fields {
            EventFormatEnum::V1(format) => format.validate(&self.common_fields),
            EventFormatEnum::V2V3(format) => format.validate(&self.common_fields),
            EventFormatEnum::V4(format) => format.validate(&self.common_fields),
            EventFormatEnum::VMSC4242(format) => format.validate(&self.common_fields),
        }
    }
}

impl<E> FormattedEvent<E>
where
    E: Into<EventFormatEnum>,
{
    /// Transforms a container of a specific event format into a container of
    /// the enum type.
    pub fn into_general(self) -> FormattedEvent {
        let format: Arc<EventFormatEnum> = Arc::new(self.specific_fields.into());
        FormattedEvent {
            signatures: self.signatures,
            unsigned: self.unsigned,
            specific_fields: format,
            common_fields: self.common_fields,
        }
    }
}

impl<E> From<FormattedEvent<E>> for FormattedEvent
where
    E: Into<EventFormatEnum>,
{
    fn from(container: FormattedEvent<E>) -> Self {
        container.into_general()
    }
}

/// Fields that appear in every supported event format.
///
/// Anything not recognised by the format-specific tail or by the fields
/// named here is captured into `other_fields` so events round-trip
/// losslessly even when they carry experimental or future-version
/// keys.
#[derive(Serialize, Deserialize)]
pub struct EventCommonFields {
    pub content: JsonObject,
    pub depth: i64,
    pub hashes: HashMap<Box<str>, Box<str>>,
    pub origin_server_ts: i64,
    pub sender: Box<str>,
    #[serde(
        default,
        with = "crate::json::allow_missing",
        skip_serializing_if = "AllowMissing::is_absent"
    )]
    pub state_key: AllowMissing<Box<str>>,

    /// The `type` field of the event (we use `type_` in Rust to avoid the
    /// reserved keyword).
    #[serde(rename = "type")]
    pub type_: Box<str>,

    /// All other fields that are not required/parsed by the specific/common
    /// fields. This allows us to round-trip events that contain extra fields.
    ///
    /// Generally, optional fields should be handled via `other_fields`, as this
    /// saves space when they are not present. However, that does mean we don't
    /// do any type-checking until they get used.
    #[serde(flatten)]
    pub other_fields: HashMap<Box<str>, serde_json::Value>,
}

impl EventCommonFields {
    /// Helper method to check if the event is a state event and return the
    /// tuple of `(type, state_key)` if so.
    fn type_state_key_tuple(&self) -> Option<(&str, &str)> {
        if let AllowMissing::Some(state_key) = &self.state_key {
            Some((&self.type_, state_key))
        } else {
            None
        }
    }
}

/// Type-erased version-specific tail.
///
/// Used as the default `E` parameter on [`FormattedEvent`] so the
/// pyclass [`Event`](crate::events::Event) can hold any room version
/// behind a single type. The enum is `#[serde(untagged)]` because the
/// discriminator (the room version) lives outside the JSON; in
/// practice the only direction this is serialised in is `Event ->
/// JSON`, where the chosen variant alone determines the shape.
#[derive(Serialize)]
#[serde(untagged)]
pub enum EventFormatEnum {
    V1(EventFormatV1),
    V2V3(EventFormatV2V3),
    V4(EventFormatV4),
    VMSC4242(EventFormatVMSC4242),
}

impl From<EventFormatV1> for EventFormatEnum {
    fn from(format: EventFormatV1) -> Self {
        EventFormatEnum::V1(format)
    }
}

impl From<EventFormatV2V3> for EventFormatEnum {
    fn from(format: EventFormatV2V3) -> Self {
        EventFormatEnum::V2V3(format)
    }
}

impl From<EventFormatV4> for EventFormatEnum {
    fn from(format: EventFormatV4) -> Self {
        EventFormatEnum::V4(format)
    }
}

impl From<EventFormatVMSC4242> for EventFormatEnum {
    fn from(format: EventFormatVMSC4242) -> Self {
        EventFormatEnum::VMSC4242(format)
    }
}
