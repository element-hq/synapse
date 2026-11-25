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

//! The X-Matrix `Authorization` header.

use std::fmt;

use base64::{engine::general_purpose::STANDARD_NO_PAD, Engine as _};
use ed25519_dalek::{Signer as _, SigningKey};
use http::HeaderValue;
use serde::Serialize;

use crate::canonical_json::{to_vec_canonical, CanonicalizationOptions};

/// Signs federation requests as one server with one key.
pub struct RequestSigner {
    key: SigningKey,
    origin: String,
    key_id: String,
}

/// What a request signature covers.
pub struct SigningInput<'a> {
    pub method: &'a str,
    pub uri: &'a str,
    pub destination: Option<&'a str>,
    pub destination_is: Option<&'a str>,
    /// Canonical JSON from Python's encoder, spliced into the signed object
    /// unchanged.
    pub content: Option<&'a [u8]>,
}

/// The signed request object apart from `content`, which sorts before every
/// other key and so is spliced in at the front.
#[derive(Serialize)]
struct SignedFields<'a> {
    #[serde(skip_serializing_if = "Option::is_none")]
    destination: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    destination_is: Option<&'a str>,
    method: &'a str,
    origin: &'a str,
    uri: &'a str,
}

impl RequestSigner {
    /// `origin` is our server name; `seed` is the 32-byte Ed25519 seed of the
    /// key named `key_id`.
    pub fn new(origin: String, key_id: String, seed: &[u8]) -> Result<Self, SigningKeyError> {
        let key = SigningKey::from_bytes(seed.try_into().map_err(|_| SigningKeyError)?);
        Ok(RequestSigner {
            key,
            origin,
            key_id,
        })
    }

    /// The `Authorization` header value.
    ///
    /// Exactly one of `destination` and `destination_is` must be set and
    /// non-empty. Its value goes in the header's `destination` parameter.
    pub fn authorization(&self, input: &SigningInput<'_>) -> Result<HeaderValue, SigningError> {
        let header_destination = match (input.destination, input.destination_is) {
            (Some(d), None) if !d.is_empty() => d,
            (None, Some(d)) if !d.is_empty() => d,
            _ => return Err(SigningError::NoDestination),
        };

        let fields = to_vec_canonical(
            &SignedFields {
                destination: input.destination,
                destination_is: input.destination_is,
                method: input.method,
                origin: &self.origin,
                uri: input.uri,
            },
            CanonicalizationOptions::strict(),
        )
        .expect("a struct of strings always serializes");

        // Temporary, until request bodies are canonicalized in Rust.
        let message = match input.content {
            Some(content) => [b"{\"content\":".as_slice(), content, b",", &fields[1..]].concat(),
            None => fields,
        };

        let header = format!(
            "X-Matrix origin=\"{}\",key=\"{}\",sig=\"{}\",destination=\"{}\"",
            self.origin,
            self.key_id,
            self.sign(&message),
            header_destination,
        );
        // ASCII only, as the Python client always sent. `HeaderValue` refuses
        // every other control character but allows tab, which no field holds.
        if !header.is_ascii() || header.contains('\t') {
            return Err(SigningError::InvalidHeader);
        }
        HeaderValue::from_str(&header).map_err(|_| SigningError::InvalidHeader)
    }

    fn sign(&self, message: &[u8]) -> String {
        STANDARD_NO_PAD.encode(self.key.sign(message).to_bytes())
    }
}

#[derive(Debug)]
pub struct SigningKeyError;

/// Why a request could not be signed.
#[derive(Debug, PartialEq, Eq)]
pub enum SigningError {
    /// Not exactly one of `destination` and `destination_is` is set and
    /// non-empty.
    NoDestination,
    /// A field holds a byte that is not printable ASCII.
    InvalidHeader,
}

impl fmt::Display for SigningKeyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("invalid Ed25519 seed")
    }
}

impl std::error::Error for SigningKeyError {}

impl fmt::Display for SigningError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SigningError::NoDestination => f.write_str(
                "Exactly one of the arguments destination and destination_is must be a nonempty string.",
            ),
            SigningError::InvalidHeader => f.write_str("not a valid header value"),
        }
    }
}

impl std::error::Error for SigningError {}

#[cfg(test)]
mod tests {
    use super::*;

    /// `YJDBA9Xnr2sVqXD9Vj7XVUnmFZcZrlw8Md7kMW+3XA1`, the seed in the spec's
    /// [test vectors](https://spec.matrix.org/latest/appendices/#signing-key).
    const SPEC_SEED: [u8; 32] = [
        0x60, 0x90, 0xc1, 0x03, 0xd5, 0xe7, 0xaf, 0x6b, 0x15, 0xa9, 0x70, 0xfd, 0x56, 0x3e, 0xd7,
        0x55, 0x49, 0xe6, 0x15, 0x97, 0x19, 0xae, 0x5c, 0x3c, 0x31, 0xde, 0xe4, 0x31, 0x6f, 0xb7,
        0x5c, 0x0d,
    ];

    fn spec_signer() -> RequestSigner {
        RequestSigner::new("domain".to_owned(), "ed25519:1".to_owned(), &SPEC_SEED).unwrap()
    }

    fn input(content: Option<&[u8]>) -> SigningInput<'_> {
        SigningInput {
            method: "PUT",
            uri: "/_matrix/federation/v1/send/1",
            destination: Some("remote"),
            destination_is: None,
            content,
        }
    }

    /// Spec: [JSON signing](https://spec.matrix.org/latest/appendices/#json-signing).
    #[test]
    fn spec_json_signing_vectors() {
        let signer = spec_signer();
        assert_eq!(
            signer.sign(b"{}"),
            "K8280/U9SSy9IVtjBuVeLr+HpOB4BQFWbg+UZaADMtTdGYI7Geitb76LTrr5QV/7Xg4ahLwYGYZzuHGZKM5ZAQ"
        );
        assert_eq!(
            signer.sign(br#"{"one":1,"two":"Two"}"#),
            "KqmLSbO39/Bzb0QIYE82zqLwsA+PDzYIpIRA2sRQ4sL53+sN6/fpNSoqE7BP7vBZhG6kYdD13EIMJpvhJI+6Bw"
        );
    }

    /// Spec: [request authentication](https://spec.matrix.org/latest/server-server-api/#request-authentication).
    /// Expected signatures are from Python's `signedjson.sign_json` on the same
    /// request object.
    #[test]
    fn headers_match_signedjson() {
        let signer = spec_signer();
        let header = |sig: &str| {
            format!(r#"X-Matrix origin="domain",key="ed25519:1",sig="{sig}",destination="remote""#)
        };
        assert_eq!(
            signer.authorization(&input(None)).unwrap(),
            header("jnAMm2rg4WxdeDnYU1EU6JfwIUOeM073o/6jEjrqYfXuzuh4Zc76ftrPbMCbFgLlKPHl8GTdP63APAvwYomCBQ")
        );
        assert_eq!(
            signer
                .authorization(&input(Some(b"{\"pdus\":[],\"z\":\"\xc3\xa9\"}")))
                .unwrap(),
            header("3K/ej34O3jNQKn6cCRn4MXUviETWLh1l51krvCXUSK95HxXcFt0WARqOiC1XCAUWSQkevUCqYj2qBD1F36FCCw")
        );
    }

    /// An identity server is named by `destination_is`.
    #[test]
    fn destination_is_named() {
        let signer = spec_signer();
        let header = signer
            .authorization(&SigningInput {
                destination: None,
                destination_is: Some("id.example"),
                ..input(None)
            })
            .unwrap();
        assert_eq!(
            header,
            r#"X-Matrix origin="domain",key="ed25519:1",sig="SRa3wUd9qsrDCqI3zoYKh7wk6GLRDOsFg82BmurUBYZGMyWU8GjfPMl9Tt/tKijURGqqWEY5jS/wa8Ajn3gODQ",destination="id.example""#
        );
    }

    #[test]
    fn not_exactly_one_destination() {
        let signer = spec_signer();
        for (destination, destination_is) in [
            (None, None),
            (Some(""), None),
            (None, Some("")),
            (Some("remote"), Some("id.example")),
            (Some(""), Some("id.example")),
        ] {
            let result = signer.authorization(&SigningInput {
                destination,
                destination_is,
                ..input(None)
            });
            assert_eq!(result.unwrap_err(), SigningError::NoDestination);
        }
    }

    #[test]
    fn only_printable_ascii_is_accepted() {
        let signer = spec_signer();
        for destination in ["remote\r\nX-Injected: 1", "remote\t", "r\u{e9}mote"] {
            let result = signer.authorization(&SigningInput {
                destination: Some(destination),
                ..input(None)
            });
            assert_eq!(result.unwrap_err(), SigningError::InvalidHeader);
        }
    }
}
