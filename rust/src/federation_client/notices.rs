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
 */

//! What happened during an attempt, for Python to log.

use std::{net::IpAddr, sync::Mutex};

#[derive(Debug, PartialEq, Eq)]
pub enum Notice {
    /// `Blocked %s from DNS resolution to %s`
    AddressBlocked { ip: IpAddr, host: String },
}

/// What Python reads about an attempt besides its result.
#[derive(Default)]
pub struct AttemptState {
    notices: Mutex<Vec<Notice>>,
}

impl AttemptState {
    pub fn record(&self, notice: Notice) {
        self.notices.lock().expect("notices poisoned").push(notice);
    }

    pub fn take_notices(&self) -> Vec<Notice> {
        std::mem::take(&mut *self.notices.lock().expect("notices poisoned"))
    }
}
