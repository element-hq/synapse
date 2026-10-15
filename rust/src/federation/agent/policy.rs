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

//! Which addresses outbound federation may connect to, and through which
//! proxy.

use std::{net::IpAddr, sync::Arc};

use anyhow::{anyhow, bail, Context};
use base64::{prelude::BASE64_STANDARD, Engine};
use log::info;

use super::resolve::{parse_ip_literal, DnsError, DnsResolver};
use crate::{
    config::types::{IpRangeSet, ProxyConfig},
    federation::transport::TransportError,
};

/// The IP range block and allow lists, applied to every address we connect
/// to. This is Python's `_IPBlockingResolver` (via
/// `BlocklistingReactorWrapper`).
#[derive(Clone)]
pub struct IpPolicy {
    blocklist: IpRangeSet,
    allowlist: Option<IpRangeSet>,
}

impl IpPolicy {
    pub fn new(blocklist: IpRangeSet, allowlist: Option<IpRangeSet>) -> Self {
        IpPolicy {
            blocklist,
            allowlist,
        }
    }

    /// Blocked if in the blocklist and not in the allowlist.
    pub fn is_blocked(&self, ip: &IpAddr) -> bool {
        self.blocklist.contains(ip) && !self.allowlist.as_ref().is_some_and(|a| a.contains(ip))
    }

    /// Whether an IP literal is blocked by the blocklist alone, ignoring the
    /// allowlist. This is how Python's `BlocklistingAgentWrapper` treats the
    /// `.well-known` fetch, since it is not given the allowlist.
    pub fn is_blocked_ignoring_allowlist(&self, ip: &IpAddr) -> bool {
        self.blocklist.contains(ip)
    }

    /// Resolve `host` to the addresses we may connect to.
    ///
    /// If any address is blocked, none are returned: an attacker controlling
    /// DNS could otherwise mix a blocked address in with good ones (DNS
    /// rebinding). Python reports this as a failed DNS lookup, and so do we.
    pub async fn resolve(
        &self,
        dns: &dyn DnsResolver,
        host: &str,
    ) -> Result<Vec<IpAddr>, TransportError> {
        let ips = match dns.lookup_ip(host).await {
            Ok(ips) => ips,
            Err(DnsError::NotFound) => {
                return Err(TransportError::Dns(anyhow!("no addresses for {host}")))
            }
            Err(DnsError::Failed(err)) => return Err(TransportError::Dns(err)),
        };

        if let Some(blocked) = ips.iter().find(|ip| self.is_blocked(ip)) {
            info!("Blocked {blocked} from DNS resolution to {host}");
            return Err(TransportError::Dns(anyhow!(
                "{host} resolved to blocked address {blocked}"
            )));
        }

        Ok(ips)
    }
}

/// An `https_proxy` that federation connections tunnel through with
/// `CONNECT`, unless `no_proxy` exempts the host.
#[derive(Clone, Debug)]
pub struct Proxy {
    /// Whether to speak TLS to the proxy itself.
    pub tls: bool,
    pub host: String,
    pub port: u16,
    /// The `Proxy-Authorization` header value, from credentials in the
    /// proxy URL.
    pub authorization: Option<String>,
    no_proxy: Arc<NoProxy>,
}

impl Proxy {
    /// The proxy to use, if `https_proxy` is set. Parses the URL the way
    /// Python's `http_proxy_endpoint` does: scheme `http` (the default) or
    /// `https`, port defaulting to 1080, optional `user:password@`.
    pub fn from_config(config: &ProxyConfig) -> anyhow::Result<Option<Self>> {
        let Some(https_proxy) = config.https_proxy.as_deref().filter(|p| !p.is_empty()) else {
            return Ok(None);
        };

        let with_scheme = if https_proxy.contains("://") {
            https_proxy.to_owned()
        } else {
            format!("http://{https_proxy}")
        };
        let url = url::Url::parse(&with_scheme)
            .with_context(|| format!("parsing https_proxy {https_proxy:?}"))?;

        let tls = match url.scheme() {
            "http" => false,
            "https" => true,
            other => bail!("unsupported https_proxy scheme {other:?}"),
        };
        let host = url
            .host_str()
            .with_context(|| format!("https_proxy {https_proxy:?} has no host"))?
            .to_owned();
        let port = url.port().unwrap_or(1080);

        // Credentials are only used when both parts are present, and are
        // sent as written in the URL (Python does not percent-decode them).
        let authorization = match (url.username(), url.password()) {
            (user, Some(password)) if !user.is_empty() && !password.is_empty() => Some(format!(
                "Basic {}",
                BASE64_STANDARD.encode(format!("{user}:{password}"))
            )),
            _ => None,
        };

        Ok(Some(Proxy {
            tls,
            host,
            port,
            authorization,
            no_proxy: Arc::new(NoProxy::new(
                config.no_proxy_hosts.as_deref().unwrap_or(&[]),
            )),
        }))
    }

    /// Whether a connection to `host` should go through this proxy.
    pub fn applies_to(&self, host: &str) -> bool {
        !self.no_proxy.bypasses(host)
    }
}

/// The `no_proxy` host list, matched as Python's
/// `urllib.request.proxy_bypass_environment` does: `*` exempts everything,
/// otherwise an entry matches the host itself and its subdomains. CIDR
/// entries are only compared as strings, as in Python.
#[derive(Debug)]
pub struct NoProxy {
    all: bool,
    names: Vec<String>,
}

impl NoProxy {
    pub fn new(hosts: &[String]) -> Self {
        // Python joins the list with commas and compares the whole string
        // to `*`.
        let all = hosts.len() == 1 && hosts[0] == "*";
        let names = hosts
            .iter()
            .flat_map(|h| h.split(','))
            .map(|name| name.trim().trim_start_matches('.').to_lowercase())
            .filter(|name| !name.is_empty())
            .collect();
        NoProxy { all, names }
    }

    pub fn bypasses(&self, host: &str) -> bool {
        if self.all {
            return true;
        }

        let host = host.to_lowercase();
        // Python splits a trailing `:port` off; our hosts carry no port, but
        // an IPv6 literal must not be mistaken for one.
        let host_only = match host.rsplit_once(':') {
            Some((h, port))
                if parse_ip_literal(&host).is_none()
                    && port.bytes().all(|b| b.is_ascii_digit()) =>
            {
                h.to_owned()
            }
            _ => host.clone(),
        };

        self.names.iter().any(|name| {
            let dotted = format!(".{name}");
            host_only == *name
                || host == *name
                || host_only.ends_with(&dotted)
                || host.ends_with(&dotted)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::federation::agent::resolve::InMemoryResolver;

    fn policy(block: &[&str], allow: Option<&[&str]>) -> IpPolicy {
        let set =
            |nets: &[&str]| IpRangeSet::new(nets.iter().map(|n| n.parse().unwrap()).collect());
        IpPolicy::new(set(block), allow.map(set))
    }

    #[test]
    fn allowlist_overrides_blocklist() {
        let p = policy(&["10.0.0.0/8"], Some(&["10.1.0.0/16"]));
        assert!(p.is_blocked(&"10.2.0.1".parse().unwrap()));
        assert!(!p.is_blocked(&"10.1.0.1".parse().unwrap()));
        assert!(!p.is_blocked(&"11.0.0.1".parse().unwrap()));
        assert!(p.is_blocked_ignoring_allowlist(&"10.1.0.1".parse().unwrap()));
    }

    #[tokio::test]
    async fn one_blocked_address_blocks_the_name() {
        let dns = InMemoryResolver::new();
        dns.add_host(
            "mixed.example",
            vec!["1.2.3.4".parse().unwrap(), "10.0.0.1".parse().unwrap()],
        );
        dns.add_host("good.example", vec!["1.2.3.4".parse().unwrap()]);
        let p = policy(&["10.0.0.0/8"], None);

        let err = p.resolve(&dns, "mixed.example").await.unwrap_err();
        assert!(err.is_dns(), "{err}");
        assert_eq!(
            p.resolve(&dns, "good.example").await.unwrap(),
            ["1.2.3.4".parse::<IpAddr>().unwrap()]
        );

        // IP literals are checked too.
        assert!(p.resolve(&dns, "10.0.0.1").await.unwrap_err().is_dns());
        assert!(p
            .resolve(&dns, "unknown.example")
            .await
            .unwrap_err()
            .is_dns());
    }

    #[test]
    fn no_proxy_matching() {
        let no_proxy = NoProxy::new(&[
            "example.com".to_owned(),
            ".other.org".to_owned(),
            " spaced.net ".to_owned(),
        ]);
        assert!(no_proxy.bypasses("example.com"));
        assert!(no_proxy.bypasses("sub.example.com"));
        assert!(no_proxy.bypasses("EXAMPLE.COM"));
        assert!(!no_proxy.bypasses("notexample.com"));
        assert!(no_proxy.bypasses("other.org"));
        assert!(no_proxy.bypasses("a.other.org"));
        assert!(no_proxy.bypasses("spaced.net"));
        assert!(!no_proxy.bypasses("matrix.org"));

        assert!(NoProxy::new(&["*".to_owned()]).bypasses("anything"));
        assert!(!NoProxy::new(&[]).bypasses("anything"));
    }

    #[test]
    fn proxy_url_parsing() {
        let config = |p: &str| ProxyConfig {
            http_proxy: None,
            https_proxy: Some(p.to_owned()),
            no_proxy_hosts: None,
        };

        let proxy = Proxy::from_config(&config("proxy.example"))
            .unwrap()
            .unwrap();
        assert!(!proxy.tls);
        assert_eq!((proxy.host.as_str(), proxy.port), ("proxy.example", 1080));
        assert_eq!(proxy.authorization, None);

        let proxy = Proxy::from_config(&config("https://bob:pa%20ss@proxy.example:3128"))
            .unwrap()
            .unwrap();
        assert!(proxy.tls);
        assert_eq!(proxy.port, 3128);
        assert_eq!(
            proxy.authorization.as_deref(),
            Some(format!("Basic {}", BASE64_STANDARD.encode("bob:pa%20ss")).as_str())
        );

        // A user without a password sends no credentials.
        let proxy = Proxy::from_config(&config("http://bob@proxy.example"))
            .unwrap()
            .unwrap();
        assert_eq!(proxy.authorization, None);

        assert!(Proxy::from_config(&config("socks5://proxy.example")).is_err());
        assert!(Proxy::from_config(&ProxyConfig::default())
            .unwrap()
            .is_none());
    }
}
