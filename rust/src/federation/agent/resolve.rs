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
 * Derived from `src/server_resolver.rs` of
 * <https://github.com/erikjohnston/matrix-hyper-federation-client>.
 *
 * [This file includes modifications made by Element Creations Ltd]
 */

//! Resolving a Matrix server name to the hosts and ports to connect to.
//!
//! This is `MatrixHostnameEndpoint._resolve_server` and `SrvResolver` from
//! Python. The `.well-known` lookup happens before this, in
//! [`super::well_known`], because it rewrites the request's `Host` header.

use std::{
    collections::{BTreeMap, HashMap},
    net::IpAddr,
    sync::{Arc, Mutex},
};

use anyhow::anyhow;
use async_trait::async_trait;
use log::{debug, warn};
use rand::{seq::SliceRandom, Rng, RngExt};

use crate::federation::transport::TransportError;

/// The port federation uses when a server name gives none and has no SRV
/// record.
pub const DEFAULT_FEDERATION_PORT: u16 = 8448;

/// One SRV record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SrvRecord {
    pub priority: u16,
    pub weight: u16,
    pub port: u16,
    /// The target host, without a trailing dot. `.` for the root, which a
    /// lone record uses to say the service is unavailable.
    pub target: String,
    /// The record's TTL, in seconds.
    pub ttl: u32,
}

/// Why a DNS lookup found nothing.
#[derive(Debug)]
pub enum DnsError {
    /// The name does not exist, or has no records of this type.
    NotFound,
    /// The lookup itself failed (no answer, server failure, and so on).
    Failed(anyhow::Error),
}

/// The DNS lookups the agent makes.
#[async_trait]
pub trait DnsResolver: Send + Sync + 'static {
    async fn lookup_srv(&self, name: &str) -> Result<Vec<SrvRecord>, DnsError>;

    /// The addresses of `host`. An IP literal resolves to itself.
    async fn lookup_ip(&self, host: &str) -> Result<Vec<IpAddr>, DnsError>;
}

/// The production resolver. SRV records come from hickory, which reads the
/// system's `resolv.conf` as Twisted's DNS client does. Addresses come from
/// the system resolver (`getaddrinfo`), as Twisted's `HostnameEndpoint` uses,
/// so `/etc/hosts` and `nsswitch.conf` apply.
pub struct SystemResolver {
    hickory: hickory_resolver::TokioResolver,
}

impl SystemResolver {
    pub fn new() -> anyhow::Result<Self> {
        let hickory = hickory_resolver::TokioResolver::builder_tokio()?.build();
        Ok(SystemResolver { hickory })
    }
}

#[async_trait]
impl DnsResolver for SystemResolver {
    async fn lookup_srv(&self, name: &str) -> Result<Vec<SrvRecord>, DnsError> {
        let lookup = match self.hickory.srv_lookup(name).await {
            Ok(lookup) => lookup,
            Err(err) if err.is_no_records_found() => return Err(DnsError::NotFound),
            Err(err) => return Err(DnsError::Failed(err.into())),
        };

        let records = lookup
            .as_lookup()
            .record_iter()
            .filter_map(|record| {
                let srv = record.data().as_srv()?;
                let target = srv.target();
                let target = if target.is_root() {
                    ".".to_owned()
                } else {
                    target.to_ascii().trim_end_matches('.').to_owned()
                };
                Some(SrvRecord {
                    priority: srv.priority(),
                    weight: srv.weight(),
                    port: srv.port(),
                    target,
                    ttl: record.ttl(),
                })
            })
            .collect();

        Ok(records)
    }

    async fn lookup_ip(&self, host: &str) -> Result<Vec<IpAddr>, DnsError> {
        if let Some(ip) = parse_ip_literal(host) {
            return Ok(vec![ip]);
        }

        // `lookup_host` wants a port, which it ignores for our purposes.
        let addrs = tokio::net::lookup_host((host, 0)).await.map_err(|err| {
            // getaddrinfo's errors do not tell "no such name" apart from a
            // failed lookup in a way std exposes, so treat all as failures.
            DnsError::Failed(anyhow!(err).context(format!("resolving {host}")))
        })?;

        let ips: Vec<IpAddr> = addrs.map(|addr| addr.ip()).collect();
        if ips.is_empty() {
            return Err(DnsError::NotFound);
        }
        Ok(ips)
    }
}

/// A resolver that answers from fixed tables, for tests.
#[derive(Default)]
pub struct InMemoryResolver {
    srv: Mutex<HashMap<String, Result<Vec<SrvRecord>, String>>>,
    ips: Mutex<HashMap<String, Vec<IpAddr>>>,
}

impl InMemoryResolver {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn add_srv(&self, name: &str, records: Vec<SrvRecord>) {
        self.srv
            .lock()
            .expect("poisoned")
            .insert(name.to_owned(), Ok(records));
    }

    /// Make SRV lookups of `name` fail (rather than find nothing).
    pub fn fail_srv(&self, name: &str, message: &str) {
        self.srv
            .lock()
            .expect("poisoned")
            .insert(name.to_owned(), Err(message.to_owned()));
    }

    pub fn add_host(&self, host: &str, ips: Vec<IpAddr>) {
        self.ips
            .lock()
            .expect("poisoned")
            .insert(host.to_owned(), ips);
    }
}

#[async_trait]
impl DnsResolver for InMemoryResolver {
    async fn lookup_srv(&self, name: &str) -> Result<Vec<SrvRecord>, DnsError> {
        match self.srv.lock().expect("poisoned").get(name) {
            Some(Ok(records)) if !records.is_empty() => Ok(records.clone()),
            Some(Ok(_)) | None => Err(DnsError::NotFound),
            Some(Err(message)) => Err(DnsError::Failed(anyhow!("{message}"))),
        }
    }

    async fn lookup_ip(&self, host: &str) -> Result<Vec<IpAddr>, DnsError> {
        if let Some(ip) = parse_ip_literal(host) {
            return Ok(vec![ip]);
        }
        match self.ips.lock().expect("poisoned").get(host) {
            Some(ips) if !ips.is_empty() => Ok(ips.clone()),
            _ => Err(DnsError::NotFound),
        }
    }
}

/// Parse an IP literal, as a URI host gives it (IPv6 in brackets) or bare.
pub fn parse_ip_literal(host: &str) -> Option<IpAddr> {
    host.strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host)
        .parse()
        .ok()
}

/// A host and port to try connecting to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Server {
    pub host: String,
    pub port: u16,
}

/// A cached SRV record, with the time it expires in seconds since the epoch.
#[derive(Clone, Debug)]
struct CachedRecord {
    record: SrvRecord,
    expires: u64,
}

/// Resolves server names via SRV, caching the answers.
///
/// The cache follows Python's `SrvResolver`: a cached answer is used only
/// while all its records are unexpired, and when a refetch fails the stale
/// answer is used instead. The cache belongs to the agent, where Python's is
/// module-global.
pub struct ServerResolver {
    dns: Arc<dyn DnsResolver>,
    cache: Mutex<HashMap<String, Vec<CachedRecord>>>,
}

impl ServerResolver {
    pub fn new(dns: Arc<dyn DnsResolver>) -> Self {
        ServerResolver {
            dns,
            cache: Mutex::new(HashMap::new()),
        }
    }

    pub fn dns(&self) -> &Arc<dyn DnsResolver> {
        &self.dns
    }

    /// The servers to try for `host` and optional `port`, in order.
    ///
    /// An IP literal or an explicit port bypasses SRV. Otherwise we look up
    /// `_matrix-fed._tcp`, then the deprecated `_matrix._tcp`, and fall back
    /// to `host:8448`.
    pub async fn resolve_server(
        &self,
        host: &str,
        port: Option<u16>,
        now_secs: u64,
    ) -> Result<Vec<Server>, TransportError> {
        if port.is_some() || parse_ip_literal(host).is_some() {
            return Ok(vec![Server {
                host: host.to_owned(),
                port: port.unwrap_or(DEFAULT_FEDERATION_PORT),
            }]);
        }

        for service in ["_matrix-fed._tcp", "_matrix._tcp"] {
            let servers = self
                .resolve_service(&format!("{service}.{host}"), now_secs)
                .await?;
            if !servers.is_empty() {
                debug!("Got {servers:?} from {service} SRV lookup for {host}");
                return Ok(servers);
            }
        }

        debug!("No SRV records for {host}");
        Ok(vec![Server {
            host: host.to_owned(),
            port: DEFAULT_FEDERATION_PORT,
        }])
    }

    /// Look up one SRV name, returning the servers in RFC 2782 order, or
    /// nothing if there are no records.
    async fn resolve_service(
        &self,
        service_name: &str,
        now_secs: u64,
    ) -> Result<Vec<Server>, TransportError> {
        let cached = self
            .cache
            .lock()
            .expect("poisoned")
            .get(service_name)
            .cloned();

        // An empty cached answer is refetched, as in Python.
        if let Some(cached) = &cached {
            if !cached.is_empty() && cached.iter().all(|c| c.expires > now_secs) {
                let records = cached.iter().map(|c| c.record.clone()).collect();
                return Ok(sort_srv_records(records, &mut rand::rng()));
            }
        }

        let records = match self.dns.lookup_srv(service_name).await {
            Ok(records) => records,
            Err(DnsError::NotFound) => return Ok(Vec::new()),
            Err(DnsError::Failed(err)) => {
                return match cached {
                    Some(cached) if !cached.is_empty() => {
                        warn!("Failed to resolve {service_name}, falling back to cache: {err:#}");
                        let records = cached.into_iter().map(|c| c.record).collect();
                        Ok(sort_srv_records(records, &mut rand::rng()))
                    }
                    _ => Err(TransportError::Dns(
                        err.context(format!("SRV lookup of {service_name}")),
                    )),
                };
            }
        };

        if let [only] = records.as_slice() {
            if only.target == "." {
                return Err(TransportError::ServiceUnavailable(service_name.to_owned()));
            }
        }

        let cached = records
            .iter()
            .map(|record| CachedRecord {
                record: record.clone(),
                expires: now_secs + u64::from(record.ttl),
            })
            .collect();
        self.cache
            .lock()
            .expect("poisoned")
            .insert(service_name.to_owned(), cached);

        Ok(sort_srv_records(records, &mut rand::rng()))
    }
}

/// Order SRV records as Python's `_sort_server_list` does: by priority, and
/// within a priority by a weighted random pick (RFC 2782, without its
/// off-by-one). Zero-weight records left over are shuffled onto the end.
pub fn sort_srv_records<R: Rng + ?Sized>(records: Vec<SrvRecord>, rng: &mut R) -> Vec<Server> {
    let mut by_priority: BTreeMap<u16, Vec<SrvRecord>> = BTreeMap::new();
    for record in records {
        by_priority.entry(record.priority).or_default().push(record);
    }

    let mut results = Vec::new();
    for (_, mut records) in by_priority {
        let mut total_weight: u32 = records.iter().map(|r| u32::from(r.weight)).sum();

        while !records.is_empty() && total_weight > 0 {
            let mut target = rng.random_range(1..=total_weight);
            let mut chosen = records.len() - 1;
            for (i, record) in records.iter().enumerate() {
                target = target.saturating_sub(u32::from(record.weight));
                if target == 0 {
                    chosen = i;
                    break;
                }
            }
            let record = records.remove(chosen);
            total_weight -= u32::from(record.weight);
            results.push(record);
        }

        records.shuffle(rng);
        results.extend(records);
    }

    results
        .into_iter()
        .map(|record| Server {
            host: record.target,
            port: record.port,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use rand::{rngs::StdRng, SeedableRng};

    use super::*;

    fn srv(priority: u16, weight: u16, target: &str, port: u16, ttl: u32) -> SrvRecord {
        SrvRecord {
            priority,
            weight,
            port,
            target: target.to_owned(),
            ttl,
        }
    }

    fn server(host: &str, port: u16) -> Server {
        Server {
            host: host.to_owned(),
            port,
        }
    }

    #[test]
    fn sort_orders_by_priority() {
        let mut rng = StdRng::seed_from_u64(1);
        let sorted = sort_srv_records(
            vec![
                srv(10, 0, "c", 1, 0),
                srv(0, 0, "a", 1, 0),
                srv(5, 0, "b", 1, 0),
            ],
            &mut rng,
        );
        let hosts: Vec<_> = sorted.iter().map(|s| s.host.as_str()).collect();
        assert_eq!(hosts, ["a", "b", "c"]);
    }

    /// Within a priority, a record is picked first in proportion to its
    /// weight, and zero-weight records come last.
    #[test]
    fn sort_weights_within_a_priority() {
        let mut rng = StdRng::seed_from_u64(2);
        let mut heavy_first = 0;
        for _ in 0..1000 {
            let sorted = sort_srv_records(
                vec![
                    srv(0, 0, "zero", 1, 0),
                    srv(0, 1, "light", 1, 0),
                    srv(0, 9, "heavy", 1, 0),
                ],
                &mut rng,
            );
            assert_eq!(sorted.len(), 3);
            assert_eq!(sorted[2].host, "zero");
            if sorted[0].host == "heavy" {
                heavy_first += 1;
            }
        }
        // Expect ~900.
        assert!((850..950).contains(&heavy_first), "{heavy_first}");
    }

    #[test]
    fn sort_all_zero_weights_keeps_everything() {
        let mut rng = StdRng::seed_from_u64(3);
        let mut sorted =
            sort_srv_records(vec![srv(0, 0, "a", 1, 0), srv(0, 0, "b", 1, 0)], &mut rng);
        sorted.sort_by(|a, b| a.host.cmp(&b.host));
        assert_eq!(sorted, [server("a", 1), server("b", 1)]);
    }

    fn resolver() -> (Arc<InMemoryResolver>, ServerResolver) {
        let dns = Arc::new(InMemoryResolver::new());
        let resolver = ServerResolver::new(dns.clone());
        (dns, resolver)
    }

    #[tokio::test]
    async fn literal_or_port_bypasses_srv() {
        let (dns, resolver) = resolver();
        dns.add_srv(
            "_matrix-fed._tcp.example.com",
            vec![srv(0, 0, "other", 1, 60)],
        );

        assert_eq!(
            resolver
                .resolve_server("example.com", Some(1234), 0)
                .await
                .unwrap(),
            [server("example.com", 1234)]
        );
        assert_eq!(
            resolver.resolve_server("1.2.3.4", None, 0).await.unwrap(),
            [server("1.2.3.4", 8448)]
        );
        assert_eq!(
            resolver.resolve_server("[::1]", None, 0).await.unwrap(),
            [server("[::1]", 8448)]
        );
    }

    #[tokio::test]
    async fn srv_lookup_order() {
        let (dns, resolver) = resolver();

        // Nothing: host and the default port.
        assert_eq!(
            resolver
                .resolve_server("example.com", None, 0)
                .await
                .unwrap(),
            [server("example.com", 8448)]
        );

        // The deprecated record is used when it is the only one.
        dns.add_srv("_matrix._tcp.example.com", vec![srv(0, 0, "old", 1111, 60)]);
        assert_eq!(
            resolver
                .resolve_server("example.com", None, 0)
                .await
                .unwrap(),
            [server("old", 1111)]
        );

        // `_matrix-fed._tcp` wins over it.
        dns.add_srv(
            "_matrix-fed._tcp.example.com",
            vec![srv(0, 0, "new", 2222, 60)],
        );
        assert_eq!(
            resolver
                .resolve_server("example.com", None, 0)
                .await
                .unwrap(),
            [server("new", 2222)]
        );
    }

    #[tokio::test]
    async fn lone_root_target_is_unavailable() {
        let (dns, resolver) = resolver();
        dns.add_srv("_matrix-fed._tcp.example.com", vec![srv(0, 0, ".", 0, 60)]);

        let err = resolver
            .resolve_server("example.com", None, 0)
            .await
            .unwrap_err();
        assert!(
            matches!(err, TransportError::ServiceUnavailable(_)),
            "{err}"
        );
    }

    #[tokio::test]
    async fn cache_and_stale_fallback() {
        let (dns, resolver) = resolver();
        let name = "_matrix-fed._tcp.example.com";
        dns.add_srv(name, vec![srv(0, 0, "first", 1, 60)]);

        assert_eq!(
            resolver
                .resolve_server("example.com", None, 1000)
                .await
                .unwrap(),
            [server("first", 1)]
        );

        // Still cached: a changed answer is not seen.
        dns.add_srv(name, vec![srv(0, 0, "second", 2, 60)]);
        assert_eq!(
            resolver
                .resolve_server("example.com", None, 1059)
                .await
                .unwrap(),
            [server("first", 1)]
        );

        // Expired: refetched.
        assert_eq!(
            resolver
                .resolve_server("example.com", None, 1060)
                .await
                .unwrap(),
            [server("second", 2)]
        );

        // Expired and the lookup fails: the stale answer is used.
        dns.fail_srv(name, "SERVFAIL");
        assert_eq!(
            resolver
                .resolve_server("example.com", None, 5000)
                .await
                .unwrap(),
            [server("second", 2)]
        );

        // A failure with nothing cached is a DNS error.
        dns.fail_srv("_matrix-fed._tcp.other.com", "SERVFAIL");
        let err = resolver
            .resolve_server("other.com", None, 0)
            .await
            .unwrap_err();
        assert!(err.is_dns(), "{err}");
    }
}
