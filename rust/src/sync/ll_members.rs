/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 */

//! The membership events a lazy-loading client has been sent, so that later
//! syncs can leave them out: `LazyLoadedMembersCache` in
//! `synapse.handlers.sync`, one per `(user, device)`.
//!
//! A member may only be left out if the client processed the response that
//! carried it. Each entry therefore records the `next_batch` position of
//! the response that carried it. A client that has processed a response
//! syncs from its `next_batch` next, so a request from `since` means the
//! client processed every response up to `since` and none after it.
//! [`LazyLoadedMembersCache::start_request`] forgets the entries recorded
//! after `since`: they were in responses the client lost, by timing out a
//! slow request and retrying it, or by reloading from an older persisted
//! token. Entries at or before `since` stay. An initial sync forgets
//! everything.
//!
//! [`LazyLoadedMembersCaches`] holds the per-device caches and drops the
//! ones not used for [`LAZY_LOADED_MEMBERS_CACHE_MAX_AGE`], as the Python
//! `ExpiringCache` does.

use std::{
    collections::HashMap,
    num::NonZeroUsize,
    sync::Arc,
    time::{Duration, SystemTime},
};

use pyo3::prelude::*;

use crate::types::stream_token::RoomStreamToken;

/// Remember the last 100 members we sent to a client.
pub const LAZY_LOADED_MEMBERS_CACHE_MAX_SIZE: usize = 100;

/// Keep a device's cache for no more than 30 minutes without use.
pub const LAZY_LOADED_MEMBERS_CACHE_MAX_AGE: Duration = Duration::from_secs(30 * 60);

/// The members sent to one device. See the module docs.
#[pyclass(name = "LazyLoadedMembersCache")]
pub struct LazyLoadedMembersCache {
    /// User ID to the event ID of the membership we last sent and the room
    /// position of the `next_batch` of the response that carried it. Least
    /// recently used first.
    sent: lru::LruCache<Arc<str>, (Arc<str>, RoomStreamToken)>,

    /// An upper bound on the positions of the entries, so that
    /// `start_request` need not look at them when nothing was recorded
    /// after `since`. That is the steady state.
    newest: Option<RoomStreamToken>,
}

impl Default for LazyLoadedMembersCache {
    fn default() -> Self {
        Self::new()
    }
}

impl LazyLoadedMembersCache {
    pub fn new() -> Self {
        LazyLoadedMembersCache {
            sent: lru::LruCache::new(
                NonZeroUsize::new(LAZY_LOADED_MEMBERS_CACHE_MAX_SIZE).expect("non-zero"),
            ),
            newest: None,
        }
    }

    /// Forget the members the client did not receive. Called at the start
    /// of each sync request for this device, with the request's `since`
    /// room position, or `None` for an initial sync.
    pub fn start_request(&mut self, since: Option<&RoomStreamToken>) {
        let Some(since) = since else {
            self.sent.clear();
            self.newest = None;
            return;
        };

        // The common case: nothing has been recorded after `since`.
        if self
            .newest
            .as_ref()
            .is_none_or(|newest| newest.is_before_or_eq(since))
        {
            return;
        }

        let lost: Vec<Arc<str>> = self
            .sent
            .iter()
            .filter(|(_, (_, sent_at))| !sent_at.is_before_or_eq(since))
            .map(|(user_id, _)| Arc::clone(user_id))
            .collect();
        for user_id in lost {
            self.sent.pop(&user_id);
        }

        // Everything left is at or before `since`.
        self.newest = Some(since.clone());
    }

    /// Whether `event_id` is the membership of `user_id` we last sent. A hit
    /// counts as a use.
    pub fn was_sent(&mut self, user_id: &str, event_id: &str) -> bool {
        match self.sent.get(user_id) {
            Some((sent_event_id, _)) => &**sent_event_id == event_id,
            None => false,
        }
    }

    /// Record that `event_id` is being sent in the response ending at
    /// `next_batch`.
    pub fn mark_sent(&mut self, user_id: &str, event_id: &str, next_batch: &RoomStreamToken) {
        self.sent.push(
            Arc::from(user_id),
            (Arc::from(event_id), next_batch.clone()),
        );

        // Only move the bound forwards. Positions taken by this process only
        // ever advance, but a response that started earlier may record its
        // entries after a later one has.
        if self
            .newest
            .as_ref()
            .is_none_or(|newest| newest.is_before_or_eq(next_batch))
        {
            self.newest = Some(next_batch.clone());
        }
    }

    pub fn len(&self) -> usize {
        self.sent.len()
    }

    pub fn is_empty(&self) -> bool {
        self.sent.is_empty()
    }
}

#[pymethods]
impl LazyLoadedMembersCache {
    #[new]
    fn py_new() -> Self {
        Self::new()
    }

    #[pyo3(name = "start_request", signature = (since))]
    fn py_start_request(&mut self, since: Option<RoomStreamToken>) {
        self.start_request(since.as_ref())
    }

    #[pyo3(name = "was_sent")]
    fn py_was_sent(&mut self, user_id: &str, event_id: &str) -> bool {
        self.was_sent(user_id, event_id)
    }

    #[pyo3(name = "mark_sent")]
    fn py_mark_sent(&mut self, user_id: &str, event_id: &str, next_batch: RoomStreamToken) {
        self.mark_sent(user_id, event_id, &next_batch)
    }

    fn __len__(&self) -> usize {
        self.len()
    }
}

/// A device's cache, and when it was last used.
struct Entry {
    cache: LazyLoadedMembersCache,
    last_used: SystemTime,
}

/// The caches of every `(user, device)`, dropped after
/// [`LAZY_LOADED_MEMBERS_CACHE_MAX_AGE`] without use. The Python
/// `ExpiringCache` prunes on a timer; this prunes on use, at most once per
/// half the max age, which bounds the work the same way.
pub struct LazyLoadedMembersCaches {
    entries: HashMap<(Arc<str>, Option<Arc<str>>), Entry>,
    last_pruned: Option<SystemTime>,
}

impl Default for LazyLoadedMembersCaches {
    fn default() -> Self {
        Self::new()
    }
}

impl LazyLoadedMembersCaches {
    pub fn new() -> Self {
        LazyLoadedMembersCaches {
            entries: HashMap::new(),
            last_pruned: None,
        }
    }

    /// The cache for `(user_id, device_id)`, created if it is not here, as
    /// used at `now`.
    pub fn get_or_create(
        &mut self,
        user_id: &str,
        device_id: Option<&str>,
        now: SystemTime,
    ) -> &mut LazyLoadedMembersCache {
        self.maybe_prune(now);
        let entry = self
            .entries
            .entry((Arc::from(user_id), device_id.map(Arc::from)))
            .or_insert_with(|| Entry {
                cache: LazyLoadedMembersCache::new(),
                last_used: now,
            });
        entry.last_used = now;
        &mut entry.cache
    }

    /// Drop the caches not used since `now - max_age`, if it has been half a
    /// max age since the last prune.
    fn maybe_prune(&mut self, now: SystemTime) {
        let due = match self.last_pruned {
            None => true,
            Some(last) => now
                .duration_since(last)
                .is_ok_and(|since| since >= LAZY_LOADED_MEMBERS_CACHE_MAX_AGE / 2),
        };
        if !due {
            return;
        }
        self.last_pruned = Some(now);
        self.entries.retain(|_, entry| {
            now.duration_since(entry.last_used)
                .is_ok_and(|unused| unused <= LAZY_LOADED_MEMBERS_CACHE_MAX_AGE)
                || now < entry.last_used
        });
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use std::time::UNIX_EPOCH;

    use super::*;

    fn at(stream: i64) -> RoomStreamToken {
        RoomStreamToken::live(stream)
    }

    #[test]
    fn forgets_only_entries_after_since() {
        let mut cache = LazyLoadedMembersCache::new();
        cache.mark_sent("@a:test", "$a", &at(5));
        cache.mark_sent("@b:test", "$b", &at(7));

        // The client synced past both responses.
        cache.start_request(Some(&at(7)));
        assert!(cache.was_sent("@a:test", "$a"));
        assert!(cache.was_sent("@b:test", "$b"));

        // The client lost the response that ended at 7.
        cache.start_request(Some(&at(6)));
        assert!(cache.was_sent("@a:test", "$a"));
        assert!(!cache.was_sent("@b:test", "$b"));

        // A later request from an even older token forgets the rest.
        cache.start_request(Some(&at(4)));
        assert!(!cache.was_sent("@a:test", "$a"));
    }

    #[test]
    fn initial_sync_forgets_everything() {
        let mut cache = LazyLoadedMembersCache::new();
        cache.mark_sent("@a:test", "$a", &at(5));
        cache.start_request(None);
        assert!(!cache.was_sent("@a:test", "$a"));
        assert!(cache.is_empty());
    }

    #[test]
    fn different_event_is_not_sent() {
        let mut cache = LazyLoadedMembersCache::new();
        cache.mark_sent("@a:test", "$a1", &at(5));
        assert!(!cache.was_sent("@a:test", "$a2"));
    }

    #[test]
    fn out_of_order_marks_are_still_forgotten() {
        let mut cache = LazyLoadedMembersCache::new();
        cache.mark_sent("@b:test", "$b", &at(7));
        cache.mark_sent("@a:test", "$a", &at(5));
        cache.start_request(Some(&at(6)));
        assert!(cache.was_sent("@a:test", "$a"));
        assert!(!cache.was_sent("@b:test", "$b"));
    }

    #[test]
    fn evicts_least_recently_used() {
        let mut cache = LazyLoadedMembersCache::new();
        for i in 0..LAZY_LOADED_MEMBERS_CACHE_MAX_SIZE {
            cache.mark_sent(&format!("@u{i}:test"), &format!("$u{i}"), &at(1));
        }
        // Touch the oldest entry so that it is no longer the eviction
        // candidate.
        assert!(cache.was_sent("@u0:test", "$u0"));
        cache.mark_sent("@new:test", "$new", &at(1));
        assert!(cache.was_sent("@u0:test", "$u0"));
        assert!(!cache.was_sent("@u1:test", "$u1"));
        assert!(cache.was_sent("@new:test", "$new"));
        assert_eq!(cache.len(), LAZY_LOADED_MEMBERS_CACHE_MAX_SIZE);
    }

    #[test]
    fn per_device_caches_expire_without_use() {
        let mut caches = LazyLoadedMembersCaches::new();
        let t0 = UNIX_EPOCH + Duration::from_secs(1_000_000);
        caches
            .get_or_create("@a:test", Some("D1"), t0)
            .mark_sent("@b:test", "$b", &at(1));
        caches.get_or_create("@a:test", None, t0);
        assert_eq!(caches.len(), 2);

        // Used within the max age: kept, and the use restamps it.
        let t1 = t0 + LAZY_LOADED_MEMBERS_CACHE_MAX_AGE - Duration::from_secs(1);
        assert!(caches
            .get_or_create("@a:test", Some("D1"), t1)
            .was_sent("@b:test", "$b"));
        assert_eq!(caches.len(), 2);

        // At the next prune, the one unused for over a max age goes and
        // the one used at `t1` stays.
        let t2 = t1 + LAZY_LOADED_MEMBERS_CACHE_MAX_AGE / 2;
        caches.get_or_create("@c:test", None, t2);
        assert_eq!(caches.len(), 2);
        assert!(caches
            .get_or_create("@a:test", Some("D1"), t2)
            .was_sent("@b:test", "$b"));
    }
}
