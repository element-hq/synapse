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

//! Which destinations have new to-device messages or device list updates.
//!
//! Python skips the two device queries for a destination with nothing new,
//! by asking a stream change cache. [`StreamChanges`] answers the same
//! question for the Rust loop. Python feeds it every change it feeds its
//! cache, through a listener on the cache, so the two agree.
//!
//! It keeps one position per destination and never evicts, as there is one
//! entry per destination we have sent device updates to.

use std::{collections::HashMap, sync::Mutex};

/// A mirror of a Python `StreamChangeCache` keyed by destination.
pub struct StreamChanges {
    inner: Mutex<Inner>,
}

struct Inner {
    /// Nothing is known about changes at or before this position.
    earliest: i64,
    latest: HashMap<String, i64>,
}

impl StreamChanges {
    /// A mirror that knows about the changes after `earliest`.
    pub fn new(earliest: i64) -> Self {
        StreamChanges {
            inner: Mutex::new(Inner {
                earliest,
                latest: HashMap::new(),
            }),
        }
    }

    /// `entity_has_changed`.
    pub fn entity_has_changed(&self, destination: &str, stream_pos: i64) {
        let mut inner = self.inner.lock().expect("poisoned");
        if stream_pos <= inner.earliest {
            return;
        }
        match inner.latest.get_mut(destination) {
            Some(latest) => *latest = (*latest).max(stream_pos),
            None => {
                inner.latest.insert(destination.to_owned(), stream_pos);
            }
        }
    }

    /// `all_entities_changed`.
    pub fn all_entities_changed(&self, stream_pos: i64) {
        let mut inner = self.inner.lock().expect("poisoned");
        inner.latest.clear();
        inner.earliest = stream_pos;
    }

    /// `has_entity_changed`: whether the destination may have changed after
    /// `stream_pos`.
    pub fn has_entity_changed(&self, destination: &str, stream_pos: i64) -> bool {
        let inner = self.inner.lock().expect("poisoned");
        if stream_pos < inner.earliest {
            return true;
        }
        inner
            .latest
            .get(destination)
            .is_some_and(|latest| stream_pos < *latest)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn as_the_python_cache() {
        let changes = StreamChanges::new(10);

        // Nothing is known before the start.
        assert!(changes.has_entity_changed("a", 9));
        assert!(!changes.has_entity_changed("a", 10));

        // A change at or before the start is already accounted for.
        changes.entity_has_changed("a", 10);
        assert!(!changes.has_entity_changed("a", 10));

        changes.entity_has_changed("a", 12);
        assert!(changes.has_entity_changed("a", 11));
        assert!(!changes.has_entity_changed("a", 12));
        assert!(!changes.has_entity_changed("b", 11));

        // Positions only move forward.
        changes.entity_has_changed("a", 11);
        assert!(changes.has_entity_changed("a", 11));

        changes.all_entities_changed(20);
        assert!(changes.has_entity_changed("b", 19));
        assert!(!changes.has_entity_changed("a", 20));
    }
}
