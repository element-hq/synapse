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

//! # Matrix Identifiers
//!
//! This module contains definitions and utilities for working with matrix identifiers.

use std::{fmt, ops::Deref};

/// Errors that can occur when parsing a matrix identifier.
#[derive(Clone, Debug, PartialEq)]
pub enum IdentifierError {
    IncorrectSigil,
    MissingColon,
}

impl fmt::Display for IdentifierError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}

/// A Matrix user_id.
#[derive(Clone, Debug, PartialEq)]
pub struct UserID(String);

impl UserID {
    /// Returns the `localpart` of the user_id.
    pub fn localpart(&self) -> &str {
        &self[1..self.colon_pos()]
    }

    /// Returns the `server_name` / `domain` of the user_id.
    pub fn server_name(&self) -> &str {
        &self[self.colon_pos() + 1..]
    }

    /// Returns the position of the ':' inside of the user_id.
    /// Used when splitting the user_id into it's respective parts.
    fn colon_pos(&self) -> usize {
        self.find(':').unwrap()
    }
}

impl TryFrom<&str> for UserID {
    type Error = IdentifierError;

    /// Will try creating a `UserID` from the provided `&str`.
    /// Can fail if the user_id is incorrectly formatted.
    fn try_from(s: &str) -> Result<Self, Self::Error> {
        if !s.starts_with('@') {
            return Err(IdentifierError::IncorrectSigil);
        }

        if s.find(':').is_none() {
            return Err(IdentifierError::MissingColon);
        }

        Ok(UserID(s.to_string()))
    }
}

impl TryFrom<String> for UserID {
    type Error = IdentifierError;

    /// Will try creating a `UserID` from the provided `&str`.
    /// Can fail if the user_id is incorrectly formatted.
    fn try_from(s: String) -> Result<Self, Self::Error> {
        if !s.starts_with('@') {
            return Err(IdentifierError::IncorrectSigil);
        }

        if s.find(':').is_none() {
            return Err(IdentifierError::MissingColon);
        }

        Ok(UserID(s))
    }
}

impl<'de> serde::Deserialize<'de> for UserID {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let s: String = serde::Deserialize::deserialize(deserializer)?;
        UserID::try_from(s).map_err(serde::de::Error::custom)
    }
}

impl Deref for UserID {
    type Target = str;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl fmt::Display for UserID {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// A Matrix room_id.
#[derive(Clone, Debug, PartialEq)]
pub struct RoomID(String);

impl RoomID {
    /// Returns the `localpart` of the room_id.
    pub fn localpart(&self) -> &str {
        &self[1..self.colon_pos()]
    }

    /// Returns the `server_name` / `domain` of the room_id.
    pub fn server_name(&self) -> &str {
        &self[self.colon_pos() + 1..]
    }

    /// Returns the position of the ':' inside of the room_id.
    /// Used when splitting the room_id into it's respective parts.
    fn colon_pos(&self) -> usize {
        self.find(':').unwrap()
    }
}

impl TryFrom<&str> for RoomID {
    type Error = IdentifierError;

    /// Will try creating a `RoomID` from the provided `&str`.
    /// Can fail if the room_id is incorrectly formatted.
    fn try_from(s: &str) -> Result<Self, Self::Error> {
        if !s.starts_with('!') {
            return Err(IdentifierError::IncorrectSigil);
        }

        if s.find(':').is_none() {
            return Err(IdentifierError::MissingColon);
        }

        Ok(RoomID(s.to_string()))
    }
}

impl TryFrom<String> for RoomID {
    type Error = IdentifierError;

    /// Will try creating a `RoomID` from the provided `String`.
    /// Can fail if the room_id is incorrectly formatted.
    fn try_from(s: String) -> Result<Self, Self::Error> {
        if !s.starts_with('!') {
            return Err(IdentifierError::IncorrectSigil);
        }

        if s.find(':').is_none() {
            return Err(IdentifierError::MissingColon);
        }

        Ok(RoomID(s))
    }
}

impl<'de> serde::Deserialize<'de> for RoomID {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let s: String = serde::Deserialize::deserialize(deserializer)?;
        RoomID::try_from(s).map_err(serde::de::Error::custom)
    }
}

impl Deref for RoomID {
    type Target = str;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl fmt::Display for RoomID {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// A Matrix event_id.
#[derive(Clone, Debug, PartialEq)]
pub struct EventID(String);

impl TryFrom<&str> for EventID {
    type Error = IdentifierError;

    /// Will try creating a `EventID` from the provided `&str`.
    /// Can fail if the event_id is incorrectly formatted.
    fn try_from(s: &str) -> Result<Self, Self::Error> {
        if !s.starts_with('$') {
            return Err(IdentifierError::IncorrectSigil);
        }

        Ok(EventID(s.to_string()))
    }
}

impl TryFrom<String> for EventID {
    type Error = IdentifierError;

    /// Will try creating a `EventID` from the provided `String`.
    /// Can fail if the event_id is incorrectly formatted.
    fn try_from(s: String) -> Result<Self, Self::Error> {
        if !s.starts_with('$') {
            return Err(IdentifierError::IncorrectSigil);
        }

        Ok(EventID(s))
    }
}

impl<'de> serde::Deserialize<'de> for EventID {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let s: String = serde::Deserialize::deserialize(deserializer)?;
        EventID::try_from(s).map_err(serde::de::Error::custom)
    }
}

impl Deref for EventID {
    type Target = str;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl fmt::Display for EventID {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}
