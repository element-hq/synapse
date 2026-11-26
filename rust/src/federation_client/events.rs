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

//! What happened during an attempt that Python logs, in today's words.

use std::{net::IpAddr, sync::Mutex};

#[derive(Debug, PartialEq, Eq)]
pub enum Event {
    /// `Blocked %s from DNS resolution to %s`
    AddressBlocked { ip: IpAddr, host: String },
}

/// What Python reads about an attempt besides its result.
#[derive(Default)]
pub struct AttemptState {
    events: Mutex<Vec<Event>>,
}

impl AttemptState {
    pub fn record(&self, event: Event) {
        self.events.lock().expect("events poisoned").push(event);
    }

    pub fn take_events(&self) -> Vec<Event> {
        std::mem::take(&mut *self.events.lock().expect("events poisoned"))
    }
}
