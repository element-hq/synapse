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

use std::sync::{Arc, LazyLock, RwLock};

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
    const ROOM_V1_V2: i32 = 1;
    /// MSC1659-style $hash event id format: used for room v3
    #[classattr]
    const ROOM_V3: i32 = 2;
    /// MSC1884-style $hash format: introduced for room v4
    #[classattr]
    const ROOM_V4_PLUS: i32 = 3;
    /// MSC4291 room IDs as hashes: introduced for room HydraV11
    #[classattr]
    const ROOM_V11_HYDRA_PLUS: i32 = 4;
}

/// Enum to identify the state resolution algorithms.
#[pyclass(frozen)]
pub struct StateResolutionVersions {}

#[pymethods]
impl StateResolutionVersions {
    /// Room v1 state res
    #[classattr]
    const V1: i32 = 1;
    /// MSC1442 state res: room v2 and later
    #[classattr]
    const V2: i32 = 2;
    /// MSC4297 state res
    #[classattr]
    const V2_1: i32 = 3;
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
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct RoomVersion {
    /// The identifier for this version.
    pub identifier: &'static str,
    /// One of the RoomDisposition constants.
    pub disposition: &'static str,
    /// One of the EventFormatVersions constants.
    pub event_format: i32,
    /// One of the StateResolutionVersions constants.
    pub state_res: i32,
    pub enforce_key_validity: bool,
    /// Before MSC2432, m.room.aliases had special auth rules and redaction rules.
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
}

const ROOM_VERSION_V1: RoomVersion = RoomVersion {
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
};

const ROOM_VERSION_V2: RoomVersion = RoomVersion {
    identifier: "2",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V1_V2,
    state_res: StateResolutionVersions::V2,
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
};

const ROOM_VERSION_V3: RoomVersion = RoomVersion {
    identifier: "3",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V3,
    state_res: StateResolutionVersions::V2,
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
};

const ROOM_VERSION_V4: RoomVersion = RoomVersion {
    identifier: "4",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
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
};

const ROOM_VERSION_V5: RoomVersion = RoomVersion {
    identifier: "5",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
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
};

const ROOM_VERSION_V6: RoomVersion = RoomVersion {
    identifier: "6",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
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
};

const ROOM_VERSION_V7: RoomVersion = RoomVersion {
    identifier: "7",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: false,
    updated_redaction_rules: false,
    restricted_join_rule: false,
    restricted_join_rule_fix: false,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: false,
    enforce_int_power_levels: false,
    msc3931_push_features: &[],
    msc3757_enabled: false,
    msc4289_creator_power_enabled: false,
    msc4291_room_ids_as_hashes: false,
    strict_event_byte_limits_room_versions: false,
};

const ROOM_VERSION_V8: RoomVersion = RoomVersion {
    identifier: "8",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: false,
    updated_redaction_rules: false,
    restricted_join_rule: true,
    restricted_join_rule_fix: false,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: false,
    enforce_int_power_levels: false,
    msc3931_push_features: &[],
    msc3757_enabled: false,
    msc4289_creator_power_enabled: false,
    msc4291_room_ids_as_hashes: false,
    strict_event_byte_limits_room_versions: false,
};

const ROOM_VERSION_V9: RoomVersion = RoomVersion {
    identifier: "9",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: false,
    updated_redaction_rules: false,
    restricted_join_rule: true,
    restricted_join_rule_fix: true,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: false,
    enforce_int_power_levels: false,
    msc3931_push_features: &[],
    msc3757_enabled: false,
    msc4289_creator_power_enabled: false,
    msc4291_room_ids_as_hashes: false,
    strict_event_byte_limits_room_versions: false,
};

const ROOM_VERSION_V10: RoomVersion = RoomVersion {
    identifier: "10",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: false,
    updated_redaction_rules: false,
    restricted_join_rule: true,
    restricted_join_rule_fix: true,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: true,
    enforce_int_power_levels: true,
    msc3931_push_features: &[],
    msc3757_enabled: false,
    msc4289_creator_power_enabled: false,
    msc4291_room_ids_as_hashes: false,
    strict_event_byte_limits_room_versions: false,
};

/// MSC3389 (Redaction changes for events with a relation) based on room version "10".
const ROOM_VERSION_MSC3389V10: RoomVersion = RoomVersion {
    identifier: "org.matrix.msc3389.10",
    disposition: RoomDisposition::UNSTABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: false,
    updated_redaction_rules: false,
    restricted_join_rule: true,
    restricted_join_rule_fix: true,
    knock_join_rule: true,
    msc3389_relation_redactions: true, // Changed from v10
    knock_restricted_join_rule: true,
    enforce_int_power_levels: true,
    msc3931_push_features: &[],
    msc3757_enabled: false,
    msc4289_creator_power_enabled: false,
    msc4291_room_ids_as_hashes: false,
    strict_event_byte_limits_room_versions: true,
};

/// MSC1767 (Extensible Events) based on room version "10".
const ROOM_VERSION_MSC1767V10: RoomVersion = RoomVersion {
    identifier: "org.matrix.msc1767.10",
    disposition: RoomDisposition::UNSTABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: false,
    updated_redaction_rules: false,
    restricted_join_rule: true,
    restricted_join_rule_fix: true,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: true,
    enforce_int_power_levels: true,
    msc3931_push_features: &[PushRuleRoomFlag::EXTENSIBLE_EVENTS],
    msc3757_enabled: false,
    msc4289_creator_power_enabled: false,
    msc4291_room_ids_as_hashes: false,
    strict_event_byte_limits_room_versions: false,
};

/// MSC3757 (Restricting who can overwrite a state event) based on room version "10".
const ROOM_VERSION_MSC3757V10: RoomVersion = RoomVersion {
    identifier: "org.matrix.msc3757.10",
    disposition: RoomDisposition::UNSTABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: false,
    updated_redaction_rules: false,
    restricted_join_rule: true,
    restricted_join_rule_fix: true,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: true,
    enforce_int_power_levels: true,
    msc3931_push_features: &[],
    msc3757_enabled: true,
    msc4289_creator_power_enabled: false,
    msc4291_room_ids_as_hashes: false,
    strict_event_byte_limits_room_versions: false,
};

const ROOM_VERSION_V11: RoomVersion = RoomVersion {
    identifier: "11",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: true,   // Used by MSC3820
    updated_redaction_rules: true, // Used by MSC3820
    restricted_join_rule: true,
    restricted_join_rule_fix: true,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: true,
    enforce_int_power_levels: true,
    msc3931_push_features: &[],
    msc3757_enabled: false,
    msc4289_creator_power_enabled: false,
    msc4291_room_ids_as_hashes: false,
    strict_event_byte_limits_room_versions: true, // Changed from v10
};

/// MSC3757 (Restricting who can overwrite a state event) based on room version "11".
const ROOM_VERSION_MSC3757V11: RoomVersion = RoomVersion {
    identifier: "org.matrix.msc3757.11",
    disposition: RoomDisposition::UNSTABLE,
    event_format: EventFormatVersions::ROOM_V4_PLUS,
    state_res: StateResolutionVersions::V2,
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: true,   // Used by MSC3820
    updated_redaction_rules: true, // Used by MSC3820
    restricted_join_rule: true,
    restricted_join_rule_fix: true,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: true,
    enforce_int_power_levels: true,
    msc3931_push_features: &[],
    msc3757_enabled: true,
    msc4289_creator_power_enabled: false,
    msc4291_room_ids_as_hashes: false,
    strict_event_byte_limits_room_versions: true,
};

const ROOM_VERSION_HYDRA_V11: RoomVersion = RoomVersion {
    identifier: "org.matrix.hydra.11",
    disposition: RoomDisposition::UNSTABLE,
    event_format: EventFormatVersions::ROOM_V11_HYDRA_PLUS,
    state_res: StateResolutionVersions::V2_1, // Changed from v11
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: true,   // Used by MSC3820
    updated_redaction_rules: true, // Used by MSC3820
    restricted_join_rule: true,
    restricted_join_rule_fix: true,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: true,
    enforce_int_power_levels: true,
    msc3931_push_features: &[],
    msc3757_enabled: false,
    msc4289_creator_power_enabled: true, // Changed from v11
    msc4291_room_ids_as_hashes: true,    // Changed from v11
    strict_event_byte_limits_room_versions: true,
};

const ROOM_VERSION_V12: RoomVersion = RoomVersion {
    identifier: "12",
    disposition: RoomDisposition::STABLE,
    event_format: EventFormatVersions::ROOM_V11_HYDRA_PLUS,
    state_res: StateResolutionVersions::V2_1, // Changed from v11
    enforce_key_validity: true,
    special_case_aliases_auth: false,
    strict_canonicaljson: true,
    limit_notifications_power_levels: true,
    implicit_room_creator: true,   // Used by MSC3820
    updated_redaction_rules: true, // Used by MSC3820
    restricted_join_rule: true,
    restricted_join_rule_fix: true,
    knock_join_rule: true,
    msc3389_relation_redactions: false,
    knock_restricted_join_rule: true,
    enforce_int_power_levels: true,
    msc3931_push_features: &[],
    msc3757_enabled: false,
    msc4289_creator_power_enabled: true, // Changed from v11
    msc4291_room_ids_as_hashes: true,    // Changed from v11
    strict_event_byte_limits_room_versions: true,
};

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

    fn __getitem__(&self, key: &str) -> PyResult<RoomVersion> {
        let versions = self.versions.read().unwrap();
        versions
            .iter()
            .find(|v| v.identifier == key)
            .copied()
            .ok_or_else(|| PyKeyError::new_err(key.to_string()))
    }

    fn __contains__(&self, key: &str) -> PyResult<bool> {
        let versions = self.versions.read().unwrap();
        Ok(versions.iter().any(|v| v.identifier == key))
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
        Ok(versions.iter().map(|v| (v.identifier, *v)).collect())
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
        if let Some(version) = versions.iter().find(|v| v.identifier == key).copied() {
            return Ok(Some(version.into_bound_py_any(py)?));
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
        ROOM_VERSION_V1,
        ROOM_VERSION_V2,
        ROOM_VERSION_V3,
        ROOM_VERSION_V4,
        ROOM_VERSION_V5,
        ROOM_VERSION_V6,
        ROOM_VERSION_V7,
        ROOM_VERSION_V8,
        ROOM_VERSION_V9,
        ROOM_VERSION_V10,
        ROOM_VERSION_V11,
        ROOM_VERSION_V12,
        ROOM_VERSION_MSC3757V10,
        ROOM_VERSION_MSC3757V11,
        ROOM_VERSION_HYDRA_V11,
    ];

    KnownRoomVersionsMapping {
        versions: Arc::new(RwLock::new(vec)),
    }
});

/// Container class for room version constants.
///
/// This should contain all room versions that we know about.
#[pyclass(frozen)]
pub struct RoomVersions {}

#[pymethods]
#[allow(non_snake_case)]
impl RoomVersions {
    #[classattr]
    fn V1(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V1.into_py_any(py)
    }
    #[classattr]
    fn V2(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V2.into_py_any(py)
    }
    #[classattr]
    fn V3(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V3.into_py_any(py)
    }
    #[classattr]
    fn V4(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V4.into_py_any(py)
    }
    #[classattr]
    fn V5(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V5.into_py_any(py)
    }
    #[classattr]
    fn V6(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V6.into_py_any(py)
    }
    #[classattr]
    fn V7(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V7.into_py_any(py)
    }
    #[classattr]
    fn V8(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V8.into_py_any(py)
    }
    #[classattr]
    fn V9(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V9.into_py_any(py)
    }
    #[classattr]
    fn V10(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V10.into_py_any(py)
    }
    #[classattr]
    fn MSC1767v10(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_MSC1767V10.into_py_any(py)
    }
    #[classattr]
    fn MSC3389v10(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_MSC3389V10.into_py_any(py)
    }
    #[classattr]
    fn MSC3757v10(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_MSC3757V10.into_py_any(py)
    }
    #[classattr]
    fn V11(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V11.into_py_any(py)
    }
    #[classattr]
    fn MSC3757v11(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_MSC3757V11.into_py_any(py)
    }
    #[classattr]
    fn HydraV11(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_HYDRA_V11.into_py_any(py)
    }
    #[classattr]
    fn V12(py: Python<'_>) -> PyResult<Py<PyAny>> {
        ROOM_VERSION_V12.into_py_any(py)
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
    let known_ef: [i32; 4] = [
        EventFormatVersions::ROOM_V1_V2,
        EventFormatVersions::ROOM_V3,
        EventFormatVersions::ROOM_V4_PLUS,
        EventFormatVersions::ROOM_V11_HYDRA_PLUS,
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
