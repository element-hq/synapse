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

//! Block-set membership for IP-literal destinations. Resolved addresses are
//! checked by Python's blocklisting resolver.

use std::{
    fmt,
    net::{IpAddr, Ipv4Addr, Ipv6Addr},
};

/// IP ranges to refuse. An address only matches ranges of its own version,
/// as in `netaddr`, so an IPv4-mapped IPv6 address needs its own range;
/// Synapse's config adds those.
pub struct IpBlocklist {
    v4: Vec<(u32, u32)>,
    v6: Vec<(u128, u128)>,
}

impl IpBlocklist {
    /// Ranges as `netaddr`'s `IPSet.iter_cidrs()` gives them, such as
    /// `127.0.0.0/8` or `::1/128`; Synapse's `generate_ip_set` has already
    /// added the IPv4-mapped, IPv4-compatible and 6to4 ranges. `0.0.0.0` and
    /// `::` are always blocked.
    pub fn from_cidrs<'a>(cidrs: impl IntoIterator<Item = &'a str>) -> Result<Self, CidrError> {
        let mut blocklist = IpBlocklist {
            v4: vec![(0, u32::MAX)],
            v6: vec![(0, u128::MAX)],
        };
        for cidr in cidrs {
            let invalid = || CidrError(cidr.to_owned());
            let (addr, len) = match cidr.split_once('/') {
                Some((addr, len)) => (addr, Some(len.parse::<u32>().map_err(|_| invalid())?)),
                None => (cidr, None),
            };
            match addr.parse::<IpAddr>().map_err(|_| invalid())? {
                IpAddr::V4(addr) => {
                    let mask = mask(len.unwrap_or(32), 32).ok_or_else(invalid)? as u32;
                    blocklist.v4.push((u32::from(addr) & mask, mask));
                }
                IpAddr::V6(addr) => {
                    let mask = mask(len.unwrap_or(128), 128).ok_or_else(invalid)?;
                    blocklist.v6.push((u128::from(addr) & mask, mask));
                }
            }
        }
        Ok(blocklist)
    }

    pub fn blocks(&self, ip: IpAddr) -> bool {
        match ip {
            IpAddr::V4(ip) => self.blocks_v4(ip),
            IpAddr::V6(ip) => self.blocks_v6(ip),
        }
    }

    fn blocks_v4(&self, ip: Ipv4Addr) -> bool {
        let ip = u32::from(ip);
        self.v4.iter().any(|&(net, mask)| ip & mask == net)
    }

    fn blocks_v6(&self, ip: Ipv6Addr) -> bool {
        let ip = u128::from(ip);
        self.v6.iter().any(|&(net, mask)| ip & mask == net)
    }
}

/// The netmask for a prefix of `len` bits in a `bits`-bit address, in the
/// low `bits` bits.
fn mask(len: u32, bits: u32) -> Option<u128> {
    (len <= bits).then(|| match len {
        0 => 0,
        len => (u128::MAX << (128 - len)) >> (128 - bits),
    })
}

#[derive(Debug)]
pub struct CidrError(String);

impl fmt::Display for CidrError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Invalid IP range provided: {}.", self.0)
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
        let list = blocklist(&["10.0.0.0/8", "192.168.1.7", "fc00::/7", "::1/128"]);
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
    fn unspecified_always_blocked() {
        let list = blocklist(&[]);
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
            "nope",
            "10.0.0.0/8/8",
        ] {
            assert!(IpBlocklist::from_cidrs([cidr]).is_err(), "{cidr}");
        }
    }
}
