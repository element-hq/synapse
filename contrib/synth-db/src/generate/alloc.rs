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

//! Stream ordering allocation.
//!
//! The profile carries real stream orderings (membership positions, room
//! tails, receipt lags) from the source server. The synthetic database keeps
//! that stream space: events the profile positions exactly are placed at
//! their recorded stream ordering, and everything else is slotted in
//! between. A growable bitmap tracks used positions; a collision probes
//! upwards to the next free slot, which moves an event by a negligible
//! amount. Because allocation is deterministic, the generator can replay
//! it in several passes and get identical positions each time.

pub struct StreamAlloc {
    bits: Vec<u64>,
    max_used: i64,
}

impl StreamAlloc {
    pub fn new() -> Self {
        Self {
            bits: Vec::new(),
            max_used: 0,
        }
    }

    /// Reserve the first free stream ordering at or above `target`
    /// (and at least 1).
    pub fn take(&mut self, target: i64) -> i64 {
        let mut so = target.max(1) as u64;
        loop {
            let word = (so / 64) as usize;
            if word >= self.bits.len() {
                self.bits.resize(word + 1024, 0);
            }
            let free = !self.bits[word] >> (so % 64);
            if free == 0 {
                so = (so / 64 + 1) * 64;
                continue;
            }
            so += free.trailing_zeros() as u64;
            let word = (so / 64) as usize;
            if word >= self.bits.len() {
                self.bits.resize(word + 1024, 0);
            }
            self.bits[word] |= 1 << (so % 64);
            break;
        }
        let so = so as i64;
        self.max_used = self.max_used.max(so);
        so
    }

    pub fn max_used(&self) -> i64 {
        self.max_used
    }
}

/// Linear mapping between stream ordering and wall-clock time, anchored so
/// that the highest allocated position is "now".
#[derive(Clone, Copy)]
pub struct Timeline {
    pub now_ms: i64,
    pub max_so: i64,
    /// Milliseconds per stream ordering unit.
    pub ms_per_so: f64,
}

impl Timeline {
    pub fn ts(&self, so: i64) -> i64 {
        let back = (self.max_so - so) as f64 * self.ms_per_so;
        self.now_ms - back.round() as i64
    }

    pub fn so_for_ms_ago(&self, ms_ago: i64) -> i64 {
        self.max_so - (ms_ago as f64 / self.ms_per_so).round() as i64
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probes_past_used_slots() {
        let mut a = StreamAlloc::new();
        assert_eq!(a.take(10), 10);
        assert_eq!(a.take(10), 11);
        assert_eq!(a.take(0), 1);
        assert_eq!(a.take(-5), 2);
        for _ in 0..200 {
            a.take(100);
        }
        assert_eq!(a.take(100), 300);
        assert_eq!(a.max_used(), 300);
    }

    #[test]
    fn replay_is_identical() {
        let run = || {
            let mut a = StreamAlloc::new();
            (0..1000).map(|i| a.take((i * 7) % 300)).collect::<Vec<_>>()
        };
        assert_eq!(run(), run());
    }
}
