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

use std::{
    io,
    net::{SocketAddr, ToSocketAddrs},
    sync::Arc,
};

use async_trait::async_trait;
use tokio::sync::Semaphore;

use super::AddrLookup;

const MAX_LOOKUPS: usize = 20;

/// `getaddrinfo` with `SOCK_STREAM`, `AF_UNSPEC` and no flags, at most
/// [`MAX_LOOKUPS`] at once.
pub struct GaiLookup {
    permits: Arc<Semaphore>,
}

impl Default for GaiLookup {
    fn default() -> Self {
        GaiLookup {
            permits: Arc::new(Semaphore::new(MAX_LOOKUPS)),
        }
    }
}

impl GaiLookup {
    /// Runs `f` on a blocking thread once one of the permits is free. The
    /// permit is released when `f` returns, even if the caller has gone.
    async fn blocking<T: Send + 'static>(
        &self,
        f: impl FnOnce() -> T + Send + 'static,
    ) -> io::Result<T> {
        let permit = Arc::clone(&self.permits)
            .acquire_owned()
            .await
            .map_err(io::Error::other)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "getaddrinfo can't be cancelled, so a lookup running at shutdown \
                      keeps its thread until the system resolver gives up; it touches \
                      no Python or runtime state"
        )]
        let task = tokio::task::spawn_blocking(move || {
            let _permit = permit;
            f()
        });
        task.await.map_err(io::Error::other)
    }
}

#[async_trait]
impl AddrLookup for GaiLookup {
    async fn lookup(&self, host: &str, port: u16) -> io::Result<Vec<SocketAddr>> {
        let host = host.to_owned();
        self.blocking(move || (host.as_str(), port).to_socket_addrs().map(Vec::from_iter))
            .await?
    }
}

#[cfg(test)]
mod tests {
    use std::sync::RwLock;

    use super::*;

    #[test]
    fn looks_up_a_literal() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let addrs = runtime.block_on(GaiLookup::default().lookup("127.0.0.1", 8448));
        assert_eq!(addrs.unwrap(), [SocketAddr::from(([127, 0, 0, 1], 8448))]);
    }

    /// A 21st lookup waits, and a lookup keeps its permit until it returns,
    /// even once its caller is gone.
    #[test]
    fn permits_outlive_dropped_callers() {
        let gai = Arc::new(GaiLookup::default());
        let gate = Arc::new(RwLock::new(()));
        let runtime = tokio::runtime::Builder::new_multi_thread().build().unwrap();
        let _closed = gate.write().unwrap();
        runtime.block_on(async {
            let lookups: Vec<_> = (0..MAX_LOOKUPS)
                .map(|_| {
                    let (gai, gate) = (Arc::clone(&gai), Arc::clone(&gate));
                    tokio::spawn(async move { gai.blocking(move || drop(gate.read())).await })
                })
                .collect();
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
            while gai.permits.available_permits() > 0 {
                assert!(std::time::Instant::now() < deadline, "permits not taken");
                tokio::task::yield_now().await;
            }

            let waiting = {
                let gai = Arc::clone(&gai);
                tokio::spawn(async move { gai.blocking(|| panic!("a 21st lookup ran")).await })
            };
            tokio::task::yield_now().await;
            waiting.abort();
            assert!(waiting.await.unwrap_err().is_cancelled());
            for lookup in lookups {
                lookup.abort();
                assert!(lookup.await.unwrap_err().is_cancelled());
            }
            assert_eq!(gai.permits.available_permits(), 0);
        });
    }
}
