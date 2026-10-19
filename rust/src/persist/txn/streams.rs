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

//! The streams a persist takes IDs from: inside its transaction, as
//! Python's `get_next_txn` and `get_next_mult_txn` do, the cache stream,
//! the sticky events stream and the profile updates stream. Before its
//! transaction, as Python's `get_next_mult` does, the events or the
//! backfill stream, for the chunk's stream orderings.
//!
//! The IDs stay unfinished until the transaction has committed and Python
//! has applied its effects, as Python finishes them only after the
//! transaction's earlier `call_after` callbacks.

use std::sync::{Arc, Mutex};

use anyhow::anyhow;
use tokio_postgres::Row;

use crate::{
    database::postgres::write::{WriteError, WriteTxn},
    storage::{events::Querier, stream_ids::StreamIdTracker},
};

/// `_load_next_mult_id_txn`'s statement: `$2` IDs from the sequence `$1`.
pub const NEXTVAL_SQL: &str = "SELECT nextval($1::text) FROM generate_series(1, $2::bigint)";

/// How many IDs [`TxnStream::add_prefetched`] is asked for: enough for a
/// chunk's cache invalidations, as a rule. The rest are finished unused.
pub const PREFETCH: i64 = 8;

/// A stream's IDs taken by one persist, over all its attempts. An ID the
/// committed attempt took is handed to Python with the effects. The rest
/// are marked finished here: when an attempt fails, at the start of the
/// next attempt, or when this is dropped.
pub struct TxnStream {
    tracker: Arc<StreamIdTracker>,
    /// The Postgres sequence the IDs come from.
    sequence: &'static str,
    /// The IDs allocated and neither finished nor handed to Python.
    pending: Mutex<Vec<i64>>,
    /// Allocated ahead, for [`TxnStream::next_ids`] to hand out before it
    /// asks the sequence. Also in `pending`.
    spare: Mutex<Vec<i64>>,
}

impl TxnStream {
    fn new(tracker: Arc<StreamIdTracker>, sequence: &'static str) -> Self {
        TxnStream {
            tracker,
            sequence,
            pending: Mutex::new(Vec::new()),
            spare: Mutex::new(Vec::new()),
        }
    }

    /// The cache stream, `_cache_id_gen`.
    pub fn caches(tracker: Arc<StreamIdTracker>) -> Self {
        Self::new(tracker, "cache_invalidation_stream_seq")
    }

    /// The sticky events stream, `_sticky_events_id_gen`.
    pub fn sticky_events(tracker: Arc<StreamIdTracker>) -> Self {
        Self::new(tracker, "sticky_events_sequence")
    }

    /// The profile updates stream, `_profile_updates_id_gen`.
    pub fn profile_updates(tracker: Arc<StreamIdTracker>) -> Self {
        Self::new(tracker, "profile_updates_sequence")
    }

    /// The events stream, `_stream_id_gen`.
    pub fn events(tracker: Arc<StreamIdTracker>) -> Self {
        Self::new(tracker, "events_stream_seq")
    }

    /// The backfill stream, `_backfill_id_gen`. Its IDs are positive, as
    /// the tracker holds them. The events take them negated.
    pub fn backfill(tracker: Arc<StreamIdTracker>) -> Self {
        Self::new(tracker, "events_backfill_stream_seq")
    }

    fn lock_pending(&self) -> std::sync::MutexGuard<'_, Vec<i64>> {
        self.pending.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn lock_spare(&self) -> std::sync::MutexGuard<'_, Vec<i64>> {
        self.spare.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// The Postgres sequence the IDs come from, for a [`NEXTVAL_SQL`] sent
    /// with other reads, whose rows go to [`TxnStream::add_prefetched`].
    pub fn sequence(&self) -> &'static str {
        self.sequence
    }

    /// Keep the IDs `rows` of a [`NEXTVAL_SQL`] allocated, for
    /// [`TxnStream::next_ids`] to hand out without a round trip of its own.
    pub fn add_prefetched(&self, rows: &[Row]) -> anyhow::Result<()> {
        let ids = Fetch::begin(self).allocated_rows(rows)?;
        self.lock_spare().extend(ids);
        Ok(())
    }

    /// `_load_next_id_txn`: the next ID on the stream, unfinished.
    pub async fn next_id(&self, txn: &WriteTxn) -> Result<i64, WriteError> {
        let ids = self.next_ids(txn, 1).await?;
        ids.first()
            .copied()
            .ok_or_else(|| anyhow!("nextval returned no rows").into())
    }

    /// `_load_next_mult_id_txn`: the next `n` IDs on the stream,
    /// unfinished.
    pub async fn next_ids(&self, txn: &WriteTxn, n: usize) -> Result<Vec<i64>, WriteError> {
        let mut ids: Vec<i64> = {
            let mut spare = self.lock_spare();
            let taken = n.min(spare.len());
            spare.drain(..taken).collect()
        };
        if ids.len() < n {
            let more = i64::try_from(n - ids.len()).map_err(anyhow::Error::new)?;
            let fetch = Fetch::begin(self);
            let rows = txn.query(NEXTVAL_SQL, &[&self.sequence, &more]).await?;
            ids.extend(fetch.allocated_rows(&rows)?);
        }
        Ok(ids)
    }

    /// As [`Self::next_ids`], but outside a transaction, as `get_next_mult`
    /// fetches its IDs in autocommit. The sequence ignores transactions.
    pub async fn next_ids_outside_txn(
        &self,
        querier: &impl Querier,
        n: usize,
    ) -> anyhow::Result<Vec<i64>> {
        let n = i64::try_from(n)?;
        let fetch = Fetch::begin(self);
        let rows = querier.query(NEXTVAL_SQL, &[&self.sequence, &n]).await?;
        fetch.allocated_rows(&rows)
    }

    /// Mark finished the IDs of an attempt that failed, as Python's
    /// `call_on_exception` does.
    pub fn finish_pending(&self) {
        let ids = std::mem::take(&mut *self.lock_pending());
        if !ids.is_empty() {
            self.tracker.mark_finished(&ids);
        }
    }

    /// The transaction that took `handed` committed: Python marks them
    /// finished once it has applied the effects.
    pub fn hand_over(&self, handed: &[i64]) {
        self.lock_pending().retain(|id| !handed.contains(id));
    }
}

/// A fetch of IDs from the sequence, between the tracker's `begin_fetch`
/// and `end_fetch`. Python's `try`/`finally` cannot be interrupted, but a
/// future can be dropped at any await, so the fetch ends on drop if it did
/// not allocate. Otherwise the stream's position would stay behind it.
struct Fetch<'a> {
    stream: &'a TxnStream,
    token: Option<i64>,
}

impl<'a> Fetch<'a> {
    fn begin(stream: &'a TxnStream) -> Self {
        Fetch {
            stream,
            token: Some(stream.tracker.begin_fetch()),
        }
    }

    /// The fetch returned `rows` of IDs, which stay unfinished. Ends the
    /// fetch without allocating if a row cannot be read.
    fn allocated_rows(self, rows: &[Row]) -> anyhow::Result<Vec<i64>> {
        let ids = rows
            .iter()
            .map(|row| row.try_get(0))
            .collect::<Result<Vec<i64>, _>>()?;
        self.allocated(&ids);
        Ok(ids)
    }

    /// The fetch allocated `ids`, which stay unfinished.
    fn allocated(mut self, ids: &[i64]) {
        if let Some(token) = self.token.take() {
            self.stream.tracker.end_fetch(token, Some(ids));
            self.stream.lock_pending().extend_from_slice(ids);
        }
    }
}

impl Drop for Fetch<'_> {
    fn drop(&mut self) {
        if let Some(token) = self.token.take() {
            self.stream.tracker.end_fetch(token, None);
        }
    }
}

impl Drop for TxnStream {
    fn drop(&mut self) {
        self.finish_pending();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Allocate `ids`, as `next_ids` does without the sequence.
    fn allocate(stream: &TxnStream, ids: &[i64]) {
        Fetch::begin(stream).allocated(ids);
    }

    fn tracker() -> Arc<StreamIdTracker> {
        Arc::new(StreamIdTracker::new(
            "caches".to_owned(),
            "master".to_owned(),
            true,
        ))
    }

    #[test]
    fn ids_are_finished_unless_handed_over() {
        let tracker = tracker();
        let stream = TxnStream::caches(Arc::clone(&tracker));

        // An attempt that failed: the next one finishes its IDs.
        allocate(&stream, &[2, 3]);
        assert_eq!(tracker.get_persisted_upto_position(), 1);
        stream.finish_pending();
        assert_eq!(tracker.get_persisted_upto_position(), 3);

        // The attempt that committed hands its IDs to Python.
        allocate(&stream, &[4]);
        stream.hand_over(&[4]);
        // Allocated after the hand-over, so not Python's: finished on drop.
        allocate(&stream, &[5]);
        drop(stream);
        assert_eq!(tracker.get_persisted_upto_position(), 3);

        // Python finishes the IDs it was handed.
        tracker.mark_finished(&[4]);
        assert_eq!(tracker.get_persisted_upto_position(), 5);
    }

    #[test]
    fn a_dropped_fetch_ends() {
        let tracker = tracker();
        let stream = TxnStream::caches(Arc::clone(&tracker));
        allocate(&stream, &[2]);
        // Dropped before the sequence answered, as a cancelled future is.
        drop(Fetch::begin(&stream));
        stream.finish_pending();
        // A fetch still in flight would hold the position at 1.
        assert_eq!(tracker.get_persisted_upto_position(), 2);
    }
}
