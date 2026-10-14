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

//! The `generate` subcommand: build a synthetic Synapse database from a
//! profile.
//!
//! The target database must already carry the Synapse schema (start a
//! homeserver against an empty database once, let its background updates
//! finish, stop it). Generation then runs in passes:
//!
//! 1. plan: allocate stream orderings and size the remote user pools;
//! 2. pass A: every table without a foreign key onto `events`;
//! 3. pass B: the tables that reference `events` (loaded after A commits).
//!
//! Each pass re-plans every room from the same seeds, so events land at the
//! same positions with the same IDs in every pass.

mod alloc;
mod data;
mod event;
mod room;
mod sink;
mod sinks;
mod text;
mod user;

use std::collections::HashMap;
use std::path::PathBuf;
use std::str::FromStr;
use std::time::Instant;

use anyhow::{bail, Context as _, Result};
use clap::Args;
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use tokio_postgres::NoTls;
use tracing::{info, warn};

use self::alloc::{StreamAlloc, Timeline};
use self::data::{Profile, SizeModel, ROOMS_PER_BUCKET};
use self::sinks::{Pass, Sinks};
use self::text::Vocabulary;

#[derive(Args, Debug, Clone)]
pub struct GenerateArgs {
    /// Profile directory written by `profile`.
    #[arg(long)]
    pub profile: PathBuf,

    /// Postgres URL of the target database. It must already hold the
    /// Synapse schema and be otherwise empty.
    #[arg(long, env = "SYNTH_TARGET_DB_URL")]
    pub db: String,

    /// The `server_name` the homeserver will run with.
    #[arg(long, default_value = "synth.test")]
    pub server_name: String,

    /// Seed for every random choice; the same seed and profile give the
    /// same database.
    #[arg(long, default_value_t = 1)]
    pub seed: u64,

    /// Number of copies of the profiled population to generate. 2 gives a
    /// server twice the size with the same shape.
    #[arg(long, default_value_t = 1)]
    pub scale: u32,

    /// Keep only this fraction of the profile's rooms, for a smaller
    /// database than the profile. Every user is still created.
    #[arg(long, default_value_t = 1.0)]
    pub room_fraction: f64,

    /// Maximum timeline (non-state) events generated per room.
    #[arg(long, default_value_t = 50)]
    pub timeline_events: u32,

    /// Minimum timeline events per room, even for silent rooms.
    #[arg(long, default_value_t = 10)]
    pub timeline_min: u32,

    /// Write a full state snapshot every this many state deltas, as Synapse
    /// does (100). Large rooms produce many rows this way; 0 disables
    /// snapshots after the first.
    #[arg(long, default_value_t = 100)]
    pub state_snapshot_every: u32,

    /// Password every generated user can log in with.
    #[arg(long, default_value = "synthpassword")]
    pub password: String,

    /// Scratch directory for bucketed memberships (defaults to the system
    /// temp dir).
    #[arg(long)]
    pub work_dir: Option<PathBuf>,

    /// Proceed even if the target already has events.
    #[arg(long)]
    pub allow_nonempty: bool,

    /// Plan only: print what would be generated and exit.
    #[arg(long)]
    pub dry_run: bool,

    /// Skip the final ANALYZE.
    #[arg(long)]
    pub no_analyze: bool,
}

/// Monotonic IDs for the streams the generator writes. Reset at the start
/// of every pass so replays allocate identically.
#[derive(Default, Clone, Debug)]
pub struct Counters {
    pub state_group: i64,
    pub receipt: i64,
    pub account_data: i64,
    pub presence: i64,
    pub cross_signing: i64,
    pub token_id: i64,
    pub push_rule_id: i64,
}

pub struct GenCtx {
    pub args: GenerateArgs,
    pub profile: Profile,
    pub sizes: SizeModel,
    pub vocab: Vocabulary,
    pub server: String,
    pub password_hash: String,
    /// Stream/time mapping anchored on the profile, used while planning.
    pub profile_tl: Timeline,
    /// Stream/time mapping anchored on the generated database's "now".
    pub timeline: Timeline,
    pub rooms_per_copy: u32,
    pub users_per_copy: u32,
    pub users_total: u32,
    remote_pools: HashMap<String, u32>,
}

impl GenCtx {
    pub fn room_id(&self, ridx: u32) -> String {
        format!("!r{ridx}:{}", self.server)
    }

    pub fn user_id(&self, uidx: u32) -> String {
        format!("@u{uidx}:{}", self.server)
    }

    pub fn remote_pool_size(&self, server: &str) -> u32 {
        self.remote_pools.get(server).copied().unwrap_or(1).max(1)
    }
}

fn make_pool(url: &str) -> Result<Pool> {
    let mut config = tokio_postgres::Config::from_str(url).context("parsing database URL")?;
    config.options(
        "-c synchronous_commit=off -c statement_timeout=0 -c application_name=synth-db-generate",
    );
    let mgr = Manager::from_config(
        config,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );
    Ok(Pool::builder(mgr).max_size(64).build()?)
}

/// Whether a profile room is kept under `--room-fraction`.
fn keep_room(args: &GenerateArgs, ridx: u32) -> bool {
    if args.room_fraction >= 1.0 {
        return true;
    }
    let h = (ridx as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ args.seed;
    let unit = (h >> 11) as f64 / (1u64 << 53) as f64;
    unit < args.room_fraction
}

pub async fn run(args: GenerateArgs) -> Result<()> {
    if args.scale == 0 {
        bail!("--scale must be at least 1");
    }
    let work_dir = args
        .work_dir
        .clone()
        .unwrap_or_else(|| std::env::temp_dir().join(format!("synth-db-{}", std::process::id())));
    std::fs::create_dir_all(&work_dir)?;
    let profile = Profile::load(&args.profile, &work_dir)?;

    let pool = make_pool(&args.db)?;
    {
        let client = pool
            .get()
            .await
            .context("connecting to the target database")?;
        let version: i32 = client
            .query_one("SELECT version FROM schema_version", &[])
            .await
            .context("target has no schema_version table; run Synapse against it once first")?
            .get(0);
        if version < 88 {
            bail!("target schema version {version} is older than this tool supports (88+)");
        }
        if let Some(pv) = profile.meta.schema_version {
            if pv != version as i64 {
                warn!(
                    profile = pv,
                    target = version,
                    "schema versions differ; proceeding"
                );
            }
        }
        let events: i64 = client
            .query_one("SELECT count(*) FROM events", &[])
            .await?
            .get(0);
        if events > 0 && !args.allow_nonempty {
            bail!("target already has {events} events; pass --allow-nonempty to add to it");
        }
    }

    let window_ms = (profile.meta.window_days.max(1) * 86_400_000) as f64;
    let ms_per_so = if profile.events_in_window > 0 {
        window_ms / profile.events_in_window as f64
    } else {
        60_000.0
    };
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_millis() as i64;
    let profile_tl = Timeline {
        now_ms: profile.meta.now_ms,
        max_so: profile.max_so,
        ms_per_so,
    };
    info!(
        ms_per_stream_unit = format!("{ms_per_so:.1}"),
        profile_max_so = profile.max_so,
        "stream/time mapping"
    );

    let password_hash = bcrypt::hash(&args.password, 10).context("hashing the password")?;
    let rooms_per_copy = profile.rooms.len() as u32;
    let users_per_copy = profile.users.len() as u32;
    let mut ctx = GenCtx {
        sizes: SizeModel::new(&profile.event_sizes),
        vocab: Vocabulary::new(args.seed),
        server: args.server_name.clone(),
        password_hash,
        profile_tl,
        timeline: profile_tl,
        rooms_per_copy,
        users_per_copy,
        users_total: users_per_copy * args.scale,
        remote_pools: HashMap::new(),
        profile,
        args: args.clone(),
    };

    // Pass 0: plan.
    let start = Instant::now();
    let mut alloc = StreamAlloc::new();
    let mut remote_slots: HashMap<String, u64> = HashMap::new();
    let mut remote_max_need: HashMap<String, u32> = HashMap::new();
    let mut planned_events = 0u64;
    let mut planned_state_rows = 0u64;
    let mut planned_rooms = 0u64;
    for_each_room(&ctx, |ridx, room, rows| {
        let plan = room::plan(&ctx, &mut alloc, ridx, room, rows);
        for (slot, server) in plan.servers.iter().enumerate() {
            let n = plan.remote_count[slot];
            *remote_slots.entry(server.clone()).or_insert(0) += n as u64;
            let m = remote_max_need.entry(server.clone()).or_insert(0);
            *m = (*m).max(n);
        }
        planned_events += plan.len() as u64;
        planned_state_rows += plan.planned_state_rows;
        planned_rooms += 1;
        Ok(())
    })?;
    let max_so = alloc.max_used();
    info!(
        rooms = planned_rooms,
        users = ctx.users_total,
        events = planned_events,
        state_groups_state_rows = planned_state_rows,
        max_stream_ordering = max_so,
        remote_servers = remote_slots.len(),
        seconds = format!("{:.1}", start.elapsed().as_secs_f64()),
        "plan"
    );
    if args.dry_run {
        return Ok(());
    }
    ctx.remote_pools = remote_slots
        .iter()
        .map(|(s, &total)| {
            let need = remote_max_need.get(s).copied().unwrap_or(1);
            (s.clone(), ((total / 5) as u32).max(need).max(1))
        })
        .collect();
    ctx.timeline = Timeline {
        now_ms,
        max_so,
        ms_per_so,
    };

    // Pass A.
    let start = Instant::now();
    let mut counters = Counters::default();
    let mut sinks = Sinks::open(&pool, Pass::A).await?;
    let mut dms: HashMap<u32, Vec<(String, String)>> = HashMap::new();
    let mut alloc = StreamAlloc::new();
    run_rooms(
        &ctx,
        &mut counters,
        &mut sinks,
        Pass::A,
        &mut alloc,
        Some(&mut dms),
    )
    .await?;
    for copy in 0..args.scale {
        for u in &ctx.profile.users {
            let uidx = copy * users_per_copy + u.idx;
            let user_dms = dms.remove(&uidx).unwrap_or_default();
            user::emit(&ctx, &mut counters, &mut sinks, uidx, u, &user_dms).await?;
        }
    }
    let rows_a = sinks.close().await?;
    info!(
        rows = rows_a,
        seconds = format!("{:.1}", start.elapsed().as_secs_f64()),
        "pass A committed"
    );
    let final_counters = counters.clone();

    // Pass B.
    let start = Instant::now();
    let mut counters = Counters::default();
    let mut sinks = Sinks::open(&pool, Pass::B).await?;
    let mut alloc = StreamAlloc::new();
    run_rooms(&ctx, &mut counters, &mut sinks, Pass::B, &mut alloc, None).await?;
    let rows_b = sinks.close().await?;
    info!(
        rows = rows_b,
        seconds = format!("{:.1}", start.elapsed().as_secs_f64()),
        "pass B committed"
    );

    finalise(&pool, max_so, &final_counters, !args.no_analyze).await?;
    let _ = std::fs::remove_dir_all(&work_dir);
    info!(
        "done: users are @u<N>:{server} (N < {users}), password {password:?}, device access tokens synt_u<N>_d<D>",
        server = ctx.server,
        users = ctx.users_total,
        password = args.password,
    );
    Ok(())
}

/// Iterate every (copy, kept room) with its local membership rows, in
/// synthetic room index order within each bucket.
fn for_each_room<F>(ctx: &GenCtx, mut f: F) -> Result<()>
where
    F: FnMut(u32, &crate::model::RoomRow, &[crate::model::MembershipRow]) -> Result<()>,
{
    let n_buckets = ctx.rooms_per_copy.div_ceil(ROOMS_PER_BUCKET);
    for copy in 0..ctx.args.scale {
        for bucket in 0..n_buckets {
            let mut rows = ctx.profile.membership_bucket(bucket)?;
            let first = bucket * ROOMS_PER_BUCKET;
            let last = (first + ROOMS_PER_BUCKET).min(ctx.rooms_per_copy);
            for ridx in first..last {
                if !keep_room(&ctx.args, ridx) {
                    continue;
                }
                let room = &ctx.profile.rooms[ridx as usize];
                let mut local = rows.remove(&ridx).unwrap_or_default();
                for m in &mut local {
                    m.u += copy * ctx.users_per_copy;
                }
                f(copy * ctx.rooms_per_copy + ridx, room, &local)?;
            }
        }
    }
    Ok(())
}

async fn run_rooms(
    ctx: &GenCtx,
    counters: &mut Counters,
    sinks: &mut Sinks,
    pass: Pass,
    alloc: &mut StreamAlloc,
    mut dms: Option<&mut HashMap<u32, Vec<(String, String)>>>,
) -> Result<()> {
    let n_buckets = ctx.rooms_per_copy.div_ceil(ROOMS_PER_BUCKET);
    let mut done = 0u64;
    let mut events = 0u64;
    let started = Instant::now();
    for copy in 0..ctx.args.scale {
        for bucket in 0..n_buckets {
            let mut rows = ctx.profile.membership_bucket(bucket)?;
            let first = bucket * ROOMS_PER_BUCKET;
            let last = (first + ROOMS_PER_BUCKET).min(ctx.rooms_per_copy);
            for ridx in first..last {
                if !keep_room(&ctx.args, ridx) {
                    continue;
                }
                let room = &ctx.profile.rooms[ridx as usize];
                let mut local = rows.remove(&ridx).unwrap_or_default();
                for m in &mut local {
                    m.u += copy * ctx.users_per_copy;
                }
                let sidx = copy * ctx.rooms_per_copy + ridx;
                let plan = room::plan(ctx, alloc, sidx, room, &local);
                let outcome =
                    room::emit(ctx, counters, sinks, pass, sidx, room, &local, plan).await?;
                if let Some(dms) = dms.as_deref_mut() {
                    for (u, other, room_id) in outcome.dms {
                        dms.entry(u).or_default().push((other, room_id));
                    }
                }
                done += 1;
                events += outcome.events;
                if done % 10_000 == 0 {
                    info!(
                        ?pass,
                        rooms = done,
                        events,
                        seconds = format!("{:.0}", started.elapsed().as_secs_f64()),
                        "progress"
                    );
                }
            }
        }
    }
    Ok(())
}

/// Advance sequences and stream-position markers past everything written,
/// so Synapse starts cleanly on top of the data.
async fn finalise(pool: &Pool, max_so: i64, c: &Counters, analyze: bool) -> Result<()> {
    let client = pool.get().await?;
    for (seq, value) in [
        ("events_stream_seq", max_so),
        ("state_group_id_seq", c.state_group),
        ("account_data_sequence", c.account_data),
        ("receipts_sequence", c.receipt),
        ("presence_stream_sequence", c.presence),
        ("e2e_cross_signing_keys_sequence", c.cross_signing),
    ] {
        let sql = format!(
            "SELECT setval('{seq}', greatest((SELECT last_value FROM {seq}), $1::bigint), true)"
        );
        client
            .execute(&sql, &[&value.max(1)])
            .await
            .with_context(|| format!("advancing {seq}"))?;
    }
    for sql in [
        "UPDATE event_push_summary_stream_ordering SET stream_ordering = greatest(stream_ordering, $1)",
        "UPDATE stats_incremental_position SET stream_id = greatest(stream_id, $1)",
        "UPDATE user_directory_stream_pos SET stream_id = greatest(coalesce(stream_id, 0), $1)",
        "UPDATE room_forgetter_stream_pos SET stream_id = greatest(stream_id, $1)",
        "INSERT INTO stream_positions (stream_name, instance_name, stream_id) VALUES ('events', 'master', $1)
         ON CONFLICT (stream_name, instance_name) DO UPDATE SET stream_id = greatest(stream_positions.stream_id, EXCLUDED.stream_id)",
    ] {
        client.execute(sql, &[&max_so]).await.with_context(|| sql.to_owned())?;
    }
    client
        .execute(
            "UPDATE event_push_summary_last_receipt_stream_id SET stream_id = greatest(stream_id, $1)",
            &[&c.receipt],
        )
        .await?;
    if analyze {
        info!("running ANALYZE");
        client.batch_execute("ANALYZE").await.context("ANALYZE")?;
    }
    Ok(())
}

/// A few events of different kinds, for `check-event`.
pub fn sample_events() -> Vec<event::BuiltEvent> {
    use serde_json::json;
    let create = event::build(
        "!r:synth.test",
        "@u1:synth.test",
        "m.room.create",
        Some(""),
        json!({"room_version": "11", "type": "m.space"}),
        1_700_000_000_000,
        1,
        &[],
        &[],
        "synth.test",
        None,
    );
    let member = event::build(
        "!r:synth.test",
        "@u1:synth.test",
        "m.room.member",
        Some("@u1:synth.test"),
        json!({"membership": "join", "displayname": "x"}),
        1_700_000_000_001,
        2,
        std::slice::from_ref(&create.event_id),
        std::slice::from_ref(&create.event_id),
        "synth.test",
        None,
    );
    let pl = event::build(
        "!r:synth.test",
        "@u1:synth.test",
        "m.room.power_levels",
        Some(""),
        json!({"users": {"@u1:synth.test": 100}, "users_default": 0, "events": {"m.room.name": 50}, "events_default": 0, "state_default": 50, "ban": 50, "kick": 50, "redact": 50, "invite": 0, "historical": 100}),
        1_700_000_000_002,
        3,
        std::slice::from_ref(&member.event_id),
        &[create.event_id.clone(), member.event_id.clone()],
        "synth.test",
        None,
    );
    let msg = event::build(
        "!r:synth.test",
        "@u1:synth.test",
        "m.room.message",
        None,
        json!({"msgtype": "m.text", "body": "hello \"world\" \\ /"}),
        1_700_000_000_003,
        4,
        std::slice::from_ref(&pl.event_id),
        &[
            create.event_id.clone(),
            member.event_id.clone(),
            pl.event_id.clone(),
        ],
        "synth.test",
        None,
    );
    vec![create, member, pl, msg]
}
