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

//! End-to-end check against a real Synapse database. Skipped unless
//! `SYNTH_DB_TEST_URL` points at one (any populated Synapse Postgres
//! database works; see README for how the CI-less fixture is built).
//!
//! The important assertion is the leak check: nothing that identifies a
//! real room, user, server or piece of content may appear in the output.

use std::collections::HashSet;
use std::io::Read;
use std::path::Path;
use std::process::Command;

fn decompressed(dir: &Path) -> String {
    let mut all = String::new();
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        if name.ends_with(".jsonl.zst") {
            let mut s = String::new();
            zstd::Decoder::new(std::fs::File::open(&path).unwrap())
                .unwrap()
                .read_to_string(&mut s)
                .unwrap();
            all.push_str(&s);
        } else if name == "meta.json" {
            all.push_str(&std::fs::read_to_string(&path).unwrap());
        }
    }
    all
}

#[tokio::test]
async fn profile_leaks_nothing() {
    let Ok(url) = std::env::var("SYNTH_DB_TEST_URL") else {
        eprintln!("SYNTH_DB_TEST_URL not set; skipping");
        return;
    };
    let out = tempdir();
    let status = Command::new(env!("CARGO_BIN_EXE_synth-db"))
        .args(["profile", "--db", &url, "--out"])
        .arg(&out)
        .args(["--sample-rooms", "5", "--window-days", "30"])
        .status()
        .expect("running synth-db");
    assert!(status.success());

    for section in [
        "rooms",
        "users",
        "memberships",
        "room_detail",
        "event_sizes",
        "rates",
        "rates_global",
    ] {
        assert!(
            out.join(format!("{section}.jsonl.zst")).exists(),
            "{section} missing"
        );
    }
    assert!(out.join("meta.json").exists());

    let text = decompressed(&out);
    assert!(!text.is_empty());

    // Every raw identifier in the database must be absent from the output.
    let (client, conn) = tokio_postgres::connect(&url, tokio_postgres::NoTls)
        .await
        .unwrap();
    tokio::spawn(conn);
    let mut raw: HashSet<String> = HashSet::new();
    for (sql, col) in [
        ("SELECT room_id FROM rooms", "room"),
        ("SELECT name FROM users", "user"),
        ("SELECT event_id FROM events", "event"),
        ("SELECT device_id FROM devices", "device"),
        ("SELECT room_alias FROM room_aliases", "alias"),
        (
            "SELECT name FROM room_stats_state WHERE name IS NOT NULL",
            "room name",
        ),
        (
            "SELECT topic FROM room_stats_state WHERE topic IS NOT NULL",
            "topic",
        ),
    ] {
        for row in client.query(sql, &[]).await.unwrap() {
            let v: String = row.get(0);
            if v.len() >= 4 {
                assert!(!text.contains(&v), "{col} {v:?} leaked into the profile");
                raw.insert(v);
            }
        }
    }
    assert!(!raw.is_empty(), "fixture database is empty");

    let server: String = client
        .query_one("SELECT split_part(name, ':', 2) FROM users LIMIT 1", &[])
        .await
        .unwrap()
        .get(0);
    assert!(!text.contains(&server), "server name {server:?} leaked");

    // Spot-check that the structure survived.
    let n_rooms: i64 = client
        .query_one(
            "SELECT count(DISTINCT room_id) FROM local_current_membership",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    let rooms_lines = {
        let mut s = String::new();
        zstd::Decoder::new(std::fs::File::open(out.join("rooms.jsonl.zst")).unwrap())
            .unwrap()
            .read_to_string(&mut s)
            .unwrap();
        s.lines().count() as i64
    };
    assert_eq!(rooms_lines, n_rooms);

    // inspect must be able to read what profile wrote.
    let status = Command::new(env!("CARGO_BIN_EXE_synth-db"))
        .arg("inspect")
        .arg(&out)
        .status()
        .unwrap();
    assert!(status.success());
}

fn tempdir() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("synth-db-test-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}
