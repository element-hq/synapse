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

//! Filler text. Real message bodies compress at roughly the rate of
//! natural language, which matters for TOAST and for on-disk size; random
//! bytes would not. A fixed pseudo-vocabulary gets close enough.

use rand::seq::SliceRandom;
use rand::{Rng, SeedableRng};

pub struct Vocabulary {
    words: Vec<String>,
}

impl Vocabulary {
    pub fn new(seed: u64) -> Self {
        let mut rng = rand::rngs::StdRng::seed_from_u64(seed ^ 0x5eed_7e57);
        let syllables = [
            "ka", "ri", "to", "ne", "mu", "sa", "lo", "vi", "en", "or", "ta", "il", "us", "em",
            "ar", "on", "de", "th", "an", "in", "er", "es", "re", "ti", "ng", "st", "al", "le",
        ];
        let words = (0..3000)
            .map(|_| {
                let n = rng.gen_range(1..=4);
                (0..n)
                    .map(|_| *syllables.choose(&mut rng).unwrap())
                    .collect::<String>()
            })
            .collect();
        Self { words }
    }

    /// Roughly `len` bytes of space-separated words.
    pub fn text(&self, rng: &mut impl Rng, len: usize) -> String {
        let mut s = String::with_capacity(len + 16);
        while s.len() < len {
            if !s.is_empty() {
                s.push(' ');
            }
            // A Zipf-ish bias towards the first words.
            let i = (rng.gen::<f64>().powf(2.5) * self.words.len() as f64) as usize;
            s.push_str(&self.words[i.min(self.words.len() - 1)]);
        }
        s.truncate(len);
        s
    }
}

/// A fixed-length base64-looking token.
pub fn b64ish(rng: &mut impl Rng, len: usize) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    (0..len)
        .map(|_| ALPHABET[rng.gen_range(0..ALPHABET.len())] as char)
        .collect()
}
