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

//! Send finished spans to the Jaeger agent over UDP.
//!
//! [`Reporter::report`] never blocks: it queues the span on a bounded channel
//! and drops it if the queue is full. A dedicated `jaeger-reporter` thread
//! encodes the spans and sends them in `emitBatch` datagrams. A batch goes out
//! when the next span would not fit in one datagram, when it holds
//! [`MAX_BATCH_SPANS`] spans, or [`FLUSH_INTERVAL`] after its first span was
//! queued.
//!
//! The thread is a plain std thread rather than a tokio task, so reporting
//! works before the runtime starts and does not compete with request work.
//!
//! Like jaeger-client, the reporter sends from an unconnected socket and
//! tolerates an agent host name that does not resolve yet. It resolves the
//! name in the reporter thread, again every [`RESOLVE_INTERVAL`], and again
//! after a failed send, so spans follow an agent that moves to a new address.

use std::io;
use std::net::{SocketAddr, ToSocketAddrs, UdpSocket};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use log::{error, warn};

use super::thrift::{
    emit_batch_len, encode_emit_batch, encode_span, encoded_process_len, FinishedSpan, Process,
};

/// How many spans may wait for the reporter thread before new ones are
/// dropped.
const QUEUE_CAPACITY: usize = 1000;

/// The most spans sent in one batch.
pub const MAX_BATCH_SPANS: usize = 100;

/// The largest datagram sent. The agent reads UDP packets of up to 65000
/// bytes.
pub const MAX_PACKET_SIZE: usize = 65_000;

/// How long a span may wait in a part-filled batch.
pub const FLUSH_INTERVAL: Duration = Duration::from_secs(1);

/// How long a resolved agent address is used before resolving the host name
/// again.
const RESOLVE_INTERVAL: Duration = Duration::from_secs(30);

/// Counters for the reporter, read by Python's metrics collector.
#[derive(Debug, Default)]
pub struct ReporterStats {
    /// Spans queued but not yet taken by the reporter thread.
    pub queue_depth: AtomicU64,
    pub spans_sent: AtomicU64,
    pub batches_sent: AtomicU64,
    /// Spans dropped because the queue was full.
    pub dropped_queue_full: AtomicU64,
    /// Spans dropped because a batch of one would not fit in a datagram.
    pub dropped_oversize: AtomicU64,
    /// Spans dropped because the reporter has shut down.
    pub dropped_closed: AtomicU64,
    /// Batches whose send failed, including when the agent's address did
    /// not resolve. Their spans are lost.
    pub send_errors: AtomicU64,
    /// The last send error, logged when it differs from the one before.
    last_error: Mutex<Option<String>>,
}

impl ReporterStats {
    fn record_error(&self, err: &io::Error) {
        self.send_errors.fetch_add(1, Ordering::Relaxed);

        let message = err.to_string();
        let Ok(mut last) = self.last_error.lock() else {
            return;
        };
        if last.as_deref() != Some(message.as_str()) {
            warn!("Failed to send spans to the Jaeger agent: {message}");
            *last = Some(message);
        }
    }

    /// Note a successful send, so that the next error is logged again.
    fn record_success(&self) {
        if let Ok(mut last) = self.last_error.lock() {
            *last = None;
        }
    }
}

enum Message {
    Span(Box<FinishedSpan>),
    /// Send whatever is queued now, then signal.
    Flush(SyncSender<()>),
}

/// Reports spans to one Jaeger agent. See the module docs.
pub struct Reporter {
    /// `None` once shut down.
    sender: Mutex<Option<SyncSender<Message>>>,
    /// Signalled by the thread when it exits.
    finished: Mutex<Option<Receiver<()>>>,
    stats: Arc<ReporterStats>,
}

impl Reporter {
    /// Start the reporter thread, sending to the agent at `host:port`. The
    /// host name is resolved by the thread, so a name that does not resolve
    /// yet is not an error here.
    pub fn start(process: Process, host: &str, port: u16) -> io::Result<Reporter> {
        let agent = AgentSocket::new(host.to_owned(), port);
        let (sender, receiver) = mpsc::sync_channel(QUEUE_CAPACITY);
        let (finished_tx, finished_rx) = mpsc::sync_channel(1);
        let stats = Arc::new(ReporterStats::default());

        let thread_stats = Arc::clone(&stats);
        thread::Builder::new()
            .name("jaeger-reporter".to_owned())
            .spawn(move || {
                let batcher = Batcher::new(process, agent, thread_stats);
                // A panic would otherwise end reporting silently: spans would
                // count as dropped because the reporter had shut down.
                if catch_unwind(AssertUnwindSafe(|| batcher.run(receiver))).is_err() {
                    error!(
                        "The Jaeger reporter thread panicked; spans from Rust are no longer sent"
                    );
                }
                let _ = finished_tx.send(());
            })?;

        Ok(Reporter {
            sender: Mutex::new(Some(sender)),
            finished: Mutex::new(Some(finished_rx)),
            stats,
        })
    }

    /// Queue a span for sending. Never blocks: drops the span if the queue is
    /// full or the reporter has shut down.
    pub fn report(&self, span: FinishedSpan) {
        let sender = match self.sender.lock() {
            Ok(sender) => sender.clone(),
            Err(_) => None,
        };
        let Some(sender) = sender else {
            self.stats.dropped_closed.fetch_add(1, Ordering::Relaxed);
            return;
        };

        // Count before sending, so that the thread's decrement never runs
        // first and wraps the counter.
        self.stats.queue_depth.fetch_add(1, Ordering::Relaxed);
        match sender.try_send(Message::Span(Box::new(span))) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) => {
                self.stats.queue_depth.fetch_sub(1, Ordering::Relaxed);
                self.stats
                    .dropped_queue_full
                    .fetch_add(1, Ordering::Relaxed);
            }
            Err(TrySendError::Disconnected(_)) => {
                self.stats.queue_depth.fetch_sub(1, Ordering::Relaxed);
                self.stats.dropped_closed.fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    /// Send everything queued so far, waiting up to `timeout`. Returns
    /// whether the reporter thread handled it in time, which it does even if
    /// the send fails (see [`ReporterStats::send_errors`]).
    pub fn flush(&self, timeout: Duration) -> bool {
        let sender = match self.sender.lock() {
            Ok(sender) => sender.clone(),
            Err(_) => None,
        };
        let Some(sender) = sender else {
            return false;
        };

        let (done_tx, done_rx) = mpsc::sync_channel(1);
        let deadline = Instant::now() + timeout;
        // Unlike spans, a flush waits for room in the queue.
        let mut message = Message::Flush(done_tx);
        loop {
            match sender.try_send(message) {
                Ok(()) => break,
                Err(TrySendError::Full(returned)) if Instant::now() < deadline => {
                    message = returned;
                    thread::sleep(Duration::from_millis(1));
                }
                Err(_) => return false,
            }
        }

        done_rx
            .recv_timeout(deadline.saturating_duration_since(Instant::now()))
            .is_ok()
    }

    /// Stop accepting spans, send the ones queued, and wait up to `timeout`
    /// for the reporter thread to exit. A thread that does not exit in time
    /// is left to finish on its own.
    pub fn shutdown(&self, timeout: Duration) {
        // Dropping the last sender ends the thread's loop once it has drained
        // the queue.
        if let Ok(mut sender) = self.sender.lock() {
            sender.take();
        }

        let finished = match self.finished.lock() {
            Ok(mut finished) => finished.take(),
            Err(_) => None,
        };
        if let Some(finished) = finished {
            if finished.recv_timeout(timeout).is_err() {
                warn!("The Jaeger reporter thread did not finish within {timeout:?}");
            }
        }
    }

    pub fn stats(&self) -> &ReporterStats {
        &self.stats
    }
}

impl Drop for Reporter {
    fn drop(&mut self) {
        // Let the thread drain and exit without waiting for it.
        if let Ok(mut sender) = self.sender.lock() {
            sender.take();
        }
    }
}

/// The reporter thread's state: the batch being filled.
struct Batcher {
    process: Process,
    process_len: usize,
    agent: AgentSocket,
    stats: Arc<ReporterStats>,
    spans: Vec<Vec<u8>>,
    spans_len: usize,
    /// When the first span of the current batch arrived.
    batch_started: Option<Instant>,
    seq_id: u32,
}

impl Batcher {
    fn new(process: Process, agent: AgentSocket, stats: Arc<ReporterStats>) -> Self {
        let process_len = encoded_process_len(&process);
        if emit_batch_len(process_len, 0, 0) > MAX_PACKET_SIZE {
            warn!(
                "The Jaeger process tags take {process_len} bytes, too many for a datagram; \
                 no spans from Rust will be sent"
            );
        }

        Batcher {
            process_len,
            process,
            agent,
            stats,
            spans: Vec::new(),
            spans_len: 0,
            batch_started: None,
            seq_id: 0,
        }
    }

    fn run(mut self, receiver: Receiver<Message>) {
        loop {
            let message = match self.batch_started {
                Some(started) => {
                    let wait = (started + FLUSH_INTERVAL).saturating_duration_since(Instant::now());
                    receiver.recv_timeout(wait)
                }
                None => receiver.recv().map_err(|_| RecvTimeoutError::Disconnected),
            };

            match message {
                Ok(Message::Span(span)) => {
                    self.stats.queue_depth.fetch_sub(1, Ordering::Relaxed);
                    self.add(&span);
                }
                Ok(Message::Flush(done)) => {
                    self.send();
                    let _ = done.send(());
                }
                Err(RecvTimeoutError::Timeout) => self.send(),
                Err(RecvTimeoutError::Disconnected) => {
                    self.send();
                    return;
                }
            }
        }
    }

    fn add(&mut self, span: &FinishedSpan) {
        let encoded = encode_span(span);
        if emit_batch_len(self.process_len, 1, encoded.len()) > MAX_PACKET_SIZE {
            self.stats.dropped_oversize.fetch_add(1, Ordering::Relaxed);
            return;
        }

        let len_with_span = emit_batch_len(
            self.process_len,
            self.spans.len() + 1,
            self.spans_len + encoded.len(),
        );
        if len_with_span > MAX_PACKET_SIZE {
            self.send();
        }

        self.spans_len += encoded.len();
        self.spans.push(encoded);
        self.batch_started.get_or_insert_with(Instant::now);

        if self.spans.len() >= MAX_BATCH_SPANS {
            self.send();
        }
    }

    fn send(&mut self) {
        self.batch_started = None;
        if self.spans.is_empty() {
            return;
        }

        self.seq_id = self.seq_id.wrapping_add(1);
        let message = encode_emit_batch(&self.process, &self.spans, self.seq_id);
        let count = self.spans.len() as u64;
        self.spans.clear();
        self.spans_len = 0;

        match self.agent.send(&message) {
            Ok(()) => {
                self.stats.batches_sent.fetch_add(1, Ordering::Relaxed);
                self.stats.spans_sent.fetch_add(count, Ordering::Relaxed);
                self.stats.record_success();
            }
            Err(err) => self.stats.record_error(&err),
        }
    }
}

/// Sends datagrams to the agent's host name, resolving it as needed.
struct AgentSocket {
    host: String,
    port: u16,
    /// The resolved address and when it was resolved.
    address: Option<(SocketAddr, Instant)>,
    /// Unconnected sockets, one per address family, bound on first use.
    v4: Option<UdpSocket>,
    v6: Option<UdpSocket>,
}

impl AgentSocket {
    fn new(host: String, port: u16) -> Self {
        AgentSocket {
            host,
            port,
            address: None,
            v4: None,
            v6: None,
        }
    }

    fn send(&mut self, datagram: &[u8]) -> io::Result<()> {
        let address = self.address()?;
        let slot = if address.is_ipv4() {
            &mut self.v4
        } else {
            &mut self.v6
        };
        let socket = match slot {
            Some(socket) => socket,
            None => {
                let local: SocketAddr = if address.is_ipv4() {
                    ([0, 0, 0, 0], 0).into()
                } else {
                    ([0u16; 8], 0).into()
                };
                slot.insert(UdpSocket::bind(local)?)
            }
        };

        let result = socket.send_to(datagram, address).map(|_| ());
        if result.is_err() {
            // Resolve again next time, in case the agent has moved.
            self.address = None;
        }
        result
    }

    /// The agent's address, resolving the host name if it has not been
    /// resolved for [`RESOLVE_INTERVAL`]. A failed re-resolution keeps the
    /// old address.
    fn address(&mut self) -> io::Result<SocketAddr> {
        if let Some((address, resolved)) = self.address {
            if resolved.elapsed() < RESOLVE_INTERVAL {
                return Ok(address);
            }
        }

        match (self.host.as_str(), self.port).to_socket_addrs() {
            Ok(mut addresses) => {
                if let Some(address) = addresses.next() {
                    self.address = Some((address, Instant::now()));
                    return Ok(address);
                }
            }
            Err(err) => {
                if self.address.is_none() {
                    return Err(err);
                }
            }
        }

        match &mut self.address {
            Some((address, resolved)) => {
                // Keep using the old address, and try again later.
                *resolved = Instant::now();
                Ok(*address)
            }
            None => Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("no address for {}:{}", self.host, self.port),
            )),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::borrow::Cow;
    use std::time::UNIX_EPOCH;

    use super::super::thrift::Tag;
    use super::*;

    fn process() -> Process {
        Process {
            service_name: "test".to_owned(),
            tags: vec![Tag::string("hostname", "host")],
        }
    }

    fn span(span_id: u64, tag_len: usize) -> FinishedSpan {
        FinishedSpan {
            trace_id: 1,
            span_id,
            parent_id: 0,
            flags: 1,
            operation: Cow::Borrowed("op"),
            start: UNIX_EPOCH + Duration::from_secs(1_760_000_000),
            duration: Duration::from_micros(5),
            tags: if tag_len > 0 {
                vec![Tag::string("big", "x".repeat(tag_len))]
            } else {
                vec![]
            },
            logs: vec![],
            references: vec![],
        }
    }

    /// A reporter sending to a socket the test reads.
    fn reporter() -> (Reporter, UdpSocket) {
        let agent = UdpSocket::bind("127.0.0.1:0").unwrap();
        agent
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let port = agent.local_addr().unwrap().port();
        (
            Reporter::start(process(), "127.0.0.1", port).unwrap(),
            agent,
        )
    }

    fn receive(agent: &UdpSocket) -> Vec<u8> {
        let mut buf = vec![0; 70_000];
        let len = agent.recv(&mut buf).expect("expected a datagram");
        buf.truncate(len);
        buf
    }

    fn assert_nothing_more(agent: &UdpSocket) {
        agent
            .set_read_timeout(Some(Duration::from_millis(50)))
            .unwrap();
        let mut buf = vec![0; 70_000];
        assert!(agent.recv(&mut buf).is_err(), "unexpected datagram");
    }

    #[test]
    fn flush_sends_one_batch() {
        let (reporter, agent) = reporter();
        let spans = [span(1, 0), span(2, 10)];
        for span in &spans {
            reporter.report(span.clone());
        }
        assert!(reporter.flush(Duration::from_secs(5)));

        let encoded: Vec<_> = spans.iter().map(encode_span).collect();
        assert_eq!(receive(&agent), encode_emit_batch(&process(), &encoded, 1));
        assert_nothing_more(&agent);

        let stats = reporter.stats();
        assert_eq!(stats.spans_sent.load(Ordering::Relaxed), 2);
        assert_eq!(stats.batches_sent.load(Ordering::Relaxed), 1);
        assert_eq!(stats.queue_depth.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn batches_are_capped_by_span_count() {
        let (reporter, agent) = reporter();
        for id in 0..(MAX_BATCH_SPANS as u64 + 10) {
            reporter.report(span(id, 0));
        }
        assert!(reporter.flush(Duration::from_secs(5)));

        let first: Vec<_> = (0..MAX_BATCH_SPANS as u64)
            .map(|id| encode_span(&span(id, 0)))
            .collect();
        assert_eq!(receive(&agent), encode_emit_batch(&process(), &first, 1));
        let rest: Vec<_> = (MAX_BATCH_SPANS as u64..MAX_BATCH_SPANS as u64 + 10)
            .map(|id| encode_span(&span(id, 0)))
            .collect();
        assert_eq!(receive(&agent), encode_emit_batch(&process(), &rest, 2));
        assert_nothing_more(&agent);
    }

    #[test]
    fn batches_fit_in_a_datagram() {
        let (reporter, agent) = reporter();
        // Three spans of ~30 KB: two fit in one datagram, the third does not.
        for id in 0..3 {
            reporter.report(span(id, 30_000));
        }
        assert!(reporter.flush(Duration::from_secs(5)));

        let first = receive(&agent);
        let second = receive(&agent);
        assert!(first.len() <= MAX_PACKET_SIZE);
        let encoded: Vec<_> = (0..2).map(|id| encode_span(&span(id, 30_000))).collect();
        assert_eq!(first, encode_emit_batch(&process(), &encoded, 1));
        assert_eq!(
            second,
            encode_emit_batch(&process(), &[encode_span(&span(2, 30_000))], 2)
        );
    }

    #[test]
    fn oversize_span_is_dropped() {
        let (reporter, agent) = reporter();
        reporter.report(span(1, MAX_PACKET_SIZE));
        reporter.report(span(2, 0));
        assert!(reporter.flush(Duration::from_secs(5)));

        assert_eq!(
            receive(&agent),
            encode_emit_batch(&process(), &[encode_span(&span(2, 0))], 1)
        );
        assert_eq!(reporter.stats().dropped_oversize.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn partial_batch_goes_after_the_flush_interval() {
        let (reporter, agent) = reporter();
        let started = Instant::now();
        reporter.report(span(1, 0));

        receive(&agent);
        let waited = started.elapsed();
        assert!(waited >= FLUSH_INTERVAL, "sent after {waited:?}");
    }

    #[test]
    fn shutdown_sends_queued_spans_then_drops_new_ones() {
        let (reporter, agent) = reporter();
        reporter.report(span(1, 0));
        reporter.shutdown(Duration::from_secs(5));

        assert_eq!(
            receive(&agent),
            encode_emit_batch(&process(), &[encode_span(&span(1, 0))], 1)
        );

        reporter.report(span(2, 0));
        assert_eq!(reporter.stats().dropped_closed.load(Ordering::Relaxed), 1);
        assert!(!reporter.flush(Duration::from_millis(10)));
    }

    #[test]
    fn unresolvable_agent_counts_send_errors() {
        // `.invalid` never resolves (RFC 6761). Starting still succeeds: the
        // name is resolved when a batch is sent.
        let reporter = Reporter::start(process(), "agent.invalid", 6831).unwrap();
        for id in 0..2 {
            reporter.report(span(id, 0));
            assert!(reporter.flush(Duration::from_secs(30)));
        }

        let stats = reporter.stats();
        assert_eq!(stats.send_errors.load(Ordering::Relaxed), 2);
        assert_eq!(stats.spans_sent.load(Ordering::Relaxed), 0);
        assert!(stats.last_error.lock().unwrap().is_some());
    }

    #[test]
    fn agent_restart_loses_no_batch() {
        // On a connected socket, the ICMP error from a send to a closed port
        // fails the next send. The reporter's socket is unconnected, so once
        // an agent listens again its first batch arrives.
        let closed = UdpSocket::bind("127.0.0.1:0").unwrap();
        let port = closed.local_addr().unwrap().port();
        drop(closed);

        let reporter = Reporter::start(process(), "127.0.0.1", port).unwrap();
        reporter.report(span(1, 0));
        assert!(reporter.flush(Duration::from_secs(5)));

        let Ok(agent) = UdpSocket::bind(("127.0.0.1", port)) else {
            // Another test took the port in between: nothing to check.
            return;
        };
        agent
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        reporter.report(span(2, 0));
        assert!(reporter.flush(Duration::from_secs(5)));

        assert_eq!(
            receive(&agent),
            encode_emit_batch(&process(), &[encode_span(&span(2, 0))], 2)
        );
    }
}
