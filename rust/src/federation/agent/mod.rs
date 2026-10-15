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

//! The outbound federation agent: Python's `MatrixFederationAgent` and its
//! `HTTPConnectionPool`. This holds its parts so far.
//!
//! - [`resolve`]: SRV lookups and the order to try servers in.
//! - [`well_known`]: `.well-known` delegation and its cache.
//! - [`policy`]: the IP range lists and the proxy.
//! - [`tls`]: certificate verification.

use std::sync::Arc;

pub mod policy;
pub mod resolve;
pub mod tls;
pub mod well_known;

/// The current time in milliseconds since the epoch. Production reads the
/// homeserver's [`Clock`](crate::clock::Clock). Rust tests use their own,
/// since the virtual clock is process-global.
pub type Now = Arc<dyn Fn() -> u64 + Send + Sync>;
