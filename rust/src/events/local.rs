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
 */

//! Hashing and signing the events this server creates.
//!
//! This mirrors `synapse.crypto.event_signing.add_hashes_and_signatures`
//! followed by `make_event_from_dict`: add the sha256 content hash, sign the
//! redacted event with the server's key, and parse the result as an
//! [`Event`].

use std::sync::Arc;

use anyhow::{bail, Context};
use base64::{prelude::BASE64_STANDARD_NO_PAD, Engine};
use pyo3::{exceptions::PyValueError, prelude::*, types::PyDict};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use super::{
    constants::{
        event_field::{HASHES, SIGNATURES, UNSIGNED},
        unsigned_field::AGE_TS,
    },
    event_dict_from_json_str,
    internal_metadata::EventInternalMetadata,
    utils::{redact, reference_hash_of_canonical_json},
    Event,
};
use crate::{
    canonical_json::{to_vec_canonical, CanonicalizationOptions},
    federation::signing::FederationSigningKey,
    room_versions::{EventFormatVersions, RoomVersion},
};

/// Signs the events this server creates, as `server_name` with its signing
/// key. One per homeserver, so the key is only parsed once.
#[pyclass(frozen)]
pub struct LocalEventSigner {
    server_name: String,
    signing_key: FederationSigningKey,
}

#[pymethods]
impl LocalEventSigner {
    #[new]
    fn py_new(server_name: String, signing_key: FederationSigningKey) -> Self {
        LocalEventSigner {
            server_name,
            signing_key,
        }
    }

    /// Add the content hash and this server's signature to the event in
    /// `event_json` (its canonical JSON encoding) and parse it as an
    /// [`Event`].
    ///
    /// Raises `ValueError` if the JSON is not a valid event for the room
    /// version, as constructing an `Event` does.
    fn build_event(
        &self,
        event_json: &[u8],
        room_version: &Bound<'_, PyAny>,
        internal_metadata_dict: &Bound<'_, PyDict>,
    ) -> PyResult<Event> {
        let room_version: &'static RoomVersion = room_version
            .getattr("identifier")?
            .extract::<&str>()?
            .parse()
            .map_err(|e| PyValueError::new_err(format!("Unsupported room version: {e}")))?;

        let internal_metadata = EventInternalMetadata::new(internal_metadata_dict)?;

        self.sign(event_json, room_version, internal_metadata)
            .map_err(|err| PyValueError::new_err(format!("{err:#}")))
    }
}

impl LocalEventSigner {
    fn sign(
        &self,
        event_json: &[u8],
        room_version: &'static RoomVersion,
        internal_metadata: EventInternalMetadata,
    ) -> anyhow::Result<Event> {
        let mut event: Map<String, Value> =
            serde_json::from_slice(event_json).context("Failed to parse event JSON")?;

        let reference_hash = add_hashes_and_signature(
            &mut event,
            room_version,
            &self.server_name,
            &self.signing_key,
        )?;

        // Format v1 events carry their event ID, which `Event` reads from
        // the JSON. For the others it is the reference hash.
        let event_id = (room_version.event_format != EventFormatVersions::ROOM_V1_V2)
            .then(|| Arc::from(format!("${reference_hash}")));

        let event_json = serde_json::to_string(&event)?;
        let parsed_event = event_dict_from_json_str(room_version, &event_json).map_err(|err| {
            anyhow::anyhow!("Failed to parse event for room version {room_version}: {err}")
        })?;
        Event::from_parts_with_event_id(
            parsed_event,
            event_id,
            internal_metadata,
            room_version,
            None,
        )
    }
}

/// Add the sha256 content hash to `event`, then this server's signature of
/// the redacted event, keeping any hashes and signatures already there.
/// Returns the event's reference hash, which the signature covers.
///
/// Numbers keep the text they were parsed from, so this hashes and signs
/// exactly the bytes Python's `encode_canonical_json` would.
pub fn add_hashes_and_signature(
    event: &mut Map<String, Value>,
    room_version: &RoomVersion,
    server_name: &str,
    signing_key: &FederationSigningKey,
) -> anyhow::Result<String> {
    // The content hash covers the event without its hashes, signatures and
    // the fields that are not part of the federated event.
    let mut hashed = event.clone();
    for key in [
        AGE_TS,
        UNSIGNED,
        SIGNATURES,
        HASHES,
        "outlier",
        "destinations",
    ] {
        hashed.remove(key);
    }
    let hashed = to_vec_canonical(&hashed, CanonicalizationOptions::relaxed())?;
    let content_hash = BASE64_STANDARD_NO_PAD.encode(Sha256::digest(&hashed));

    match event
        .entry(HASHES)
        .or_insert_with(|| Value::Object(Map::new()))
    {
        Value::Object(hashes) => {
            hashes.insert("sha256".to_owned(), Value::String(content_hash));
        }
        _ => bail!("`{HASHES}` is not an object"),
    }

    // The signature covers the redacted event without its signatures.
    let redacted = redact(&Value::Object(event.clone()), room_version)?;
    let Value::Object(mut redacted) = redacted else {
        bail!("Redacted event is not an object");
    };
    let mut signatures = match redacted.remove(SIGNATURES) {
        None => Map::new(),
        Some(Value::Object(signatures)) => signatures,
        Some(_) => bail!("`{SIGNATURES}` is not an object"),
    };
    redacted.remove(UNSIGNED);
    redacted.remove(AGE_TS);

    let signed = to_vec_canonical(&redacted, CanonicalizationOptions::relaxed())?;
    let signature = signing_key.sign_base64(&signed);

    match signatures
        .entry(server_name)
        .or_insert_with(|| Value::Object(Map::new()))
    {
        Value::Object(ours) => {
            ours.insert(signing_key.key_id().to_owned(), Value::String(signature));
        }
        _ => bail!("`{SIGNATURES}.{server_name}` is not an object"),
    }
    event.insert(SIGNATURES.to_owned(), Value::Object(signatures));

    Ok(reference_hash_of_canonical_json(&signed, room_version))
}
