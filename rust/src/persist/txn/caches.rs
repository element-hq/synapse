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

//! The cache stream: `_invalidate_cache_and_stream`, for the caches that
//! other workers cannot tell they must invalidate from the other streams.
//!
//! Each invalidation writes a `cache_invalidation_stream_by_instance` row
//! inside the transaction, with an ID from the cache stream's
//! [`super::streams::TxnStream`], and asks Python to invalidate the cache
//! locally. The IDs stay unfinished until Python has done so, as Python's
//! `get_next_txn` finishes them only after the transaction's earlier
//! `call_after` callbacks.

use super::TxnInput;
use crate::{
    database::postgres::write::{WriteError, WriteTxn},
    persist::effects::PersistEffects,
};

/// `_invalidate_cache_and_stream`: invalidate `keys` in the named cache
/// here once the transaction commits, and on the other workers through the
/// cache stream.
pub async fn invalidate_cache_and_stream(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    effects: &mut PersistEffects,
    cache_name: &str,
    keys: Vec<String>,
) -> Result<(), WriteError> {
    send_invalidation_to_replication(txn, input, effects, cache_name, &keys).await?;
    effects.add_cache_invalidation(cache_name, keys);
    Ok(())
}

/// `_send_invalidation_to_replication`: invalidate `keys` in the named
/// cache on the other workers, through the cache stream. Nothing is
/// invalidated locally.
pub async fn send_invalidation_to_replication(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    effects: &mut PersistEffects,
    cache_name: &str,
    keys: &[String],
) -> Result<(), WriteError> {
    let stream_id = input.cache_stream.next_id(txn).await?;
    txn.execute_later(
        "INSERT INTO cache_invalidation_stream_by_instance
            (stream_id, instance_name, cache_func, keys, invalidation_ts)
        VALUES ($1, $2, $3, $4, $5)",
        crate::owned_params![
            stream_id,
            input.instance_name,
            cache_name,
            keys,
            input.now_ms()?
        ],
    );
    effects.add_cache_stream_id(stream_id);
    effects.set_new_replication_data();
    Ok(())
}
