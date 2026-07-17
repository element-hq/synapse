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

//! Rust counterparts to the `synapse.logging` Python package.
//!
//! Submodules live at the Rust module path that mirrors their Python logging
//! namespace (e.g. `synapse::logging::context` -> `synapse.logging.context`), so
//! that log records emitted from here via the `log` crate land under the matching
//! Python logger without an explicit `target:`.

pub mod context;
