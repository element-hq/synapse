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

use crate::db::python_db_pool::DatabasePool as PythonDatabasePool;

/// A `tokio-postgres` looking thing that we can use on the Rust side to interact with
/// the database
pub trait Database {
    pub fn query(&self) -> None {
        todo!("TODO");
    }
}

/// Database access backed by the database pool we already have in the Python side of Synapse
struct SynapsePythonDatabase {
    db_pool: PythonDatabasePool,
}

impl Database for SynapsePythonDatabase {
    pub fn query(&self) -> None {
        todo!("TODO");
    }
}

/// Native Rust database access backed by `tokio-postgres` (for use in synapse-rust-apps)
struct TokioPostgresDatabase {
    // TODO
}

impl Database for TokioPostgresDatabase {
    pub fn query(&self) -> None {
        todo!("TODO");
    }
}
