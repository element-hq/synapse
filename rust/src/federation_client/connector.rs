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

//! New connections: which address to connect to, and when.

use std::{io, net::SocketAddr, pin::pin, time::Duration};

use futures::{
    future::{select, Either},
    stream::FuturesUnordered,
    StreamExt,
};

use crate::{
    clock::Clock,
    federation_client::transport::{BoxedIo, Transport},
};

const ATTEMPT_DELAY: Duration = Duration::from_millis(300);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);

/// Connects to the first of `addrs` to answer: one attempt every 0.3 s in the
/// given order, each given 30 s. If all fail, the last failure is returned.
///
/// The delay and timeout are the defaults of Twisted's `HostnameEndpoint`,
/// which this replaces.
pub async fn connect(
    transport: &dyn Transport,
    addrs: &[SocketAddr],
    clock: &Clock,
) -> io::Result<BoxedIo> {
    // Runs every attempt concurrently, polling only the ones that woke, and
    // yields each result as it finishes.
    let mut attempts: FuturesUnordered<_> = addrs
        .iter()
        .zip(0..)
        .map(|(&addr, n)| async move {
            // Attempt n starts at n × 0.3 s unless an earlier one has
            // connected, even if earlier ones are still pending or have
            // failed. An address that never answers (dropped packets, a
            // broken IPv6 route) then delays the next by 0.3 s rather than
            // the 30 s timeout, and an address that answers within 0.3 s, as
            // most do, is the only connection opened.
            if n > 0 {
                clock.sleep(ATTEMPT_DELAY * n).await;
            }
            match select(transport.connect(addr), pin!(clock.sleep(CONNECT_TIMEOUT))).await {
                Either::Left((result, _)) => result,
                Either::Right(_) => Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    format!("connecting to {addr} timed out"),
                )),
            }
        })
        .collect();

    let mut last_error = io::Error::new(io::ErrorKind::NotFound, "no addresses");
    while let Some(result) = attempts.next().await {
        match result {
            Ok(io) => return Ok(io),
            Err(error) => last_error = error,
        }
    }
    Err(last_error)
}

#[cfg(test)]
mod tests {
    use std::{
        collections::HashMap,
        future::Future,
        sync::Mutex,
        task::{Context, Poll, Waker},
    };

    use async_trait::async_trait;

    use super::*;
    use crate::clock::VirtualTime;

    enum Outcome {
        Connect,
        Fail(io::ErrorKind),
        Hang,
    }

    /// Each address answers `Outcome` after its delay in milliseconds.
    struct FakeTransport {
        time: VirtualTime,
        outcomes: HashMap<SocketAddr, (u64, Outcome)>,
        started: Mutex<Vec<(SocketAddr, u64)>>,
    }

    #[async_trait]
    impl Transport for FakeTransport {
        async fn connect(&self, addr: SocketAddr) -> io::Result<BoxedIo> {
            let clock = Clock::Internal(self.time.clone());
            self.started
                .lock()
                .unwrap()
                .push((addr, clock.now_millis()));
            let (delay, outcome) = &self.outcomes[&addr];
            clock.sleep(Duration::from_millis(*delay)).await;
            match outcome {
                Outcome::Connect => Ok(Box::new(io::Cursor::new(Vec::new()))),
                Outcome::Fail(kind) => Err((*kind).into()),
                Outcome::Hang => futures::future::pending::<io::Result<BoxedIo>>().await,
            }
        }
    }

    fn addr(n: u8) -> SocketAddr {
        SocketAddr::from(([192, 0, 2, n], 8448))
    }

    /// Polls `connect` over `addrs` at each of the times in `steps`, which
    /// all but the last must leave pending.
    fn run(
        outcomes: Vec<(u64, Outcome)>,
        steps: &[u64],
    ) -> (io::Result<BoxedIo>, Vec<(SocketAddr, u64)>) {
        let time = VirtualTime::new(0);
        let addrs: Vec<_> = (1..).map(addr).take(outcomes.len()).collect();
        let transport = FakeTransport {
            time: time.clone(),
            outcomes: addrs.iter().copied().zip(outcomes).collect(),
            started: Mutex::new(Vec::new()),
        };
        let clock = Clock::Internal(time.clone());
        let mut cx = Context::from_waker(Waker::noop());
        let (last, pending) = steps.split_last().unwrap();
        let mut connecting = pin!(connect(&transport, &addrs, &clock));
        for &millis in pending {
            time.set_millis(millis);
            assert!(
                connecting.as_mut().poll(&mut cx).is_pending(),
                "at {millis}"
            );
        }
        time.set_millis(*last);
        let Poll::Ready(result) = connecting.as_mut().poll(&mut cx) else {
            panic!("pending at {last}");
        };
        let started = transport.started.lock().unwrap().clone();
        (result, started)
    }

    #[test]
    fn attempts_are_staggered_in_order() {
        let outcomes = vec![
            (0, Outcome::Hang),
            (0, Outcome::Hang),
            (0, Outcome::Connect),
        ];
        let (result, started) = run(outcomes, &[0, 299, 300, 599, 600]);
        assert!(result.is_ok());
        assert_eq!(started, [(addr(1), 0), (addr(2), 300), (addr(3), 600)]);
    }

    /// The first address fails last, at 500 ms, so its error is the one
    /// returned.
    #[test]
    fn the_last_failure_is_returned() {
        let outcomes = vec![
            (500, Outcome::Fail(io::ErrorKind::ConnectionRefused)),
            (100, Outcome::Fail(io::ErrorKind::ConnectionReset)),
        ];
        let (result, _) = run(outcomes, &[0, 300, 400, 499, 500]);
        assert_eq!(
            result.err().unwrap().kind(),
            io::ErrorKind::ConnectionRefused
        );
    }

    #[test]
    fn each_attempt_times_out_after_30_seconds() {
        let (result, _) = run(vec![(0, Outcome::Hang)], &[0, 29_999, 30_000]);
        assert_eq!(result.err().unwrap().kind(), io::ErrorKind::TimedOut);
    }
}
