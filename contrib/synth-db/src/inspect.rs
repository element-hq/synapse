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

//! The `inspect` subcommand: a human-readable summary of a profile.

use std::collections::BTreeMap;
use std::path::PathBuf;

use anyhow::{Context as _, Result};
use clap::Args;

use crate::anon::log2_bucket;
use crate::model::*;
use crate::out::{self, read_section};

#[derive(Args, Debug)]
pub struct InspectArgs {
    /// Profile directory written by `profile`.
    pub dir: PathBuf,
}

fn bucket_label(b: usize) -> String {
    if b == 0 {
        "0".to_owned()
    } else {
        format!("{}-{}", 1u64 << (b - 1), (1u64 << b) - 1)
    }
}

fn print_hist(title: &str, hist: &BTreeMap<usize, u64>) {
    println!("  {title}");
    let total: u64 = hist.values().sum();
    for (b, n) in hist {
        println!(
            "    {:>16}  {:>10}  {:5.1}%",
            bucket_label(*b),
            n,
            100.0 * *n as f64 / total.max(1) as f64
        );
    }
}

pub fn run(args: InspectArgs) -> Result<()> {
    let meta_path = args.dir.join("meta.json");
    let meta: Meta = serde_json::from_slice(
        &std::fs::read(&meta_path).with_context(|| format!("reading {}", meta_path.display()))?,
    )?;
    println!(
        "profile taken at unix ms {}, window {} days",
        meta.now_ms, meta.window_days
    );
    println!(
        "server: {}",
        meta.server_version.lines().next().unwrap_or("")
    );
    println!("schema version: {:?}", meta.schema_version);
    println!("\nsections:");
    for s in out::SECTIONS {
        match meta.sections.get(*s) {
            Some(st) => println!("  {:<14} {:>10} rows  {:>8.1}s", s, st.rows, st.seconds),
            None => println!("  {:<14} (missing)", s),
        }
    }
    println!("\nlargest source tables (planner estimates):");
    let mut tables: Vec<_> = meta.tables.iter().collect();
    tables.sort_by_key(|(_, t)| std::cmp::Reverse(t.bytes));
    for (name, t) in tables.iter().take(12) {
        println!(
            "  {:<40} {:>14} rows {:>8.1} GB",
            name,
            t.rows,
            t.bytes as f64 / 1e9
        );
    }

    if out::section_done(&args.dir, "rooms") {
        let mut joined = BTreeMap::new();
        let mut state = BTreeMap::new();
        let mut n = 0u64;
        let mut encrypted = 0u64;
        let mut spaces = 0u64;
        let mut public = 0u64;
        for r in read_section::<RoomRow>(&args.dir, "rooms")? {
            let r = r?;
            n += 1;
            *joined
                .entry(log2_bucket(r.joined.unwrap_or(0).max(0) as u64))
                .or_insert(0) += 1;
            *state
                .entry(log2_bucket(r.state_events.unwrap_or(0).max(0) as u64))
                .or_insert(0) += 1;
            encrypted += r.encryption.is_some() as u64;
            spaces += (r.room_type.as_deref() == Some("m.space")) as u64;
            public += (r.join_rules.as_deref() == Some("public")) as u64;
        }
        println!(
            "\nrooms: {n} ({public} publicly joinable, {encrypted} encrypted, {spaces} spaces)"
        );
        print_hist("joined members", &joined);
        print_hist("current state events", &state);
    }

    if out::section_done(&args.dir, "users") {
        let mut rooms = BTreeMap::new();
        let mut devices = BTreeMap::new();
        let mut n = 0u64;
        let mut deactivated = 0u64;
        let mut active_30d = 0u64;
        for u in read_section::<UserRow>(&args.dir, "users")? {
            let u = u?;
            n += 1;
            deactivated += u.deactivated as u64;
            active_30d += u.last_seen_h.map(|h| h < 24 * 30).unwrap_or(false) as u64;
            *rooms
                .entry(log2_bucket(u.joined_rooms.unwrap_or(0).max(0) as u64))
                .or_insert(0) += 1;
            *devices
                .entry(log2_bucket(u.devices.max(0) as u64))
                .or_insert(0) += 1;
        }
        println!("\nusers: {n} ({deactivated} deactivated, {active_30d} seen in the last 30 days)");
        print_hist("joined rooms", &rooms);
        print_hist("devices", &devices);
    }

    if out::section_done(&args.dir, "memberships") {
        let mut by_membership: BTreeMap<String, u64> = BTreeMap::new();
        let mut with_notifs = 0u64;
        let mut with_receipt = 0u64;
        for m in read_section::<MembershipRow>(&args.dir, "memberships")? {
            let m = m?;
            *by_membership.entry(m.m).or_insert(0) += 1;
            with_notifs += m.notif.map(|n| n > 0).unwrap_or(false) as u64;
            with_receipt += m.read_lag.is_some() as u64;
        }
        println!("\nmemberships: {by_membership:?}");
        println!("  with unread notifications: {with_notifs}, with a read receipt: {with_receipt}");
    }

    if out::section_done(&args.dir, "event_sizes") {
        println!(
            "\nevent sizes (sampled at {:.4}%):",
            meta.event_sample_percent
        );
        for e in read_section::<EventSizeRow>(&args.dir, "event_sizes")?.take(12) {
            let e = e?;
            println!(
                "  {:<32} {:>5} {:>9} events, mean {:>6} bytes",
                e.t,
                if e.state { "state" } else { "" },
                e.count,
                e.bytes / e.count.max(1)
            );
        }
    }

    if out::section_done(&args.dir, "rates") {
        let mut total = 0u64;
        let mut active_rooms = 0u64;
        let mut top: Vec<(u64, u32)> = Vec::new();
        let mut by_bucket: BTreeMap<(String, bool), u64> = BTreeMap::new();
        for r in read_section::<RateRow>(&args.dir, "rates")? {
            let r = r?;
            let n: u64 = r.days.iter().sum();
            total += n;
            active_rooms += (n > 0) as u64;
            top.push((n, r.r));
            for (b, l, c) in r.by {
                *by_bucket.entry((b, l)).or_insert(0) += c;
            }
        }
        top.sort_unstable_by(|a, b| b.cmp(a));
        println!(
            "\nevents in window: {total} across {active_rooms} rooms ({:.1}/hour)",
            total as f64 / (meta.window_days as f64 * 24.0)
        );
        for ((b, l), c) in &by_bucket {
            println!(
                "  {:<20} {:<6} {:>10}",
                b,
                if *l { "local" } else { "remote" },
                c
            );
        }
        println!(
            "  busiest rooms (idx: events): {:?}",
            top.iter().take(10).map(|(n, r)| (r, n)).collect::<Vec<_>>()
        );
    }
    if out::section_done(&args.dir, "rates_global") {
        for g in read_section::<RatesGlobalRow>(&args.dir, "rates_global")? {
            let g = g?;
            println!(
                "  events in rooms without local members: {}; daily active users: {:?}",
                g.events_in_unprofiled_rooms,
                g.daily_active
                    .iter()
                    .map(|(_, u, _)| *u)
                    .collect::<Vec<_>>()
            );
        }
    }
    Ok(())
}
