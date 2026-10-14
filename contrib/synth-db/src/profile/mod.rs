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

//! The `profile` subcommand: read a live Synapse database and write the
//! anonymised profile.

mod event_sizes;
mod memberships;
mod rates;
mod room_detail;
mod rooms;
mod users;

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{bail, Context as _, Result};
use clap::Args;
use deadpool_postgres::Pool;
use rand::RngCore;
use tokio::task::JoinSet;
use tracing::{info, warn};

use crate::anon::{Anonymiser, Indexer};
use crate::db::{self, DbOptions};
use crate::model::{Meta, SectionStats};
use crate::out;

#[derive(Args, Debug, Clone)]
pub struct ProfileArgs {
    /// Postgres connection URL, e.g. postgres://user:pass@host/synapse
    #[arg(long, env = "SYNAPSE_DB_URL")]
    pub db: String,

    /// Output directory for the profile.
    #[arg(long, short)]
    pub out: PathBuf,

    /// The homeserver's server_name. Detected from the users table when omitted.
    #[arg(long)]
    pub server_name: Option<String>,

    /// Length of the trailing window used for event rates.
    #[arg(long, default_value_t = 30)]
    pub window_days: i64,

    /// Number of rooms to collect per-room state/server detail for
    /// (a quarter are the largest rooms, the rest random). 0 disables.
    #[arg(long, default_value_t = 2000)]
    pub sample_rooms: usize,

    /// Approximate number of events to sample for the event size histogram.
    #[arg(long, default_value_t = 500_000)]
    pub sample_events: u64,

    /// Maximum number of concurrent database connections.
    #[arg(long, default_value_t = 4)]
    pub concurrency: usize,

    /// Per-statement timeout. The rates query over a large window is the one
    /// most likely to need raising.
    #[arg(long, default_value_t = 4 * 3600)]
    pub statement_timeout_secs: u64,

    /// `work_mem` for the session; the aggregate queries benefit from a lot.
    #[arg(long, default_value = "512MB")]
    pub work_mem: String,

    /// File holding the pseudonymisation key. Created (mode 0600) when it
    /// does not exist. Without it a fresh random key is used and discarded,
    /// so the run cannot be resumed and pseudonyms are not comparable across
    /// runs. Never place this inside the output directory.
    #[arg(long)]
    pub salt_file: Option<PathBuf>,

    /// Skip sections that already exist in the output directory. Requires
    /// --salt-file so that pseudonyms line up with the earlier run.
    #[arg(long)]
    pub resume: bool,

    /// Print the query plan of each section's main query before running it.
    #[arg(long)]
    pub explain: bool,

    /// Only run the named sections (comma separated), e.g. for a quick look
    /// at rates alone. `rooms` and `users` always run (or load) first.
    #[arg(long, value_delimiter = ',')]
    pub only: Vec<String>,
}

/// Shared read-only state for the concurrent sections.
pub struct Ctx {
    pub pool: Pool,
    pub anon: Anonymiser,
    pub out: PathBuf,
    pub args: ProfileArgs,
    pub now_ms: i64,
    pub window_start_ms: i64,
    pub rooms: Indexer,
    pub users: Indexer,
    /// Raw room IDs by index, for the per-room sampled queries.
    pub room_raw: Vec<String>,
    /// `last_so` by room index (None when no local user is joined).
    pub room_last_so: Vec<Option<i64>>,
    /// `state_events` by room index, used to pick the sample.
    pub room_state_events: Vec<i64>,
    pub event_sample_percent: f32,
}

impl Ctx {
    pub fn hours_ago(&self, ts_ms: Option<i64>) -> Option<i64> {
        ts_ms.map(|t| (self.now_ms - t) / 3_600_000)
    }

    pub fn wants(&self, section: &str) -> bool {
        self.args.only.is_empty() || self.args.only.iter().any(|s| s == section)
    }
}

pub async fn run(args: ProfileArgs) -> Result<()> {
    if args.resume && args.salt_file.is_none() {
        bail!("--resume needs --salt-file so that pseudonyms match the earlier run");
    }
    if let Some(salt) = &args.salt_file {
        if salt.starts_with(&args.out) {
            bail!("--salt-file must not be inside the output directory");
        }
    }
    std::fs::create_dir_all(&args.out).context("creating output directory")?;

    let pool = db::make_pool(&DbOptions {
        url: args.db.clone(),
        concurrency: args.concurrency.max(1),
        statement_timeout_secs: args.statement_timeout_secs,
        work_mem: args.work_mem.clone(),
    })?;

    let client = pool.get().await.context("connecting to the database")?;
    let server_version: String = client.query_one("SELECT version()", &[]).await?.get(0);
    let schema_version: Option<i64> = client
        .query_opt("SELECT version FROM schema_version", &[])
        .await
        .ok()
        .flatten()
        .map(|r| r.get::<_, i32>(0) as i64);
    info!(server_version, ?schema_version, "connected");

    let server_name = match &args.server_name {
        Some(s) => s.clone(),
        None => detect_server_name(&client).await?,
    };
    let anon = Anonymiser::new(load_or_make_key(args.salt_file.as_deref())?, server_name);

    let tables = db::table_estimates(&client).await?;
    let event_json_rows = tables.get("event_json").map(|t| t.rows).unwrap_or(0);
    let event_sample_percent = if event_json_rows <= 0 {
        100.0
    } else {
        ((args.sample_events as f64 / event_json_rows as f64) * 100.0).clamp(0.0001, 100.0) as f32
    };
    drop(client);

    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_millis() as i64;
    let mut ctx = Ctx {
        pool,
        anon,
        out: args.out.clone(),
        now_ms,
        window_start_ms: now_ms - args.window_days * 86_400_000,
        rooms: Indexer::default(),
        users: Indexer::default(),
        room_raw: Vec::new(),
        room_last_so: Vec::new(),
        room_state_events: Vec::new(),
        event_sample_percent,
        args: args.clone(),
    };

    let mut stats: HashMap<String, SectionStats> = HashMap::new();

    // The two index-defining sections run first and sequentially; everything
    // else refers to rooms and users by index.
    timed(&mut stats, "rooms", rooms::run(&mut ctx)).await?;
    timed(&mut stats, "users", users::run(&mut ctx)).await?;

    let ctx = Arc::new(ctx);
    let mut set = JoinSet::new();
    for section in ["memberships", "room_detail", "event_sizes", "rates"] {
        if !ctx.wants(section) {
            info!(section, "skipped (--only)");
            continue;
        }
        if ctx.args.resume && out::section_done(&ctx.out, section) {
            info!(section, "already done, skipping");
            continue;
        }
        let ctx = ctx.clone();
        set.spawn(async move {
            let start = Instant::now();
            let rows = match section {
                "memberships" => memberships::run(&ctx).await,
                "room_detail" => room_detail::run(&ctx).await,
                "event_sizes" => event_sizes::run(&ctx).await,
                "rates" => rates::run(&ctx).await,
                _ => unreachable!(),
            }
            .with_context(|| format!("section {section}"))?;
            Ok::<_, anyhow::Error>((section, rows, start.elapsed().as_secs_f64()))
        });
    }
    while let Some(res) = set.join_next().await {
        let (section, rows, seconds) = res.context("section task panicked")??;
        info!(section, rows, seconds = format!("{seconds:.1}"), "done");
        stats.insert(section.to_owned(), SectionStats { rows, seconds });
        if section == "rates" {
            stats.insert("rates_global".to_owned(), SectionStats { rows: 1, seconds });
        }
    }

    let meta = Meta {
        tool_version: env!("CARGO_PKG_VERSION").to_owned(),
        now_ms: ctx.now_ms,
        window_days: ctx.args.window_days,
        window_start_ms: ctx.window_start_ms,
        server_version,
        schema_version,
        sample_rooms: ctx.args.sample_rooms,
        sample_events: ctx.args.sample_events,
        event_sample_percent: ctx.event_sample_percent,
        tables,
        sections: stats,
    };
    let meta_path = ctx.out.join("meta.json");
    std::fs::write(&meta_path, serde_json::to_vec_pretty(&meta)?)
        .with_context(|| format!("writing {}", meta_path.display()))?;
    info!(out = %ctx.out.display(), "profile complete");
    Ok(())
}

async fn timed<F>(stats: &mut HashMap<String, SectionStats>, name: &str, fut: F) -> Result<()>
where
    F: std::future::Future<Output = Result<u64>>,
{
    let start = Instant::now();
    let rows = fut.await.with_context(|| format!("section {name}"))?;
    let seconds = start.elapsed().as_secs_f64();
    info!(
        section = name,
        rows,
        seconds = format!("{seconds:.1}"),
        "done"
    );
    stats.insert(name.to_owned(), SectionStats { rows, seconds });
    Ok(())
}

async fn detect_server_name(client: &tokio_postgres::Client) -> Result<String> {
    let row = client
        .query_opt("SELECT name FROM users WHERE name LIKE '@%:%' LIMIT 1", &[])
        .await?;
    let name: String = row
        .context("no users in the database; pass --server-name")?
        .get(0);
    let server = crate::anon::server_of(&name)
        .context("malformed user ID in users table")?
        .to_owned();
    info!(server, "detected server name");
    Ok(server)
}

fn load_or_make_key(path: Option<&Path>) -> Result<Vec<u8>> {
    let mut key = vec![0u8; 32];
    match path {
        Some(p) if p.exists() => {
            key = std::fs::read(p).with_context(|| format!("reading {}", p.display()))?;
            if key.len() < 16 {
                bail!("{} is too short to be a key", p.display());
            }
            info!(path = %p.display(), "using existing pseudonymisation key");
        }
        Some(p) => {
            rand::thread_rng().fill_bytes(&mut key);
            write_private(p, &key)?;
            info!(path = %p.display(), "generated pseudonymisation key");
        }
        None => {
            rand::thread_rng().fill_bytes(&mut key);
            warn!("no --salt-file: this run cannot be resumed and its pseudonyms are unique to it");
        }
    }
    Ok(key)
}

#[cfg(unix)]
fn write_private(path: &Path, data: &[u8]) -> Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("creating {}", path.display()))?;
    f.write_all(data)?;
    Ok(())
}

#[cfg(not(unix))]
fn write_private(path: &Path, data: &[u8]) -> Result<()> {
    std::fs::write(path, data).with_context(|| format!("creating {}", path.display()))
}
