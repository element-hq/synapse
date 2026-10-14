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

//! Per-room generation: plan the room's events in stream order, then emit
//! them with correct hashes, state groups and derived rows.
//!
//! Planning is deterministic (seeded by the room index) and is run in every
//! pass; it allocates stream orderings through the shared allocator, so the
//! replay in each pass lands every event at the same position.

use std::collections::{BTreeMap, HashMap};

use anyhow::Result;
use rand::rngs::StdRng;
use rand::seq::SliceRandom;
use rand::{Rng, SeedableRng};
use serde_json::{json, Value};

use super::alloc::StreamAlloc;
use super::event;
use super::sink::F;
use super::sinks::{put, Pass, Sinks};
use super::{Counters, GenCtx};
use crate::model::{MembershipRow, RoomRow};

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Who {
    Local(u32),
    /// (server slot in the plan, index into that server's user pool).
    Remote(u16, u32),
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
pub enum Membership {
    Join,
    Leave,
    Ban,
    Invite,
    Knock,
}

impl Membership {
    pub fn as_str(self) -> &'static str {
        match self {
            Membership::Join => "join",
            Membership::Leave => "leave",
            Membership::Ban => "ban",
            Membership::Invite => "invite",
            Membership::Knock => "knock",
        }
    }

    fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "join" => Membership::Join,
            "leave" => Membership::Leave,
            "ban" => Membership::Ban,
            "invite" => Membership::Invite,
            "knock" => Membership::Knock,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Bucket {
    Message,
    Encrypted,
    State,
    Other,
}

#[derive(Clone, Debug)]
enum StateKind {
    PowerLevels,
    JoinRules,
    HistoryVisibility,
    GuestAccess,
    Name,
    Topic,
    Avatar,
    Alias,
    Encryption,
    Custom(String, String),
}

#[derive(Clone, Debug)]
enum Kind {
    Create,
    Member(Who, Membership),
    State(StateKind),
    /// (bucket, sender is local)
    Timeline(Bucket, bool),
    /// (index of the root event in the plan, sender is local)
    ThreadReply(usize, bool),
}

type MakeContent = Box<dyn Fn(Option<&str>) -> Value>;

/// Event content, either fixed or padded with filler text to a target
/// serialised size once the event's skeleton size is known.
enum Pad {
    Fixed(Value),
    Sized {
        target: usize,
        make: MakeContent,
        /// Bytes the content keys add on top of the filler itself.
        key_overhead: usize,
        max: usize,
    },
}

impl Pad {
    fn fixed(v: Value) -> Self {
        Pad::Fixed(v)
    }

    fn new(
        target: usize,
        make: impl Fn(Option<&str>) -> Value + 'static,
        key_overhead: usize,
        max: usize,
    ) -> Self {
        Pad::Sized {
            target,
            make: Box::new(make),
            key_overhead,
            max,
        }
    }
}

#[derive(Clone, Debug)]
struct Planned {
    so: i64,
    kind: Kind,
}

pub struct Plan {
    events: Vec<Planned>,
    /// Remote server names used by this room, by slot.
    pub servers: Vec<String>,
    /// Distinct remote users per server slot.
    pub remote_count: Vec<u32>,
    creator: Who,
    /// Event count before remote identities are resolved; used by pass 0.
    pub planned_state_rows: u64,
}

impl Plan {
    pub fn len(&self) -> usize {
        self.events.len()
    }
}

fn pick<'a>(v: Option<&str>, allowed: &[&'a str], default: &'a str) -> &'a str {
    match v {
        Some(s) => allowed.iter().copied().find(|a| *a == s).unwrap_or(default),
        None => default,
    }
}

fn room_rng(ctx: &GenCtx, ridx: u32, stream: u64) -> StdRng {
    StdRng::seed_from_u64(ctx.args.seed ^ ((ridx as u64) << 8) ^ stream)
}

/// Spread `n` items over `weights.len()` slots in proportion to the weights.
fn distribute(rng: &mut StdRng, n: u32, weights: &[f64]) -> Vec<u32> {
    let mut out = vec![0u32; weights.len()];
    if weights.is_empty() || n == 0 {
        return out;
    }
    let total: f64 = weights.iter().sum();
    for _ in 0..n {
        let mut pick = rng.gen::<f64>() * total;
        let mut slot = weights.len() - 1;
        for (i, w) in weights.iter().enumerate() {
            if pick < *w {
                slot = i;
                break;
            }
            pick -= w;
        }
        out[slot] += 1;
    }
    out
}

/// Plan the room: decide every event and its stream ordering.
pub fn plan(
    ctx: &GenCtx,
    alloc: &mut StreamAlloc,
    ridx: u32,
    room: &RoomRow,
    local_rows: &[MembershipRow],
) -> Plan {
    let mut rng = room_rng(ctx, ridx, 1);
    let tl = &ctx.profile_tl;
    let so_per_day = (86_400_000.0 / tl.ms_per_so).max(1.0);

    let local_min_so = local_rows.iter().filter_map(|m| m.so).min();
    let local_max_so = local_rows.iter().filter_map(|m| m.so).max();
    let created_so = room.created_h.map(|h| tl.so_for_ms_ago(h * 3_600_000));
    let so_last = room
        .last_so
        .or(local_max_so)
        .or(created_so.map(|c| c + (so_per_day * 7.0) as i64))
        .unwrap_or(1)
        .max(2);

    // Non-member current state beyond the basics, from the room's own
    // detail if it was sampled, else from the sampled room nearest in size.
    let mut initial: Vec<StateKind> = vec![
        StateKind::PowerLevels,
        StateKind::JoinRules,
        StateKind::HistoryVisibility,
    ];
    if room.guest_access.is_some() {
        initial.push(StateKind::GuestAccess);
    }
    if room.has_name {
        initial.push(StateKind::Name);
    }
    if room.has_topic {
        initial.push(StateKind::Topic);
    }
    if room.has_avatar {
        initial.push(StateKind::Avatar);
    }
    if room.aliases > 0 {
        initial.push(StateKind::Alias);
    }
    if room.encryption.is_some() {
        initial.push(StateKind::Encryption);
    }
    let members_total = room.joined.unwrap_or(0) as i64
        + room.invited.unwrap_or(0) as i64
        + room.left.unwrap_or(0) as i64
        + room.banned.unwrap_or(0) as i64
        + room.knocked.unwrap_or(0) as i64;
    let nonmember_target =
        (room.state_events.unwrap_or(0) as i64 - members_total - 1).max(0) as usize;
    let detail = ctx
        .profile
        .detail_for(ridx % ctx.rooms_per_copy)
        .or_else(|| ctx.profile.nearest_detail(room.joined.unwrap_or(0) as i64));
    let mut custom: Vec<(String, String)> = Vec::new();
    if let Some(d) = detail {
        for (t, n) in &d.state_types {
            let known = matches!(
                t.as_str(),
                "m.room.create"
                    | "m.room.member"
                    | "m.room.power_levels"
                    | "m.room.join_rules"
                    | "m.room.history_visibility"
                    | "m.room.guest_access"
                    | "m.room.name"
                    | "m.room.topic"
                    | "m.room.avatar"
                    | "m.room.canonical_alias"
                    | "m.room.encryption"
            );
            if known {
                continue;
            }
            for i in 0..(*n).max(0) as usize {
                if initial.len() + custom.len() >= nonmember_target {
                    break;
                }
                let sk = if t == "m.space.child" {
                    format!(
                        "!r{}:{}",
                        rng.gen_range(0..ctx.rooms_per_copy.max(1)),
                        ctx.server
                    )
                } else if *n == 1 {
                    String::new()
                } else {
                    format!("k{i}")
                };
                custom.push((t.clone(), sk));
            }
        }
    }
    let mut filler = 0;
    while initial.len() + custom.len() < nonmember_target {
        custom.push(("org.synth.state".to_owned(), format!("f{filler}")));
        filler += 1;
    }
    initial.extend(custom.into_iter().map(|(t, sk)| StateKind::Custom(t, sk)));

    // The create event goes early enough that every positioned membership
    // comes after the initial state.
    let n_initial = 2 + initial.len() as i64;
    let mut create_target = so_last - n_initial - 1;
    if let Some(c) = created_so {
        create_target = create_target.min(c);
    }
    if let Some(m) = local_min_so {
        create_target = create_target.min(m - n_initial - 1);
    }
    let create_so = alloc.take(create_target.max(1));
    let mut events = vec![Planned {
        so: create_so,
        kind: Kind::Create,
    }];
    let mut last = create_so;

    // Remote server mix.
    let mut servers: Vec<String> = Vec::new();
    let mut weights: Vec<f64> = Vec::new();
    if let Some(d) = detail {
        let mut by_server: BTreeMap<&str, i64> = BTreeMap::new();
        for (s, _, n) in &d.members {
            if s != "local" {
                *by_server.entry(s.as_str()).or_insert(0) += n;
            }
        }
        for (s, n) in by_server {
            servers.push(format!("{s}.remote"));
            weights.push(n as f64);
        }
    }
    if servers.is_empty() {
        servers.push(format!("remote{}.remote", rng.gen_range(0..64)));
        weights.push(1.0);
    }

    // Creator: a local user when the profile says so, else a remote one.
    let mut remote_placeholders = vec![0u32; servers.len()];
    let creator = if room.creator_local {
        local_rows
            .iter()
            .filter(|m| m.m == "join")
            .min_by_key(|m| m.so)
            .or_else(|| local_rows.first())
            .map(|m| Who::Local(m.u))
            .unwrap_or(Who::Local(0))
    } else {
        remote_placeholders[0] += 1;
        Who::Remote(0, 0)
    };
    last = alloc.take(last + 1);
    events.push(Planned {
        so: last,
        kind: Kind::Member(creator, Membership::Join),
    });
    for s in initial {
        last = alloc.take(last + 1);
        events.push(Planned {
            so: last,
            kind: Kind::State(s),
        });
    }
    let floor = last + 1;

    // Local members at their recorded positions.
    let mut counts = [0i64; 5]; // join, invite, leave, ban, knock (local)
    for m in local_rows {
        let Some(membership) = Membership::parse(&m.m) else {
            continue;
        };
        let who = Who::Local(m.u);
        counts[match membership {
            Membership::Join => 0,
            Membership::Invite => 1,
            Membership::Leave => 2,
            Membership::Ban => 3,
            Membership::Knock => 4,
        }] += 1;
        if who == creator && membership == Membership::Join {
            continue;
        }
        let target = m.so.unwrap_or(so_last).max(floor);
        match membership {
            Membership::Join | Membership::Invite | Membership::Knock => {
                let so = alloc.take(target);
                events.push(Planned {
                    so,
                    kind: Kind::Member(who, membership),
                });
            }
            Membership::Leave | Membership::Ban => {
                if who != creator {
                    let gap = rng.gen_range(1..=(target - floor).max(1));
                    let so = alloc.take(target - gap);
                    events.push(Planned {
                        so,
                        kind: Kind::Member(who, Membership::Join),
                    });
                }
                let so = alloc.take(target);
                events.push(Planned {
                    so,
                    kind: Kind::Member(who, membership),
                });
            }
        }
    }

    // Remote members make up the difference to the room's totals.
    let remote_totals = [
        (room.joined.unwrap_or(0) as i64 - counts[0]).max(0) as u32,
        (room.invited.unwrap_or(0) as i64 - counts[1]).max(0) as u32,
        (room.left.unwrap_or(0) as i64 - counts[2]).max(0) as u32,
        (room.banned.unwrap_or(0) as i64 - counts[3]).max(0) as u32,
        (room.knocked.unwrap_or(0) as i64 - counts[4]).max(0) as u32,
    ];
    let kinds = [
        Membership::Join,
        Membership::Invite,
        Membership::Leave,
        Membership::Ban,
        Membership::Knock,
    ];
    let mut remote_targets: Vec<(i64, u16, Membership)> = Vec::new();
    for (k, &n) in remote_totals.iter().enumerate() {
        let mut n = n;
        if k == 0 && !room.creator_local {
            n = n.saturating_sub(1);
        }
        for (slot, cnt) in distribute(&mut rng, n, &weights).into_iter().enumerate() {
            for _ in 0..cnt {
                let so = rng.gen_range(floor..=so_last.max(floor));
                remote_targets.push((so, slot as u16, kinds[k]));
            }
        }
    }
    remote_targets.sort_unstable();
    for (target, slot, membership) in remote_targets {
        let p = remote_placeholders[slot as usize];
        remote_placeholders[slot as usize] += 1;
        let who = Who::Remote(slot, p);
        match membership {
            Membership::Leave | Membership::Ban => {
                let gap = rng.gen_range(1..=(target - floor).max(1));
                let so = alloc.take(target - gap);
                events.push(Planned {
                    so,
                    kind: Kind::Member(who, Membership::Join),
                });
                let so = alloc.take(target);
                events.push(Planned {
                    so,
                    kind: Kind::Member(who, membership),
                });
            }
            _ => {
                let so = alloc.take(target);
                events.push(Planned {
                    so,
                    kind: Kind::Member(who, membership),
                });
            }
        }
    }

    // Timeline: the most recent events, spaced by the room's rate.
    let rate = ctx.profile.rates.get(&(ridx % ctx.rooms_per_copy));
    let window_events: u64 = rate.map(|r| r.days.iter().sum()).unwrap_or(0);
    let cap = ctx.args.timeline_events as u64;
    let n_timeline = window_events.clamp(ctx.args.timeline_min as u64, cap) as usize;
    let spacing = if window_events > 0 {
        (ctx.profile.meta.window_days as f64 * so_per_day) / window_events as f64
    } else {
        so_per_day * 7.0
    }
    .max(1.0);
    let mut mix: Vec<(Bucket, bool, f64)> = rate
        .map(|r| {
            r.by.iter()
                .map(|(b, l, n)| {
                    let bucket = match b.as_str() {
                        "m.room.message" => Bucket::Message,
                        "m.room.encrypted" => Bucket::Encrypted,
                        "state" => Bucket::State,
                        _ => Bucket::Other,
                    };
                    (bucket, *l, *n as f64)
                })
                .collect()
        })
        .unwrap_or_default();
    if mix.is_empty() {
        mix.push((Bucket::Message, true, 1.0));
    }
    if room.encryption.is_some() {
        for m in &mut mix {
            if m.0 == Bucket::Message {
                m.0 = Bucket::Encrypted;
            }
        }
    }
    let mix_weights: Vec<f64> = mix.iter().map(|m| m.2).collect();
    let mut targets: Vec<i64> = (0..n_timeline)
        .map(|k| {
            let jitter = rng.gen_range(0.5..1.5);
            (so_last as f64 - k as f64 * spacing * jitter).round() as i64
        })
        .collect();
    targets.sort_unstable();
    let timeline_start = events.len();
    for target in targets {
        let so = alloc.take(target.max(floor));
        let choice = distribute(&mut rng, 1, &mix_weights);
        let slot = choice.iter().position(|&c| c > 0).unwrap_or(0);
        let (bucket, local, _) = mix[slot];
        events.push(Planned {
            so,
            kind: Kind::Timeline(bucket, local),
        });
    }
    // Threads: roots in the older half, one reply each in the newer half.
    let n = events.len() - timeline_start;
    for t in 0..(room.threads.max(0) as usize).min(n / 2) {
        let root = timeline_start + rng.gen_range(0..n / 2);
        let reply = timeline_start + n / 2 + t;
        if let Kind::Timeline(_, local) = events[reply].kind {
            events[reply].kind = Kind::ThreadReply(root, local);
        }
    }

    // Keep plan indexes stable for thread roots: sort a permutation and
    // remap.
    let mut order: Vec<usize> = (0..events.len()).collect();
    order.sort_by_key(|&i| events[i].so);
    let mut position = vec![0usize; events.len()];
    for (new_i, &old_i) in order.iter().enumerate() {
        position[old_i] = new_i;
    }
    let mut sorted: Vec<Planned> = order.iter().map(|&i| events[i].clone()).collect();
    for p in &mut sorted {
        if let Kind::ThreadReply(root, _) = &mut p.kind {
            *root = position[*root];
        }
    }

    let planned_state_rows = estimate_state_rows(&sorted, ctx.args.state_snapshot_every);
    Plan {
        events: sorted,
        servers,
        remote_count: remote_placeholders,
        creator,
        planned_state_rows,
    }
}

fn estimate_state_rows(events: &[Planned], snapshot_every: u32) -> u64 {
    let mut rows = 0u64;
    let mut state = 0u64;
    let mut since = 0u32;
    let mut first = true;
    for e in events {
        if matches!(
            e.kind,
            Kind::Create | Kind::Member(..) | Kind::State(_) | Kind::Timeline(Bucket::State, _)
        ) {
            state += 1;
            if first || (snapshot_every > 0 && since >= snapshot_every) {
                rows += state;
                since = 0;
                first = false;
            } else {
                rows += 1;
                since += 1;
            }
        }
    }
    rows
}

/// Resolve remote placeholders to distinct users from each server's pool.
fn resolve_remote(ctx: &GenCtx, ridx: u32, plan: &mut Plan) {
    let mut rng = room_rng(ctx, ridx, 2);
    let mut maps: Vec<Vec<u32>> = Vec::new();
    for (slot, server) in plan.servers.iter().enumerate() {
        let need = plan.remote_count[slot] as usize;
        let pool = ctx.remote_pool_size(server).max(need as u32) as usize;
        let picks = rand::seq::index::sample(&mut rng, pool, need).into_vec();
        maps.push(picks.into_iter().map(|p| p as u32).collect());
    }
    let fix = |who: &mut Who| {
        if let Who::Remote(slot, p) = who {
            *p = maps[*slot as usize][*p as usize];
        }
    };
    for e in &mut plan.events {
        if let Kind::Member(who, _) = &mut e.kind {
            fix(who);
        }
    }
    fix(&mut plan.creator);
}

/// What the room pass hands back for the user pass.
#[derive(Default)]
pub struct RoomOutcome {
    /// (local user, other user id, room id) for direct-message-shaped rooms.
    pub dms: Vec<(u32, String, String)>,
    pub events: u64,
}

struct MemberState {
    event_id: String,
    membership: Membership,
    so: i64,
    sender: String,
}

/// Generate one room's rows. `plan` must come from [`plan`] for the same
/// room in this pass.
#[allow(clippy::too_many_arguments)]
pub async fn emit(
    ctx: &GenCtx,
    counters: &mut Counters,
    sinks: &mut Sinks,
    pass: Pass,
    ridx: u32,
    room: &RoomRow,
    local_rows: &[MembershipRow],
    mut plan: Plan,
) -> Result<RoomOutcome> {
    if pass == Pass::Plan {
        return Ok(RoomOutcome {
            events: plan.len() as u64,
            ..Default::default()
        });
    }
    resolve_remote(ctx, ridx, &mut plan);
    let mut rng = room_rng(ctx, ridx, 3);
    let room_id = ctx.room_id(ridx);
    let server = ctx.server.as_str();
    let tl = ctx.timeline;

    let who_id = |w: Who| -> String {
        match w {
            Who::Local(u) => ctx.user_id(u),
            Who::Remote(slot, k) => format!("@r{k}:{}", plan.servers[slot as usize]),
        }
    };
    let creator_id = who_id(plan.creator);

    let join_rule = pick(
        room.join_rules.as_deref(),
        &[
            "public",
            "invite",
            "knock",
            "restricted",
            "knock_restricted",
            "private",
        ],
        "invite",
    );
    let join_rule = match join_rule {
        "restricted" => "invite",
        "knock_restricted" => "knock",
        other => other,
    };
    let history_visibility = pick(
        room.history_visibility.as_deref(),
        &["world_readable", "shared", "invited", "joined"],
        "shared",
    );
    let guest_access = pick(
        room.guest_access.as_deref(),
        &["can_join", "forbidden"],
        "forbidden",
    );
    let room_type = room
        .room_type
        .as_deref()
        .filter(|t| *t == "m.space")
        .map(str::to_owned);
    let room_name = room.has_name.then(|| format!("Room {ridx}"));
    let alias = (room.aliases > 0).then(|| format!("#room{ridx}:{server}"));
    let is_public = join_rule == "public" || history_visibility == "world_readable";

    // Running room state.
    let mut depth: i64 = 0;
    let mut prev: Option<String> = None;
    let mut create_id = String::new();
    let mut pl_id: Option<String> = None;
    let mut jr_id: Option<String> = None;
    let mut members: HashMap<Who, MemberState> = HashMap::new();
    let mut state_map: BTreeMap<(String, String), (String, i64, Option<Membership>)> =
        BTreeMap::new();
    let mut left: std::collections::HashSet<Who> = std::collections::HashSet::new();
    let mut state_group: Option<i64> = None;
    let mut deltas_since_snapshot: u32 = 0;
    let mut all_events: Vec<(i64, String, i64)> = Vec::with_capacity(plan.events.len());
    let mut joined_local: Vec<Who> = Vec::new();
    let mut joined_remote: Vec<Who> = Vec::new();
    let mut last_message: Option<(i64, String)> = None;
    let mut thread_rows: Vec<(String, String, i64, i64)> = Vec::new();
    // Stripped state handed to invitees in `unsigned.invite_room_state`.
    let mut stripped: Vec<Value> = Vec::new();

    for i in 0..plan.events.len() {
        let so = plan.events[i].so;
        let kind = plan.events[i].kind.clone();
        let ts = tl.ts(so);
        depth += 1;

        let mut sender_who: Option<Who> = Some(plan.creator);
        // `content` is built with `pad` characters of filler; the exact
        // filler length is settled once the event's metadata (and so its
        // skeleton size) is known.
        let (sender, event_type, state_key, content, is_state): (
            String,
            String,
            Option<String>,
            Pad,
            bool,
        ) = match &kind {
            Kind::Create => {
                let mut c = json!({ "room_version": event::ROOM_VERSION });
                if let Some(t) = &room_type {
                    c["type"] = json!(t);
                }
                (
                    creator_id.clone(),
                    "m.room.create".into(),
                    Some(String::new()),
                    Pad::fixed(c),
                    true,
                )
            }
            Kind::Member(who, membership) => {
                let target = who_id(*who);
                let sender = match membership {
                    Membership::Invite | Membership::Ban => creator_id.clone(),
                    _ => {
                        sender_who = Some(*who);
                        target.clone()
                    }
                };
                let size = ctx.sizes.sample(&mut rng, "m.room.member", true);
                let avatar = format!("mxc://{server}/{}", super::text::b64ish(&mut rng, 24));
                let m = membership.as_str();
                (
                    sender,
                    "m.room.member".into(),
                    Some(target),
                    Pad::new(
                        size,
                        move |text| {
                            let mut c = json!({ "membership": m });
                            if let Some(name) = text {
                                c["displayname"] = json!(name);
                                c["avatar_url"] = json!(avatar);
                            }
                            c
                        },
                        38,
                        256,
                    ),
                    true,
                )
            }
            Kind::State(s) => {
                let (t, sk, c): (&str, String, Pad) = match s {
                    StateKind::PowerLevels => (
                        "m.room.power_levels",
                        String::new(),
                        Pad::fixed(json!({
                            "ban": 50, "events": {"m.room.avatar": 50, "m.room.canonical_alias": 50,
                            "m.room.encryption": 100, "m.room.history_visibility": 100, "m.room.name": 50,
                            "m.room.power_levels": 100, "m.room.server_acl": 100, "m.room.tombstone": 100},
                            "events_default": 0, "historical": 100, "invite": 0, "kick": 50, "redact": 50,
                            "state_default": 50, "users": { creator_id.clone(): 100 }, "users_default": 0
                        })),
                    ),
                    StateKind::JoinRules => (
                        "m.room.join_rules",
                        String::new(),
                        Pad::fixed(json!({ "join_rule": join_rule })),
                    ),
                    StateKind::HistoryVisibility => (
                        "m.room.history_visibility",
                        String::new(),
                        Pad::fixed(json!({ "history_visibility": history_visibility })),
                    ),
                    StateKind::GuestAccess => (
                        "m.room.guest_access",
                        String::new(),
                        Pad::fixed(json!({ "guest_access": guest_access })),
                    ),
                    StateKind::Name => (
                        "m.room.name",
                        String::new(),
                        Pad::fixed(json!({ "name": room_name.clone().unwrap_or_default() })),
                    ),
                    StateKind::Topic => {
                        let size = ctx.sizes.sample(&mut rng, "m.room.topic", true);
                        (
                            "m.room.topic",
                            String::new(),
                            Pad::new(size, |t| json!({ "topic": t.unwrap_or("topic") }), 12, 2000),
                        )
                    }
                    StateKind::Avatar => (
                        "m.room.avatar",
                        String::new(),
                        Pad::fixed(
                            json!({ "url": format!("mxc://{server}/{}", super::text::b64ish(&mut rng, 24)) }),
                        ),
                    ),
                    StateKind::Alias => (
                        "m.room.canonical_alias",
                        String::new(),
                        Pad::fixed(json!({ "alias": alias.clone().unwrap_or_default() })),
                    ),
                    StateKind::Encryption => (
                        "m.room.encryption",
                        String::new(),
                        Pad::fixed(
                            json!({ "algorithm": "m.megolm.v1.aes-sha2", "rotation_period_ms": 604800000, "rotation_period_msgs": 100 }),
                        ),
                    ),
                    StateKind::Custom(t, sk) => {
                        let c = if t == "m.space.child" {
                            Pad::fixed(json!({ "via": [server] }))
                        } else {
                            let size = ctx.sizes.sample(&mut rng, t, true);
                            Pad::new(size, |t| json!({ "synth": t.unwrap_or("") }), 12, 4000)
                        };
                        (t.as_str(), sk.clone(), c)
                    }
                };
                (creator_id.clone(), t.to_owned(), Some(sk), c, true)
            }
            Kind::Timeline(_, local) | Kind::ThreadReply(_, local) => {
                let pool = if *local {
                    &joined_local
                } else {
                    &joined_remote
                };
                sender_who = None;
                for _ in 0..8 {
                    let pick = pool
                        .choose(&mut rng)
                        .or_else(|| joined_local.choose(&mut rng))
                        .copied();
                    if pick.is_some_and(|w| !left.contains(&w)) {
                        sender_who = pick;
                        break;
                    }
                }
                let sender = sender_who.map(who_id).unwrap_or_else(|| creator_id.clone());
                let thread_root: Option<String> = match &kind {
                    Kind::ThreadReply(root, _) => Some(all_events[*root].1.clone()),
                    _ => None,
                };
                let bucket = match &kind {
                    Kind::Timeline(b, _) => *b,
                    _ => {
                        if room.encryption.is_some() {
                            Bucket::Encrypted
                        } else {
                            Bucket::Message
                        }
                    }
                };
                match bucket {
                    Bucket::Message => {
                        let size = ctx.sizes.sample(&mut rng, "m.room.message", false);
                        let relates = thread_root.clone();
                        (
                            sender,
                            "m.room.message".into(),
                            None,
                            Pad::new(
                                size,
                                move |t| {
                                    let mut c =
                                        json!({ "msgtype": "m.text", "body": t.unwrap_or("hi") });
                                    if let Some(root) = &relates {
                                        c["m.relates_to"] = json!({ "rel_type": "m.thread", "event_id": root, "is_falling_back": true, "m.in_reply_to": { "event_id": root } });
                                    }
                                    c
                                },
                                28,
                                60000,
                            ),
                            false,
                        )
                    }
                    Bucket::Encrypted => {
                        let size = ctx.sizes.sample(&mut rng, "m.room.encrypted", false);
                        let relates = thread_root.clone();
                        let sender_key = super::text::b64ish(&mut rng, 43);
                        let session_id = super::text::b64ish(&mut rng, 43);
                        (
                            sender,
                            "m.room.encrypted".into(),
                            None,
                            Pad::new(
                                size,
                                move |t| {
                                    let mut c = json!({
                                        "algorithm": "m.megolm.v1.aes-sha2", "ciphertext": t.unwrap_or("x"),
                                        "device_id": "SYNTHDEVICE", "sender_key": sender_key, "session_id": session_id
                                    });
                                    if let Some(root) = &relates {
                                        c["m.relates_to"] = json!({ "rel_type": "m.thread", "event_id": root, "is_falling_back": true, "m.in_reply_to": { "event_id": root } });
                                    }
                                    c
                                },
                                180,
                                60000,
                            ),
                            false,
                        )
                    }
                    Bucket::State => {
                        // State churn in a live room is mostly member
                        // events: profile changes, joins and leaves.
                        // A display name change keeps the member
                        // counts intact.
                        let size = ctx.sizes.sample(&mut rng, "m.room.member", true);
                        (
                            sender.clone(),
                            "m.room.member".into(),
                            Some(sender),
                            Pad::new(
                                size,
                                |t| json!({ "membership": "join", "displayname": t.unwrap_or("u") }),
                                38,
                                256,
                            ),
                            true,
                        )
                    }
                    Bucket::Other => {
                        let size = ctx.sizes.sample(&mut rng, "org.synth.other", false);
                        (
                            sender,
                            "org.synth.other".into(),
                            None,
                            Pad::new(size, |t| json!({ "synth": t.unwrap_or("") }), 12, 60000),
                            false,
                        )
                    }
                }
            }
        };

        // Auth events.
        let mut auth: Vec<String> = Vec::new();
        if !matches!(kind, Kind::Create) {
            auth.push(create_id.clone());
            if let Some(pl) = &pl_id {
                auth.push(pl.clone());
            }
            if let Some(id) = sender_who
                .and_then(|w| members.get(&w))
                .map(|m| m.event_id.clone())
            {
                auth.push(id);
            }
            if let Kind::Member(who, _) = &kind {
                if let Some(jr) = &jr_id {
                    auth.push(jr.clone());
                }
                if let Some(m) = members.get(who) {
                    if !auth.contains(&m.event_id) {
                        auth.push(m.event_id.clone());
                    }
                }
            }
        }
        let prev_events: Vec<String> = prev.iter().cloned().collect();
        let content = match content {
            Pad::Fixed(c) => c,
            Pad::Sized {
                target,
                make,
                key_overhead,
                max,
            } => {
                let skeleton = event::overhead(
                    &room_id,
                    &sender,
                    &event_type,
                    state_key.as_deref(),
                    ts,
                    depth,
                    &prev_events,
                    &auth,
                    server,
                );
                let want = target.saturating_sub(skeleton + key_overhead).min(max);
                let text = if want == 0 {
                    None
                } else {
                    Some(ctx.vocab.text(&mut rng, want))
                };
                make(text.as_deref())
            }
        };
        let unsigned_extra = match &kind {
            Kind::Member(_, Membership::Invite) => {
                let mut u = serde_json::Map::new();
                u.insert("invite_room_state".into(), json!(stripped));
                Some(u)
            }
            _ => None,
        };
        let built = event::build(
            &room_id,
            &sender,
            &event_type,
            state_key.as_deref(),
            content.clone(),
            ts,
            depth,
            &prev_events,
            &auth,
            server,
            unsigned_extra,
        );
        let event_id = built.event_id;
        if is_state
            && matches!(
                event_type.as_str(),
                "m.room.create"
                    | "m.room.name"
                    | "m.room.join_rules"
                    | "m.room.canonical_alias"
                    | "m.room.avatar"
                    | "m.room.encryption"
                    | "m.room.topic"
            )
        {
            stripped.retain(|e| e["type"] != event_type);
            stripped.push(json!({ "type": event_type, "state_key": state_key.clone().unwrap_or_default(), "sender": sender, "content": content }));
        }

        put(
            &mut sinks.events,
            &[
                F::I(so),
                F::I(depth),
                F::S(&event_id),
                F::S(&event_type),
                F::S(&room_id),
                F::B(true),
                F::B(false),
                F::I(depth),
                F::I(ts),
                F::I(ts),
                F::S(&sender),
                F::B(false),
                F::S("master"),
                match &state_key {
                    Some(sk) => F::S(sk),
                    None => F::Null,
                },
            ],
        )
        .await?;
        put(
            &mut sinks.event_json,
            &[
                F::S(&event_id),
                F::S(&room_id),
                F::S("{}"),
                F::S(&built.json),
                F::I(event::FORMAT_VERSION as i64),
            ],
        )
        .await?;
        for a in &auth {
            put(
                &mut sinks.event_auth,
                &[F::S(&event_id), F::S(a), F::S(&room_id)],
            )
            .await?;
        }
        if let Some(p) = &prev {
            put(
                &mut sinks.event_edges,
                &[F::S(&event_id), F::S(p), F::S(&room_id), F::B(false)],
            )
            .await?;
        }

        if is_state {
            let sk = state_key.clone().unwrap_or_default();
            let key = (event_type.clone(), sk.clone());
            let membership = match &kind {
                Kind::Member(_, m) => Some(*m),
                Kind::Timeline(Bucket::State, _) => Some(Membership::Join),
                _ => None,
            };
            let prev_for_key = state_map
                .insert(key, (event_id.clone(), so, membership))
                .map(|(id, _, _)| id);
            put(
                &mut sinks.current_state_delta_stream,
                &[
                    F::I(so),
                    F::S(&room_id),
                    F::S(&event_type),
                    F::S(&sk),
                    F::S(&event_id),
                    match &prev_for_key {
                        Some(p) => F::S(p),
                        None => F::Null,
                    },
                    F::S("master"),
                ],
            )
            .await?;

            // State group: a delta on the previous group, with a full
            // snapshot every `state_snapshot_every` deltas as Synapse does.
            counters.state_group += 1;
            let group = counters.state_group;
            put(
                &mut sinks.state_groups,
                &[F::I(group), F::S(&room_id), F::S(&event_id)],
            )
            .await?;
            let snapshot = state_group.is_none()
                || (ctx.args.state_snapshot_every > 0
                    && deltas_since_snapshot >= ctx.args.state_snapshot_every);
            if snapshot {
                for ((t, k), (id, _, _)) in &state_map {
                    put(
                        &mut sinks.state_groups_state,
                        &[F::I(group), F::S(&room_id), F::S(t), F::S(k), F::S(id)],
                    )
                    .await?;
                }
                deltas_since_snapshot = 0;
            } else {
                put(
                    &mut sinks.state_group_edges,
                    &[F::I(group), F::I(state_group.unwrap())],
                )
                .await?;
                put(
                    &mut sinks.state_groups_state,
                    &[
                        F::I(group),
                        F::S(&room_id),
                        F::S(&event_type),
                        F::S(&sk),
                        F::S(&event_id),
                    ],
                )
                .await?;
                deltas_since_snapshot += 1;
            }
            state_group = Some(group);
        }
        put(
            &mut sinks.event_to_state_groups,
            &[F::S(&event_id), F::I(state_group.unwrap_or(0))],
        )
        .await?;

        match &kind {
            Kind::Create => create_id = event_id.clone(),
            Kind::State(StateKind::PowerLevels) => pl_id = Some(event_id.clone()),
            Kind::State(StateKind::JoinRules) => jr_id = Some(event_id.clone()),
            Kind::Member(who, membership) => {
                let target = state_key.clone().unwrap_or_default();
                put(
                    &mut sinks.room_memberships,
                    &[
                        F::S(&event_id),
                        F::S(&target),
                        F::S(&sender),
                        F::S(&room_id),
                        F::S(membership.as_str()),
                        F::I(0),
                        F::I(so),
                        F::B(false),
                    ],
                )
                .await?;
                if *membership == Membership::Join {
                    if !left.remove(who) {
                        match who {
                            Who::Local(_) => joined_local.push(*who),
                            Who::Remote(..) => joined_remote.push(*who),
                        }
                    }
                } else {
                    left.insert(*who);
                }
                members.insert(
                    *who,
                    MemberState {
                        event_id: event_id.clone(),
                        membership: *membership,
                        so,
                        sender: sender.clone(),
                    },
                );
            }
            Kind::Timeline(Bucket::Message | Bucket::Encrypted, _) => {
                last_message = Some((so, event_id.clone()))
            }
            Kind::Timeline(Bucket::State, _) => {
                if let Some(m) = sender_who.and_then(|w| members.get_mut(&w)) {
                    m.event_id = event_id.clone();
                    m.so = so;
                }
            }
            Kind::ThreadReply(root, _) => {
                last_message = Some((so, event_id.clone()));
                let root_id = all_events[*root].1.clone();
                thread_rows.push((root_id, event_id.clone(), depth, so));
            }
            _ => {}
        }
        all_events.push((so, event_id.clone(), depth));
        prev = Some(event_id);
    }

    // Final per-room rows.
    let (last_so, last_id, _) = all_events.last().cloned().expect("a room has events");
    for ((t, k), (id, so, membership)) in &state_map {
        put(
            &mut sinks.current_state_events,
            &[
                F::S(id),
                F::S(&room_id),
                F::S(t),
                F::S(k),
                membership.map(|m| F::S(m.as_str())).unwrap_or(F::Null),
                F::I(*so),
            ],
        )
        .await?;
    }
    put(
        &mut sinks.event_forward_extremities,
        &[F::S(&last_id), F::S(&room_id)],
    )
    .await?;
    put(
        &mut sinks.stream_ordering_to_exterm,
        &[F::I(last_so), F::S(&room_id), F::S(&last_id)],
    )
    .await?;
    put(&mut sinks.room_depth, &[F::S(&room_id), F::I(1)]).await?;
    put(
        &mut sinks.rooms,
        &[
            F::S(&room_id),
            F::B(room.public),
            F::S(&creator_id),
            F::S(event::ROOM_VERSION),
            F::B(false),
        ],
    )
    .await?;
    if let Some(a) = &alias {
        put(
            &mut sinks.room_aliases,
            &[F::S(a), F::S(&room_id), F::S(&creator_id)],
        )
        .await?;
    }

    let mut n = [0i64; 5];
    let mut local_joined_count = 0i64;
    for (who, m) in &members {
        n[match m.membership {
            Membership::Join => 0,
            Membership::Invite => 1,
            Membership::Leave => 2,
            Membership::Ban => 3,
            Membership::Knock => 4,
        }] += 1;
        if matches!(who, Who::Local(_)) && m.membership == Membership::Join {
            local_joined_count += 1;
        }
    }
    put(
        &mut sinks.room_stats_current,
        &[
            F::S(&room_id),
            F::I(state_map.len() as i64),
            F::I(n[0]),
            F::I(n[1]),
            F::I(n[2]),
            F::I(n[3]),
            F::I(n[4]),
            F::I(local_joined_count),
            F::I(last_so),
        ],
    )
    .await?;
    put(
        &mut sinks.room_stats_state,
        &[
            F::S(&room_id),
            room_name.as_deref().map(F::S).unwrap_or(F::Null),
            alias.as_deref().map(F::S).unwrap_or(F::Null),
            F::S(join_rule),
            F::S(history_visibility),
            room.encryption
                .as_deref()
                .map(|_| F::S("m.megolm.v1.aes-sha2"))
                .unwrap_or(F::Null),
            if room.has_avatar {
                F::S("mxc://synth/avatar")
            } else {
                F::Null
            },
            if room.guest_access.is_some() {
                F::S(guest_access)
            } else {
                F::Null
            },
            F::B(room.federatable.unwrap_or(true)),
            if room.has_topic {
                F::S("topic")
            } else {
                F::Null
            },
            room_type.as_deref().map(F::S).unwrap_or(F::Null),
        ],
    )
    .await?;

    // Sliding sync tables.
    let bump_stamp = last_message.as_ref().map(|(so, _)| *so).unwrap_or(last_so);
    if local_joined_count > 0 {
        put(
            &mut sinks.sliding_sync_joined_rooms,
            &[
                F::S(&room_id),
                F::I(last_so),
                F::I(bump_stamp),
                room_type.as_deref().map(F::S).unwrap_or(F::Null),
                room_name.as_deref().map(F::S).unwrap_or(F::Null),
                F::B(room.encryption.is_some()),
                F::Null,
            ],
        )
        .await?;
    }

    // Per local member rows.
    let mut outcome = RoomOutcome {
        events: all_events.len() as u64,
        ..Default::default()
    };
    let joined_ids: Vec<String> = members
        .iter()
        .filter(|(_, m)| m.membership == Membership::Join)
        .map(|(w, _)| who_id(*w))
        .collect();
    let find_event_at = |target: i64| -> &(i64, String, i64) {
        let i = all_events.partition_point(|(so, _, _)| *so <= target);
        &all_events[i.saturating_sub(1)]
    };
    for row in local_rows {
        let who = Who::Local(row.u);
        let Some(m) = members.get(&who) else {
            continue;
        };
        let user_id = ctx.user_id(row.u);
        put(
            &mut sinks.local_current_membership,
            &[
                F::S(&room_id),
                F::S(&user_id),
                F::S(&m.event_id),
                F::S(m.membership.as_str()),
                F::I(m.so),
            ],
        )
        .await?;
        put(
            &mut sinks.sliding_sync_membership_snapshots,
            &[
                F::S(&room_id),
                F::S(&user_id),
                F::S(&m.sender),
                F::S(&m.event_id),
                F::S(m.membership.as_str()),
                F::I(0),
                F::I(m.so),
                F::S("master"),
                F::B(true),
                room_type.as_deref().map(F::S).unwrap_or(F::Null),
                room_name.as_deref().map(F::S).unwrap_or(F::Null),
                F::B(room.encryption.is_some()),
                F::Null,
            ],
        )
        .await?;

        let mut receipt_so: Option<i64> = None;
        for (receipt_type, lag) in [
            ("m.read", row.read_lag),
            ("m.read.private", row.private_read_lag),
        ] {
            let Some(lag) = lag else { continue };
            let (so, eid, _) = find_event_at(last_so - lag);
            counters.receipt += 1;
            let data = format!("{{\"ts\":{}}}", tl.ts(*so));
            put(
                &mut sinks.receipts_linearized,
                &[
                    F::I(counters.receipt),
                    F::S(&room_id),
                    F::S(receipt_type),
                    F::S(&user_id),
                    F::S(eid),
                    F::S(&data),
                    F::S("master"),
                    F::I(*so),
                    F::Null,
                ],
            )
            .await?;
            let ids = format!("[\"{eid}\"]");
            put(
                &mut sinks.receipts_graph,
                &[
                    F::S(&room_id),
                    F::S(receipt_type),
                    F::S(&user_id),
                    F::S(&ids),
                    F::S(&data),
                    F::Null,
                ],
            )
            .await?;
            receipt_so = Some(receipt_so.map_or(*so, |r| r.max(*so)));
        }
        if row.notif.is_some() || row.unread.is_some() {
            put(
                &mut sinks.event_push_summary,
                &[
                    F::S(&user_id),
                    F::S(&room_id),
                    F::I(row.notif.unwrap_or(0)),
                    F::I(last_so),
                    row.unread.map(F::I).unwrap_or(F::Null),
                    receipt_so.map(F::I).unwrap_or(F::Null),
                    F::S("main"),
                ],
            )
            .await?;
        }
        for i in 0..row.rad.max(0) {
            counters.account_data += 1;
            let (t, content) = if i == 0 {
                (
                    "m.fully_read".to_owned(),
                    format!("{{\"event_id\":\"{last_id}\"}}"),
                )
            } else {
                (format!("org.synth.rad{i}"), "{}".to_owned())
            };
            put(
                &mut sinks.room_account_data,
                &[
                    F::S(&user_id),
                    F::S(&room_id),
                    F::S(&t),
                    F::I(counters.account_data),
                    F::S(&content),
                    F::S("master"),
                ],
            )
            .await?;
        }
        if row.tags > 0 {
            for i in 0..row.tags {
                let (tag, content) = match i {
                    0 => ("m.favourite".to_owned(), "{\"order\":0.5}"),
                    1 => ("m.lowpriority".to_owned(), "{}"),
                    _ => (format!("u.synth{i}"), "{}"),
                };
                put(
                    &mut sinks.room_tags,
                    &[F::S(&user_id), F::S(&room_id), F::S(&tag), F::S(content)],
                )
                .await?;
            }
            counters.account_data += 1;
            put(
                &mut sinks.room_tags_revisions,
                &[
                    F::S(&user_id),
                    F::S(&room_id),
                    F::I(counters.account_data),
                    F::S("master"),
                ],
            )
            .await?;
        }
        if m.membership == Membership::Join {
            if is_public {
                put(
                    &mut sinks.users_in_public_rooms,
                    &[F::S(&user_id), F::S(&room_id)],
                )
                .await?;
            } else {
                for other in &joined_ids {
                    if other != &user_id {
                        put(
                            &mut sinks.users_who_share_private_rooms,
                            &[F::S(&user_id), F::S(other), F::S(&room_id)],
                        )
                        .await?;
                    }
                }
            }
            if joined_ids.len() == 2 && room_name.is_none() && !is_public && room_type.is_none() {
                if let Some(other) = joined_ids.iter().find(|o| *o != &user_id) {
                    outcome.dms.push((row.u, other.clone(), room_id.clone()));
                }
            }
        }
    }

    for (root, reply, depth, so) in &thread_rows {
        put(
            &mut sinks.threads,
            &[
                F::S(&room_id),
                F::S(root),
                F::S(reply),
                F::I(*depth),
                F::I(*so),
            ],
        )
        .await?;
        put(
            &mut sinks.event_relations,
            &[F::S(reply), F::S(root), F::S("m.thread"), F::Null],
        )
        .await?;
    }
    Ok(outcome)
}
