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

//! The sliding sync extensions (`synapse.handlers.sliding_sync.extensions`).

use pyo3::{prelude::*, types::PyModule};

pub mod relevant;

pub fn register_test_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
    relevant::register_test_functions(m)
}
