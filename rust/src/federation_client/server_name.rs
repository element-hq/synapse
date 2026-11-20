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

//! Server names as the federation client accepts them.

use std::{
    fmt,
    net::{IpAddr, Ipv4Addr, Ipv6Addr},
};

/// The hostname part of a [`ServerName`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Host {
    Dns,
    Ipv4(Ipv4Addr),
    Ipv6(Ipv6Addr),
}

/// A server name that Synapse's `parse_and_validate_server_name` accepts and
/// whose port, if any, is 1–5 ASCII digits up to 65535 (R28).
///
/// Kept as written: the destination goes into headers unchanged, and
/// `Ipv6Addr`'s display form can differ from what was written.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ServerName {
    name: String,
    host: Host,
    port: Option<u16>,
}

impl ServerName {
    pub fn parse(name: &str) -> Result<Self, ServerNameError> {
        let invalid = || ServerNameError(name.to_owned());

        let (host_str, port) = match name.rsplit_once(':') {
            Some((host, port)) if !name.ends_with(']') => {
                (host, Some(parse_port(port).ok_or_else(invalid)?))
            }
            _ => (name, None),
        };

        let host = if let Some(inner) = host_str.strip_prefix('[') {
            let inner = inner.strip_suffix(']').ok_or_else(invalid)?;
            Host::Ipv6(inner.parse().map_err(|_| invalid())?)
        } else if !is_dns_name(host_str) {
            return Err(invalid());
        } else if let Ok(ip) = host_str.parse() {
            Host::Ipv4(ip)
        } else {
            Host::Dns
        };

        Ok(ServerName {
            name: name.to_owned(),
            host,
            port,
        })
    }

    pub fn as_str(&self) -> &str {
        &self.name
    }

    /// The hostname as written, with brackets around an IPv6 literal.
    pub fn host_str(&self) -> &str {
        match self.port {
            Some(_) => self
                .name
                .rsplit_once(':')
                .map_or(&self.name, |(host, _)| host),
            None => &self.name,
        }
    }

    pub fn host(&self) -> Host {
        self.host
    }

    pub fn port(&self) -> Option<u16> {
        self.port
    }

    /// The address, if the host is an IP literal.
    pub fn ip(&self) -> Option<IpAddr> {
        match self.host {
            Host::Dns => None,
            Host::Ipv4(ip) => Some(ip.into()),
            Host::Ipv6(ip) => Some(ip.into()),
        }
    }
}

/// 1–5 ASCII digits, at most 65535.
fn parse_port(port: &str) -> Option<u16> {
    if (1..=5).contains(&port.len()) && port.bytes().all(|b| b.is_ascii_digit()) {
        port.parse().ok()
    } else {
        None
    }
}

/// Synapse's `VALID_HOST_REGEX`: dot-separated, non-empty labels of ASCII
/// letters, digits and hyphens.
fn is_dns_name(host: &str) -> bool {
    host.split('.').all(|label| {
        !label.is_empty()
            && label
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    })
}

impl fmt::Display for ServerName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.name)
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct ServerNameError(String);

impl fmt::Display for ServerNameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Invalid server name '{}'", self.0)
    }
}

impl std::error::Error for ServerNameError {}

#[cfg(test)]
mod tests {
    use super::*;

    /// Spec: [server name](https://spec.matrix.org/latest/appendices/#server-name) examples.
    #[test]
    fn spec_examples() {
        let cases: [(&str, &str, Host, Option<u16>); 6] = [
            ("matrix.org", "matrix.org", Host::Dns, None),
            ("matrix.org:8888", "matrix.org", Host::Dns, Some(8888)),
            (
                "1.2.3.4",
                "1.2.3.4",
                Host::Ipv4(Ipv4Addr::new(1, 2, 3, 4)),
                None,
            ),
            (
                "1.2.3.4:1234",
                "1.2.3.4",
                Host::Ipv4(Ipv4Addr::new(1, 2, 3, 4)),
                Some(1234),
            ),
            (
                "[1234:5678::abcd]",
                "[1234:5678::abcd]",
                Host::Ipv6("1234:5678::abcd".parse().unwrap()),
                None,
            ),
            (
                "[1234:5678::abcd]:5678",
                "[1234:5678::abcd]",
                Host::Ipv6("1234:5678::abcd".parse().unwrap()),
                Some(5678),
            ),
        ];
        for (name, host_str, host, port) in cases {
            let parsed = ServerName::parse(name).unwrap();
            assert_eq!(
                (
                    parsed.as_str(),
                    parsed.host_str(),
                    parsed.host(),
                    parsed.port()
                ),
                (name, host_str, host, port),
                "{name}"
            );
        }
    }

    /// R28: only 1–5 ASCII digits, at most 65535.
    #[test]
    fn ports() {
        for (name, port) in [("h:0", 0), ("h:08448", 8448), ("h:65535", 65535)] {
            assert_eq!(
                ServerName::parse(name).unwrap().port(),
                Some(port),
                "{name}"
            );
        }
        for name in [
            "h:", "h:65536", "h:008448", "h: 8448", "h:+8448", "h:8_448", "h:-1", "h:8448\n",
        ] {
            assert!(ServerName::parse(name).is_err(), "{name:?}");
        }
    }

    #[test]
    fn written_form_is_kept() {
        let name = ServerName::parse("[1234:5678:0::ABCD]:08448").unwrap();
        assert_eq!(name.to_string(), "[1234:5678:0::ABCD]:08448");
        assert_eq!(name.host_str(), "[1234:5678:0::ABCD]");
        assert_eq!(name.ip(), Some("1234:5678::abcd".parse().unwrap()));
    }
}
