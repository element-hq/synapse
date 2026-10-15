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

//! What the federation transmission loop reads and writes, through the
//! native Postgres driver.
//!
//! Each method is a port of the store method it is named after, and
//! `tests/storage/test_federation_store.py` runs both against the same
//! database. The stream change cache checks those methods start with are
//! not here: the loop keeps its own, see `federation::queue::changes`.
//!
//! None of these writes has a Python cache to invalidate.

use std::collections::HashMap;

use anyhow::Context;
use base64::Engine;
use log::warn;
use pyo3::{exceptions::PyRuntimeError, prelude::*};
use serde_json::{Map, Value};
use tokio::runtime::Handle;
use tokio_postgres::{types::ToSql, Row};

use crate::database::postgres::pool::{ConnectionPool, PooledConnection, PyConnectionPool};
use crate::federation::queue::Edu;

/// `EduTypes.DEVICE_LIST_UPDATE`.
const DEVICE_LIST_UPDATE: &str = "m.device_list_update";
/// `EduTypes.SIGNING_KEY_UPDATE`.
const SIGNING_KEY_UPDATE: &str = "m.signing_key_update";
/// The unstable name of `m.signing_key_update`, which is sent as well.
const UNSTABLE_SIGNING_KEY_UPDATE: &str = "org.matrix.signing_key_update";

/// The parts of the homeserver config the queries apply.
#[derive(Debug, Clone, Copy, Default)]
pub struct FederationStoreConfig {
    /// `allow_device_name_lookup_over_federation`.
    pub allow_device_name_lookup_over_federation: bool,
}

/// The transmission loop's queries.
pub struct FederationStore {
    config: FederationStoreConfig,
}

impl FederationStore {
    pub fn new(config: FederationStoreConfig) -> Self {
        FederationStore { config }
    }

    /// `get_new_device_msgs_for_remote`, less its stream change cache check:
    /// the contents of the to-device messages queued for `destination`
    /// after `last_stream_id`, and how far through the stream they go.
    pub async fn get_new_device_msgs_for_remote(
        &self,
        pool: &ConnectionPool,
        destination: &str,
        last_stream_id: i64,
        current_stream_id: i64,
        limit: usize,
    ) -> anyhow::Result<(Vec<Value>, i64)> {
        if last_stream_id == current_stream_id {
            return Ok((Vec::new(), current_stream_id));
        }
        if limit == 0 {
            // We ran out of room for EDUs in the transaction.
            return Ok((Vec::new(), last_stream_id));
        }

        let conn = Conn::borrow(pool).await?;
        let rows = conn
            .query(
                "SELECT stream_id, messages_json FROM device_federation_outbox
                WHERE destination = $1 AND $2 < stream_id AND stream_id <= $3
                ORDER BY stream_id ASC
                LIMIT $4",
                &[
                    &destination,
                    &last_stream_id,
                    &current_stream_id,
                    &(limit as i64),
                ],
            )
            .await?;

        let mut messages = Vec::with_capacity(rows.len());
        let mut stream_pos = current_stream_id;
        for row in &rows {
            stream_pos = row.try_get(0)?;
            let json: &str = row.try_get(1)?;
            messages.push(serde_json::from_str(json).context("parsing messages_json")?);
        }

        // Short of the limit, there is nothing more up to the current
        // position.
        if messages.len() < limit {
            stream_pos = current_stream_id;
        }
        Ok((messages, stream_pos))
    }

    /// `delete_device_msgs_for_remote`: the destination has the to-device
    /// messages up to `up_to_stream_id`.
    pub async fn delete_device_msgs_for_remote(
        &self,
        pool: &ConnectionPool,
        destination: &str,
        up_to_stream_id: i64,
    ) -> anyhow::Result<()> {
        Conn::borrow(pool)
            .await?
            .query(
                "DELETE FROM device_federation_outbox
                WHERE destination = $1 AND stream_id <= $2",
                &[&destination, &up_to_stream_id],
            )
            .await?;
        Ok(())
    }

    /// `get_device_updates_by_remote`, less its stream change cache check:
    /// the device list and cross-signing key updates to send to
    /// `destination`, at most `limit`, and the stream ID they go up to.
    ///
    /// The EDUs of a user come in stream order, with the prev ID chain
    /// through them. The users come in the order of their first update,
    /// where Python has the order the database returns them in.
    pub async fn get_device_updates_by_remote(
        &self,
        pool: &ConnectionPool,
        destination: &str,
        from_stream_id: i64,
        now_stream_id: i64,
        limit: usize,
    ) -> anyhow::Result<(i64, Vec<Edu>)> {
        if from_stream_id == now_stream_id {
            return Ok((now_stream_id, Vec::new()));
        }

        let conn = Conn::borrow(pool).await?;
        let rows = conn
            .query(
                "SELECT user_id, device_id, stream_id, opentracing_context
                FROM device_lists_outbound_pokes
                WHERE destination = $1 AND $2 < stream_id AND stream_id <= $3
                ORDER BY stream_id
                LIMIT $4",
                &[
                    &destination,
                    &from_stream_id,
                    &now_stream_id,
                    &(limit as i64),
                ],
            )
            .await?;
        if rows.is_empty() {
            return Ok((now_stream_id, Vec::new()));
        }
        let updates = rows
            .iter()
            .map(|row| -> anyhow::Result<Poke> {
                Ok(Poke {
                    user_id: row.try_get(0)?,
                    device_id: row.try_get(1)?,
                    stream_id: row.try_get(2)?,
                    context: row.try_get(3)?,
                })
            })
            .collect::<anyhow::Result<Vec<_>>>()?;

        // The users' cross-signing keys, to tell which updates were of them.
        let mut users: Vec<&str> = Vec::new();
        for update in &updates {
            if !users.contains(&update.user_id.as_str()) {
                users.push(&update.user_id);
            }
        }
        let keys = cross_signing_keys(&conn, &users).await?;
        let key_device = |user_id: &str, key_type: &str| -> anyhow::Result<Option<String>> {
            keys.get(&(user_id.to_owned(), key_type.to_owned()))
                .filter(|key| is_truthy(key))
                .map(cross_signing_key_device_id)
                .transpose()
        };
        let mut master_keys = HashMap::new();
        let mut self_signing_keys = HashMap::new();
        for user_id in &users {
            if let Some(device_id) = key_device(user_id, "master")? {
                master_keys.insert(*user_id, device_id);
            }
            if let Some(device_id) = key_device(user_id, "self_signing")? {
                self_signing_keys.insert(*user_id, device_id);
            }
        }

        // The latest update per device, and the cross-signing key updates
        // per user, within the budget. A cross-signing key update becomes
        // two EDUs, so it costs two.
        let mut last_processed_stream_id = from_stream_id;
        let mut devices: OrderedDevices = OrderedDevices::default();
        let mut cross_signing: Vec<(&str, Map<String, Value>)> = Vec::new();
        for update in &updates {
            let remaining = limit - (devices.len() + 2 * cross_signing.len());
            let user_id = update.user_id.as_str();
            let is_master = master_keys.get(user_id) == Some(&update.device_id);
            let is_self_signing = self_signing_keys.get(user_id) == Some(&update.device_id);
            let user_index = cross_signing.iter().position(|(user, _)| *user == user_id);

            if (is_master || is_self_signing) && user_index.is_none() && remaining < 2 {
                break;
            }

            if is_master || is_self_signing {
                let index = user_index.unwrap_or_else(|| {
                    cross_signing.push((user_id, Map::new()));
                    cross_signing.len() - 1
                });
                let (field, key_type) = if is_master {
                    ("master_key", "master")
                } else {
                    ("self_signing_key", "self_signing")
                };
                let key = keys
                    .get(&(user_id.to_owned(), key_type.to_owned()))
                    .cloned()
                    .unwrap_or(Value::Null);
                cross_signing[index].1.insert(field.to_owned(), key);
            } else {
                let key = (update.user_id.clone(), update.device_id.clone());
                if !devices.contains(&key) && remaining < 1 {
                    break;
                }
                devices.update(key, update.stream_id, update.context.clone());
            }

            last_processed_stream_id = update.stream_id;
        }

        let mut edus = self
            .device_update_edus(&conn, destination, from_stream_id, &devices)
            .await?;

        for (user_id, mut content) in cross_signing {
            content.insert("user_id".to_owned(), Value::from(user_id));
            let content = Value::Object(content);
            edus.push(Edu {
                edu_type: SIGNING_KEY_UPDATE.to_owned(),
                content: content.clone(),
            });
            edus.push(Edu {
                edu_type: UNSTABLE_SIGNING_KEY_UPDATE.to_owned(),
                content,
            });
        }

        Ok((last_processed_stream_id, edus))
    }

    /// `_get_device_update_edus_by_remote`: an `m.device_list_update` EDU
    /// per device, with its keys, or marked deleted.
    async fn device_update_edus(
        &self,
        conn: &Conn,
        destination: &str,
        from_stream_id: i64,
        devices: &OrderedDevices,
    ) -> anyhow::Result<Vec<Edu>> {
        if devices.is_empty() {
            return Ok(Vec::new());
        }
        let found = device_keys(conn, devices.keys()).await?;

        let mut users: Vec<&str> = Vec::new();
        for (user_id, _) in devices.keys() {
            if !users.contains(&user_id.as_str()) {
                users.push(user_id);
            }
        }
        let prev_ids =
            last_device_updates_for_remote(conn, destination, &users, from_stream_id).await?;

        let mut edus = Vec::with_capacity(devices.len());
        for user_id in users {
            // The prev ID of the first update is the last one sent before
            // `from_stream_id`.
            let mut prev_id = prev_ids.get(user_id).copied().unwrap_or(0);

            let mut user_devices: Vec<(&DeviceKey, &DeviceUpdate)> = devices
                .entries
                .iter()
                .filter(|(key, _)| key.0 == user_id)
                .map(|(key, value)| (key, value))
                .collect();
            user_devices.sort_by_key(|(_, (stream_id, _))| *stream_id);

            for (key, (stream_id, context)) in user_devices {
                let mut content = Map::new();
                content.insert("user_id".to_owned(), Value::from(user_id));
                content.insert("device_id".to_owned(), Value::from(key.1.as_str()));
                content.insert(
                    "prev_id".to_owned(),
                    if prev_id != 0 {
                        Value::from(vec![prev_id])
                    } else {
                        Value::Array(Vec::new())
                    },
                );
                content.insert("stream_id".to_owned(), Value::from(*stream_id));
                if context.as_deref() != Some("{}") {
                    content.insert(
                        "org.matrix.opentracing_context".to_owned(),
                        context.clone().map_or(Value::Null, Value::from),
                    );
                }
                prev_id = *stream_id;

                match found.get(key) {
                    Some(device) => {
                        if let Some(keys) = device.keys.as_ref().filter(|keys| is_truthy(keys)) {
                            content.insert("keys".to_owned(), keys.clone());
                        }
                        if self.config.allow_device_name_lookup_over_federation {
                            if let Some(name) =
                                device.display_name.as_ref().filter(|name| !name.is_empty())
                            {
                                content.insert(
                                    "device_display_name".to_owned(),
                                    Value::from(name.as_str()),
                                );
                            }
                        }
                    }
                    None => {
                        content.insert("deleted".to_owned(), Value::Bool(true));
                    }
                }

                edus.push(Edu {
                    edu_type: DEVICE_LIST_UPDATE.to_owned(),
                    content: Value::Object(content),
                });
            }
        }
        Ok(edus)
    }

    /// `mark_as_sent_devices_by_remote`: the destination has the device
    /// list updates up to `stream_id`. Deletes the pokes and records, per
    /// user, the last update sent, in one transaction.
    pub async fn mark_as_sent_devices_by_remote(
        &self,
        pool: &ConnectionPool,
        destination: &str,
        stream_id: i64,
    ) -> anyhow::Result<()> {
        let mut conn = Conn::borrow(pool).await?;
        conn.begin().await?;

        let rows = conn
            .query(
                "DELETE FROM device_lists_outbound_pokes
                WHERE destination = $1 AND stream_id <= $2
                RETURNING user_id, stream_id",
                &[&destination, &stream_id],
            )
            .await?;
        let mut max_stream_id_by_user: HashMap<String, i64> = HashMap::new();
        for row in &rows {
            let user_id: String = row.try_get(0)?;
            let poke_stream_id: i64 = row.try_get(1)?;
            let max = max_stream_id_by_user.entry(user_id).or_insert(0);
            *max = (*max).max(poke_stream_id);
        }

        if !max_stream_id_by_user.is_empty() {
            let (user_ids, stream_ids): (Vec<String>, Vec<i64>) =
                max_stream_id_by_user.into_iter().unzip();
            conn.query(
                "INSERT INTO device_lists_outbound_last_success (destination, user_id, stream_id)
                SELECT $1, user_id, stream_id FROM unnest($2::text[], $3::bigint[])
                    AS u(user_id, stream_id)
                ON CONFLICT (destination, user_id) DO UPDATE SET stream_id = EXCLUDED.stream_id",
                &[&destination, &user_ids, &stream_ids],
            )
            .await?;
        }

        conn.commit().await
    }

    /// `set_destination_last_successful_stream_ordering`.
    pub async fn set_destination_last_successful_stream_ordering(
        &self,
        pool: &ConnectionPool,
        destination: &str,
        last_successful_stream_ordering: i64,
    ) -> anyhow::Result<()> {
        Conn::borrow(pool)
            .await?
            .query(
                "INSERT INTO destinations (destination, last_successful_stream_ordering)
                VALUES ($1, $2)
                ON CONFLICT (destination) DO UPDATE
                    SET last_successful_stream_ordering = EXCLUDED.last_successful_stream_ordering",
                &[&destination, &last_successful_stream_ordering],
            )
            .await?;
        Ok(())
    }
}

/// A row of `device_lists_outbound_pokes`.
struct Poke {
    user_id: String,
    device_id: String,
    stream_id: i64,
    context: Option<String>,
}

/// `(user ID, device ID)`.
type DeviceKey = (String, String);

/// `(stream ID, tracing context)` of a device's update.
type DeviceUpdate = (i64, Option<String>);

/// The latest update of each device, in the order the devices were first
/// seen.
#[derive(Default)]
struct OrderedDevices {
    entries: Vec<(DeviceKey, DeviceUpdate)>,
}

impl OrderedDevices {
    fn len(&self) -> usize {
        self.entries.len()
    }

    fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    fn contains(&self, key: &(String, String)) -> bool {
        self.entries.iter().any(|(k, _)| k == key)
    }

    fn keys(&self) -> impl Iterator<Item = &(String, String)> {
        self.entries.iter().map(|(key, _)| key)
    }

    /// Keep the update with the higher stream ID.
    fn update(&mut self, key: (String, String), stream_id: i64, context: Option<String>) {
        match self.entries.iter_mut().find(|(k, _)| *k == key) {
            Some((_, existing)) => {
                if stream_id > existing.0 {
                    *existing = (stream_id, context);
                }
            }
            None => self.entries.push((key, (stream_id, context))),
        }
    }
}

/// A device's keys and name: `DeviceKeyLookupResult`.
struct DeviceKeys {
    display_name: Option<String>,
    keys: Option<Value>,
}

/// Whether Python would take `value` as true.
fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// The latest cross-signing keys of each user, keyed by `(user ID, key
/// type)`: `_get_bare_e2e_cross_signing_keys_bulk`.
async fn cross_signing_keys(
    conn: &Conn,
    users: &[&str],
) -> anyhow::Result<HashMap<(String, String), Value>> {
    let rows = conn
        .query(
            "SELECT DISTINCT ON (user_id, keytype) user_id, keytype, keydata
            FROM e2e_cross_signing_keys
            WHERE user_id = ANY($1)
            ORDER BY user_id, keytype, stream_id DESC",
            &[&users],
        )
        .await?;
    let mut keys = HashMap::with_capacity(rows.len());
    for row in &rows {
        let user_id: String = row.try_get(0)?;
        let key_type: String = row.try_get(1)?;
        let key_data: &str = row.try_get(2)?;
        keys.insert(
            (user_id, key_type),
            serde_json::from_str(key_data).context("parsing keydata")?,
        );
    }
    Ok(keys)
}

/// Decode base64 as leniently as `unpaddedbase64.decode_base64`. That takes
/// the standard and URL-safe alphabets, skips other characters and ignores
/// padding and trailing bits.
fn decode_base64_lenient(data: &str) -> Result<Vec<u8>, base64::DecodeError> {
    use base64::engine::{general_purpose, DecodePaddingMode, GeneralPurpose};

    const LENIENT: GeneralPurpose = GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        general_purpose::NO_PAD
            .with_decode_allow_trailing_bits(true)
            .with_decode_padding_mode(DecodePaddingMode::Indifferent),
    );

    let cleaned: String = data
        .chars()
        .take_while(|c| *c != '=')
        .filter_map(|c| match c {
            '-' => Some('+'),
            '_' => Some('/'),
            c if c.is_ascii_alphanumeric() || c == '+' || c == '/' => Some(c),
            _ => None,
        })
        .collect();
    LENIENT.decode(cleaned)
}

/// The device ID of a cross-signing key: the part of its one key ID after
/// the algorithm. `get_verify_key_from_cross_signing_key` fails on anything
/// but a single valid ed25519 key, and so does this.
fn cross_signing_key_device_id(key: &Value) -> anyhow::Result<String> {
    let keys = key
        .get("keys")
        .and_then(Value::as_object)
        .context("Invalid key")?;
    let mut entries = keys.iter();
    let (Some((key_id, key_data)), None) = (entries.next(), entries.next()) else {
        anyhow::bail!("Invalid key");
    };
    let (algorithm, version) = key_id.split_once(':').context("Invalid key")?;
    if algorithm != "ed25519" {
        anyhow::bail!("Unsupported algorithm {algorithm}");
    }
    let key_data = key_data.as_str().context("Invalid key")?;
    let bytes = decode_base64_lenient(key_data).context("Invalid key")?;
    if bytes.len() != 32 {
        anyhow::bail!("Invalid key");
    }
    Ok(version.to_owned())
}

/// The devices' keys, with the owners' cross-signatures on them, and names:
/// `get_e2e_device_keys_and_signatures` with `include_all_devices`. Hidden
/// devices are left out. A device that is not in the result has been
/// deleted.
async fn device_keys(
    conn: &Conn,
    devices: impl Iterator<Item = &(String, String)>,
) -> anyhow::Result<HashMap<(String, String), DeviceKeys>> {
    let (user_ids, device_ids): (Vec<&str>, Vec<&str>) = devices
        .map(|(user_id, device_id)| (user_id.as_str(), device_id.as_str()))
        .unzip();
    let rows = conn
        .query(
            "SELECT user_id, device_id, d.display_name, k.key_json
            FROM devices d
            LEFT JOIN e2e_device_keys_json k USING (user_id, device_id)
            WHERE (user_id, device_id) IN (
                SELECT * FROM unnest($1::text[], $2::text[])
            ) AND NOT d.hidden",
            &[&user_ids, &device_ids],
        )
        .await?;

    let mut found = HashMap::with_capacity(rows.len());
    for row in &rows {
        let user_id: String = row.try_get(0)?;
        let device_id: String = row.try_get(1)?;
        let display_name: Option<String> = row.try_get(2)?;
        let key_json: Option<&str> = row.try_get(3)?;
        let keys = key_json
            .filter(|json| !json.is_empty())
            .map(serde_json::from_str::<Value>)
            .transpose()
            .context("parsing key_json")?;
        found.insert((user_id, device_id), DeviceKeys { display_name, keys });
    }

    // Add each owner's cross-signatures on the devices with keys.
    let (user_ids, device_ids): (Vec<&str>, Vec<&str>) = found
        .iter()
        .filter(|(_, device)| device.keys.is_some())
        .map(|((user_id, device_id), _)| (user_id.as_str(), device_id.as_str()))
        .unzip();
    if user_ids.is_empty() {
        return Ok(found);
    }
    let rows = conn
        .query(
            "SELECT user_id, key_id, target_device_id, signature
            FROM e2e_cross_signing_signatures
            WHERE (target_user_id, target_device_id, user_id) IN (
                SELECT u, d, u FROM unnest($1::text[], $2::text[]) AS t(u, d)
            )",
            &[&user_ids, &device_ids],
        )
        .await?;
    let mut signatures: Vec<(String, String, String, String)> = Vec::with_capacity(rows.len());
    for row in &rows {
        signatures.push((
            row.try_get(0)?,
            row.try_get(1)?,
            row.try_get(2)?,
            row.try_get(3)?,
        ));
    }
    for (user_id, key_id, device_id, signature) in signatures {
        let Some(keys) = found
            .get_mut(&(user_id.clone(), device_id))
            .and_then(|device| device.keys.as_mut())
            .and_then(Value::as_object_mut)
        else {
            continue;
        };
        let by_user = keys
            .entry("signatures")
            .or_insert_with(|| Value::Object(Map::new()));
        let Some(by_user) = by_user.as_object_mut() else {
            continue;
        };
        let by_key = by_user
            .entry(user_id)
            .or_insert_with(|| Value::Object(Map::new()));
        if let Some(by_key) = by_key.as_object_mut() {
            by_key.insert(key_id, Value::from(signature));
        }
    }
    Ok(found)
}

/// The last device list update sent to `destination` for each user, at or
/// before `from_stream_id`: `_get_last_device_update_for_remote_user`, for
/// several users at once. A user with none is missing.
async fn last_device_updates_for_remote(
    conn: &Conn,
    destination: &str,
    users: &[&str],
    from_stream_id: i64,
) -> anyhow::Result<HashMap<String, i64>> {
    let rows = conn
        .query(
            "SELECT user_id, max(stream_id) FROM device_lists_outbound_last_success
            WHERE destination = $1 AND user_id = ANY($2) AND stream_id <= $3
            GROUP BY user_id",
            &[&destination, &users, &from_stream_id],
        )
        .await?;
    let mut last = HashMap::with_capacity(rows.len());
    for row in &rows {
        let stream_id: Option<i64> = row.try_get(1)?;
        last.insert(row.try_get(0)?, stream_id.unwrap_or(0));
    }
    Ok(last)
}

/// A connection borrowed for a group of statements, rolled back if it is
/// dropped inside a transaction.
struct Conn {
    conn: Option<PooledConnection>,
    in_transaction: bool,
}

impl Conn {
    async fn borrow(pool: &ConnectionPool) -> anyhow::Result<Conn> {
        let conn = pool
            .get()
            .await
            .map_err(|e| anyhow::anyhow!("checking out a database connection: {e}"))?;
        Ok(Conn {
            conn: Some(conn),
            in_transaction: false,
        })
    }

    fn conn(&self) -> &PooledConnection {
        self.conn.as_ref().expect("held until dropped")
    }

    /// Run a statement through the connection's prepared statement cache.
    async fn query(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> anyhow::Result<Vec<Row>> {
        let conn = self.conn();
        let statement = conn
            .prepare_cached(sql, false)
            .await
            .with_context(|| format!("preparing {}", sql.trim()))?;
        conn.query(&statement, params)
            .await
            .with_context(|| format!("running {}", sql.trim()))
    }

    /// Open a `READ COMMITTED` transaction. Its writes are upserts and
    /// deletes on rows only this sender writes, so it needs no retry loop.
    async fn begin(&mut self) -> anyhow::Result<()> {
        // Set before awaiting: if we are dropped after BEGIN is sent but
        // before it returns, the transaction is open and must be rolled back.
        // A ROLLBACK outside one only warns.
        self.in_transaction = true;
        self.conn()
            .batch_execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            .await
            .context("opening a transaction")?;
        Ok(())
    }

    async fn commit(mut self) -> anyhow::Result<()> {
        self.conn()
            .batch_execute("COMMIT")
            .await
            .context("committing")?;
        self.in_transaction = false;
        Ok(())
    }
}

impl Drop for Conn {
    fn drop(&mut self) {
        let Some(conn) = self.conn.take() else { return };
        if !self.in_transaction {
            return;
        }
        // Roll back before the connection goes back to the pool, so the next
        // user doesn't find it inside our transaction. Without a runtime to
        // do that on, the connection is dropped rather than returned.
        match Handle::try_current() {
            Ok(handle) => {
                handle.spawn(async move {
                    if let Err(e) = conn.batch_execute("ROLLBACK").await {
                        warn!("Failed to roll back a federation transaction: {e}");
                    }
                    drop(conn);
                });
            }
            Err(_) => {
                drop(PooledConnection::take(conn));
            }
        }
    }
}

/// The store, for tests that compare it with Python's on one database.
/// Each method blocks until its query is done.
#[pyclass(frozen, name = "FederationStoreForTest")]
pub struct PyFederationStoreForTest {
    store: FederationStore,
    pool: ConnectionPool,
    handle: Handle,
}

impl PyFederationStoreForTest {
    fn run<T: Send>(
        &self,
        py: Python<'_>,
        f: impl std::future::Future<Output = anyhow::Result<T>> + Send,
    ) -> PyResult<T> {
        py.detach(|| self.handle.block_on(f))
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))
    }
}

#[pymethods]
impl PyFederationStoreForTest {
    #[new]
    fn new(
        pool: &Bound<'_, PyConnectionPool>,
        allow_device_name_lookup_over_federation: bool,
    ) -> Self {
        let pool = pool.get();
        PyFederationStoreForTest {
            store: FederationStore::new(FederationStoreConfig {
                allow_device_name_lookup_over_federation,
            }),
            pool: pool.pool.clone(),
            handle: pool.handle.clone(),
        }
    }

    fn get_new_device_msgs_for_remote<'py>(
        &self,
        py: Python<'py>,
        destination: &str,
        last_stream_id: i64,
        current_stream_id: i64,
        limit: usize,
    ) -> PyResult<(Bound<'py, PyAny>, i64)> {
        let (messages, stream_id) = self.run(
            py,
            self.store.get_new_device_msgs_for_remote(
                &self.pool,
                destination,
                last_stream_id,
                current_stream_id,
                limit,
            ),
        )?;
        Ok((pythonize::pythonize(py, &messages)?, stream_id))
    }

    fn delete_device_msgs_for_remote(
        &self,
        py: Python<'_>,
        destination: &str,
        up_to_stream_id: i64,
    ) -> PyResult<()> {
        self.run(
            py,
            self.store
                .delete_device_msgs_for_remote(&self.pool, destination, up_to_stream_id),
        )
    }

    #[allow(clippy::type_complexity)]
    fn get_device_updates_by_remote<'py>(
        &self,
        py: Python<'py>,
        destination: &str,
        from_stream_id: i64,
        now_stream_id: i64,
        limit: usize,
    ) -> PyResult<(i64, Vec<(String, Bound<'py, PyAny>)>)> {
        let (stream_id, edus) = self.run(
            py,
            self.store.get_device_updates_by_remote(
                &self.pool,
                destination,
                from_stream_id,
                now_stream_id,
                limit,
            ),
        )?;
        let edus = edus
            .into_iter()
            .map(|edu| Ok((edu.edu_type, pythonize::pythonize(py, &edu.content)?)))
            .collect::<PyResult<_>>()?;
        Ok((stream_id, edus))
    }

    fn mark_as_sent_devices_by_remote(
        &self,
        py: Python<'_>,
        destination: &str,
        stream_id: i64,
    ) -> PyResult<()> {
        self.run(
            py,
            self.store
                .mark_as_sent_devices_by_remote(&self.pool, destination, stream_id),
        )
    }

    fn set_destination_last_successful_stream_ordering(
        &self,
        py: Python<'_>,
        destination: &str,
        stream_ordering: i64,
    ) -> PyResult<()> {
        self.run(
            py,
            self.store.set_destination_last_successful_stream_ordering(
                &self.pool,
                destination,
                stream_ordering,
            ),
        )
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn device_id_of_a_cross_signing_key() {
        let key = json!({
            "user_id": "@a:test",
            "usage": ["master"],
            "keys": {"ed25519:AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdE": "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdE"},
        });
        assert_eq!(
            cross_signing_key_device_id(&key).unwrap(),
            "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdE"
        );

        for bad in [
            json!({}),
            json!({"keys": {}}),
            json!({"keys": {"ed25519:a": "AAAA", "ed25519:b": "AAAA"}}),
            json!({"keys": {"curve25519:a": "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdE"}}),
            json!({"keys": {"ed25519:a": "too short"}}),
        ] {
            assert!(cross_signing_key_device_id(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn lenient_base64() {
        // What `unpaddedbase64.decode_base64` returns for each.
        let key = "01b09d11f1a12232a53273a942b4ad52f5b1633d35db7e39ebbf3dfbf01b09d1";
        for (input, expected) in [
            ("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdE", key),
            ("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE", key),
            ("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdF", key),
            ("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdE=", key),
            ("AbCd EfGh!", "01b09d11f1a1"),
        ] {
            let decoded = decode_base64_lenient(input).unwrap();
            let hex: String = decoded.iter().map(|b| format!("{b:02x}")).collect();
            assert_eq!(hex, expected, "{input}");
        }
    }

    #[test]
    fn truthiness() {
        assert!(!is_truthy(&json!({})));
        assert!(!is_truthy(&Value::Null));
        assert!(is_truthy(&json!({"a": 1})));
    }

    #[test]
    fn latest_update_per_device() {
        let mut devices = OrderedDevices::default();
        let a = ("@a:test".to_owned(), "A".to_owned());
        let b = ("@a:test".to_owned(), "B".to_owned());
        devices.update(a.clone(), 1, None);
        devices.update(b.clone(), 2, Some("x".to_owned()));
        devices.update(a.clone(), 3, Some("y".to_owned()));
        assert_eq!(devices.len(), 2);
        assert_eq!(devices.entries[0], (a, (3, Some("y".to_owned()))));
        assert_eq!(devices.entries[1], (b, (2, Some("x".to_owned()))));
    }
}
