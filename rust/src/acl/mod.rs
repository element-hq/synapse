/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright 2023 The Matrix.org Foundation C.I.C.
 * Copyright (C) 2023 New Vector, Ltd
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

//! An implementation of Matrix server ACL rules.

use std::collections::HashSet;
use std::net::Ipv4Addr;
use std::str::FromStr;

use anyhow::Error;
use pyo3::{prelude::*, pybacked::PyBackedStr};
use regex::Regex;

use crate::push::utils::{glob_to_regex, GlobMatchType};

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "acl")?;
    child_module.add_class::<ServerAclEvaluator>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import acl` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.acl", child_module)?;

    Ok(())
}

#[derive(Debug, Clone, Default)]
struct AclList {
    exact: HashSet<String>,
    patterns: Vec<Regex>,
}

impl AclList {
    fn new(entries: &[PyBackedStr]) -> Result<Self, Error> {
        let mut list = AclList::default();
        for entry in entries {
            let entry: &str = entry;
            if entry.is_ascii() && !entry.contains(['*', '?']) {
                list.exact.insert(entry.to_ascii_lowercase());
            } else {
                list.patterns
                    .push(glob_to_regex(entry, GlobMatchType::Whole)?);
            }
        }
        Ok(list)
    }

    fn matches(&self, server_name: &str, lowercased: Option<&str>) -> bool {
        lowercased.is_some_and(|l| self.exact.contains(l))
            || self.patterns.iter().any(|e| e.is_match(server_name))
    }
}

#[derive(Debug, Clone)]
#[pyclass(frozen, skip_from_py_object)]
pub struct ServerAclEvaluator {
    allow_ip_literals: bool,
    allow: AclList,
    deny: AclList,
}

#[pymethods]
impl ServerAclEvaluator {
    #[new]
    pub fn py_new(
        allow_ip_literals: bool,
        allow: Vec<PyBackedStr>,
        deny: Vec<PyBackedStr>,
    ) -> Result<Self, Error> {
        let allow = AclList::new(&allow)?;
        let deny = AclList::new(&deny)?;

        Ok(ServerAclEvaluator {
            allow_ip_literals,
            allow,
            deny,
        })
    }

    pub fn server_matches_acl_event(&self, server_name: &str) -> bool {
        // first of all, check if literal IPs are blocked, and if so, whether the
        // server name is a literal IP
        if !self.allow_ip_literals {
            // check for ipv6 literals. These start with '['.
            if server_name.starts_with('[') {
                return false;
            }

            // check for ipv4 literals. We can just lift the routine from std::net.
            if Ipv4Addr::from_str(server_name).is_ok() {
                return false;
            }
        }

        let lowercased = server_name
            .is_ascii()
            .then(|| server_name.to_ascii_lowercase());

        // next, check the deny list
        if self.deny.matches(server_name, lowercased.as_deref()) {
            return false;
        }

        // then the allow list.
        if self.allow.matches(server_name, lowercased.as_deref()) {
            return true;
        }

        // everything else should be rejected.
        false
    }
}
