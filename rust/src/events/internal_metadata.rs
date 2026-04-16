/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2024 New Vector, Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 * Originally licensed under the Apache License, Version 2.0:
 * <http://www.apache.org/licenses/LICENSE-2.0>.
 *
 * [This file includes modifications made by New Vector Limited]
 *
 */

//! Implements the internal metadata class attached to events.
//!
//! The internal metadata is a bit like a `TypedDict`, in that most of
//! it is stored as a JSON dict in the DB (the exceptions being `outlier`
//! and `stream_ordering` which have their own columns in the database).
//! Most events have zero, or only a few, of these keys
//! set. Therefore, since we care more about memory size than performance here,
//! we store these fields in a mapping.
//!
//! We want to store (most) of the fields as Rust objects, so we implement the
//! mapping by using a vec of enums. This is less efficient than using
//! attributes, but for small number of keys is actually faster than using a
//! hash or btree map.

use std::{
    num::NonZeroI64,
    ops::Deref,
    sync::{Arc, RwLock, RwLockReadGuard, RwLockWriteGuard},
};

use anyhow::Context;
use log::warn;
use pyo3::{
    exceptions::{PyAttributeError, PyRuntimeError},
    pybacked::PyBackedStr,
    pyclass, pymethods,
    types::{PyAnyMethods, PyDict, PyDictMethods, PyString},
    Bound, IntoPyObject, Py, PyAny, PyResult, Python,
};

use crate::UnwrapInfallible;

/// Definitions of the various fields of the internal metadata.
#[derive(Clone)]
enum EventInternalMetadataData {
    OutOfBandMembership(bool),
    SendOnBehalfOf(Box<str>),
    RecheckRedaction(bool),
    SoftFailed(bool),
    ProactivelySend(bool),
    PolicyServerSpammy(bool),
    SpamCheckerSpammy(bool),
    Redacted(bool),
    TxnId(Box<str>),
    DelayId(Box<str>),
    TokenId(i64),
    DeviceId(Box<str>),
}

impl EventInternalMetadataData {
    /// Convert the field to its name and python object.
    fn to_python_pair<'a>(&self, py: Python<'a>) -> (&'a Bound<'a, PyString>, Bound<'a, PyAny>) {
        match self {
            EventInternalMetadataData::OutOfBandMembership(o) => (
                pyo3::intern!(py, "out_of_band_membership"),
                o.into_pyobject(py)
                    .unwrap_infallible()
                    .to_owned()
                    .into_any(),
            ),
            EventInternalMetadataData::SendOnBehalfOf(o) => (
                pyo3::intern!(py, "send_on_behalf_of"),
                o.into_pyobject(py).unwrap_infallible().into_any(),
            ),
            EventInternalMetadataData::RecheckRedaction(o) => (
                pyo3::intern!(py, "recheck_redaction"),
                o.into_pyobject(py)
                    .unwrap_infallible()
                    .to_owned()
                    .into_any(),
            ),
            EventInternalMetadataData::SoftFailed(o) => (
                pyo3::intern!(py, "soft_failed"),
                o.into_pyobject(py)
                    .unwrap_infallible()
                    .to_owned()
                    .into_any(),
            ),
            EventInternalMetadataData::ProactivelySend(o) => (
                pyo3::intern!(py, "proactively_send"),
                o.into_pyobject(py)
                    .unwrap_infallible()
                    .to_owned()
                    .into_any(),
            ),
            EventInternalMetadataData::PolicyServerSpammy(o) => (
                pyo3::intern!(py, "policy_server_spammy"),
                o.into_pyobject(py)
                    .unwrap_infallible()
                    .to_owned()
                    .into_any(),
            ),
            EventInternalMetadataData::SpamCheckerSpammy(o) => (
                pyo3::intern!(py, "spam_checker_spammy"),
                o.into_pyobject(py)
                    .unwrap_infallible()
                    .to_owned()
                    .into_any(),
            ),
            EventInternalMetadataData::Redacted(o) => (
                pyo3::intern!(py, "redacted"),
                o.into_pyobject(py)
                    .unwrap_infallible()
                    .to_owned()
                    .into_any(),
            ),
            EventInternalMetadataData::TxnId(o) => (
                pyo3::intern!(py, "txn_id"),
                o.into_pyobject(py).unwrap_infallible().into_any(),
            ),
            EventInternalMetadataData::DelayId(o) => (
                pyo3::intern!(py, "delay_id"),
                o.into_pyobject(py).unwrap_infallible().into_any(),
            ),
            EventInternalMetadataData::TokenId(o) => (
                pyo3::intern!(py, "token_id"),
                o.into_pyobject(py).unwrap_infallible().into_any(),
            ),
            EventInternalMetadataData::DeviceId(o) => (
                pyo3::intern!(py, "device_id"),
                o.into_pyobject(py).unwrap_infallible().into_any(),
            ),
        }
    }

    /// Converts from python key/values to the field.
    ///
    /// Returns `None` if the key is a valid but unrecognized string.
    fn from_python_pair(
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Self>> {
        let key_str: PyBackedStr = key.extract()?;

        let e = match &*key_str {
            "out_of_band_membership" => EventInternalMetadataData::OutOfBandMembership(
                value
                    .extract()
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),

            "send_on_behalf_of" => EventInternalMetadataData::SendOnBehalfOf(
                value
                    .extract()
                    .map(String::into_boxed_str)
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "recheck_redaction" => EventInternalMetadataData::RecheckRedaction(
                value
                    .extract()
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "soft_failed" => EventInternalMetadataData::SoftFailed(
                value
                    .extract()
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "proactively_send" => EventInternalMetadataData::ProactivelySend(
                value
                    .extract()
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "policy_server_spammy" => EventInternalMetadataData::PolicyServerSpammy(
                value
                    .extract()
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "spam_checker_spammy" => EventInternalMetadataData::SpamCheckerSpammy(
                value
                    .extract()
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "redacted" => EventInternalMetadataData::Redacted(
                value
                    .extract()
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "txn_id" => EventInternalMetadataData::TxnId(
                value
                    .extract()
                    .map(String::into_boxed_str)
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "delay_id" => EventInternalMetadataData::DelayId(
                value
                    .extract()
                    .map(String::into_boxed_str)
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "token_id" => EventInternalMetadataData::TokenId(
                value
                    .extract()
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            "device_id" => EventInternalMetadataData::DeviceId(
                value
                    .extract()
                    .map(String::into_boxed_str)
                    .with_context(|| format!("'{key_str}' has invalid type"))?,
            ),
            _ => return Ok(None),
        };

        Ok(Some(e))
    }
}

/// Helper macro to find the given field in internal metadata, returning None if
/// not found.
macro_rules! get_property_opt {
    ($self:expr, $name:ident) => {
        $self.data.iter().find_map(|entry| {
            if let EventInternalMetadataData::$name(data) = entry {
                Some(data)
            } else {
                None
            }
        })
    };
}

/// Helper macro to set the give field.
macro_rules! set_property {
    ($self:expr, $name:ident, $obj:expr) => {
        for entry in &mut $self.data {
            if let EventInternalMetadataData::$name(data) = entry {
                *data = $obj;
                return;
            }
        }

        $self.data.push(EventInternalMetadataData::$name($obj))
    };
}

/// The inner data for `EventInternalMetadata`, containing all fields and
/// business logic with pure Rust types.
#[derive(Clone)]
struct EventInternalMetadataInner {
    /// The fields of internal metadata. This functions as a mapping.
    data: Vec<EventInternalMetadataData>,

    /// The stream ordering of this event. None, until it has been persisted.
    pub stream_ordering: Option<NonZeroI64>,
    pub instance_name: Option<String>,

    /// The event ID of the redaction event, if this event has been redacted.
    /// This is set dynamically at load time and is not persisted to the database.
    pub redacted_by: Option<String>,

    /// whether this event is an outlier (ie, whether we have the state at that
    /// point in the DAG)
    pub outlier: bool,
}

impl EventInternalMetadataInner {
    pub fn is_outlier(&self) -> bool {
        self.outlier
    }

    /// Whether this event is an out-of-band membership.
    ///
    /// OOB memberships are a special case of outlier events: they are
    /// membership events for federated rooms that we aren't full members of.
    /// Examples include invites received over federation, and rejections for
    /// such invites.
    ///
    /// The concept of an OOB membership is needed because these events need to
    /// be processed as if they're new regular events (e.g. updating membership
    /// state in the database, relaying to clients via /sync, etc) despite being
    /// outliers.
    ///
    /// See also
    /// https://element-hq.github.io/synapse/develop/development/room-dag-concepts.html#out-of-band-membership-events.
    ///
    /// (Added in synapse 0.99.0, so may be unreliable for events received
    /// before that)
    pub fn is_out_of_band_membership(&self) -> bool {
        get_property_opt!(self, OutOfBandMembership)
            .copied()
            .unwrap_or(false)
    }

    /// Whether this server should send the event on behalf of another server.
    /// This is used by the federation "send_join" API to forward the initial
    /// join event for a server in the room.
    ///
    /// returns a str with the name of the server this event is sent on behalf
    /// of.
    pub fn get_send_on_behalf_of(&self) -> Option<&str> {
        get_property_opt!(self, SendOnBehalfOf).map(|a| a.deref())
    }

    /// Whether the redaction event needs to be rechecked when fetching
    /// from the database.
    ///
    /// Starting in room v3 redaction events are accepted up front, and later
    /// checked to see if the redacter and redactee's domains match.
    ///
    /// If the sender of the redaction event is allowed to redact any event
    /// due to auth rules, then this will always return false.
    pub fn need_to_check_redaction(&self) -> bool {
        get_property_opt!(self, RecheckRedaction)
            .copied()
            .unwrap_or(false)
    }

    /// Whether the event has been soft failed.
    ///
    /// Soft failed events should be handled as usual, except:
    /// 1. They should not go down sync or event streams, or generally sent to
    ///    clients.
    /// 2. They should not be added to the forward extremities (and therefore
    ///    not to current state).
    pub fn is_soft_failed(&self) -> bool {
        get_property_opt!(self, SoftFailed)
            .copied()
            .unwrap_or(false)
    }

    /// Whether the event, if ours, should be sent to other clients and servers.
    ///
    /// This is used for sending dummy events internally. Servers and clients
    /// can still explicitly fetch the event.
    pub fn should_proactively_send(&self) -> bool {
        get_property_opt!(self, ProactivelySend)
            .copied()
            .unwrap_or(true)
    }

    /// Whether the event has been redacted.
    ///
    /// This is used for efficiently checking whether an event has been marked
    /// as redacted without needing to make another database call.
    pub fn is_redacted(&self) -> bool {
        get_property_opt!(self, Redacted).copied().unwrap_or(false)
    }

    /// Whether this event can trigger a push notification
    pub fn is_notifiable(&self) -> bool {
        !self.outlier || self.is_out_of_band_membership()
    }

    pub fn get_out_of_band_membership(&self) -> Option<bool> {
        get_property_opt!(self, OutOfBandMembership).copied()
    }

    pub fn get_recheck_redaction(&self) -> Option<bool> {
        get_property_opt!(self, RecheckRedaction).copied()
    }

    pub fn get_soft_failed(&self) -> Option<bool> {
        get_property_opt!(self, SoftFailed).copied()
    }

    pub fn get_proactively_send(&self) -> Option<bool> {
        get_property_opt!(self, ProactivelySend).copied()
    }

    pub fn get_policy_server_spammy(&self) -> bool {
        get_property_opt!(self, PolicyServerSpammy)
            .copied()
            .unwrap_or(false)
    }

    pub fn get_redacted(&self) -> Option<bool> {
        get_property_opt!(self, Redacted).copied()
    }

    pub fn get_txn_id(&self) -> Option<&str> {
        get_property_opt!(self, TxnId).map(|s| s.deref())
    }

    pub fn get_delay_id(&self) -> Option<&str> {
        get_property_opt!(self, DelayId).map(|s| s.deref())
    }

    pub fn get_token_id(&self) -> Option<i64> {
        get_property_opt!(self, TokenId).copied()
    }

    pub fn get_device_id(&self) -> Option<&str> {
        get_property_opt!(self, DeviceId).map(|s| s.deref())
    }

    pub fn set_out_of_band_membership(&mut self, obj: bool) {
        set_property!(self, OutOfBandMembership, obj);
    }

    pub fn set_send_on_behalf_of(&mut self, obj: String) {
        set_property!(self, SendOnBehalfOf, obj.into_boxed_str());
    }

    pub fn set_recheck_redaction(&mut self, obj: bool) {
        set_property!(self, RecheckRedaction, obj);
    }

    pub fn set_soft_failed(&mut self, obj: bool) {
        set_property!(self, SoftFailed, obj);
    }

    pub fn set_proactively_send(&mut self, obj: bool) {
        set_property!(self, ProactivelySend, obj);
    }

    pub fn set_policy_server_spammy(&mut self, obj: bool) {
        set_property!(self, PolicyServerSpammy, obj);
    }

    fn get_spam_checker_spammy(&self) -> bool {
        get_property_opt!(self, SpamCheckerSpammy)
            .copied()
            .unwrap_or(false)
    }

    fn set_spam_checker_spammy(&mut self, obj: bool) {
        set_property!(self, SpamCheckerSpammy, obj);
    }

    pub fn set_redacted(&mut self, obj: bool) {
        set_property!(self, Redacted, obj);
    }

    pub fn set_txn_id(&mut self, obj: String) {
        set_property!(self, TxnId, obj.into_boxed_str());
    }

    pub fn set_delay_id(&mut self, obj: String) {
        set_property!(self, DelayId, obj.into_boxed_str());
    }

    pub fn set_token_id(&mut self, obj: i64) {
        set_property!(self, TokenId, obj);
    }

    pub fn set_device_id(&mut self, obj: String) {
        set_property!(self, DeviceId, obj.into_boxed_str());
    }
}

#[pyclass(frozen)]
#[derive(Clone)]
pub struct EventInternalMetadata {
    inner: Arc<RwLock<EventInternalMetadataInner>>,
}

impl EventInternalMetadata {
    fn read_inner(&self) -> PyResult<RwLockReadGuard<'_, EventInternalMetadataInner>> {
        self.inner
            .read()
            .map_err(|_| PyRuntimeError::new_err("EventInternalMetadata lock poisoned"))
    }

    /// Get a write lock on the inner data.
    ///
    /// Note that callers should be careful not to panic while holding the write
    /// lock, as this will poison the lock.q
    fn write_inner(&self) -> PyResult<RwLockWriteGuard<'_, EventInternalMetadataInner>> {
        self.inner
            .write()
            .map_err(|_| PyRuntimeError::new_err("EventInternalMetadata lock poisoned"))
    }
}

/// Helper to convert `None` to an `AttributeError` for a property getter.
fn attr_err<T>(val: Option<T>, name: &str) -> PyResult<T> {
    val.ok_or_else(|| {
        PyAttributeError::new_err(format!("'EventInternalMetadata' has no attribute '{name}'",))
    })
}

#[pymethods]
impl EventInternalMetadata {
    #[new]
    fn new(dict: &Bound<'_, PyDict>) -> PyResult<Self> {
        let mut data = Vec::with_capacity(dict.len());

        for (key, value) in dict.iter() {
            match EventInternalMetadataData::from_python_pair(&key, &value) {
                Ok(Some(entry)) => data.push(entry),
                Ok(None) => {}
                Err(err) => {
                    warn!("Ignoring internal metadata field '{key}', as failed to convert to Rust due to {err}")
                }
            }
        }

        data.shrink_to_fit();

        Ok(EventInternalMetadata {
            inner: Arc::new(RwLock::new(EventInternalMetadataInner {
                data,
                stream_ordering: None,
                instance_name: None,
                redacted_by: None,
                outlier: false,
            })),
        })
    }

    fn copy(&self) -> PyResult<Self> {
        let guard = self.read_inner()?;
        Ok(EventInternalMetadata {
            inner: Arc::new(RwLock::new(guard.clone())),
        })
    }

    /// Get a dict holding the data stored in the `internal_metadata` column in
    /// the database.
    ///
    /// Note that `outlier` and `stream_ordering` are stored in separate columns
    /// so are not returned here.
    fn get_dict(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let guard = self.read_inner()?;
        let dict = PyDict::new(py);

        for entry in &guard.data {
            let (key, value) = entry.to_python_pair(py);
            dict.set_item(key, value)?;
        }

        Ok(dict.into())
    }

    fn is_outlier(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.is_outlier())
    }

    fn is_out_of_band_membership(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.is_out_of_band_membership())
    }

    fn get_send_on_behalf_of(&self) -> PyResult<Option<String>> {
        Ok(self
            .read_inner()?
            .get_send_on_behalf_of()
            .map(|s| s.to_owned()))
    }

    fn need_to_check_redaction(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.need_to_check_redaction())
    }

    fn is_soft_failed(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.is_soft_failed())
    }

    fn should_proactively_send(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.should_proactively_send())
    }

    fn is_redacted(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.is_redacted())
    }

    fn is_notifiable(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.is_notifiable())
    }

    #[getter]
    fn get_stream_ordering(&self) -> PyResult<Option<NonZeroI64>> {
        Ok(self.read_inner()?.stream_ordering)
    }
    #[setter]
    fn set_stream_ordering(&self, val: Option<NonZeroI64>) -> PyResult<()> {
        self.write_inner()?.stream_ordering = val;
        Ok(())
    }

    #[getter]
    fn get_instance_name(&self) -> PyResult<Option<String>> {
        Ok(self.read_inner()?.instance_name.clone())
    }
    #[setter]
    fn set_instance_name(&self, val: Option<String>) -> PyResult<()> {
        self.write_inner()?.instance_name = val;
        Ok(())
    }

    #[getter]
    fn get_redacted_by(&self) -> PyResult<Option<String>> {
        Ok(self.read_inner()?.redacted_by.clone())
    }
    #[setter]
    fn set_redacted_by(&self, val: Option<String>) -> PyResult<()> {
        self.write_inner()?.redacted_by = val;
        Ok(())
    }

    #[getter]
    fn get_outlier(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.outlier)
    }
    #[setter]
    fn set_outlier(&self, val: bool) -> PyResult<()> {
        self.write_inner()?.outlier = val;
        Ok(())
    }

    #[getter]
    fn get_out_of_band_membership(&self) -> PyResult<bool> {
        attr_err(
            self.read_inner()?.get_out_of_band_membership(),
            "out_of_band_membership",
        )
    }
    #[setter]
    fn set_out_of_band_membership(&self, obj: bool) -> PyResult<()> {
        self.write_inner()?.set_out_of_band_membership(obj);
        Ok(())
    }

    #[getter(send_on_behalf_of)]
    fn getter_send_on_behalf_of(&self) -> PyResult<String> {
        let guard = self.read_inner()?;
        attr_err(
            guard.get_send_on_behalf_of().map(|s| s.to_owned()),
            "send_on_behalf_of",
        )
    }
    #[setter]
    fn set_send_on_behalf_of(&self, obj: String) -> PyResult<()> {
        self.write_inner()?.set_send_on_behalf_of(obj);
        Ok(())
    }

    #[getter]
    fn get_recheck_redaction(&self) -> PyResult<bool> {
        attr_err(
            self.read_inner()?.get_recheck_redaction(),
            "recheck_redaction",
        )
    }
    #[setter]
    fn set_recheck_redaction(&self, obj: bool) -> PyResult<()> {
        self.write_inner()?.set_recheck_redaction(obj);
        Ok(())
    }

    #[getter]
    fn get_soft_failed(&self) -> PyResult<bool> {
        attr_err(self.read_inner()?.get_soft_failed(), "soft_failed")
    }
    #[setter]
    fn set_soft_failed(&self, obj: bool) -> PyResult<()> {
        self.write_inner()?.set_soft_failed(obj);
        Ok(())
    }

    #[getter]
    fn get_proactively_send(&self) -> PyResult<bool> {
        attr_err(
            self.read_inner()?.get_proactively_send(),
            "proactively_send",
        )
    }
    #[setter]
    fn set_proactively_send(&self, obj: bool) -> PyResult<()> {
        self.write_inner()?.set_proactively_send(obj);
        Ok(())
    }

    #[getter]
    fn get_policy_server_spammy(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.get_policy_server_spammy())
    }
    #[setter]
    fn set_policy_server_spammy(&self, obj: bool) -> PyResult<()> {
        self.write_inner()?.set_policy_server_spammy(obj);
        Ok(())
    }

    #[getter]
    fn get_spam_checker_spammy(&self) -> PyResult<bool> {
        Ok(self.read_inner()?.get_spam_checker_spammy())
    }
    #[setter]
    fn set_spam_checker_spammy(&self, obj: bool) -> PyResult<()> {
        self.write_inner()?.set_spam_checker_spammy(obj);
        Ok(())
    }

    #[getter]
    fn get_redacted(&self) -> PyResult<bool> {
        attr_err(self.read_inner()?.get_redacted(), "redacted")
    }
    #[setter]
    fn set_redacted(&self, obj: bool) -> PyResult<()> {
        self.write_inner()?.set_redacted(obj);
        Ok(())
    }

    /// The transaction ID, if it was set when the event was created.
    #[getter]
    fn get_txn_id(&self) -> PyResult<String> {
        let guard = self.read_inner()?;
        attr_err(guard.get_txn_id().map(|s| s.to_owned()), "txn_id")
    }
    #[setter]
    fn set_txn_id(&self, obj: String) -> PyResult<()> {
        self.write_inner()?.set_txn_id(obj);
        Ok(())
    }

    /// The delay ID, set only if the event was a delayed event.
    #[getter]
    fn get_delay_id(&self) -> PyResult<String> {
        let guard = self.read_inner()?;
        attr_err(guard.get_delay_id().map(|s| s.to_owned()), "delay_id")
    }
    #[setter]
    fn set_delay_id(&self, obj: String) -> PyResult<()> {
        self.write_inner()?.set_delay_id(obj);
        Ok(())
    }

    /// The access token ID of the user who sent this event, if any.
    #[getter]
    fn get_token_id(&self) -> PyResult<i64> {
        attr_err(self.read_inner()?.get_token_id(), "token_id")
    }
    #[setter]
    fn set_token_id(&self, obj: i64) -> PyResult<()> {
        self.write_inner()?.set_token_id(obj);
        Ok(())
    }

    /// The device ID of the user who sent this event, if any.
    #[getter]
    fn get_device_id(&self) -> PyResult<String> {
        let guard = self.read_inner()?;
        attr_err(guard.get_device_id().map(|s| s.to_owned()), "device_id")
    }
    #[setter]
    fn set_device_id(&self, obj: String) -> PyResult<()> {
        self.write_inner()?.set_device_id(obj);
        Ok(())
    }
}
