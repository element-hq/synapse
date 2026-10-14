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

//! Loading a profile into the compact in-memory form the generator works
//! from. Rooms, users, rates and sampled detail fit in memory even for a
//! large server; memberships (the bulk) are bucketed on disk by room so
//! that each room's local members can be read when that room is generated.

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};

use anyhow::{Context as _, Result};
use tracing::info;

use crate::model::*;
use crate::out::read_section;

pub const ROOMS_PER_BUCKET: u32 = 16_384;

pub struct Profile {
    pub meta: Meta,
    pub rooms: Vec<RoomRow>,
    pub users: Vec<UserRow>,
    pub rates: HashMap<u32, RateRow>,
    /// Sampled room detail, sorted by joined member count for nearest lookup.
    pub detail: Vec<(i64, RoomDetailRow)>,
    pub event_sizes: Vec<EventSizeRow>,
    pub events_in_window: u64,
    /// Highest stream ordering mentioned anywhere in the profile.
    pub max_so: i64,
    detail_by_room: HashMap<u32, usize>,
    membership_dir: PathBuf,
}

impl Profile {
    pub fn load(dir: &Path, work_dir: &Path) -> Result<Self> {
        let meta: Meta = serde_json::from_slice(
            &std::fs::read(dir.join("meta.json")).context("reading meta.json")?,
        )?;
        let mut rooms: Vec<RoomRow> = read_section(dir, "rooms")?.collect::<Result<_>>()?;
        rooms.sort_by_key(|r| r.idx);
        for (i, r) in rooms.iter().enumerate() {
            anyhow::ensure!(r.idx as usize == i, "rooms section has gaps in idx");
        }
        let mut users: Vec<UserRow> = read_section(dir, "users")?.collect::<Result<_>>()?;
        users.sort_by_key(|u| u.idx);
        for (i, u) in users.iter().enumerate() {
            anyhow::ensure!(u.idx as usize == i, "users section has gaps in idx");
        }
        let rates: HashMap<u32, RateRow> = read_section::<RateRow>(dir, "rates")?
            .map(|r| r.map(|r| (r.r, r)))
            .collect::<Result<_>>()?;
        let mut events_in_window: u64 = rates.values().map(|r| r.days.iter().sum::<u64>()).sum();
        for g in read_section::<RatesGlobalRow>(dir, "rates_global")? {
            events_in_window += g?.events_in_unprofiled_rooms;
        }
        let mut detail: Vec<(i64, RoomDetailRow)> =
            read_section::<RoomDetailRow>(dir, "room_detail")?
                .map(|d| {
                    d.map(|d| {
                        let joined =
                            rooms.get(d.r as usize).and_then(|r| r.joined).unwrap_or(0) as i64;
                        (joined, d)
                    })
                })
                .collect::<Result<_>>()?;
        detail.sort_by_key(|(j, _)| *j);
        let detail_by_room = detail
            .iter()
            .enumerate()
            .map(|(i, (_, d))| (d.r, i))
            .collect();
        let event_sizes: Vec<EventSizeRow> =
            read_section(dir, "event_sizes")?.collect::<Result<_>>()?;

        let membership_dir = work_dir.join("memberships");
        let (memberships, mut max_so) = bucket_memberships(dir, &membership_dir)?;
        for r in &rooms {
            max_so = max_so.max(r.last_so.unwrap_or(0));
        }
        info!(
            rooms = rooms.len(),
            users = users.len(),
            memberships,
            rooms_with_rates = rates.len(),
            sampled_rooms = detail.len(),
            events_in_window,
            "profile loaded"
        );
        Ok(Self {
            meta,
            rooms,
            users,
            rates,
            detail,
            event_sizes,
            events_in_window,
            max_so,
            detail_by_room,
            membership_dir,
        })
    }

    /// Detail for this exact room, if it was sampled.
    pub fn detail_for(&self, ridx: u32) -> Option<&RoomDetailRow> {
        self.detail_by_room.get(&ridx).map(|&i| &self.detail[i].1)
    }

    /// The membership rows of one bucket of rooms, grouped by room index.
    pub fn membership_bucket(&self, bucket: u32) -> Result<HashMap<u32, Vec<MembershipRow>>> {
        let path = self.membership_dir.join(format!("{bucket}.jsonl"));
        let mut out: HashMap<u32, Vec<MembershipRow>> = HashMap::new();
        let Ok(file) = File::open(&path) else {
            return Ok(out);
        };
        for line in BufReader::new(file).lines() {
            let m: MembershipRow = serde_json::from_str(&line?)?;
            out.entry(m.r).or_default().push(m);
        }
        for v in out.values_mut() {
            v.sort_by_key(|m| (m.so, m.u));
        }
        Ok(out)
    }

    /// Detail of the sampled room whose joined count is closest to `joined`.
    pub fn nearest_detail(&self, joined: i64) -> Option<&RoomDetailRow> {
        if self.detail.is_empty() {
            return None;
        }
        let i = self.detail.partition_point(|(j, _)| *j < joined);
        let candidates = [i.checked_sub(1), (i < self.detail.len()).then_some(i)];
        candidates
            .into_iter()
            .flatten()
            .min_by_key(|&k| (self.detail[k].0 - joined).abs())
            .map(|k| &self.detail[k].1)
    }
}

fn bucket_memberships(dir: &Path, out: &Path) -> Result<(u64, i64)> {
    std::fs::create_dir_all(out)?;
    let mut writers: HashMap<u32, BufWriter<File>> = HashMap::new();
    let mut n = 0u64;
    let mut max_so = 0i64;
    for row in read_section::<MembershipRow>(dir, "memberships")? {
        let row = row?;
        max_so = max_so.max(row.so.unwrap_or(0));
        let bucket = row.r / ROOMS_PER_BUCKET;
        let w = match writers.get_mut(&bucket) {
            Some(w) => w,
            None => {
                let f = File::create(out.join(format!("{bucket}.jsonl")))?;
                writers.entry(bucket).or_insert(BufWriter::new(f))
            }
        };
        serde_json::to_writer(&mut *w, &row)?;
        w.write_all(b"\n")?;
        n += 1;
    }
    for (_, mut w) in writers {
        w.flush()?;
    }
    Ok((n, max_so))
}

/// Picks event sizes from the profile's per-type log2 histograms.
pub struct SizeModel {
    by_type: HashMap<(String, bool), Vec<u64>>,
    /// Pooled histograms for unknown types: [timeline, state].
    fallback: [Vec<u64>; 2],
}

impl SizeModel {
    pub fn new(rows: &[EventSizeRow]) -> Self {
        let mut by_type = HashMap::new();
        let mut fallback = [vec![0u64; 16], vec![0u64; 16]];
        for r in rows {
            by_type.insert((r.t.clone(), r.state), r.buckets.clone());
            let pooled = &mut fallback[r.state as usize];
            for (i, b) in r.buckets.iter().enumerate() {
                if pooled.len() <= i {
                    pooled.resize(i + 1, 0);
                }
                pooled[i] += b;
            }
        }
        for pooled in &mut fallback {
            if pooled.iter().all(|&b| b == 0) {
                pooled[10] = 1; // ~512-1023 bytes
            }
        }
        Self { by_type, fallback }
    }

    /// A target serialised size in bytes for an event of this type.
    pub fn sample(&self, rng: &mut impl rand::Rng, event_type: &str, state: bool) -> usize {
        let hist = self
            .by_type
            .get(&(event_type.to_owned(), state))
            .filter(|h| h.iter().any(|&b| b > 0))
            .unwrap_or(&self.fallback[state as usize]);
        let total: u64 = hist.iter().sum();
        let mut pick = rng.gen_range(0..total);
        let mut bucket = 0;
        for (i, &b) in hist.iter().enumerate() {
            if pick < b {
                bucket = i;
                break;
            }
            pick -= b;
        }
        if bucket == 0 {
            return 0;
        }
        let lo = 1usize << (bucket - 1);
        let hi = (1usize << bucket).max(lo + 1);
        rng.gen_range(lo..hi)
    }
}
