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

//! The TLS policy for outbound federation: Python's
//! `FederationPolicyForHTTPS`.
//!
//! Certificates are checked against the platform roots, or the
//! `federation_custom_ca_list` instead when set. Verification can be turned
//! off entirely, or for hosts matching
//! `federation_certificate_verification_whitelist`. With verification off we
//! still send SNI.
//!
//! rustls has no TLS 1.0 or 1.1, so a `federation_client_minimum_tls_version`
//! below 1.2 means 1.2.

use std::sync::Arc;

use anyhow::{bail, Context};
use log::warn;
use tokio_rustls::rustls::{
    self,
    client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    crypto::{verify_tls12_signature, verify_tls13_signature, CryptoProvider},
    pki_types::{CertificateDer, ServerName, UnixTime},
    ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme,
};

use crate::config::{types::PythonRegex, TlsConfig};

pub struct TlsPolicy {
    verifying: Arc<ClientConfig>,
    not_verifying: Arc<ClientConfig>,
    verify: bool,
    verification_whitelist: Vec<PythonRegex>,
}

impl TlsPolicy {
    pub fn new(config: &TlsConfig) -> anyhow::Result<Self> {
        let provider = Arc::new(rustls::crypto::ring::default_provider());

        let versions: &[&rustls::SupportedProtocolVersion] =
            match config.federation_client_minimum_tls_version.as_str() {
                "1" | "1.1" | "1.2" => &[&rustls::version::TLS12, &rustls::version::TLS13],
                "1.3" => &[&rustls::version::TLS13],
                other => bail!("unknown federation_client_minimum_tls_version {other:?}"),
            };

        let roots = match &config.federation_custom_ca_pems {
            Some(pems) => custom_roots(pems)?,
            None => platform_roots(),
        };

        let verifying = ClientConfig::builder_with_provider(provider.clone())
            .with_protocol_versions(versions)?
            .with_root_certificates(roots)
            .with_no_client_auth();

        let not_verifying = ClientConfig::builder_with_provider(provider.clone())
            .with_protocol_versions(versions)?
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(NoVerification(provider)))
            .with_no_client_auth();

        Ok(TlsPolicy {
            verifying: Arc::new(verifying),
            not_verifying: Arc::new(not_verifying),
            verify: config.federation_verify_certificates,
            verification_whitelist: config.federation_certificate_verification_whitelist.clone(),
        })
    }

    /// The client config to connect to `host` with. `host` is the name we
    /// verify the certificate against: the server name, or the delegated
    /// name after a `.well-known` lookup, never the SRV target.
    pub fn config_for(&self, host: &str) -> Arc<ClientConfig> {
        let verify = self.verify
            && !self
                .verification_whitelist
                .iter()
                .any(|regex| regex.is_match_at_start(host));

        if verify {
            self.verifying.clone()
        } else {
            self.not_verifying.clone()
        }
    }
}

/// The name to verify (and send as SNI) for `host`. IP literals verify
/// against the certificate's IP addresses and send no SNI.
pub fn server_name(host: &str) -> anyhow::Result<ServerName<'static>> {
    let bare = host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host);
    ServerName::try_from(bare.to_owned())
        .with_context(|| format!("invalid TLS server name {host:?}"))
}

fn custom_roots(pems: &[String]) -> anyhow::Result<RootCertStore> {
    let mut roots = RootCertStore::empty();
    for pem in pems {
        for cert in rustls_pemfile::certs(&mut pem.as_bytes()) {
            let cert = cert.context("parsing federation_custom_ca_list")?;
            roots
                .add(cert)
                .context("adding a federation_custom_ca_list certificate")?;
        }
    }
    if roots.is_empty() {
        bail!("federation_custom_ca_list contains no certificates");
    }
    Ok(roots)
}

fn platform_roots() -> RootCertStore {
    let mut roots = RootCertStore::empty();
    let loaded = rustls_native_certs::load_native_certs();
    for err in &loaded.errors {
        warn!("Error loading platform TLS roots: {err}");
    }
    let (added, ignored) = roots.add_parsable_certificates(loaded.certs);
    if ignored > 0 {
        warn!("Ignored {ignored} unparsable platform TLS roots ({added} loaded)");
    }
    roots
}

/// Accepts any certificate. The handshake signatures are still checked,
/// since they are part of TLS itself rather than of trusting the peer.
#[derive(Debug)]
struct NoVerification(Arc<CryptoProvider>);

impl ServerCertVerifier for NoVerification {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(verify: bool, whitelist: &[&str]) -> TlsConfig {
        TlsConfig {
            federation_verify_certificates: verify,
            federation_client_minimum_tls_version: "1".to_owned(),
            federation_certificate_verification_whitelist: whitelist
                .iter()
                .map(|p| PythonRegex::from_python_pattern(p, true).unwrap())
                .collect(),
            federation_custom_ca_pems: None,
        }
    }

    #[test]
    fn whitelist_turns_off_verification_per_host() {
        let policy = TlsPolicy::new(&config(true, &[r"\A(.{0,}\.insecure\.example)\Z"])).unwrap();
        assert!(Arc::ptr_eq(
            &policy.config_for("matrix.org"),
            &policy.verifying
        ));
        assert!(Arc::ptr_eq(
            &policy.config_for("a.insecure.example"),
            &policy.not_verifying
        ));

        let policy = TlsPolicy::new(&config(false, &[])).unwrap();
        assert!(Arc::ptr_eq(
            &policy.config_for("matrix.org"),
            &policy.not_verifying
        ));
    }

    #[test]
    fn minimum_version() {
        let mut c = config(true, &[]);
        c.federation_client_minimum_tls_version = "1.3".to_owned();
        assert!(TlsPolicy::new(&c).is_ok());
        c.federation_client_minimum_tls_version = "2".to_owned();
        assert!(TlsPolicy::new(&c).is_err());
    }

    #[test]
    fn server_names() {
        assert!(matches!(
            server_name("example.com").unwrap(),
            ServerName::DnsName(_)
        ));
        assert!(matches!(
            server_name("1.2.3.4").unwrap(),
            ServerName::IpAddress(_)
        ));
        assert!(matches!(
            server_name("[::1]").unwrap(),
            ServerName::IpAddress(_)
        ));
    }

    #[test]
    fn custom_ca_list_must_hold_certificates() {
        let mut c = config(true, &[]);
        c.federation_custom_ca_pems = Some(vec!["not a pem".to_owned()]);
        assert!(TlsPolicy::new(&c).is_err());
    }
}
