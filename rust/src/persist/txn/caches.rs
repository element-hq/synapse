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

//! The cache stream: `_invalidate_cache_and_stream`, for the caches that
//! other workers cannot tell they must invalidate from the other streams.
//!
//! Each invalidation writes a `cache_invalidation_stream_by_instance` row
//! inside the transaction, with an ID from the cache stream's
//! [`StreamIdTracker`], and asks Python to invalidate the cache locally.
//! The IDs stay unfinished until Python has done so, as Python's
//! `get_next_txn` finishes them only after the transaction's earlier
//! `call_after` callbacks.

use std::sync::{Arc, Mutex};

use anyhow::anyhow;

use super::TxnInput;
use crate::{
    database::postgres::write::{WriteError, WriteTxn},
    persist::effects::PersistEffects,
    storage::stream_ids::StreamIdTracker,
};

/// The cache stream IDs of one persist, over all its attempts. An ID the
/// committed attempt wrote is handed to Python with the effects. The rest
/// are marked finished here: at the start of the next attempt, or when
/// this is dropped.
pub struct CacheStream {
    tracker: Arc<StreamIdTracker>,
    /// The IDs allocated and neither finished nor handed to Python.
    pending: Mutex<Vec<i64>>,
}

impl CacheStream {
    pub fn new(tracker: Arc<StreamIdTracker>) -> Self {
        CacheStream {
            tracker,
            pending: Mutex::new(Vec::new()),
        }
    }

    fn pending(&self) -> std::sync::MutexGuard<'_, Vec<i64>> {
        self.pending.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `_load_next_id_txn`: the next ID on the stream, unfinished.
    async fn next_id(&self, txn: &WriteTxn) -> Result<i64, WriteError> {
        let fetch = Fetch::begin(self);
        let rows = txn
            .query("SELECT nextval('cache_invalidation_stream_seq')", &[])
            .await?;
        let row = rows
            .first()
            .ok_or_else(|| anyhow!("nextval returned no rows"))?;
        let id = row.try_get(0).map_err(anyhow::Error::new)?;
        fetch.allocated(id);
        Ok(id)
    }

    /// Mark finished the IDs of an attempt that failed, as Python's
    /// `call_on_exception` does.
    pub fn finish_pending(&self) {
        let ids = std::mem::take(&mut *self.pending());
        if !ids.is_empty() {
            self.tracker.mark_finished(&ids);
        }
    }

    /// The transaction that wrote `effects` committed: Python marks its IDs
    /// finished once it has applied them.
    pub fn hand_over(&self, effects: &PersistEffects) {
        let handed = effects.cache_stream_ids();
        self.pending().retain(|id| !handed.contains(id));
    }
}

/// A fetch of an ID from the sequence, between the tracker's `begin_fetch`
/// and `end_fetch`. Python's `try`/`finally` cannot be interrupted, but a
/// future can be dropped at any await, so the fetch ends on drop if it did
/// not allocate. Otherwise the stream's position would stay behind it.
struct Fetch<'a> {
    stream: &'a CacheStream,
    token: Option<i64>,
}

impl<'a> Fetch<'a> {
    fn begin(stream: &'a CacheStream) -> Self {
        Fetch {
            stream,
            token: Some(stream.tracker.begin_fetch()),
        }
    }

    /// The fetch allocated `id`, which stays unfinished.
    fn allocated(mut self, id: i64) {
        if let Some(token) = self.token.take() {
            self.stream.tracker.end_fetch(token, Some(&[id]));
            self.stream.pending().push(id);
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

impl Drop for CacheStream {
    fn drop(&mut self) {
        self.finish_pending();
    }
}

/// `_invalidate_cache_and_stream`: invalidate `keys` in the named cache
/// here once the transaction commits, and on the other workers through the
/// cache stream.
pub async fn invalidate_cache_and_stream(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    effects: &mut PersistEffects,
    cache_name: &str,
    keys: Vec<String>,
) -> Result<(), WriteError> {
    let stream_id = input.cache_stream.next_id(txn).await?;
    txn.execute(
        "INSERT INTO cache_invalidation_stream_by_instance
            (stream_id, instance_name, cache_func, keys, invalidation_ts)
        VALUES ($1, $2, $3, $4, $5)",
        &[
            &stream_id,
            &input.instance_name,
            &cache_name,
            &keys,
            &input.now_ms()?,
        ],
    )
    .await?;
    effects.add_cache_invalidation(cache_name, keys);
    effects.add_cache_stream_id(stream_id);
    effects.set_new_replication_data();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Allocate the next ID, as `next_id` does without the sequence.
    fn allocate(stream: &CacheStream, id: i64) {
        Fetch::begin(stream).allocated(id);
    }

    #[test]
    fn ids_are_finished_unless_handed_over() {
        let tracker = Arc::new(StreamIdTracker::new(
            "caches".to_owned(),
            "master".to_owned(),
            true,
        ));
        let stream = CacheStream::new(Arc::clone(&tracker));

        // An attempt that failed: the next one finishes its IDs.
        allocate(&stream, 2);
        allocate(&stream, 3);
        assert_eq!(tracker.get_persisted_upto_position(), 1);
        stream.finish_pending();
        assert_eq!(tracker.get_persisted_upto_position(), 3);

        // The attempt that committed hands its IDs to Python.
        allocate(&stream, 4);
        let mut effects = PersistEffects::default();
        effects.add_cache_stream_id(4);
        stream.hand_over(&effects);
        // Allocated after the hand-over, so not Python's: finished on drop.
        allocate(&stream, 5);
        drop(stream);
        assert_eq!(tracker.get_persisted_upto_position(), 3);

        // Python finishes the IDs it was handed.
        tracker.mark_finished(&[4]);
        assert_eq!(tracker.get_persisted_upto_position(), 5);
    }

    #[test]
    fn a_dropped_fetch_ends() {
        let tracker = Arc::new(StreamIdTracker::new(
            "caches".to_owned(),
            "master".to_owned(),
            true,
        ));
        let stream = CacheStream::new(Arc::clone(&tracker));
        allocate(&stream, 2);
        // Dropped before the sequence answered, as a cancelled future is.
        drop(Fetch::begin(&stream));
        stream.finish_pending();
        // A fetch still in flight would hold the position at 1.
        assert_eq!(tracker.get_persisted_upto_position(), 2);
    }
}
