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

use serde::{Serialize};

use crate::{config::SynapseConfig, storage::db::DatabasePool};

/// Currently supported per-user features
#[derive(Serialize)]
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


pub struct Store {
    pub config: SynapseConfig,
    pub db_pool: Box<dyn DatabasePool>,
}

impl Store {
    pub async fn is_feature_enabled(
        &self,
        user_id: &str,
        feature: PerUserExperimentalFeature,
    ) -> Result<bool, anyhow::Error> {
        if feature.is_globally_enabled(&self.config) {
            return Ok(true);
        }

        let is_feature_enabled_for_user = self.db_pool.run_interaction<bool>("is_feature_enabled_for_user", |txn| {
            async move {
                let rows = txn
                    .query(
                        r#"
                        SELECT enabled
                        FROM per_user_experimental_features
                        WHERE user_id = ? AND feature = ?
                        "#,
                        &[user_id, &feature.to_string()],
                    )
                    .await;

                match (rows.len(), rows.first()) {
                    (1, Some(enabled)) => enabled,
                    (0, None) => false,
                    _ => {
                        panic!("Synapse programming error");
                    }
                }
            }
            .boxed()
        }).await;
        
        Ok(is_feature_enabled_for_user)
    }
}
