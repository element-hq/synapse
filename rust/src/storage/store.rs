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

use sha2::digest::typenum::Len;

use crate::{config::SynapseConfig, storage::db::DatabasePool};

/// Currently supported per-user features
pub enum PerUserExperimentalFeature {
    MSC3881,
    MSC3575,
    MSC4222,
}

impl PerUserExperimentalFeature {
    pub fn is_globally_enabled(&self, config: SynapseConfig) -> bool {
        match self {
            PerUserExperimentalFeature::MSC3881 => config.experimental.msc3881_enabled,
            PerUserExperimentalFeature::MSC3575 => config.experimental.msc3575_enabled,
            PerUserExperimentalFeature::MSC4222 => config.experimental.msc4222_enabled,
        }
    }
}

pub struct Store {
    pub config: SynapseConfig,
    pub db_pool: dyn DatabasePool,
}

impl Store {
    pub async fn is_feature_enabled(
        &self,
        user_id: &str,
        feature: PerUserExperimentalFeature,
    ) -> Result<bool, anyhow::Error> {
        if feature.is_globally_enabled(self.config) {
            return Ok(true);
        }

        let txn = self.db_pool.get_transaction("is_feature_enabled").await;
        let rows = txn
            .query(
                r#"
                SELECT enabled
                FROM per_user_experimental_features
                WHERE user_id = ? AND feature = ?
                "#,
                &[user_id, feature],
            )
            .await;

        match (rows.len(), rows.first()) {
            (1, Some(enabled)) => Ok(enabled),
            (0, None) => Ok(false),
            _ => {
                panic!("Synapse programming error");
            }
        }
    }
}
