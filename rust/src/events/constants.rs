//! Matrix Events
//!
//! Contains types and utilities for working with Matrix events.

/// Maximum size of a PDU
pub const MAX_PDU_SIZE_BYTES: usize = 65_536;

/// Event Types
pub mod event_type {
    /// Event type: m.room.member
    pub const M_ROOM_MEMBER: &str = "m.room.member";
    /// Event type: m.room.create
    pub const M_ROOM_CREATE: &str = "m.room.create";
    /// Event type: m.room.join_rules
    pub const M_ROOM_JOIN_RULES: &str = "m.room.join_rules";
    /// Event type: m.room.power_levels
    pub const M_ROOM_POWER_LEVELS: &str = "m.room.power_levels";
    /// Event type: m.room.aliases
    pub const M_ROOM_ALIASES: &str = "m.room.aliases";
    /// Event type: m.room.history_visibility
    pub const M_ROOM_HISTORY_VISIBILITY: &str = "m.room.history_visibility";
    /// Event type: m.room.redaction
    pub const M_ROOM_REDACTION: &str = "m.room.redaction";
}

/// Event Fields
pub mod event_field {
    /// Event field: auth_events
    pub const AUTH_EVENTS: &str = "auth_events";
    /// Event field: content
    pub const CONTENT: &str = "content";
    /// Event field: depth
    pub const DEPTH: &str = "depth";
    /// Event field: hashes
    pub const HASHES: &str = "hashes";
    /// Event field: origin_server_ts
    pub const ORIGIN_SERVER_TS: &str = "origin_server_ts";
    /// Event field: prev_events
    pub const PREV_EVENTS: &str = "prev_events";
    /// Event field: room_id
    pub const ROOM_ID: &str = "room_id";
    /// Event field: sender
    pub const SENDER: &str = "sender";
    /// Event field: signatures
    pub const SIGNATURES: &str = "signatures";
    /// Event field: state_key
    pub const STATE_KEY: &str = "state_key";
    /// Event field: type
    pub const TYPE: &str = "type";
    /// Event field: unsigned
    pub const UNSIGNED: &str = "unsigned";
    /// Event field: event_id
    pub const EVENT_ID: &str = "event_id";
    /// Event field: origin
    pub const ORIGIN: &str = "origin";
    /// Event field: prev_state
    pub const PREV_STATE: &str = "prev_state";
    /// Event field: membership
    pub const MEMBERSHIP: &str = "membership";
    /// Event field: replaces_state
    pub const REPLACES_STATE: &str = "replaces_state";
    /// Event field: msc4354_sticky
    pub const MSC4354_STICKY: &str = "msc4354_sticky";
    // Event field: prev_state_events
    pub const PREV_STATE_EVENTS: &str = "prev_state_events";
    // Event field: m.relates_to
    pub const M_RELATES_TO: &str = "m.relates_to";
}

pub mod unsigned_field {
    /// Unsigned field: age
    pub const AGE: &str = "age";
    /// Unsigned field: age_ts
    pub const AGE_TS: &str = "age_ts";
    /// Unsigned field: redacted_because
    pub const REDACTED_BECAUSE: &str = "redacted_because";
    /// Unsigned field: redacted_by
    pub const REDACTED_BY: &str = "redacted_by";
    /// Unsigned field: transaction_id
    pub const TRANSACTION_ID: &str = "transaction_id";
    /// Unsigned field: org.matrix.msc4140.delay_id
    pub const DELAY_ID: &str = "org.matrix.msc4140.delay_id";
    /// Unsigned field: membership (MSC4115)
    pub const MEMBERSHIP: &str = "membership";
    /// Unsigned field: msc4354_sticky_duration_ttl_ms (MSC4354)
    pub const STICKY_TTL: &str = "msc4354_sticky_duration_ttl_ms";
    /// Unsigned field: io.element.synapse.soft_failed (admin metadata)
    pub const SOFT_FAILED: &str = "io.element.synapse.soft_failed";
    /// Unsigned field: io.element.synapse.policy_server_spammy (admin metadata)
    pub const POLICY_SERVER_SPAMMY: &str = "io.element.synapse.policy_server_spammy";
    /// Unsigned field: invite_room_state
    pub const INVITE_ROOM_STATE: &str = "invite_room_state";
    /// Unsigned field: knock_room_state
    pub const KNOCK_ROOM_STATE: &str = "knock_room_state";
    /// Unsigned field: m.relations
    pub const M_RELATIONS: &str = "m.relations";
}

/// Relation types (the `rel_type` of an `m.relates_to`).
pub mod relation_type {
    /// Relation type: m.reference
    pub const REFERENCE: &str = "m.reference";
    /// Relation type: m.replace
    pub const REPLACE: &str = "m.replace";
    /// Relation type: m.thread
    pub const THREAD: &str = "m.thread";
}

/// Membership Event Fields
pub mod membership_field {
    /// Membership event field: membership
    pub const MEMBERSHIP: &str = "membership";
    /// Membership event field: join_authorised_via_users_server
    pub const JOIN_AUTHORISED_VIA_USERS_SERVER: &str = "join_authorised_via_users_server";
    /// Membership event field: third_party_invite
    pub const THIRD_PARTY_INVITE: &str = "third_party_invite";
    /// Membership event field: signed
    pub const SIGNED: &str = "signed";
}

/// Create Event Fields
pub mod create_field {
    /// Create event field: creator
    pub const CREATOR: &str = "creator";
}

/// Join Rules Event Fields
pub mod join_rules_field {
    /// Join Rules event field: join_rule
    pub const JOIN_RULE: &str = "join_rule";
    /// Join Rules event field: allow
    pub const ALLOW: &str = "allow";
}

/// Power Levels Event Fields
pub mod power_levels_field {
    /// Power Levels event field: users
    pub const USERS: &str = "users";
    /// Power Levels event field: users_default
    pub const USERS_DEFAULT: &str = "users_default";
    /// Power Levels event field: events
    pub const EVENTS: &str = "events";
    /// Power Levels event field: events_default
    pub const EVENTS_DEFAULT: &str = "events_default";
    /// Power Levels event field: state_default
    pub const STATE_DEFAULT: &str = "state_default";
    /// Power Levels event field: ban
    pub const BAN: &str = "ban";
    /// Power Levels event field: kick
    pub const KICK: &str = "kick";
    /// Power Levels event field: redact
    pub const REDACT: &str = "redact";
    /// Power Levels event field: invite
    pub const INVITE: &str = "invite";
}

/// Aliases Event Fields
pub mod aliases_field {
    /// Aliases event field: aliases
    pub const ALIASES: &str = "aliases";
}

/// History Visibility Event Fields
pub mod history_visibility_field {
    /// History Visibility event field: history_visibility
    pub const HISTORY_VISIBILITY: &str = "history_visibility";
}

/// Redaction Event Fields
pub mod redaction_field {
    /// Redacts event field: redacts
    pub const REDACTS: &str = "redacts";
}
