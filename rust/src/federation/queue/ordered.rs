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

//! [`OrderedMap`]: the parts of Python's `OrderedDict` the queue uses.

use std::{
    borrow::Borrow,
    collections::{BTreeMap, HashMap},
    hash::Hash,
};

/// A map that remembers insertion order, as a Python `dict` does.
///
/// Inserting a key that is already there replaces its value and keeps its
/// place. Removing from either end, or by key, is `O(log n)`. An `IndexMap`
/// would make removing from the front `O(n)`, and the queue does that for
/// every transaction.
#[derive(Debug)]
pub struct OrderedMap<K, V> {
    /// Each key's place in `entries`.
    places: HashMap<K, u64>,
    entries: BTreeMap<u64, (K, V)>,
    next_place: u64,
}

impl<K, V> Default for OrderedMap<K, V> {
    fn default() -> Self {
        OrderedMap {
            places: HashMap::new(),
            entries: BTreeMap::new(),
            next_place: 0,
        }
    }
}

impl<K: Clone + Eq + Hash, V> OrderedMap<K, V> {
    pub fn len(&self) -> usize {
        self.places.len()
    }

    pub fn is_empty(&self) -> bool {
        self.places.is_empty()
    }

    /// `map[key] = value`.
    pub fn insert(&mut self, key: K, value: V) {
        if let Some(place) = self.places.get(&key) {
            if let Some(entry) = self.entries.get_mut(place) {
                entry.1 = value;
            }
            return;
        }
        let place = self.next_place;
        self.next_place += 1;
        self.places.insert(key.clone(), place);
        self.entries.insert(place, (key, value));
    }

    /// `map.pop(key, None)`.
    pub fn remove<Q>(&mut self, key: &Q) -> Option<V>
    where
        K: Borrow<Q>,
        Q: Hash + Eq + ?Sized,
    {
        let place = self.places.remove(key)?;
        self.entries.remove(&place).map(|(_, value)| value)
    }

    /// `map.popitem(last=False)`.
    pub fn pop_front(&mut self) -> Option<(K, V)> {
        let (_, (key, value)) = self.entries.pop_first()?;
        self.places.remove(&key);
        Some((key, value))
    }

    /// `map.popitem()`.
    pub fn pop_back(&mut self) -> Option<(K, V)> {
        let (_, (key, value)) = self.entries.pop_last()?;
        self.places.remove(&key);
        Some((key, value))
    }

    /// The values, oldest first.
    pub fn values(&self) -> impl Iterator<Item = &V> {
        self.entries.values().map(|(_, value)| value)
    }

    pub fn clear(&mut self) {
        self.places.clear();
        self.entries.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_insertion_order() {
        let mut map = OrderedMap::default();
        map.insert("a", 1);
        map.insert("b", 2);
        map.insert("c", 3);
        // Replacing keeps the place, as a Python dict does.
        map.insert("a", 10);
        assert_eq!(map.values().copied().collect::<Vec<_>>(), [10, 2, 3]);
        assert_eq!(map.len(), 3);

        assert_eq!(map.remove("b"), Some(2));
        assert_eq!(map.remove("b"), None);
        assert_eq!(map.pop_back(), Some(("c", 3)));
        map.insert("d", 4);
        assert_eq!(map.pop_front(), Some(("a", 10)));
        assert_eq!(map.pop_front(), Some(("d", 4)));
        assert!(map.is_empty());
        assert_eq!(map.pop_front(), None);
    }
}
