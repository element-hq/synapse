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

//! IP-set membership for IP-literal destinations. Useful for blocklist or allowlist functionality.

use std::{fmt, net::IpAddr};

use ipnet::IpNet;

/// A set of IP ranges. An address only matches ranges of its own version,
/// as in `netaddr`.
pub struct IpSet(Vec<IpNet>);

impl IpSet {
    /// Ranges as `netaddr`'s `IPSet.iter_cidrs()` gives them, such as
    /// `127.0.0.0/8` or `::1/128`. Synapse's `generate_ip_set` has already
    /// added `0.0.0.0` and `::` and the IPv4-mapped, IPv4-compatible and 6to4
    /// ranges.
    pub fn from_cidrs<'a>(cidrs: impl IntoIterator<Item = &'a str>) -> Result<Self, CidrError> {
        cidrs
            .into_iter()
            .map(|cidr| cidr.parse().map_err(|_| CidrError(cidr.to_owned())))
            .collect::<Result<_, _>>()
            .map(IpSet)
    }

    pub fn contains(&self, ip: IpAddr) -> bool {
        self.0.iter().any(|net| net.contains(&ip))
    }
}

/// The rule for resolved addresses: refused if in the block set and not in
/// the allow set.
pub struct IpPolicy {
    pub blocklist: IpBlocklist,
    pub allowlist: IpBlocklist,
}

impl IpPolicy {
    pub fn blocks(&self, ip: IpAddr) -> bool {
        self.blocklist.blocks(ip) && !self.allowlist.blocks(ip)
    }
}

#[derive(Debug)]
pub struct CidrError(String);

impl fmt::Display for CidrError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid CIDR range: {}", self.0)
    }
}

impl std::error::Error for CidrError {}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip_set(cidrs: &[&str]) -> IpSet {
        IpSet::from_cidrs(cidrs.iter().copied()).unwrap()
    }

    #[test]
    fn ranges() {
        let list = ip_set(&["10.0.0.0/8", "192.168.1.7/32", "fc00::/7", "::1/128"]);
        for (ip, contained) in [
            ("10.255.0.1", true),
            ("11.0.0.1", false),
            ("192.168.1.7", true),
            ("192.168.1.8", false),
            ("fd12::1", true),
            ("fe00::1", false),
            ("::1", true),
            ("::2", false),
        ] {
            assert_eq!(list.contains(ip.parse().unwrap()), contained, "{ip}");
        }
    }

    #[test]
    fn unspecified_blocked_only_when_listed() {
        let empty = ip_set(&[]);
        assert!(!empty.contains("0.0.0.0".parse().unwrap()));
        assert!(!empty.contains("::".parse().unwrap()));
        let list = ip_set(&["0.0.0.0/32", "::/128"]);
        assert!(list.contains("0.0.0.0".parse().unwrap()));
        assert!(list.contains("::".parse().unwrap()));
        assert!(!list.contains("0.0.0.1".parse().unwrap()));
    }

    /// As in `netaddr`: an IPv4-mapped address is not in an IPv4 range.
    #[test]
    fn versions_do_not_mix() {
        let list = ip_set(&["127.0.0.0/8"]);
        assert!(!list.contains("::ffff:127.0.0.1".parse().unwrap()));
        assert!(ip_set(&["::ffff:127.0.0.0/104"]).contains("::ffff:127.0.0.1".parse().unwrap()));
        assert!(!ip_set(&["::/96"]).contains("0.0.0.1".parse().unwrap()));
    }

    #[test]
    fn whole_space() {
        assert!(ip_set(&["0.0.0.0/0"]).contains("8.8.8.8".parse().unwrap()));
        assert!(ip_set(&["::/0"]).contains("2001:db8::1".parse().unwrap()));
    }

    #[test]
    fn invalid_ranges() {
        for cidr in [
            "10.0.0.0/33",
            "::/129",
            "10.0.0.0/x",
            "10.0.0.0",
            "nope/8",
            "10.0.0.0/8/8",
        ] {
            assert!(IpSet::from_cidrs([cidr]).is_err(), "{cidr}");
        }
    }
}
