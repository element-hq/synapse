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

//! Name resolution for new connections.

use std::{fmt, io, net::SocketAddr, sync::Arc};

use async_trait::async_trait;

use crate::federation_client::{
    events::{AttemptState, Event},
    ip_policy::IpPolicy,
};

pub mod addr;

#[async_trait]
pub trait AddrLookup: Send + Sync + 'static {
    async fn lookup(&self, host: &str, port: u16) -> io::Result<Vec<SocketAddr>>;
}

#[derive(Debug, PartialEq, Eq)]
pub enum LookupFailure {
    NoAddresses,
    Blocked,
}

impl fmt::Display for LookupFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            LookupFailure::NoAddresses => "no addresses",
            LookupFailure::Blocked => "an address is blocked",
        })
    }
}

impl std::error::Error for LookupFailure {}

pub struct Resolver {
    addr: Arc<dyn AddrLookup>,
    policy: IpPolicy,
}

impl Resolver {
    pub fn new(addr: Arc<dyn AddrLookup>, policy: IpPolicy) -> Self {
        Resolver { addr, policy }
    }

    /// Addresses for one target, in lookup order. One blocked address refuses
    /// them all; a failed lookup is no addresses.
    pub async fn addresses(
        &self,
        host: &str,
        port: u16,
        state: &AttemptState,
    ) -> Result<Vec<SocketAddr>, LookupFailure> {
        let addrs = self.addr.lookup(host, port).await.unwrap_or_default();
        let mut blocked = false;
        for ip in addrs
            .iter()
            .map(SocketAddr::ip)
            .filter(|&ip| self.policy.blocks(ip))
        {
            state.record(Event::AddressBlocked {
                ip,
                host: host.to_owned(),
            });
            blocked = true;
        }
        if blocked {
            Err(LookupFailure::Blocked)
        } else if addrs.is_empty() {
            Err(LookupFailure::NoAddresses)
        } else {
            Ok(addrs)
        }
    }
}

#[cfg(test)]
mod tests {
    use futures::executor::block_on;

    use super::*;
    use crate::federation_client::ip_policy::IpBlocklist;

    /// Answers with the given addresses, or fails if there are none given.
    struct FakeLookup(Option<Vec<SocketAddr>>);

    #[async_trait]
    impl AddrLookup for FakeLookup {
        async fn lookup(&self, _host: &str, _port: u16) -> io::Result<Vec<SocketAddr>> {
            self.0.clone().ok_or(io::ErrorKind::NotFound.into())
        }
    }

    fn resolve(answer: Option<&[&str]>) -> (Result<Vec<String>, LookupFailure>, Vec<Event>) {
        let answer = answer.map(|addrs| addrs.iter().map(|addr| addr.parse().unwrap()).collect());
        let policy = IpPolicy {
            blocklist: IpBlocklist::from_cidrs(["10.0.0.0/8", "fc00::/7"]).unwrap(),
            allowlist: IpBlocklist::from_cidrs(["10.1.0.0/16"]).unwrap(),
        };
        let resolver = Resolver::new(Arc::new(FakeLookup(answer)), policy);
        let state = AttemptState::default();
        let result = block_on(resolver.addresses("example.com", 8448, &state));
        let result = result.map(|addrs| addrs.iter().map(SocketAddr::to_string).collect());
        (result, state.take_events())
    }

    fn blocked(ip: &str) -> Event {
        Event::AddressBlocked {
            ip: ip.parse().unwrap(),
            host: "example.com".to_owned(),
        }
    }

    /// `10.1.2.3` is in both sets.
    #[test]
    fn allowed_answers_keep_their_order() {
        let answer = ["[2001:db8::1]:8448", "10.1.2.3:8448", "1.2.3.4:8448"];
        assert_eq!(
            resolve(Some(&answer)),
            (Ok(answer.map(String::from).into()), vec![])
        );
    }

    #[test]
    fn one_blocked_address_refuses_the_answer() {
        let answer = ["10.2.3.4:8448", "1.2.3.4:8448", "[fd00::1]:8448"];
        assert_eq!(
            resolve(Some(&answer)),
            (
                Err(LookupFailure::Blocked),
                vec![blocked("10.2.3.4"), blocked("fd00::1")]
            )
        );
    }

    #[test]
    fn no_addresses() {
        for answer in [Some(&[][..]), None] {
            assert_eq!(resolve(answer), (Err(LookupFailure::NoAddresses), vec![]));
        }
    }
}
