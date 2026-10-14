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

//! synth-db: profile a Synapse Postgres database into a small anonymised
//! dataset, and rebuild a synthetic database from such a profile.

mod anon;
mod db;
mod generate;
mod inspect;
mod model;
mod out;
mod profile;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Read a live database (read-only) and write an anonymised profile.
    Profile(profile::ProfileArgs),
    /// Summarise a profile directory.
    Inspect(inspect::InspectArgs),
    /// Build a synthetic database from a profile.
    Generate(generate::GenerateArgs),
    /// Print one generated event as JSON, for checking the hashing against
    /// Synapse's own implementation.
    #[command(hide = true)]
    CheckEvent,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .with_target(false)
        .init();
    match Cli::parse().command {
        Command::Profile(args) => profile::run(args).await,
        Command::Inspect(args) => inspect::run(args),
        Command::Generate(args) => generate::run(args).await,
        Command::CheckEvent => {
            for ev in generate::sample_events() {
                println!(
                    "{}",
                    serde_json::json!({ "event_id": ev.event_id, "json": ev.json })
                );
            }
            Ok(())
        }
    }
}
