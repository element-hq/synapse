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

use std::sync::Arc;

use futures::FutureExt;
use pyo3::{
    types::{PyAnyMethods, PyTuple},
    Bound,
};

use crate::caches::{python::PyCacheKey, registry::CacheRegistry, Cache};
use crate::storage::caches::StoreCaches;
use crate::storage::db::{DatabasePool, DatabasePoolExt, DbRowExt};

/// Currently supported per-user features.
///
/// Mirrors Python's `ExperimentalFeature`: the two must list the same
/// features, since Python reads the cache keyed by this through
/// [`crate::storage::python::IsFeatureEnabledForUser`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PerUserExperimentalFeature {
    MSC3881,
    MSC3575,
}

impl PerUserExperimentalFeature {
    const ALL: [Self; 2] = [Self::MSC3881, Self::MSC3575];

    /// The feature's name as stored in `per_user_experimental_features.feature`.
    /// This is the one place the name is spelled.
    pub fn name(&self) -> &'static str {
        match self {
            PerUserExperimentalFeature::MSC3881 => "msc3881",
            PerUserExperimentalFeature::MSC3575 => "msc3575",
        }
    }

    /// The feature called `name`, if Rust knows it.
    pub fn from_name(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|feature| feature.name() == name)
    }
}

/// The key of the `is_feature_enabled_for_user` cache.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct IsFeatureEnabledKey {
    user_id: Arc<str>,
    feature: PerUserExperimentalFeature,
}

impl IsFeatureEnabledKey {
    pub fn new(user_id: &str, feature: PerUserExperimentalFeature) -> Self {
        IsFeatureEnabledKey {
            user_id: user_id.into(),
            feature,
        }
    }
}

impl PyCacheKey for IsFeatureEnabledKey {
    fn from_py_key(key: &Bound<'_, PyTuple>) -> Option<Self> {
        // `ExperimentalFeature` subclasses `str`, so the feature extracts as
        // its value ("msc3881", ...) whether it arrives as the enum from this
        // worker or as a plain string over replication. Anything else matches
        // nothing: a key of a different shape, or a feature name this version
        // doesn't know, which can't be cached here anyway.
        let (user_id, feature): (String, String) = key.extract().ok()?;
        let feature = PerUserExperimentalFeature::from_name(&feature)?;

        Some(IsFeatureEnabledKey::new(&user_id, feature))
    }
}

pub struct Store {
    pub db_pool: Box<dyn DatabasePool>,

    /// The event, chain cover and state group caches, which the Rust
    /// handlers' readers consult before the database. See
    /// [`crate::storage::caches`].
    pub caches: Arc<StoreCaches>,

    /// Caches [`Store::is_feature_enabled_for_user`]. Python reads through it
    /// too, and invalidates it, as
    /// [`crate::storage::python::IsFeatureEnabledForUser`]. Sized to match the
    /// Python cache it replaced.
    pub(crate) is_feature_enabled_for_user_cache: Arc<Cache<IsFeatureEnabledKey, Option<bool>>>,
}

impl Store {
    /// A store over `db_pool`, with its caches built in `registry`, the
    /// registry of the homeserver's Rust runtime.
    pub fn new(db_pool: Box<dyn DatabasePool>, registry: &CacheRegistry) -> Store {
        Store {
            db_pool,
            caches: Arc::new(StoreCaches::new(registry)),
            is_feature_enabled_for_user_cache: registry
                .new_cache("is_feature_enabled_for_user", 1000),
        }
    }

    /// Checks whether a given feature is enabled/disabled for this user
    ///
    /// If there is no entry, returns None
    pub async fn is_feature_enabled_for_user(
        &self,
        key: IsFeatureEnabledKey,
    ) -> Result<Option<bool>, anyhow::Error> {
        // The database callback needs its own copy of the user id. `Arc<str>`
        // makes that a refcount bump.
        let user_id = Arc::clone(&key.user_id);
        let feature = key.feature;

        self.is_feature_enabled_for_user_cache
            .get_or_load(key, || {
                self.load_is_feature_enabled_for_user(Arc::clone(&user_id), feature)
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// The uncached body of [`Store::is_feature_enabled_for_user`].
    async fn load_is_feature_enabled_for_user(
        &self,
        user_id: Arc<str>,
        feature: PerUserExperimentalFeature,
    ) -> Result<Option<bool>, anyhow::Error> {
        let is_feature_enabled_for_user = self
            .db_pool
            .run_interaction("is_feature_enabled_for_user", move |txn| {
                let user_id = user_id.clone();
                async move {
                    let rows = txn
                        .query(
                            r#"
                            SELECT enabled
                            FROM per_user_experimental_features
                            WHERE user_id = ? AND feature = ?
                            "#,
                            &[user_id.as_ref(), feature.name()],
                        )
                        .await?;

                    let enabled = match &rows[..] {
                        // No row for this user
                        [] => None,
                        // Otherwise, we should only find a single row for this (user, feature)
                        [row] => Some(row.try_get(0)?),
                        rows => {
                            anyhow::bail!(
                                "Unexpected number of rows returned (expected exactly 0 or 1, saw {}). \
                                This probably means the SQL query probably doesn't match our expectations.",
                                rows.len(),
                            );
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
