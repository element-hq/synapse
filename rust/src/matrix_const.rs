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
 */

//! # Matrix Constants
//!
//! This module contains definitions for constant values described by the matrix specification.

pub const HISTORY_VISIBILITY_WORLD_READABLE: &str = "world_readable";
pub const HISTORY_VISIBILITY_SHARED: &str = "shared";
pub const HISTORY_VISIBILITY_INVITED: &str = "invited";
pub const HISTORY_VISIBILITY_JOINED: &str = "joined";

pub const MEMBERSHIP_BAN: &str = "ban";
pub const MEMBERSHIP_LEAVE: &str = "leave";
pub const MEMBERSHIP_KNOCK: &str = "knock";
pub const MEMBERSHIP_INVITE: &str = "invite";
pub const MEMBERSHIP_JOIN: &str = "join";
