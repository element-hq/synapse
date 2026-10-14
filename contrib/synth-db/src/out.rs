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

//! Profile output: a directory holding one zstd-compressed JSONL file per
//! section plus `meta.json`. A section is written to a `.tmp` file and
//! renamed into place when complete, so a half-written section from an
//! interrupted run is never mistaken for a finished one.

use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::de::DeserializeOwned;
use serde::Serialize;

pub const SECTIONS: &[&str] = &[
    "rooms",
    "users",
    "memberships",
    "room_detail",
    "event_sizes",
    "rates",
    "rates_global",
];

pub fn section_path(dir: &Path, section: &str) -> PathBuf {
    dir.join(format!("{section}.jsonl.zst"))
}

pub fn section_done(dir: &Path, section: &str) -> bool {
    section_path(dir, section).exists()
}

pub struct SectionWriter {
    final_path: PathBuf,
    tmp_path: PathBuf,
    encoder: Option<zstd::Encoder<'static, BufWriter<File>>>,
    rows: u64,
}

impl SectionWriter {
    pub fn create(dir: &Path, section: &str) -> Result<Self> {
        let final_path = section_path(dir, section);
        let tmp_path = dir.join(format!("{section}.jsonl.zst.tmp"));
        let file =
            File::create(&tmp_path).with_context(|| format!("creating {}", tmp_path.display()))?;
        let mut encoder = zstd::Encoder::new(BufWriter::new(file), 9)?;
        // Long-window matching helps a lot on JSONL with repeated keys.
        encoder.long_distance_matching(true)?;
        Ok(Self {
            final_path,
            tmp_path,
            encoder: Some(encoder),
            rows: 0,
        })
    }

    pub fn write<T: Serialize>(&mut self, row: &T) -> Result<()> {
        let enc = self.encoder.as_mut().expect("writer already finished");
        serde_json::to_writer(&mut *enc, row)?;
        enc.write_all(b"\n")?;
        self.rows += 1;
        Ok(())
    }

    pub fn finish(mut self) -> Result<u64> {
        let enc = self.encoder.take().expect("writer already finished");
        let mut inner = enc.finish()?;
        inner.flush()?;
        drop(inner);
        std::fs::rename(&self.tmp_path, &self.final_path)
            .with_context(|| format!("renaming {}", self.tmp_path.display()))?;
        Ok(self.rows)
    }
}

impl Drop for SectionWriter {
    fn drop(&mut self) {
        // An unfinished writer leaves only the `.tmp` behind; remove it so a
        // rerun starts clean.
        if self.encoder.is_some() {
            let _ = std::fs::remove_file(&self.tmp_path);
        }
    }
}

/// Iterate the rows of a finished section.
pub fn read_section<T: DeserializeOwned>(
    dir: &Path,
    section: &str,
) -> Result<impl Iterator<Item = Result<T>>> {
    let path = section_path(dir, section);
    let file = File::open(&path).with_context(|| format!("opening {}", path.display()))?;
    let decoder = zstd::Decoder::new(file)?;
    let reader = BufReader::new(decoder);
    Ok(reader.lines().map(|line| {
        let line = line?;
        serde_json::from_str(&line).context("decoding profile row")
    }))
}
