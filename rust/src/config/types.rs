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

use std::{net::IpAddr, str::FromStr};

use ipnet::IpNet;
use pyo3::{
    exceptions::{PyAssertionError, PyValueError},
    intern,
    prelude::*,
};

/// The presets available when creating a Matrix room according to the Matrix spec:
///
/// > Preset | join_rules | history_visibility | guest_access | Other
/// > --- | --- | --- | --- | ---
/// > `private_chat` | `invite` | ``shared`` | `can_join` | .
/// > `trusted_private_chat` | `invite` | shared | `can_join` | All invitees are given the same power level as the room creator.
/// > `public_chat` | `public` | `shared` | `forbidden` | .
/// >
/// > *-- https://spec.matrix.org/v1.18/client-server-api/#post_matrixclientv3createroom*
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum RoomCreationPreset {
    PrivateChat,
    PublicChat,
    TrustedPrivateChat,
}

impl FromStr for RoomCreationPreset {
    type Err = PyErr;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s {
            "private_chat" => RoomCreationPreset::PrivateChat,
            "public_chat" => RoomCreationPreset::PublicChat,
            "trusted_private_chat" => RoomCreationPreset::TrustedPrivateChat,
            other => {
                return Err(PyAssertionError::new_err(format!(
                    "Unknown variant {other:?} does not translate to `RoomCreationPreset`. \
                     This is a Synapse programming error."
                )))
            }
        })
    }
}

impl<'a, 'py> FromPyObject<'a, 'py> for RoomCreationPreset {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        value.extract::<&str>()?.parse()
    }
}

/// A set of IP ranges: a `netaddr.IPSet` in Python.
#[derive(Clone, Debug, Default)]
pub struct IpRangeSet(Vec<IpNet>);

impl IpRangeSet {
    pub fn new(ranges: Vec<IpNet>) -> Self {
        IpRangeSet(ranges)
    }

    pub fn contains(&self, ip: &IpAddr) -> bool {
        self.0.iter().any(|net| net.contains(ip))
    }
}

impl<'a, 'py> FromPyObject<'a, 'py> for IpRangeSet {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let py = value.py();
        let mut ranges = Vec::new();
        for cidr in value.call_method0(intern!(py, "iter_cidrs"))?.try_iter()? {
            let cidr = cidr?.str()?;
            let cidr = cidr.to_str()?;
            ranges.push(
                cidr.parse()
                    .map_err(|e| PyValueError::new_err(format!("bad IP range {cidr}: {e}")))?,
            );
        }
        Ok(IpRangeSet(ranges))
    }
}

/// A compiled Python regex (`re.Pattern`), translated to a Rust [`regex::Regex`].
///
/// Only handles what config produces with `glob_to_regex`: `\Z` becomes `\z`
/// and `re.IGNORECASE` becomes `(?i)`. Anything else that Rust's regex syntax
/// rejects is a config error.
#[derive(Clone, Debug)]
pub struct PythonRegex(regex::Regex);

impl PythonRegex {
    pub fn from_python_pattern(pattern: &str, ignore_case: bool) -> Result<Self, regex::Error> {
        let pattern = pattern.replace(r"\Z", r"\z");
        let pattern = if ignore_case {
            format!("(?i){pattern}")
        } else {
            pattern
        };
        Ok(PythonRegex(regex::Regex::new(&pattern)?))
    }

    /// Whether the pattern matches at the start of `s`, as `re.match` does.
    pub fn is_match_at_start(&self, s: &str) -> bool {
        self.0.find(s).is_some_and(|m| m.start() == 0)
    }
}

impl<'a, 'py> FromPyObject<'a, 'py> for PythonRegex {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let py = value.py();
        let pattern: String = value.getattr(intern!(py, "pattern"))?.extract()?;
        let flags: u32 = value.getattr(intern!(py, "flags"))?.extract()?;
        // `re.IGNORECASE`
        let ignore_case = flags & 2 != 0;

        PythonRegex::from_python_pattern(&pattern, ignore_case)
            .map_err(|e| PyValueError::new_err(format!("cannot use regex {pattern:?}: {e}")))
    }
}

/// `config.server.proxy_config`: the forward proxy for outbound HTTP.
#[derive(FromPyObject, Clone, Debug, Default)]
pub struct ProxyConfig {
    pub http_proxy: Option<String>,
    pub https_proxy: Option<String>,
    pub no_proxy_hosts: Option<Vec<String>>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn glob_regexes_translate() {
        // What `glob_to_regex("*.example.com")` compiles to.
        let regex = PythonRegex::from_python_pattern(r"\A(.{0,}\.example\.com)\Z", true).unwrap();
        assert!(regex.is_match_at_start("foo.example.com"));
        assert!(regex.is_match_at_start("FOO.Example.com"));
        assert!(!regex.is_match_at_start("example.com"));
        assert!(!regex.is_match_at_start("foo.example.com.evil"));
        assert!(!regex.is_match_at_start("foo.example.com\n"));

        // `re.escape` escapes `-`, which Rust's regex must accept too.
        let regex = PythonRegex::from_python_pattern(r"\A(my\-host)\Z", true).unwrap();
        assert!(regex.is_match_at_start("my-host"));
    }

    #[test]
    fn ip_range_set() {
        let set = IpRangeSet::new(vec![
            "10.0.0.0/8".parse().unwrap(),
            "::1/128".parse().unwrap(),
        ]);
        assert!(set.contains(&"10.1.2.3".parse().unwrap()));
        assert!(set.contains(&"::1".parse().unwrap()));
        assert!(!set.contains(&"11.0.0.1".parse().unwrap()));
    }
}
