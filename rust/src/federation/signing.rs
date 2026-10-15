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
 * Derived from `src/client.rs` of
 * <https://github.com/erikjohnston/matrix-hyper-federation-client>.
 *
 * [This file includes modifications made by Element Creations Ltd]
 */

//! Signing outbound federation requests with an `X-Matrix` `Authorization`
//! header.
//!
//! The signature covers the canonical JSON of
//! `{method, uri, origin, destination[, destination_is][, content]}`, as
//! `MatrixFederationHttpClient.build_auth_headers` builds it in Python. The
//! header uses the same layout and quoting as Python's.

use anyhow::{bail, Context};
use base64::{prelude::BASE64_STANDARD_NO_PAD, Engine};
use ed25519_dalek::{Signer, SigningKey};
use pyo3::{intern, prelude::*, types::PyBytes};
use serde::Serialize;

use crate::canonical_json::{to_string_canonical, to_vec_canonical, CanonicalizationOptions};

/// A homeserver's federation signing key: `hs.signing_key` in Python.
///
/// Python holds this as a `signedjson` key, which is a `nacl.signing.SigningKey`
/// with `alg` and `version` attributes. We take its 32-byte seed, so Rust
/// never reads the key file.
#[derive(Clone)]
pub struct FederationSigningKey {
    /// The key ID, e.g. `ed25519:a_AbCd`.
    key_id: String,
    key: SigningKey,
}

impl std::fmt::Debug for FederationSigningKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Never print the key itself.
        f.debug_struct("FederationSigningKey")
            .field("key_id", &self.key_id)
            .finish_non_exhaustive()
    }
}

impl FederationSigningKey {
    /// Build a key from its algorithm, version and 32-byte seed.
    pub fn new(alg: &str, version: &str, seed: &[u8]) -> anyhow::Result<Self> {
        if alg != "ed25519" {
            bail!("unsupported signing key algorithm {alg:?}");
        }

        let seed: &[u8; 32] = seed
            .try_into()
            .with_context(|| format!("ed25519 seed is {} bytes, not 32", seed.len()))?;

        Ok(FederationSigningKey {
            key_id: format!("{alg}:{version}"),
            key: SigningKey::from_bytes(seed),
        })
    }

    pub fn key_id(&self) -> &str {
        &self.key_id
    }

    /// Sign `message`, returning the unpadded base64 signature.
    pub fn sign_base64(&self, message: &[u8]) -> String {
        BASE64_STANDARD_NO_PAD.encode(self.key.sign(message).to_bytes())
    }
}

impl<'a, 'py> FromPyObject<'a, 'py> for FederationSigningKey {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let py = obj.py();
        let alg: String = obj.getattr(intern!(py, "alg"))?.extract()?;
        let version: String = obj.getattr(intern!(py, "version"))?.extract()?;
        // `nacl.signing.SigningKey.encode()` returns the seed.
        let seed = obj.call_method0(intern!(py, "encode"))?;
        let seed = seed.cast::<PyBytes>()?;

        Ok(FederationSigningKey::new(&alg, &version, seed.as_bytes())?)
    }
}

/// Who a request is for.
#[derive(Clone, Copy, Debug)]
pub enum Destination<'a> {
    /// Another homeserver. Signed as `destination`.
    Server(&'a str),
    /// An identity server. Signed as `destination_is`.
    IdentityServer(&'a str),
    /// Both, which Python allows although no caller does it.
    Both {
        server: &'a str,
        identity_server: &'a str,
    },
}

impl<'a> Destination<'a> {
    fn server(&self) -> Option<&'a str> {
        match *self {
            Destination::Server(s) | Destination::Both { server: s, .. } => Some(s),
            Destination::IdentityServer(_) => None,
        }
    }

    fn identity_server(&self) -> Option<&'a str> {
        match *self {
            Destination::IdentityServer(s)
            | Destination::Both {
                identity_server: s, ..
            } => Some(s),
            Destination::Server(_) => None,
        }
    }

    /// The value of the header's `destination` parameter: the server, else the
    /// identity server.
    fn header_value(&self) -> &'a str {
        self.server()
            .or_else(|| self.identity_server())
            .expect("a destination has one or the other")
    }
}

/// A request body, serialized as canonical JSON once and both signed and sent
/// in that form.
#[derive(Clone, Debug)]
pub struct CanonicalBody(String);

impl CanonicalBody {
    pub fn new<T: Serialize + ?Sized>(content: &T) -> anyhow::Result<Self> {
        Ok(CanonicalBody(
            to_string_canonical(content, CanonicalizationOptions::relaxed())
                .context("serializing request body")?,
        ))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn into_bytes(self) -> bytes::Bytes {
        self.0.into()
    }
}

/// The `Authorization` header value for one federation request.
///
/// `uri` is the path and query string, as sent on the request line.
pub fn make_auth_header(
    key: &FederationSigningKey,
    origin: &str,
    method: &str,
    uri: &str,
    destination: Destination<'_>,
    content: Option<&CanonicalBody>,
) -> anyhow::Result<String> {
    let message = signing_message(origin, method, uri, destination, content)?;
    let sig = key.sign_base64(&message);

    Ok(format!(
        r#"X-Matrix origin="{origin}",key="{key_id}",sig="{sig}",destination="{destination}""#,
        key_id = key.key_id(),
        destination = destination.header_value(),
    ))
}

/// The canonical JSON that the request signature covers.
///
/// Canonical JSON sorts keys, so this writes them in sorted order around the
/// already canonical body, rather than parsing the body back to embed it.
fn signing_message(
    origin: &str,
    method: &str,
    uri: &str,
    destination: Destination<'_>,
    content: Option<&CanonicalBody>,
) -> anyhow::Result<Vec<u8>> {
    let string = |s: &str| to_vec_canonical(s, CanonicalizationOptions::relaxed());

    let mut fields: Vec<(&str, Vec<u8>)> = Vec::with_capacity(6);
    if let Some(content) = content {
        fields.push(("content", content.as_str().as_bytes().to_vec()));
    }
    if let Some(server) = destination.server() {
        fields.push(("destination", string(server)?));
    }
    if let Some(identity_server) = destination.identity_server() {
        fields.push(("destination_is", string(identity_server)?));
    }
    fields.push(("method", string(method)?));
    fields.push(("origin", string(origin)?));
    fields.push(("uri", string(uri)?));

    debug_assert!(fields.windows(2).all(|w| w[0].0 < w[1].0));

    let mut out = Vec::with_capacity(256);
    out.push(b'{');
    for (i, (name, value)) in fields.iter().enumerate() {
        if i > 0 {
            out.push(b',');
        }
        out.push(b'"');
        out.extend_from_slice(name.as_bytes());
        out.extend_from_slice(b"\":");
        out.extend_from_slice(value);
    }
    out.push(b'}');

    Ok(out)
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde_json::json;

    use super::*;

    fn zero_key() -> FederationSigningKey {
        FederationSigningKey::new("ed25519", "test", &[0u8; 32]).unwrap()
    }

    /// The message equals what a generic canonical JSON serializer produces
    /// for the same object.
    #[test]
    fn signing_message_is_canonical_json() {
        let body = CanonicalBody::new(&json!({"b": [1, 2], "a": "é"})).unwrap();
        let message = signing_message(
            "origin.example",
            "PUT",
            "/_matrix/federation/v1/send/1?x=y",
            Destination::Both {
                server: "dest.example",
                identity_server: "is.example",
            },
            Some(&body),
        )
        .unwrap();

        let expected = to_vec_canonical(
            &json!({
                "method": "PUT",
                "uri": "/_matrix/federation/v1/send/1?x=y",
                "origin": "origin.example",
                "destination": "dest.example",
                "destination_is": "is.example",
                "content": {"a": "é", "b": [1, 2]},
            }),
            CanonicalizationOptions::relaxed(),
        )
        .unwrap();

        assert_eq!(
            String::from_utf8(message).unwrap(),
            String::from_utf8(expected).unwrap()
        );
    }

    // The signatures in the next two tests are the vectors from
    // matrix-hyper-federation-client, which signs the same objects. Only the
    // header layout differs, to match Python's.

    #[test]
    fn auth_header_no_content() {
        let header = make_auth_header(
            &zero_key(),
            "localhost",
            "GET",
            "/test",
            Destination::Server("matrix.org"),
            None,
        )
        .unwrap();

        assert_eq!(
            header,
            r#"X-Matrix origin="localhost",key="ed25519:test",sig="aemgn56SKst12mSbh2X0l3pBuzyWmAkURVknrTqz/ev2p8KDnKHXnFw/UsLOfwbD6V/om4Lh+DzeyE0MlJ1GBA",destination="matrix.org""#
        );
    }

    #[test]
    fn auth_header_content() {
        let mut map = BTreeMap::new();
        map.insert("foo", "bar");

        let header = make_auth_header(
            &zero_key(),
            "localhost",
            "GET",
            "/test",
            Destination::Server("matrix.org"),
            Some(&CanonicalBody::new(&map).unwrap()),
        )
        .unwrap();

        assert_eq!(
            header,
            r#"X-Matrix origin="localhost",key="ed25519:test",sig="JwOvw9q9rGU1FOX+nVqZkXL9P6WhsKE3aNV2Q+Ftj0urJHv8olv7r7gOMZM3nITm0gVwYBN8s0FBGJymeQt9DA",destination="matrix.org""#
        );
    }

    #[test]
    fn identity_server_destination() {
        let header = make_auth_header(
            &zero_key(),
            "localhost",
            "POST",
            "/_matrix/identity/v2/account/register",
            Destination::IdentityServer("is.example"),
            None,
        )
        .unwrap();

        assert!(header.ends_with(r#",destination="is.example""#));
    }

    #[test]
    fn rejects_bad_keys() {
        assert!(FederationSigningKey::new("ed25519", "a", &[0u8; 31]).is_err());
        assert!(FederationSigningKey::new("rsa", "a", &[0u8; 32]).is_err());
    }
}
