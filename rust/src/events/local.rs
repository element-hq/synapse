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

use std::{
    collections::{BTreeMap, HashMap},
    sync::Arc,
};

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
    canonical_json::{to_vec_canonical, CanonicalizationOptions, MAX_VALID_INTEGER},
    event_auth::auth_types_for,
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
        let room_version = room_version_from_py(room_version)?;

        let internal_metadata = EventInternalMetadata::new(internal_metadata_dict)?;

        let event: Map<String, Value> = serde_json::from_slice(event_json)
            .context("Failed to parse event JSON")
            .map_err(|err| PyValueError::new_err(format!("{err:#}")))?;

        self.sign(event, room_version, internal_metadata)
            .map_err(|err| PyValueError::new_err(format!("{err:#}")))
    }

    /// Build the state events that follow the create event of a new room, as
    /// the room handler's batch builds them one by one: each event's prev
    /// event is the one before it, its depth one more, and its auth events
    /// are picked from the state before it, which starts as `state_map`.
    ///
    /// `specs` are `(type, state_key, content)`, with the content as its
    /// canonical JSON. Every event gets `origin_server_ts` and a fresh copy
    /// of `internal_metadata_dict`.
    ///
    /// Only for room versions whose events refer to other events by ID
    /// (not v1 and v2) and that have no state DAG (MSC4242). Raises
    /// `ValueError` for anything it cannot build, and the caller builds the
    /// events in Python instead.
    #[pyo3(signature = (
        room_version, room_id, sender, prev_event_id, depth, state_map, specs,
        origin_server_ts, internal_metadata_dict,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn build_room_events(
        &self,
        room_version: &Bound<'_, PyAny>,
        room_id: &str,
        sender: &str,
        prev_event_id: String,
        depth: i64,
        state_map: HashMap<(String, String), String>,
        specs: Vec<(String, String, Vec<u8>)>,
        origin_server_ts: i64,
        internal_metadata_dict: &Bound<'_, PyDict>,
    ) -> PyResult<Vec<Event>> {
        let room_version = room_version_from_py(room_version)?;
        let to_py = |err: anyhow::Error| PyValueError::new_err(format!("{err:#}"));

        if room_version.event_format == EventFormatVersions::ROOM_V1_V2
            || room_version.msc4242_state_dags
        {
            return Err(PyValueError::new_err(format!(
                "Cannot build a room batch for room version {room_version}"
            )));
        }

        let mut state_map = state_map;
        let mut prev_event_id = prev_event_id;
        let mut events = Vec::with_capacity(specs.len());

        for (depth, (event_type, state_key, content_json)) in (depth..).zip(specs) {
            let internal_metadata = EventInternalMetadata::new(internal_metadata_dict)?;
            let content: Map<String, Value> = serde_json::from_slice(&content_json)
                .context("Failed to parse event content")
                .map_err(to_py)?;

            let auth_event_ids = room_batch_auth_event_ids(
                room_version,
                room_id,
                &event_type,
                sender,
                &state_key,
                &content,
                &state_map,
            )
            .map_err(to_py)?;

            let mut unsigned = Map::new();
            unsigned.insert(AGE_TS.to_owned(), Value::from(origin_server_ts));

            let mut event = Map::new();
            event.insert("auth_events".to_owned(), Value::from(auth_event_ids));
            event.insert(
                "prev_events".to_owned(),
                Value::from(vec![prev_event_id.clone()]),
            );
            event.insert("type".to_owned(), Value::from(event_type.clone()));
            event.insert("sender".to_owned(), Value::from(sender));
            event.insert("content".to_owned(), Value::Object(content));
            event.insert(UNSIGNED.to_owned(), Value::Object(unsigned));
            // Python caps the depth of new events, so other servers accept
            // them.
            event.insert(
                "depth".to_owned(),
                Value::from(depth.min(MAX_VALID_INTEGER)),
            );
            event.insert("room_id".to_owned(), Value::from(room_id));
            event.insert("state_key".to_owned(), Value::from(state_key.clone()));
            event.insert("origin_server_ts".to_owned(), Value::from(origin_server_ts));
            event.insert(SIGNATURES.to_owned(), Value::Object(Map::new()));

            let event = self
                .sign(event, room_version, internal_metadata)
                .map_err(to_py)?;

            prev_event_id = event.event_id().to_owned();
            state_map.insert((event_type, state_key), prev_event_id.clone());
            events.push(event);
        }

        Ok(events)
    }
}

/// The room version Python passes, by its identifier.
fn room_version_from_py(room_version: &Bound<'_, PyAny>) -> PyResult<&'static RoomVersion> {
    room_version
        .getattr("identifier")?
        .extract::<&str>()?
        .parse()
        .map_err(|e| PyValueError::new_err(format!("Unsupported room version: {e}")))
}

/// The auth events of a new state event, as `compute_auth_events` and the
/// builder pick them: the events in `state_map` for the auth types of the
/// event, in that order. In MSC4291 rooms the create event is implied by the
/// room ID, so it is left out.
fn room_batch_auth_event_ids(
    room_version: &RoomVersion,
    room_id: &str,
    event_type: &str,
    sender: &str,
    state_key: &str,
    content: &Map<String, Value>,
    state_map: &HashMap<(String, String), String>,
) -> anyhow::Result<Vec<String>> {
    let content: BTreeMap<Box<str>, Value> = content
        .iter()
        .map(|(key, value)| (Box::from(key.as_str()), value.clone()))
        .collect();
    let auth_types = auth_types_for(room_version, event_type, sender, Some(state_key), &content)
        .map_err(|err| anyhow::anyhow!("{err}"))?;

    let mut auth_event_ids: Vec<String> = auth_types
        .into_iter()
        .filter_map(|(auth_type, auth_state_key)| {
            state_map
                .get(&(auth_type.to_owned(), auth_state_key.to_owned()))
                .filter(|event_id| !event_id.is_empty())
                .cloned()
        })
        .collect();

    if room_version.msc4291_room_ids_as_hashes {
        let create_event_id = format!("${}", &room_id[1..]);
        let Some(index) = auth_event_ids.iter().position(|id| *id == create_event_id) else {
            bail!("the create event {create_event_id} is not in the state");
        };
        auth_event_ids.remove(index);
    }

    Ok(auth_event_ids)
}

impl LocalEventSigner {
    fn sign(
        &self,
        mut event: Map<String, Value>,
        room_version: &'static RoomVersion,
        internal_metadata: EventInternalMetadata,
    ) -> anyhow::Result<Event> {
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
