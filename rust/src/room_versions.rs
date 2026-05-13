/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 */

//! Rust implementation of room version definitions.

use std::{
    fmt::Display,
    str::FromStr,
    sync::{Arc, LazyLock, RwLock},
};

use pyo3::{
    exceptions::{PyKeyError, PyRuntimeError},
    prelude::*,
    types::{PyFrozenSet, PyIterator, PyModule, PyModuleMethods},
    Bound, IntoPyObjectExt, PyResult, Python,
};

/// Internal enum for tracking the version of the event format,
/// independently of the room version.
///
/// To reduce confusion, the event format versions are named after the room
/// versions that they were used or introduced in.
/// The concept of an 'event format version' is specific to Synapse (the
/// specification does not mention this term.)
#[pyclass(frozen)]
pub struct EventFormatVersions {}

#[pymethods]
impl EventFormatVersions {
    /// $id:server event id format: used for room v1 and v2
    #[classattr]
    pub const ROOM_V1_V2: i32 = 1;
    /// MSC1659-style $hash event id format: used for room v3
    #[classattr]
    pub const ROOM_V3: i32 = 2;
    /// MSC1884-style $hash format: introduced for room v4
    #[classattr]
    pub const ROOM_V4_PLUS: i32 = 3;
    /// MSC4291 room IDs as hashes: introduced for room HydraV11
    #[classattr]
    pub const ROOM_V11_HYDRA_PLUS: i32 = 4;
    /// MSC4242 state DAGs: adds prev_state_events, removes auth_events
    #[classattr]
    pub const ROOM_VMSC4242: i32 = 5;
}

/// Enum to identify the state resolution algorithms.
#[pyclass(frozen)]
pub struct StateResolutionVersions {}

#[pymethods]
impl StateResolutionVersions {
    /// Room v1 state res
    #[classattr]
    pub const V1: i32 = 1;
    /// MSC1442 state res: room v2 and later
    #[classattr]
    pub const V2: i32 = 2;
    /// MSC4297 state res
    #[classattr]
    pub const V2_1: i32 = 3;
}

/// Room disposition constants.
#[pyclass(frozen)]
pub struct RoomDisposition {}

#[pymethods]
impl RoomDisposition {
    #[classattr]
    const STABLE: &'static str = "stable";
    #[classattr]
    const UNSTABLE: &'static str = "unstable";
}

/// Enum for listing possible MSC3931 room version feature flags, for push rules.
#[pyclass(frozen)]
pub struct PushRuleRoomFlag {}

#[pymethods]
impl PushRuleRoomFlag {
    /// MSC3932: Room version supports MSC1767 Extensible Events.
    #[classattr]
    const EXTENSIBLE_EVENTS: &'static str = "org.matrix.msc3932.extensible_events";
}

/// An object which describes the unique attributes of a room version.
#[pyclass(frozen, eq, hash, get_all)]
#[derive(Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct RoomVersion {
    /// The identifier for this version.
    pub identifier: &'static str,
    /// One of the [`RoomDisposition`] constants.
    pub disposition: &'static str,
    /// One of the [`EventFormatVersions`] constants.
    pub event_format: i32,
    /// One of the [`StateResolutionVersions`] constants.
    pub state_res: i32,
    pub enforce_key_validity: bool,
    /// Before MSC2432, `m.room.aliases` had special auth rules and redaction rules.
    pub special_case_aliases_auth: bool,
    /// Strictly enforce canonicaljson, do not allow:
    /// * Integers outside the range of [-2^53 + 1, 2^53 - 1]
    /// * Floats
    /// * NaN, Infinity, -Infinity
    pub strict_canonicaljson: bool,
    /// MSC2209: Check 'notifications' key while verifying
    /// m.room.power_levels auth rules.
    pub limit_notifications_power_levels: bool,
    /// MSC3820: No longer include the creator in m.room.create events (room version 11).
    pub implicit_room_creator: bool,
    /// MSC3820: Apply updated redaction rules algorithm from room version 11.
    pub updated_redaction_rules: bool,
    /// Support the 'restricted' join rule.
    pub restricted_join_rule: bool,
    /// Support for the proper redaction rules for the restricted join rule.
    /// This requires restricted_join_rule to be enabled.
    pub restricted_join_rule_fix: bool,
    /// Support the 'knock' join rule.
    pub knock_join_rule: bool,
    /// MSC3389: Protect relation information from redaction.
    pub msc3389_relation_redactions: bool,
    /// Support the 'knock_restricted' join rule.
    pub knock_restricted_join_rule: bool,
    /// Enforce integer power levels.
    pub enforce_int_power_levels: bool,
    /// MSC3931: Adds a push rule condition for "room version feature flags", making
    /// some push rules room version dependent. Note that adding a flag to this list
    /// is not enough to mark it "supported": the push rule evaluator also needs to
    /// support the flag. Unknown flags are ignored by the evaluator, making conditions
    /// fail if used. Values from PushRuleRoomFlag.
    pub msc3931_push_features: &'static [&'static str],
    /// MSC3757: Restricting who can overwrite a state event.
    pub msc3757_enabled: bool,
    /// MSC4289: Creator power enabled.
    pub msc4289_creator_power_enabled: bool,
    /// MSC4291: Room IDs as hashes of the create event.
    pub msc4291_room_ids_as_hashes: bool,
    /// Set of room versions where Synapse strictly enforces event key size limits
    /// in bytes, rather than in codepoints.
    ///
    /// In these room versions, we are stricter with event size validation.
    pub strict_event_byte_limits_room_versions: bool,
    /// MSC4242: State DAGs. Creates events with prev_state_events instead of auth_events and derives
    /// state from it. Events are always processed in causal order without any gaps in the DAG
    /// (prev_state_events are always known), guaranteeing that processed events have a path to the
    /// create event. This is an emergent property of state DAGs as asserting that there is a path
    /// to the create event every time we insert an event would be prohibitively expensive.
    /// This is similar to how doubly-linked lists can potentially not refer to previous items correctly
    /// without verifying the list's integrity, but doing it on every insert is too expensive.
    pub msc4242_state_dags: bool,
}

impl RoomVersion {
    pub const V1: RoomVersion = RoomVersion {
        identifier: "1",
        disposition: RoomDisposition::STABLE,
        event_format: EventFormatVersions::ROOM_V1_V2,
        state_res: StateResolutionVersions::V1,
        enforce_key_validity: false,
        special_case_aliases_auth: true,
        strict_canonicaljson: false,
        limit_notifications_power_levels: false,
        implicit_room_creator: false,
        updated_redaction_rules: false,
        restricted_join_rule: false,
        restricted_join_rule_fix: false,
        knock_join_rule: false,
        msc3389_relation_redactions: false,
        knock_restricted_join_rule: false,
        enforce_int_power_levels: false,
        msc3931_push_features: &[],
        msc3757_enabled: false,
        msc4289_creator_power_enabled: false,
        msc4291_room_ids_as_hashes: false,
        strict_event_byte_limits_room_versions: false,
        msc4242_state_dags: false,
    };

    pub const V2: RoomVersion = RoomVersion {
        identifier: "2",
        state_res: StateResolutionVersions::V2,
        ..Self::V1
    };

    pub const V3: RoomVersion = RoomVersion {
        identifier: "3",
        event_format: EventFormatVersions::ROOM_V3,
        ..Self::V2
    };

    pub const V4: RoomVersion = RoomVersion {
        identifier: "4",
        event_format: EventFormatVersions::ROOM_V4_PLUS,
        ..Self::V3
    };

    pub const V5: RoomVersion = RoomVersion {
        identifier: "5",
        enforce_key_validity: true,
        ..Self::V4
    };

    pub const V6: RoomVersion = RoomVersion {
        identifier: "6",
        special_case_aliases_auth: false,
        strict_canonicaljson: true,
        limit_notifications_power_levels: true,
        ..Self::V5
    };

    pub const V7: RoomVersion = RoomVersion {
        identifier: "7",
        knock_join_rule: true,
        ..Self::V6
    };

    pub const V8: RoomVersion = RoomVersion {
        identifier: "8",
        restricted_join_rule: true,
        ..Self::V7
    };

    pub const V9: RoomVersion = RoomVersion {
        identifier: "9",
        restricted_join_rule_fix: true,
        ..Self::V8
    };

    pub const V10: RoomVersion = RoomVersion {
        identifier: "10",
        knock_restricted_join_rule: true,
        enforce_int_power_levels: true,
        ..Self::V9
    };

    /// MSC3389 (Redaction changes for events with a relation) based on room version "10".
    pub const MSC3389V10: RoomVersion = RoomVersion {
        identifier: "org.matrix.msc3389.10",
        disposition: RoomDisposition::UNSTABLE,
        msc3389_relation_redactions: true,
        strict_event_byte_limits_room_versions: true,
        ..Self::V10
    };

    /// MSC1767 (Extensible Events) based on room version "10".
    pub const MSC1767V10: RoomVersion = RoomVersion {
        identifier: "org.matrix.msc1767.10",
        disposition: RoomDisposition::UNSTABLE,
        msc3931_push_features: &[PushRuleRoomFlag::EXTENSIBLE_EVENTS],
        ..Self::V10
    };

    /// MSC3757 (Restricting who can overwrite a state event) based on room version "10".
    pub const MSC3757V10: RoomVersion = RoomVersion {
        identifier: "org.matrix.msc3757.10",
        disposition: RoomDisposition::UNSTABLE,
        msc3757_enabled: true,
        ..Self::V10
    };

    pub const V11: RoomVersion = RoomVersion {
        identifier: "11",
        implicit_room_creator: true,   // Used by MSC3820
        updated_redaction_rules: true, // Used by MSC3820
        strict_event_byte_limits_room_versions: true,
        ..Self::V10
    };

    /// MSC3757 (Restricting who can overwrite a state event) based on room version "11".
    pub const MSC3757V11: RoomVersion = RoomVersion {
        identifier: "org.matrix.msc3757.11",
        disposition: RoomDisposition::UNSTABLE,
        msc3757_enabled: true,
        ..Self::V11
    };

    pub const HYDRA_V11: RoomVersion = RoomVersion {
        identifier: "org.matrix.hydra.11",
        disposition: RoomDisposition::UNSTABLE,
        event_format: EventFormatVersions::ROOM_V11_HYDRA_PLUS,
        state_res: StateResolutionVersions::V2_1,
        msc4289_creator_power_enabled: true,
        msc4291_room_ids_as_hashes: true,
        ..Self::V11
    };

    pub const V12: RoomVersion = RoomVersion {
        identifier: "12",
        disposition: RoomDisposition::STABLE,
        event_format: EventFormatVersions::ROOM_V11_HYDRA_PLUS,
        state_res: StateResolutionVersions::V2_1,
        msc4289_creator_power_enabled: true,
        msc4291_room_ids_as_hashes: true,
        ..Self::V11
    };

    pub const MSC4242V12: RoomVersion = RoomVersion {
        identifier: "org.matrix.msc4242.12",
        disposition: RoomDisposition::UNSTABLE,
        event_format: EventFormatVersions::ROOM_VMSC4242,
        msc4242_state_dags: true,
        ..Self::V12
    };
}

impl Display for RoomVersion {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.identifier.fmt(f)
    }
}

impl FromStr for &'static RoomVersion {
    type Err = anyhow::Error;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "1" => Ok(&RoomVersion::V1),
            "2" => Ok(&RoomVersion::V2),
            "3" => Ok(&RoomVersion::V3),
            "4" => Ok(&RoomVersion::V4),
            "5" => Ok(&RoomVersion::V5),
            "6" => Ok(&RoomVersion::V6),
            "7" => Ok(&RoomVersion::V7),
            "8" => Ok(&RoomVersion::V8),
            "9" => Ok(&RoomVersion::V9),
            "10" => Ok(&RoomVersion::V10),
            "11" => Ok(&RoomVersion::V11),
            "12" => Ok(&RoomVersion::V12),
            "org.matrix.msc1767.10" => Ok(&RoomVersion::MSC1767V10),
            "org.matrix.msc3389.10" => Ok(&RoomVersion::MSC3389V10),
            "org.matrix.msc3757.10" => Ok(&RoomVersion::MSC3757V10),
            "org.matrix.msc3757.11" => Ok(&RoomVersion::MSC3757V11),
            "org.matrix.hydra.11" => Ok(&RoomVersion::HYDRA_V11),
            "org.matrix.msc4242.12" => Ok(&RoomVersion::MSC4242V12),
            _ => Err(anyhow::anyhow!("Unknown room version: {}", s)),
        }
    }
}

impl<'py> IntoPyObject<'py> for &RoomVersion {
    type Target = RoomVersion;

    type Output = Bound<'py, RoomVersion>;

    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        self.clone().into_pyobject(py)
    }
}

/// Helper class for managing the known room versions, and providing dict-like
/// access to them for Python.
///
/// Note: this is not necessarily all room versions, as we may not want to
/// support all experimental room versions.
///
/// Note: room versions can be added to this mapping at startup (allowing
/// support for experimental room versions to be behind experimental feature
/// flags).
#[pyclass(frozen, mapping)]
#[derive(Clone)]
pub struct KnownRoomVersionsMapping {
    // Note we use a Vec here to ensure that the order of keys is
    // deterministic. We rely on this when generating parameterized tests in
    // Python, as Python will generate the tests names including the room
    // version and index (and we need test names to be stable to e.g. support
    // running tests in parallel).
    pub versions: Arc<RwLock<Vec<RoomVersion>>>,
}

#[pymethods]
impl KnownRoomVersionsMapping {
    /// Add a new room version to the mapping, indicating that this instance
    /// supports it.
    fn add_room_version(&self, version: RoomVersion) -> PyResult<()> {
        let mut versions = self
            .versions
            .write()
            .map_err(|_| PyRuntimeError::new_err("KnownRoomVersionsMapping lock poisoned"))?;

        if versions.iter().any(|v| v.identifier == version.identifier) {
            // We already have this room version, so we don't add it again (as
            // otherwise we'd end up with duplicates).
            return Ok(());
        }

        versions.push(version);
        Ok(())
    }

    fn __getitem__(&self, key: &Bound<'_, PyAny>) -> PyResult<RoomVersion> {
        // We need to accept anything as the key, but we know that only strings
        // are valid keys, so if it's not a string we just raise a KeyError.
        let Ok(key) = key.extract::<&str>() else {
            return Err(PyKeyError::new_err(key.to_string()));
        };
        let versions = self.versions.read().unwrap();
        versions
            .iter()
            .find(|v| v.identifier == key)
            .cloned()
            .ok_or_else(|| PyKeyError::new_err(key.to_string()))
    }

    fn __contains__(&self, key: &Bound<'_, PyAny>) -> bool {
        // We need to accept anything as the key, but we know that only strings
        // are valid keys, so if it's not a string we just return false.
        let Ok(key) = key.extract::<&str>() else {
            return false;
        };
        let versions = self.versions.read().unwrap();
        versions.iter().any(|v| v.identifier == key)
    }

    fn keys(&self) -> PyResult<Vec<&'static str>> {
        // Note that technically we should return a view here (that also acts
        // like a Set *and* has a stable ordering). We don't depend on this, so
        // for simplicity we just return a list of the keys.
        let versions = self.versions.read().unwrap();
        Ok(versions.iter().map(|v| v.identifier).collect())
    }

    fn values(&self) -> PyResult<Vec<RoomVersion>> {
        let versions = self.versions.read().unwrap();
        Ok(versions.clone())
    }

    fn items(&self) -> PyResult<Vec<(&'static str, RoomVersion)>> {
        // Note that technically we should return a view here (that also acts
        // like a Set *and* has a stable ordering). We don't depend on this, so
        // for simplicity we just return a list of the items.
        let versions = self.versions.read().unwrap();
        Ok(versions.iter().map(|v| (v.identifier, v.clone())).collect())
    }

    #[pyo3(signature = (key, default=None))]
    fn get<'py>(
        &self,
        py: Python<'py>,
        key: Bound<'py, PyAny>,
        default: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        // We need to accept anything as the key, but we know that only strings
        // are valid keys, so if it's not a string we just return the default.
        let Ok(key) = key.extract::<&str>() else {
            return Ok(default);
        };

        let versions = self.versions.read().unwrap();
        if let Some(version) = versions.iter().find(|v| v.identifier == key) {
            return Ok(Some(version.clone().into_bound_py_any(py)?));
        }

        Ok(default)
    }

    fn __len__(&self) -> PyResult<usize> {
        let versions = self
            .versions
            .read()
            .map_err(|_| PyRuntimeError::new_err("KnownRoomVersionsMapping lock poisoned"))?;
        Ok(versions.len())
    }

    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        let key_list = self.keys()?;

        let bound_key_list = key_list.into_bound_py_any(py)?;
        PyIterator::from_object(&bound_key_list)
    }
}

impl<'py> IntoPyObject<'py> for &KnownRoomVersionsMapping {
    type Target = KnownRoomVersionsMapping;

    type Output = Bound<'py, Self::Target>;

    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        self.clone().into_pyobject(py)
    }
}

/// All room versions this instance knows about, used to build the
/// `KNOWN_ROOM_VERSIONS` dict.
///
/// Note: this is not necessarily all room versions, as we may not want to
/// support all experimental room versions.
static KNOWN_ROOM_VERSIONS: LazyLock<KnownRoomVersionsMapping> = LazyLock::new(|| {
    let vec = vec![
        RoomVersion::V1,
        RoomVersion::V2,
        RoomVersion::V3,
        RoomVersion::V4,
        RoomVersion::V5,
        RoomVersion::V6,
        RoomVersion::V7,
        RoomVersion::V8,
        RoomVersion::V9,
        RoomVersion::V10,
        RoomVersion::V11,
        RoomVersion::V12,
        RoomVersion::MSC3757V10,
        RoomVersion::MSC3757V11,
        RoomVersion::HYDRA_V11,
    ];

    KnownRoomVersionsMapping {
        versions: Arc::new(RwLock::new(vec)),
    }
});

/// Container class for room version constants.
///
/// This should contain all room versions that we know about.
#[pyclass(frozen)]
struct RoomVersions {}

#[pymethods]
#[allow(non_snake_case)]
impl RoomVersions {
    #[classattr]
    fn V1(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V1.into_py_any(py)
    }
    #[classattr]
    fn V2(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V2.into_py_any(py)
    }
    #[classattr]
    fn V3(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V3.into_py_any(py)
    }
    #[classattr]
    fn V4(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V4.into_py_any(py)
    }
    #[classattr]
    fn V5(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V5.into_py_any(py)
    }
    #[classattr]
    fn V6(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V6.into_py_any(py)
    }
    #[classattr]
    fn V7(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V7.into_py_any(py)
    }
    #[classattr]
    fn V8(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V8.into_py_any(py)
    }
    #[classattr]
    fn V9(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V9.into_py_any(py)
    }
    #[classattr]
    fn V10(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V10.into_py_any(py)
    }
    #[classattr]
    fn MSC1767v10(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::MSC1767V10.into_py_any(py)
    }
    #[classattr]
    fn MSC3389v10(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::MSC3389V10.into_py_any(py)
    }
    #[classattr]
    fn MSC3757v10(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::MSC3757V10.into_py_any(py)
    }
    #[classattr]
    fn V11(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V11.into_py_any(py)
    }
    #[classattr]
    fn MSC3757v11(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::MSC3757V11.into_py_any(py)
    }
    #[classattr]
    fn HydraV11(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::HYDRA_V11.into_py_any(py)
    }
    #[classattr]
    fn V12(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::V12.into_py_any(py)
    }
    #[classattr]
    fn MSC4242v12(py: Python<'_>) -> PyResult<Py<PyAny>> {
        RoomVersion::MSC4242V12.into_py_any(py)
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "room_versions")?;
    child_module.add_class::<EventFormatVersions>()?;
    child_module.add_class::<StateResolutionVersions>()?;
    child_module.add_class::<PushRuleRoomFlag>()?;
    child_module.add_class::<RoomVersion>()?;
    child_module.add_class::<RoomVersions>()?;
    child_module.add_class::<KnownRoomVersionsMapping>()?;
    child_module.add_class::<RoomDisposition>()?;

    // Build KNOWN_EVENT_FORMAT_VERSIONS as a frozenset
    let known_ef: [i32; 5] = [
        EventFormatVersions::ROOM_V1_V2,
        EventFormatVersions::ROOM_V3,
        EventFormatVersions::ROOM_V4_PLUS,
        EventFormatVersions::ROOM_V11_HYDRA_PLUS,
        EventFormatVersions::ROOM_VMSC4242,
    ];
    let known_event_format_versions = PyFrozenSet::new(py, known_ef)?;
    child_module.add("KNOWN_EVENT_FORMAT_VERSIONS", known_event_format_versions)?;
    child_module.add("KNOWN_ROOM_VERSIONS", &*KNOWN_ROOM_VERSIONS)?;

    m.add_submodule(&child_module)?;

    // Register in sys.modules
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.room_versions", child_module)?;

    Ok(())
}
