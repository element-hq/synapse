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

//! Building room-version-11 events with correct event IDs.
//!
//! Synapse recomputes every event's ID from its JSON when it loads it from
//! `event_json` and treats a mismatch as database corruption, so the
//! reference hash (and the content hash it covers) must be computed exactly
//! as the spec says: canonical JSON of the redacted event.

use base64::Engine;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

pub const ROOM_VERSION: &str = "11";
/// `EventFormatVersions.ROOM_V4_PLUS` in Synapse.
pub const FORMAT_VERSION: i32 = 3;

pub struct BuiltEvent {
    pub event_id: String,
    pub json: String,
}

/// Build an event dict, compute its hashes and ID, and serialise it as it
/// is stored in `event_json.json`. `content` is taken by value; the other
/// fields are the usual PDU fields.
#[allow(clippy::too_many_arguments)]
pub fn build(
    room_id: &str,
    sender: &str,
    event_type: &str,
    state_key: Option<&str>,
    content: Value,
    origin_server_ts: i64,
    depth: i64,
    prev_events: &[String],
    auth_events: &[String],
    local_server: &str,
    unsigned_extra: Option<Map<String, Value>>,
) -> BuiltEvent {
    let mut ev = Map::new();
    ev.insert("auth_events".into(), json!(auth_events));
    ev.insert("content".into(), content);
    ev.insert("depth".into(), json!(depth));
    ev.insert("origin_server_ts".into(), json!(origin_server_ts));
    ev.insert("prev_events".into(), json!(prev_events));
    ev.insert("room_id".into(), json!(room_id));
    ev.insert("sender".into(), json!(sender));
    if let Some(sk) = state_key {
        ev.insert("state_key".into(), json!(sk));
    }
    ev.insert("type".into(), json!(event_type));

    // Content hash: over everything except signatures/hashes/unsigned.
    let content_hash = sha256_b64(&canonical(&Value::Object(ev.clone())));
    ev.insert("hashes".into(), json!({ "sha256": content_hash }));

    // Signatures are not verified when loading from the database; a
    // placeholder of the right shape keeps the stored size realistic.
    ev.insert(
        "signatures".into(),
        json!({ local_server: { "ed25519:synth": "x".repeat(86) } }),
    );
    let mut unsigned = unsigned_extra.unwrap_or_default();
    unsigned.insert("age_ts".into(), json!(origin_server_ts));
    ev.insert("unsigned".into(), Value::Object(unsigned));

    let event_id = format!("${}", reference_hash(&ev));
    let json = serde_json::to_string(&Value::Object(ev)).expect("event serialises");
    BuiltEvent { event_id, json }
}

/// Serialised size of an event with empty content, so callers can pad
/// content to hit a target size. Hashes and signatures are fixed-width.
#[allow(clippy::too_many_arguments)]
pub fn overhead(
    room_id: &str,
    sender: &str,
    event_type: &str,
    state_key: Option<&str>,
    origin_server_ts: i64,
    depth: i64,
    prev_events: &[String],
    auth_events: &[String],
    local_server: &str,
) -> usize {
    let mut ev = Map::new();
    ev.insert("auth_events".into(), json!(auth_events));
    ev.insert("content".into(), json!({}));
    ev.insert("depth".into(), json!(depth));
    ev.insert("origin_server_ts".into(), json!(origin_server_ts));
    ev.insert("prev_events".into(), json!(prev_events));
    ev.insert("room_id".into(), json!(room_id));
    ev.insert("sender".into(), json!(sender));
    if let Some(sk) = state_key {
        ev.insert("state_key".into(), json!(sk));
    }
    ev.insert("type".into(), json!(event_type));
    ev.insert("hashes".into(), json!({ "sha256": "x".repeat(43) }));
    ev.insert(
        "signatures".into(),
        json!({ local_server: { "ed25519:synth": "x".repeat(86) } }),
    );
    ev.insert("unsigned".into(), json!({ "age_ts": origin_server_ts }));
    serde_json::to_vec(&Value::Object(ev))
        .map(|v| v.len())
        .unwrap_or(400)
}

/// Canonical JSON: sorted keys, no whitespace. `serde_json::Map` is a
/// BTreeMap (the `preserve_order` feature is off), so plain serialisation
/// already sorts keys. Only ASCII strings are produced, so there is no
/// escaping difference with Synapse's encoder.
fn canonical(v: &Value) -> Vec<u8> {
    serde_json::to_vec(v).expect("event serialises")
}

fn sha256_b64(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    base64::engine::general_purpose::STANDARD_NO_PAD.encode(digest)
}

/// The room-version-11 reference hash: sha256 of the canonical JSON of the
/// redacted event, minus `signatures` and `unsigned`, url-safe base64.
fn reference_hash(ev: &Map<String, Value>) -> String {
    let pruned = prune_v11(ev);
    let digest = Sha256::digest(canonical(&Value::Object(pruned)));
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(digest)
}

/// The v11 redaction algorithm, as in Synapse's `prune_event_dict`,
/// minus `signatures` and `unsigned` which the reference hash drops anyway.
fn prune_v11(ev: &Map<String, Value>) -> Map<String, Value> {
    let mut out = Map::new();
    for key in [
        "event_id",
        "sender",
        "room_id",
        "hashes",
        "type",
        "state_key",
        "depth",
        "prev_events",
        "auth_events",
        "origin_server_ts",
    ] {
        if let Some(v) = ev.get(key) {
            out.insert(key.into(), v.clone());
        }
    }
    let event_type = ev.get("type").and_then(Value::as_str).unwrap_or("");
    let content = ev
        .get("content")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let keep: &[&str] = match event_type {
        "m.room.member" => &["membership", "join_authorised_via_users_server"],
        "m.room.join_rules" => &["join_rule", "allow"],
        "m.room.power_levels" => &[
            "users",
            "users_default",
            "events",
            "events_default",
            "state_default",
            "ban",
            "kick",
            "redact",
            "invite",
        ],
        "m.room.history_visibility" => &["history_visibility"],
        "m.room.redaction" => &["redacts"],
        _ => &[],
    };
    let new_content: Map<String, Value> = if event_type == "m.room.create" {
        // v11: create event content is never redacted.
        content
    } else {
        let mut c = Map::new();
        for k in keep {
            if let Some(v) = content.get(*k) {
                c.insert((*k).into(), v.clone());
            }
        }
        if event_type == "m.room.member" {
            if let Some(signed) = content
                .get("third_party_invite")
                .and_then(|t| t.get("signed"))
            {
                c.insert(
                    "third_party_invite".into(),
                    json!({ "signed": signed.clone() }),
                );
            }
        }
        c
    };
    out.insert("content".into(), Value::Object(new_content));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_id_is_stable_and_url_safe() {
        let a = build(
            "!r:synth",
            "@u:synth",
            "m.room.message",
            None,
            json!({"msgtype": "m.text", "body": "hi"}),
            1_700_000_000_000,
            5,
            &["$prev".into()],
            &["$a".into(), "$b".into()],
            "synth",
            None,
        );
        let b = build(
            "!r:synth",
            "@u:synth",
            "m.room.message",
            None,
            json!({"msgtype": "m.text", "body": "hi"}),
            1_700_000_000_000,
            5,
            &["$prev".into()],
            &["$a".into(), "$b".into()],
            "synth",
            Some(
                json!({"invite_room_state": []})
                    .as_object()
                    .cloned()
                    .unwrap(),
            ),
        );
        // `unsigned` is not covered by the hash.
        assert_eq!(a.event_id, b.event_id);
        assert_eq!(a.event_id.len(), 44);
        assert!(
            !a.event_id.contains('+') && !a.event_id.contains('/') && !a.event_id.contains('=')
        );
        // The body is redacted away, so the ID must not depend on it...
        let c = build(
            "!r:synth",
            "@u:synth",
            "m.room.message",
            None,
            json!({"msgtype": "m.text", "body": "other"}),
            1_700_000_000_000,
            5,
            &["$prev".into()],
            &["$a".into(), "$b".into()],
            "synth",
            None,
        );
        // ...except through the content hash, which it does cover.
        assert_ne!(a.event_id, c.event_id);
        let v: Value = serde_json::from_str(&a.json).unwrap();
        assert_eq!(v["hashes"]["sha256"].as_str().unwrap().len(), 43);
        // The skeleton size is exact: the real event is the skeleton plus
        // its content.
        let o = overhead(
            "!r:synth",
            "@u:synth",
            "m.room.message",
            None,
            1_700_000_000_000,
            5,
            &["$prev".into()],
            &["$a".into(), "$b".into()],
            "synth",
        );
        assert_eq!(
            a.json.len(),
            o + r#"{"msgtype":"m.text","body":"hi"}"#.len() - 2
        );
    }
}
