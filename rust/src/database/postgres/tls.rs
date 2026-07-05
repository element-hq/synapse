// This file is licensed under the Affero General Public License (AGPL) version 3.
//
// Copyright (C) 2026 Element Creations Ltd
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
//
// See the GNU Affero General Public License for more details:
// <https://www.gnu.org/licenses/agpl-3.0.html>.

//! TLS for the native Rust Postgres backend.
//!
//! psycopg2 hands `sslmode` / `sslrootcert` / `sslcert` / `sslkey` /
//! `sslpassword` straight to libpq, so those keys already work in a Synapse
//! `database.args`. This module reproduces libpq's behaviour on top of
//! `tokio-postgres` + `rustls`, so the same config keeps working on the Rust
//! backend.
//!
//! `tokio-postgres` only understands `sslmode` `disable`/`prefer`/`require` and
//! does no verification itself, so we drive both the handshake mode *and* the
//! verification level from here:
//!
//! | `sslmode`     | encrypt      | verify chain | verify host | verifier            |
//! |---------------|--------------|--------------|-------------|---------------------|
//! | `disable`     | no           | –            | –           | (no TLS)            |
//! | `allow`       | opportunistic| no           | no          | accept-any          |
//! | `prefer`\*    | opportunistic| no           | no          | accept-any          |
//! | `require`     | yes          | no           | no          | accept-any          |
//! | `verify-ca`   | yes          | yes          | no          | chain-only          |
//! | `verify-full` | yes          | yes          | yes         | full (WebPKI)       |
//!
//! \* libpq's default when `sslmode` is unset.
//!
//! Note that libpq's `require` encrypts *without* verifying the certificate, so
//! the accept-any verifier below is the compatible behaviour, not a shortcut.

use std::fs::File;
use std::io::BufReader;
use std::sync::Arc;

use anyhow::{anyhow, Context};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::client::WebPkiServerVerifier;
use rustls::crypto::{ring, verify_tls12_signature, verify_tls13_signature, CryptoProvider};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use rustls::{
    CertificateError, ClientConfig, DigitallySignedStruct, Error as RustlsError, RootCertStore,
    SignatureScheme,
};
use tokio_postgres::config::SslMode;
use tokio_postgres_rustls::MakeRustlsConnect;

/// The libpq TLS keywords, extracted from `database.args` before the DSN is
/// built (see `synapse.storage.rust_dbapi.split_ssl_params`), since
/// `tokio-postgres` can't parse the cert paths or the `verify-*` modes.
#[derive(Debug, Default, Clone)]
pub struct TlsParams {
    pub sslmode: Option<String>,
    pub sslrootcert: Option<String>,
    pub sslcert: Option<String>,
    pub sslkey: Option<String>,
    pub sslpassword: Option<String>,
}

/// The TLS connector to hand to [`tokio_postgres::Config::connect`].
#[derive(Clone)]
pub enum TlsConnector {
    /// `sslmode=disable`: never negotiate TLS.
    NoTls,
    /// Any other mode: negotiate TLS with the verifier chosen per `sslmode`.
    Rustls(MakeRustlsConnect),
}

/// How strictly the server certificate is checked, derived from `sslmode`.
enum Verification {
    /// Encrypt but don't verify the certificate (`prefer` / `require`).
    AcceptAny,
    /// Verify the certificate chain but not the hostname (`verify-ca`).
    ChainOnly,
    /// Verify the chain and the hostname (`verify-full`).
    Full,
}

/// Resolve [`TlsParams`] into the `tokio-postgres` [`SslMode`] to set on the
/// connection config and the [`TlsConnector`] to connect with.
pub fn build(params: &TlsParams) -> anyhow::Result<(SslMode, TlsConnector)> {
    let mode = params.sslmode.as_deref().unwrap_or("prefer");
    let (ssl_mode, verification) = match mode {
        "disable" => return Ok((SslMode::Disable, TlsConnector::NoTls)),
        // libpq's `allow` tries plaintext first then TLS; `tokio-postgres` has no
        // such mode, so map it to `prefer` (TLS first, then plaintext) — both end
        // up encrypted-or-not without verification.
        "allow" | "prefer" => (SslMode::Prefer, Verification::AcceptAny),
        "require" => (SslMode::Require, Verification::AcceptAny),
        "verify-ca" => (SslMode::Require, Verification::ChainOnly),
        "verify-full" => (SslMode::Require, Verification::Full),
        other => return Err(anyhow!("unsupported sslmode: {other:?}")),
    };

    let config = build_client_config(params, verification)?;
    Ok((
        ssl_mode,
        TlsConnector::Rustls(MakeRustlsConnect::new(config)),
    ))
}

/// Build the `rustls` client config: the verifier for the chosen level, plus a
/// client certificate for mutual TLS if `sslcert`/`sslkey` are set.
fn build_client_config(
    params: &TlsParams,
    verification: Verification,
) -> anyhow::Result<ClientConfig> {
    // Use the `ring` provider explicitly (the crate is built without the default
    // `aws-lc-rs`, which needs a C toolchain we don't have here).
    let provider = Arc::new(ring::default_provider());

    let builder = ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .context("no TLS protocol versions available")?;

    let builder = match verification {
        Verification::AcceptAny => builder
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(AcceptAnyServerCert { provider })),
        Verification::ChainOnly => {
            let verifier = WebPkiServerVerifier::builder_with_provider(
                Arc::new(root_store(params)?),
                provider,
            )
            .build()
            .context("building the certificate verifier")?;
            builder
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(NoHostnameServerCert {
                    inner: verifier,
                }))
        }
        Verification::Full => builder.with_root_certificates(root_store(params)?),
    };

    // Mutual TLS: present a client certificate if configured.
    let config = match (&params.sslcert, &params.sslkey) {
        (Some(cert), Some(key)) => builder
            .with_client_auth_cert(
                load_certs(cert)?,
                load_key(key, params.sslpassword.as_deref())?,
            )
            .context("configuring the client certificate")?,
        (None, None) => builder.with_no_client_auth(),
        _ => {
            return Err(anyhow!(
                "sslcert and sslkey must be set together for client-certificate auth"
            ))
        }
    };

    Ok(config)
}

/// The root store for chain verification: `sslrootcert` if given (a PEM CA
/// bundle), otherwise the platform's native root store — as libpq falls back to
/// the system trust store.
fn root_store(params: &TlsParams) -> anyhow::Result<RootCertStore> {
    let mut store = RootCertStore::empty();
    match &params.sslrootcert {
        Some(path) => {
            for cert in load_certs(path)? {
                store
                    .add(cert)
                    .with_context(|| format!("adding a CA certificate from {path}"))?;
            }
        }
        None => {
            let result = rustls_native_certs::load_native_certs();
            for cert in result.certs {
                // Ignore individual malformed system certs, as rustls does.
                let _ = store.add(cert);
            }
            if store.is_empty() {
                return Err(anyhow!(
                    "no usable system CA certificates found; set sslrootcert"
                ));
            }
        }
    }
    Ok(store)
}

/// Load a PEM certificate (chain) file into DER certificates.
fn load_certs(path: &str) -> anyhow::Result<Vec<CertificateDer<'static>>> {
    let mut reader = BufReader::new(File::open(path).with_context(|| format!("opening {path}"))?);
    let certs = rustls_pemfile::certs(&mut reader)
        .collect::<Result<Vec<_>, _>>()
        .with_context(|| format!("reading certificates from {path}"))?;
    if certs.is_empty() {
        return Err(anyhow!("no certificates found in {path}"));
    }
    Ok(certs)
}

/// Load a PEM private-key file. Encrypted keys (needing `sslpassword`) are not
/// supported by `rustls-pemfile`, so reject them with a clear error rather than
/// silently failing.
fn load_key(path: &str, sslpassword: Option<&str>) -> anyhow::Result<PrivateKeyDer<'static>> {
    if sslpassword.is_some() {
        return Err(anyhow!(
            "encrypted client keys (sslpassword) are not supported by the Rust driver"
        ));
    }
    let mut reader = BufReader::new(File::open(path).with_context(|| format!("opening {path}"))?);
    rustls_pemfile::private_key(&mut reader)
        .with_context(|| format!("reading a private key from {path}"))?
        .ok_or_else(|| anyhow!("no private key found in {path}"))
}

/// Accept any server certificate (encryption without verification), for
/// `sslmode=prefer`/`require`. The TLS handshake signatures are still checked by
/// the crypto provider — only the certificate's trust chain is not.
#[derive(Debug)]
struct AcceptAnyServerCert {
    provider: Arc<CryptoProvider>,
}

impl ServerCertVerifier for AcceptAnyServerCert {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, RustlsError> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, RustlsError> {
        verify_tls12_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, RustlsError> {
        verify_tls13_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}

/// Verify the certificate chain but tolerate a hostname mismatch, for
/// `sslmode=verify-ca`. Delegates everything to a [`WebPkiServerVerifier`] and
/// only maps its hostname-mismatch error to success.
#[derive(Debug)]
struct NoHostnameServerCert {
    inner: Arc<WebPkiServerVerifier>,
}

impl ServerCertVerifier for NoHostnameServerCert {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, RustlsError> {
        match self.inner.verify_server_cert(
            end_entity,
            intermediates,
            server_name,
            ocsp_response,
            now,
        ) {
            Err(RustlsError::InvalidCertificate(CertificateError::NotValidForName)) => {
                Ok(ServerCertVerified::assertion())
            }
            other => other,
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, RustlsError> {
        self.inner.verify_tls12_signature(message, cert, dss)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, RustlsError> {
        self.inner.verify_tls13_signature(message, cert, dss)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.inner.supported_verify_schemes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params(sslmode: &str) -> TlsParams {
        TlsParams {
            sslmode: Some(sslmode.to_owned()),
            ..Default::default()
        }
    }

    #[test]
    fn disable_uses_no_tls() {
        let (mode, connector) = build(&params("disable")).unwrap();
        assert_eq!(mode, SslMode::Disable);
        assert!(matches!(connector, TlsConnector::NoTls));
    }

    #[test]
    fn default_is_prefer() {
        let (mode, connector) = build(&TlsParams::default()).unwrap();
        assert_eq!(mode, SslMode::Prefer);
        assert!(matches!(connector, TlsConnector::Rustls(_)));
    }

    #[test]
    fn require_and_verify_modes_use_require_handshake() {
        for mode in ["require", "verify-ca", "verify-full"] {
            let (ssl_mode, connector) = build(&params(mode)).unwrap();
            assert_eq!(ssl_mode, SslMode::Require, "mode {mode}");
            assert!(matches!(connector, TlsConnector::Rustls(_)), "mode {mode}");
        }
    }

    #[test]
    fn unknown_sslmode_is_rejected() {
        assert!(build(&params("bogus")).is_err());
    }

    #[test]
    fn sslcert_without_sslkey_is_rejected() {
        let p = TlsParams {
            sslmode: Some("require".to_owned()),
            sslcert: Some("/tmp/cert.pem".to_owned()),
            ..Default::default()
        };
        assert!(build(&p).is_err());
    }
}
