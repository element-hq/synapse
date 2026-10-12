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

//! The Python view of the Rust [`Store`].
//!
//! The main datastore builds a [`RustStore`] in its `__init__`, over its own
//! `db_pool`, and keeps it as `rust_store`. `hs.get_rust_store()` hands the
//! same store to the Rust handlers. So the datastore owns the store, as it
//! owns everything else that reads the database, and the handlers borrow it.
//!
//! A store function that Python calls as well as Rust is exposed as a
//! callable cache object, such as [`IsFeatureEnabledForUser`]. The datastore
//! binds it under the cache's name, so Python callers call it like the
//! `@cached` function it replaces, and Synapse's invalidation machinery finds
//! it by that name exactly as it would a `@cached` function. See
//! [`crate::caches::python`].

use std::sync::Arc;

use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    pyclass, pymethods,
    types::{PyAnyMethods, PyModule, PyModuleMethods},
    Bound, Py, PyAny, PyClassInitializer, PyResult, Python,
};

use crate::{
    caches::python::RustCache,
    deferred::{create_deferred, succeed},
    runtime::RustRuntime,
    storage::{
        db::python_db_pool::PythonDatabasePoolWrapper,
        store::{IsFeatureEnabledKey, PerUserExperimentalFeature, Store},
    },
};

/// The Rust [`Store`], as the main datastore holds it.
#[pyclass(frozen, name = "RustStore")]
pub struct RustStore {
    store: Arc<Store>,
    is_feature_enabled_for_user: Py<IsFeatureEnabledForUser>,
}

impl RustStore {
    pub fn store(&self) -> &Arc<Store> {
        &self.store
    }
}

#[pymethods]
impl RustStore {
    /// Build the store over `db_pool`, the datastore's `DatabasePool`, with
    /// its caches in the runtime's registry.
    #[new]
    fn py_new(py: Python<'_>, runtime: &RustRuntime, db_pool: &Bound<'_, PyAny>) -> PyResult<Self> {
        let db_pool = PythonDatabasePoolWrapper::new(db_pool, runtime.clone())?;
        let store = Arc::new(Store::new(Box::new(db_pool), runtime.caches()));

        let is_feature_enabled_for_user = Py::new(
            py,
            PyClassInitializer::from(RustCache::new(
                store.is_feature_enabled_for_user_cache.clone(),
            ))
            .add_subclass(IsFeatureEnabledForUser {
                store: Arc::clone(&store),
                runtime: runtime.clone(),
            }),
        )?;

        Ok(RustStore {
            store,
            is_feature_enabled_for_user,
        })
    }

    /// `Store::is_feature_enabled_for_user` as a callable cache, for the
    /// datastore to bind under that name.
    #[getter]
    fn is_feature_enabled_for_user(&self, py: Python<'_>) -> Py<IsFeatureEnabledForUser> {
        self.is_feature_enabled_for_user.clone_ref(py)
    }
}

/// `Store::is_feature_enabled_for_user`, callable from Python, and the cache
/// behind it.
///
/// Calling it returns a Twisted deferred of the answer. A hit is answered
/// synchronously, with an already-fired deferred, so a cached lookup costs
/// Python no more than its own caches do. Only a miss goes through tokio.
///
/// As a subclass of [`RustCache`], it is also the cache: `invalidate`,
/// `__name__` and the counters are all here, so `_invalidate_cache_and_stream`
/// and `register_cache` take it as they would a `@cached` function.
#[pyclass(frozen, extends = RustCache, name = "IsFeatureEnabledForUser")]
pub struct IsFeatureEnabledForUser {
    store: Arc<Store>,
    runtime: RustRuntime,
}

#[pymethods]
impl IsFeatureEnabledForUser {
    /// Whether `feature` is enabled for `user_id`. `False` when there is no
    /// row, as for the Python function this replaces.
    ///
    /// Raises `ValueError` for a feature Rust doesn't know. Python's
    /// `ExperimentalFeature` and [`PerUserExperimentalFeature`] list the same
    /// features, so that means one side has gained a feature without the
    /// other, which should fail loudly.
    fn __call__<'py>(
        &self,
        py: Python<'py>,
        user_id: String,
        feature: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let feature = PerUserExperimentalFeature::from_name(&feature).ok_or_else(|| {
            PyValueError::new_err(format!("unknown per-user experimental feature: {feature}"))
        })?;
        let key = IsFeatureEnabledKey::new(&user_id, feature);
        if let Some(enabled) = self.store.is_feature_enabled_for_user_cache.get(&key) {
            return succeed(py, enabled.unwrap_or(false));
        }

        let store = Arc::clone(&self.store);
        create_deferred(py, &self.runtime, async move {
            store
                .is_feature_enabled_for_user(key)
                .await
                .map(|enabled| enabled.unwrap_or(false))
                .map_err(|err| {
                    PyRuntimeError::new_err(format!("is_feature_enabled_for_user failed: {err:#}"))
                })
        })
    }
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "storage")?;

    child_module.add_class::<RustStore>()?;
    child_module.add_class::<IsFeatureEnabledForUser>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import storage` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.storage", child_module)?;

    Ok(())
}
