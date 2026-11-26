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

//! Block-set membership for IP-literal destinations.

use std::{fmt, net::IpAddr};

/// IP ranges to refuse. An address only matches ranges of its own version,
/// as in `netaddr`.
pub struct IpBlocklist {
    v4: Vec<(u128, u128)>,
    v6: Vec<(u128, u128)>,
}

impl IpBlocklist {
    /// Ranges as `netaddr`'s `IPSet.iter_cidrs()` gives them, such as
    /// `127.0.0.0/8` or `::1/128`. Synapse's `generate_ip_set` has already
    /// added `0.0.0.0` and `::` and the IPv4-mapped, IPv4-compatible and 6to4
    /// ranges.
    pub fn from_cidrs<'a>(cidrs: impl IntoIterator<Item = &'a str>) -> Result<Self, CidrError> {
        let mut blocklist = IpBlocklist {
            v4: Vec::new(),
            v6: Vec::new(),
        };
        for cidr in cidrs {
            let invalid = || CidrError(cidr.to_owned());
            let (addr, len) = cidr.split_once('/').ok_or_else(invalid)?;
            let len: u32 = len.parse().map_err(|_| invalid())?;
            let (bits, width, ranges) = match addr.parse().map_err(|_| invalid())? {
                IpAddr::V4(addr) => (u128::from(u32::from(addr)), 32, &mut blocklist.v4),
                IpAddr::V6(addr) => (u128::from(addr), 128, &mut blocklist.v6),
            };
            if len > width {
                return Err(invalid());
            }
            let mask = u128::MAX.checked_shl(width - len).unwrap_or(0);
            ranges.push((bits & mask, mask));
        }
        Ok(blocklist)
    }

    pub fn blocks(&self, ip: IpAddr) -> bool {
        let (bits, ranges) = match ip {
            IpAddr::V4(ip) => (u128::from(u32::from(ip)), &self.v4),
            IpAddr::V6(ip) => (u128::from(ip), &self.v6),
        };
        ranges.iter().any(|&(net, mask)| bits & mask == net)
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

    fn blocklist(cidrs: &[&str]) -> IpBlocklist {
        IpBlocklist::from_cidrs(cidrs.iter().copied()).unwrap()
    }

    #[test]
    fn ranges() {
        let list = blocklist(&["10.0.0.0/8", "192.168.1.7/32", "fc00::/7", "::1/128"]);
        for (ip, blocked) in [
            ("10.255.0.1", true),
            ("11.0.0.1", false),
            ("192.168.1.7", true),
            ("192.168.1.8", false),
            ("fd12::1", true),
            ("fe00::1", false),
            ("::1", true),
            ("::2", false),
        ] {
            assert_eq!(list.blocks(ip.parse().unwrap()), blocked, "{ip}");
        }
    }

    #[test]
    fn unspecified_blocked_only_when_listed() {
        let empty = blocklist(&[]);
        assert!(!empty.blocks("0.0.0.0".parse().unwrap()));
        assert!(!empty.blocks("::".parse().unwrap()));
        let list = blocklist(&["0.0.0.0/32", "::/128"]);
        assert!(list.blocks("0.0.0.0".parse().unwrap()));
        assert!(list.blocks("::".parse().unwrap()));
        assert!(!list.blocks("0.0.0.1".parse().unwrap()));
    }

    /// As in `netaddr`: an IPv4-mapped address is not in an IPv4 range.
    #[test]
    fn versions_do_not_mix() {
        let list = blocklist(&["127.0.0.0/8"]);
        assert!(!list.blocks("::ffff:127.0.0.1".parse().unwrap()));
        assert!(blocklist(&["::ffff:127.0.0.0/104"]).blocks("::ffff:127.0.0.1".parse().unwrap()));
        assert!(!blocklist(&["::/96"]).blocks("0.0.0.1".parse().unwrap()));
    }

    #[test]
    fn whole_space() {
        assert!(blocklist(&["0.0.0.0/0"]).blocks("8.8.8.8".parse().unwrap()));
        assert!(blocklist(&["::/0"]).blocks("2001:db8::1".parse().unwrap()));
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
            assert!(IpBlocklist::from_cidrs([cidr]).is_err(), "{cidr}");
        }
    }
}
