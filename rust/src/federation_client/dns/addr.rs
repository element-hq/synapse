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

/// The size of today's `gai_resolver` threadpool.
const MAX_LOOKUPS: usize = 20;

/// `getaddrinfo` with `SOCK_STREAM`, `AF_UNSPEC` and no flags, as
/// `GAIResolver` calls it.
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
                      keeps its thread until the system resolver times out, as \
                      today's gai_resolver threadpool does"
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
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Condvar, Mutex,
    };

    use super::*;

    #[test]
    fn looks_up_a_literal() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let addrs = runtime.block_on(GaiLookup::default().lookup("127.0.0.1", 8448));
        assert_eq!(addrs.unwrap(), [SocketAddr::from(([127, 0, 0, 1], 8448))]);
    }

    /// Stalled lookups hold their permits, even once their callers are gone.
    #[test]
    fn at_most_twenty_lookups_run() {
        let runtime = tokio::runtime::Builder::new_multi_thread().build().unwrap();
        let gai = Arc::new(GaiLookup::default());
        let started = Arc::new(AtomicUsize::new(0));
        let release = Arc::new((Mutex::new(false), Condvar::new()));
        let stall = || {
            let started = Arc::clone(&started);
            let release = Arc::clone(&release);
            move || {
                started.fetch_add(1, Ordering::SeqCst);
                let (released, wake) = &*release;
                let _released = wake
                    .wait_while(released.lock().unwrap(), |released| !*released)
                    .unwrap();
            }
        };

        runtime.block_on(async {
            let first: Vec<_> = (0..MAX_LOOKUPS)
                .map(|_| {
                    let (gai, stall) = (Arc::clone(&gai), stall());
                    tokio::spawn(async move { gai.blocking(stall).await })
                })
                .collect();
            while started.load(Ordering::SeqCst) < MAX_LOOKUPS {
                tokio::task::yield_now().await;
            }

            let last = {
                let (gai, stall) = (Arc::clone(&gai), stall());
                tokio::spawn(async move { gai.blocking(stall).await })
            };
            for lookup in first {
                lookup.abort();
                assert!(lookup.await.unwrap_err().is_cancelled());
            }
            let free_permits = gai.permits.available_permits();

            *release.0.lock().unwrap() = true;
            release.1.notify_all();
            assert_eq!(free_permits, 0);
            last.await.unwrap().unwrap();
            assert_eq!(started.load(Ordering::SeqCst), MAX_LOOKUPS + 1);
        });
    }
}
