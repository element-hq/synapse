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

//! The caches of events and related data on the Rust [`Store`](super::store::Store).
//!
//! Each is a [`Cache`] in the homeserver's registry, so it takes part in
//! age-based expiry, cache factors and metrics like any other. They are
//! filled by whichever Rust code loads the data, with [`Cache::insert`],
//! rather than through `get_or_load`, as a reader usually loads a batch of
//! keys in one statement.
//!
//! - [`EventCache`]: events loaded from the database, keyed on event ID and
//!   sized by count. An entry holds the event as persisted, rejected or
//!   not, and its redacted form if it had a valid redaction when loaded,
//!   so it goes stale when the event is redacted, its rejection status
//!   changes, or its internal metadata does. `EventsWorkerStore`
//!   invalidates it alongside its own event cache, by event ID and on a
//!   room purge, so the two stay in step.
//! - [`ChainPositionCache`]: the chain cover position of an event, which is
//!   fixed once assigned. Invalidated with the event.
//! - [`ChainLinkCache`]: the links out of a chain, which only grow as events
//!   are appended to the chain. See [`CachedChainLinks`].
//! - [`StateGroupCache`]: a state group's state, keyed on the group ID and
//!   weighed by its entries. State groups are immutable and their IDs are
//!   never reused, so an entry can only go stale by the group being deleted,
//!   which makes it dead weight rather than wrong.
//!
//! [`StoreCaches`] bundles them, as `EventsWorkerStore` invalidates them
//! together.

use std::sync::Arc;

use crate::caches::{registry::CacheRegistry, Cache};
use crate::state::StateMap;
use crate::storage::events::CachedEvent;

pub type EventCache = Cache<Arc<str>, CachedEvent>;

/// An event's position in the chain cover index: `(chain_id, sequence_number)`.
pub type ChainPosition = (i64, i64);

pub type ChainPositionCache = Cache<Arc<str>, ChainPosition>;

/// A link in the chain cover index: `(origin_sequence_number, target_chain_id,
/// target_sequence_number)`, read from `event_auth_chain_links` for one
/// origin chain.
pub type ChainLink = (i64, i64, i64);

/// The links out of one chain, as read from the database.
///
/// A chain grows at the end: an event appended at sequence number `n` adds
/// links out of `(chain, n)`, and nothing changes the links below it. So the
/// links are complete for every origin sequence number up to the highest one
/// known to be persisted when they were read, and a reader walking backwards
/// from a lower position can use them as they are.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CachedChainLinks {
    /// The highest sequence number on the chain known to exist when the links
    /// were read: the links are complete up to it.
    pub complete_to: i64,
    pub links: Arc<[ChainLink]>,
}

/// Weighed by links, so a chain with many links counts for more.
pub type ChainLinkCache = Cache<i64, CachedChainLinks>;

/// State group ID to its state, weighed by entries.
pub type StateGroupCache = Cache<i64, Arc<StateMap>>;

/// The bound on the event cache, in events.
/// TODO: size this from `event_cache_size`, as the Python event cache is.
const EVENT_CACHE_MAX_SIZE: usize = 10_000;

/// The bound on the chain position cache, in events. A state resolution needs
/// the position of every event in its state sets, so this holds more than the
/// event cache, whose events are only the conflicted ones and their auth
/// difference.
const CHAIN_POSITION_CACHE_MAX_SIZE: usize = 100_000;

/// The bound on the chain link cache, in links.
const CHAIN_LINK_CACHE_MAX_SIZE: usize = 1_000_000;

/// The bound on the state group cache, in state entries. The Python state
/// group caches hold 50,000 non-member and 500,000 member entries.
const STATE_GROUP_CACHE_MAX_SIZE: usize = 500_000;

/// The caches the Rust store's readers consult before the database.
pub struct StoreCaches {
    pub events: Arc<EventCache>,
    pub chain_positions: Arc<ChainPositionCache>,
    pub chain_links: Arc<ChainLinkCache>,
    pub state_groups: Arc<StateGroupCache>,
}

impl StoreCaches {
    /// Build the caches in `registry`, the registry of the homeserver's Rust
    /// runtime.
    pub fn new(registry: &CacheRegistry) -> Self {
        StoreCaches {
            events: registry.new_cache("rust_events", EVENT_CACHE_MAX_SIZE),
            chain_positions: registry
                .new_cache("rust_chain_positions", CHAIN_POSITION_CACHE_MAX_SIZE),
            chain_links: registry.new_weighted_cache(
                "rust_chain_links",
                CHAIN_LINK_CACHE_MAX_SIZE,
                |links| links.links.len(),
            ),
            state_groups: registry.new_weighted_cache(
                "rust_state_groups",
                STATE_GROUP_CACHE_MAX_SIZE,
                |state| state.len(),
            ),
        }
    }

    /// Drop what is cached about an event: the event, and its chain position
    /// in case the event is purged and later received again.
    pub fn invalidate_event(&self, event_id: &str) {
        let event_id: Arc<str> = Arc::from(event_id);
        drop(self.events.invalidate(&event_id));
        self.chain_positions.invalidate(&event_id);
    }

    /// Empty every cache. A room purge deletes its chains, links and state
    /// groups, and the chain links and state groups are keyed on IDs rather
    /// than room, so all go.
    pub fn clear(&self) {
        drop(self.events.invalidate_all());
        drop(self.chain_positions.invalidate_all());
        drop(self.chain_links.invalidate_all());
        drop(self.state_groups.invalidate_all());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn store_caches_invalidate_together() {
        let registry = CacheRegistry::new();
        let caches = StoreCaches::new(&registry);
        caches.chain_positions.insert(Arc::from("$a"), (1, 1));
        caches.chain_positions.insert(Arc::from("$b"), (1, 2));
        caches.chain_links.insert(
            1,
            CachedChainLinks {
                complete_to: 2,
                links: Arc::from([]),
            },
        );
        caches.state_groups.insert(1, Arc::new(StateMap::new()));

        caches.invalidate_event("$a");
        assert!(caches.chain_positions.get(&Arc::from("$a")).is_none());
        assert_eq!(caches.chain_positions.get(&Arc::from("$b")), Some((1, 2)));
        assert_eq!(caches.chain_links.len(), 1);

        caches.clear();
        assert!(caches.chain_positions.is_empty());
        assert!(caches.chain_links.is_empty());
        assert!(caches.state_groups.is_empty());
    }

    #[test]
    fn chain_links_are_weighed_by_link() {
        let registry = CacheRegistry::new();
        let caches = StoreCaches::new(&registry);
        caches.chain_links.insert(
            1,
            CachedChainLinks {
                complete_to: 2,
                links: Arc::from([(1, 2, 3), (2, 2, 4)]),
            },
        );
        assert_eq!(
            (caches.chain_links.len(), caches.chain_links.size()),
            (1, 2)
        );
    }
}
