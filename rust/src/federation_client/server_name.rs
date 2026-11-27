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
    net::{IpAddr, Ipv6Addr},
    ops::RangeTo,
};

/// A server name that Synapse's `parse_and_validate_server_name` accepts and
/// whose port, if any, is 1–5 ASCII digits up to 65535, as `_send_request`
/// requires.
///
/// Kept as written: the destination goes into headers unchanged, and
/// `Ipv6Addr`'s display form can differ from what was written.
#[derive(Debug)]
pub struct ServerName {
    name: String,
    host: RangeTo<usize>,
    ip: Option<IpAddr>,
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

        let ip = if let Some(inner) = host_str.strip_prefix('[') {
            let inner = inner.strip_suffix(']').ok_or_else(invalid)?;
            Some(IpAddr::V6(
                inner.parse::<Ipv6Addr>().map_err(|_| invalid())?,
            ))
        } else if let Ok(ip) = host_str.parse() {
            Some(IpAddr::V4(ip))
        } else if is_dns_name(host_str) {
            None
        } else {
            return Err(invalid());
        };

        Ok(ServerName {
            name: name.to_owned(),
            host: ..host_str.len(),
            ip,
            port,
        })
    }

    pub fn as_str(&self) -> &str {
        &self.name
    }

    /// The hostname as written, with brackets around an IPv6 literal.
    pub fn host_str(&self) -> &str {
        &self.name[self.host]
    }

    pub fn port(&self) -> Option<u16> {
        self.port
    }

    /// The address, if the host is an IP literal.
    pub fn ip(&self) -> Option<IpAddr> {
        self.ip
    }
}

/// 1–5 ASCII digits, at most 65535.
fn parse_port(port: &str) -> Option<u16> {
    if port.len() <= 5 && port.bytes().all(|b| b.is_ascii_digit()) {
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

#[derive(Debug)]
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
        let v4: Option<IpAddr> = Some("1.2.3.4".parse().unwrap());
        let v6: Option<IpAddr> = Some("1234:5678::abcd".parse().unwrap());
        let cases = [
            ("matrix.org", "matrix.org", None, None),
            ("matrix.org:8888", "matrix.org", None, Some(8888)),
            ("1.2.3.4", "1.2.3.4", v4, None),
            ("1.2.3.4:1234", "1.2.3.4", v4, Some(1234)),
            ("[1234:5678::abcd]", "[1234:5678::abcd]", v6, None),
            (
                "[1234:5678::abcd]:5678",
                "[1234:5678::abcd]",
                v6,
                Some(5678),
            ),
        ];
        for (name, host_str, ip, port) in cases {
            let parsed = ServerName::parse(name).unwrap();
            assert_eq!(
                (
                    parsed.as_str(),
                    parsed.host_str(),
                    parsed.ip(),
                    parsed.port()
                ),
                (name, host_str, ip, port),
                "{name}"
            );
        }
    }

    /// Only 1–5 ASCII digits, at most 65535.
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
            "h:",
            "h:65536",
            "h:99999",
            "h:008448",
            "h: 8448",
            "h:+8448",
            "h:8_448",
            "h:-1",
            "h:8448\n",
            "h:8448\x0b",
            "h:\u{0668}\u{0664}\u{0664}\u{0668}",
            "h:1:2",
        ] {
            assert!(ServerName::parse(name).is_err(), "{name:?}");
        }
    }

    #[test]
    fn hostnames() {
        for name in ["EXAMPLE.com", "-x.com", "x-.com", "xn--bcher-kva.example"] {
            let parsed = ServerName::parse(name).unwrap();
            assert_eq!((parsed.host_str(), parsed.ip()), (name, None), "{name}");
        }
        for name in [
            "ex_ample.com",
            "a..b",
            ".a",
            "example.com.",
            "b\u{00fc}cher.example",
            "",
        ] {
            assert!(ServerName::parse(name).is_err(), "{name:?}");
        }
    }

    /// Only dotted-quad IPv4 is an IP literal, as in `_is_ip_literal`.
    #[test]
    fn ipv4_lookalikes_are_hostnames() {
        for name in [
            "01.2.3.4",
            "1.2.3",
            "1.2.3.4.5",
            "256.1.1.1",
            "2130706433",
            "0x7f000001",
            "127.1",
        ] {
            assert_eq!(ServerName::parse(name).unwrap().ip(), None, "{name}");
        }
    }

    #[test]
    fn ipv6_literals() {
        for (name, ip) in [
            ("[::]", "::"),
            ("[::1]", "::1"),
            ("[::1]:8448", "::1"),
            ("[::ffff:1.2.3.4]", "::ffff:1.2.3.4"),
            ("[1:2:3:4:5:6:7:8]", "1:2:3:4:5:6:7:8"),
            ("[ABCD::EF01]", "abcd::ef01"),
        ] {
            assert_eq!(
                ServerName::parse(name).unwrap().ip(),
                Some(ip.parse().unwrap()),
                "{name}"
            );
        }
        for name in [
            "[::ffff:01.2.3.4]",
            "[1:2:3:4:5:6:7:8:9]",
            "[1::2::3]",
            "[fe80::1%eth0]",
            "[1.2.3.4]",
            "[]",
            "[::1",
            "::1",
            "[::1]x",
            "[[::1]]",
        ] {
            assert!(ServerName::parse(name).is_err(), "{name:?}");
        }
    }

    #[test]
    fn written_form_is_kept() {
        let name = ServerName::parse("[1234:5678:0::ABCD]:08448").unwrap();
        assert_eq!(name.as_str(), "[1234:5678:0::ABCD]:08448");
        assert_eq!(name.host_str(), "[1234:5678:0::ABCD]");
        assert_eq!(name.ip(), Some("1234:5678::abcd".parse().unwrap()));
    }
}
