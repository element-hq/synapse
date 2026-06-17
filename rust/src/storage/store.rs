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

use futures::FutureExt;
use serde::Serialize;

use crate::{config::SynapseConfig, storage::db::DatabasePool};

/// Currently supported per-user features
#[derive(Serialize, Debug)]
pub enum PerUserExperimentalFeature {
    #[serde(rename = "msc3881")]
    MSC3881,
    #[serde(rename = "msc3575")]
    MSC3575,
    #[serde(rename = "msc4222")]
    MSC4222,
}

impl PerUserExperimentalFeature {
    pub fn is_globally_enabled(&self, config: &SynapseConfig) -> bool {
        match self {
            PerUserExperimentalFeature::MSC3881 => config.experimental.msc3881_enabled,
            PerUserExperimentalFeature::MSC3575 => config.experimental.msc3575_enabled,
            PerUserExperimentalFeature::MSC4222 => config.experimental.msc4222_enabled,
        }
    }
}

impl std::fmt::Display for PerUserExperimentalFeature {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(
            f,
            "{}",
            // Serialize so we can use the serde name of the variant as the source of truth
            serde_json::to_string(self)
                .unwrap_or_else(|err| format!(
                    "<unable to serialize PerUserExperimentalFeature::{:?} to get display value: {}>",
                    self, err
                ))
                // Remove the surrounding quotes from JSON serialization
                .trim_matches('"')
        )
    }
}

pub struct Store<P: DatabasePool> {
    pub config: SynapseConfig,
    pub db_pool: P,
}

impl<P: DatabasePool> Store<P> {
    pub async fn is_feature_enabled(
        &self,
        user_id: &str,
        feature: PerUserExperimentalFeature,
    ) -> Result<bool, anyhow::Error> {
        if feature.is_globally_enabled(&self.config) {
            return Ok(true);
        }

        // It's not enabled globally, so check whether it's enabled per-user.
        //
        // Owned copies so the callback can be `'static` (it may be moved to
        // another thread and called multiple times under retries).
        let user_id = user_id.to_string();
        let feature = feature.to_string();

        let is_feature_enabled_for_user = self
            .db_pool
            .run_interaction("is_feature_enabled_for_user", move |txn| {
                let user_id = user_id.clone();
                let feature = feature.clone();
                async move {
                    let rows = txn
                        .query(
                            "SELECT enabled \
                             FROM per_user_experimental_features \
                             WHERE user_id = ? AND feature = ?",
                            &[user_id.as_str(), feature.as_str()],
                        )
                        .await?;

                    let enabled = match &rows[..] {
                        // If there is no row, default to disabled
                        [] => false,
                        // Found an entry for the user
                        [row] => row.try_get(0),
                        _ => {
                            panic!("Programming error")
                        }
                    };

                    Ok(enabled)
                }
                .boxed()
            })
            .await?;

        Ok(is_feature_enabled_for_user)
    }
}
