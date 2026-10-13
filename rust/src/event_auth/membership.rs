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

//! The `m.room.member` auth rules. Port of `_is_membership_change_allowed`.

use log::debug;
use serde_json::Value;

use super::{
    can_federate, check_joined_room, get_domain_from_id, membership_of,
    power_levels::{get_named_level, get_user_power_level},
    verify_third_party_invite, AuthFailure, AuthMap, CREATE_KEY, JOIN_RULES_KEY,
};
use crate::events::{
    constants::{event_type::M_ROOM_MEMBER, membership_field::JOIN_AUTHORISED_VIA_USERS_SERVER},
    Event,
};
use crate::room_versions::RoomVersion;

pub(super) const JOIN: &str = "join";
pub(super) const INVITE: &str = "invite";
pub(super) const LEAVE: &str = "leave";
pub(super) const BAN: &str = "ban";
pub(super) const KNOCK: &str = "knock";

const JOIN_RULE_PUBLIC: &str = "public";
const JOIN_RULE_INVITE: &str = "invite";
const JOIN_RULE_KNOCK: &str = "knock";
const JOIN_RULE_RESTRICTED: &str = "restricted";
const JOIN_RULE_KNOCK_RESTRICTED: &str = "knock_restricted";

const NOT_JOINED: &str = "ORG.MATRIX.MSC3848.NOT_JOINED";
const ALREADY_JOINED: &str = "ORG.MATRIX.MSC3848.ALREADY_JOINED";
const INSUFFICIENT_POWER: &str = "ORG.MATRIX.MSC3848.INSUFFICIENT_POWER";

/// Whether a JSON value is the given string. Python compares the raw content
/// value with `==`, so a non-string never matches.
fn is(value: &Value, s: &str) -> bool {
    value.as_str() == Some(s)
}

/// Whether `event` is an allowed membership change given `auth_events`.
pub fn is_membership_change_allowed(
    room_version: &RoomVersion,
    event: &Event,
    auth_events: &AuthMap<'_>,
) -> Result<(), AuthFailure> {
    let membership = membership_of(event)?;

    // Check if this is the room creator joining.
    let prev_event_ids = event.prev_event_ids();
    if prev_event_ids.len() == 1 && is(membership, JOIN) {
        if let Some(create) = auth_events.get(&CREATE_KEY) {
            if prev_event_ids[0] == create.event_id() {
                let is_creator = if room_version.implicit_room_creator {
                    Some(create.sender()) == event.state_key()
                } else {
                    let Some(creator) = create.content().get_field("creator") else {
                        return Err(AuthFailure::invalid("create event has no 'creator'"));
                    };
                    creator.as_str().is_some() && creator.as_str() == event.state_key()
                };
                if is_creator {
                    return Ok(());
                }
            }
        }
    }

    let Some(target_user_id) = event.state_key() else {
        return Err(AuthFailure::invalid("membership event has no state_key"));
    };

    // We need the create event in order to check if we can federate or not.
    let Some(create) = auth_events.get(&CREATE_KEY) else {
        return Err(AuthFailure::rejected(
            "Create event missing from auth_events",
        ));
    };
    let creating_domain = get_domain_from_id(create.sender())?;
    let target_domain = get_domain_from_id(target_user_id)?;
    if creating_domain != target_domain && !can_federate(auth_events) {
        return Err(AuthFailure::rejected(
            "This room has been marked as unfederatable.",
        ));
    }

    // Get info about the caller.
    let caller = auth_events.get(&(M_ROOM_MEMBER, event.sender())).copied();
    let caller_membership = caller.map(membership_of).transpose()?;
    let caller_in_room = caller_membership.is_some_and(|m| is(m, JOIN));
    let caller_invited = caller_membership.is_some_and(|m| is(m, INVITE));
    let caller_knocked =
        room_version.knock_join_rule && caller_membership.is_some_and(|m| is(m, KNOCK));

    // Get info about the target.
    let target = auth_events.get(&(M_ROOM_MEMBER, target_user_id)).copied();
    let target_membership = target.map(membership_of).transpose()?;
    let target_in_room = target_membership.is_some_and(|m| is(m, JOIN));
    let target_banned = target_membership.is_some_and(|m| is(m, BAN));

    let join_rule_default = Value::String(JOIN_RULE_INVITE.to_owned());
    let join_rule = auth_events
        .get(&JOIN_RULES_KEY)
        .and_then(|ev| ev.content().get_field("join_rule"))
        .unwrap_or(&join_rule_default);

    let user_level = get_user_power_level(event.sender(), auth_events)?;
    let target_level = get_user_power_level(target_user_id, auth_events)?;

    let invite_level = get_named_level(auth_events, "invite", 0)?;
    let ban_level = get_named_level(auth_events, "ban", 50)?;

    debug!(
        "_is_membership_change_allowed: caller_membership={caller_membership:?} \
         caller_in_room={caller_in_room} caller_invited={caller_invited} \
         caller_knocked={caller_knocked} target_banned={target_banned} \
         target_in_room={target_in_room} membership={membership} join_rule={join_rule} \
         target_user_id={target_user_id} event.sender={}",
        event.sender()
    );

    if is(membership, INVITE) && event.content().get_field("third_party_invite").is_some() {
        if !verify_third_party_invite(event, auth_events)? {
            return Err(AuthFailure::rejected("You are not invited to this room."));
        }
        if target_banned {
            return Err(AuthFailure::rejected(format!(
                "{target_user_id} is banned from the room"
            )));
        }
        return Ok(());
    }

    // Require the user to be in the room for membership changes other than
    // join/knock. The room version check for knocking is done implicitly by
    // `caller_knocked` and the ability to set a membership of `knock` in the
    // first place.
    if !is(membership, JOIN) && !is(membership, KNOCK) {
        // If the user has been invited or has knocked, they are allowed to
        // change their membership event to leave.
        if (caller_invited || caller_knocked)
            && is(membership, LEAVE)
            && target_user_id == event.sender()
        {
            return Ok(());
        }

        if !caller_in_room {
            return Err(AuthFailure::unstable(
                format!("{} not in room {}.", event.sender(), event.room_id()),
                NOT_JOINED,
            ));
        }
    }

    if is(membership, INVITE) {
        // Invites are valid iff caller is in the room and target isn't.
        if target_banned {
            return Err(AuthFailure::rejected(format!(
                "{target_user_id} is banned from the room"
            )));
        } else if target_in_room {
            return Err(AuthFailure::unstable(
                format!("{target_user_id} is already in the room."),
                ALREADY_JOINED,
            ));
        } else if user_level < invite_level {
            return Err(AuthFailure::unstable(
                "You don't have permission to invite users",
                INSUFFICIENT_POWER,
            ));
        }
    } else if is(membership, JOIN) {
        // Joins are valid iff caller == target and:
        // * They are not banned.
        // * They are accepting a previously sent invitation.
        // * They are already joined (it's a NOOP).
        // * The room is public.
        // * The room is restricted and the user meets the allows rules.
        if event.sender() != target_user_id {
            return Err(AuthFailure::rejected("Cannot force another user to join."));
        } else if target_banned {
            return Err(AuthFailure::rejected("You are banned from this room"));
        } else if is(join_rule, JOIN_RULE_PUBLIC) {
            // Allowed.
        } else if (room_version.restricted_join_rule && is(join_rule, JOIN_RULE_RESTRICTED))
            || (room_version.knock_restricted_join_rule
                && is(join_rule, JOIN_RULE_KNOCK_RESTRICTED))
        {
            // This is the same as public, but the event must contain a
            // reference to the server who authorised the join. If the caller
            // is in the room or invited, then they do not need to meet the
            // allow rules.
            if !caller_in_room && !caller_invited {
                let authorising_user =
                    match event.content().get_field(JOIN_AUTHORISED_VIA_USERS_SERVER) {
                        None | Some(Value::Null) => {
                            return Err(AuthFailure::rejected(
                                "Join event is missing authorising user.",
                            ))
                        }
                        Some(Value::String(user)) => user.as_str(),
                        Some(Value::Array(_) | Value::Object(_)) => {
                            // Python raises `TypeError: unhashable type` building
                            // the lookup key.
                            return Err(AuthFailure::invalid("authorising user is not hashable"));
                        }
                        Some(other) => {
                            // A number or boolean can never match a state key.
                            return Err(AuthFailure::rejected(format!(
                                "User {other} not in room {}",
                                event.room_id()
                            )));
                        }
                    };

                // The authorising user must be in the room.
                let member_event = auth_events.get(&(M_ROOM_MEMBER, authorising_user)).copied();
                check_joined_room(member_event, authorising_user, event.room_id())?;

                let authorising_user_level = get_user_power_level(authorising_user, auth_events)?;
                if authorising_user_level < invite_level {
                    return Err(AuthFailure::rejected(
                        "Join event authorised by invalid server.",
                    ));
                }
            }
        } else if is(join_rule, JOIN_RULE_INVITE)
            || (room_version.knock_join_rule && is(join_rule, JOIN_RULE_KNOCK))
            || (room_version.knock_restricted_join_rule
                && is(join_rule, JOIN_RULE_KNOCK_RESTRICTED))
        {
            // You can only join the room if you are invited or are already in
            // the room.
            if !(caller_in_room || caller_invited) {
                return Err(AuthFailure::rejected("You are not invited to this room."));
            }
        } else {
            return Err(AuthFailure::rejected(
                "You are not allowed to join this room",
            ));
        }
    } else if is(membership, LEAVE) {
        if target_banned && user_level < ban_level {
            return Err(AuthFailure::unstable(
                format!("You cannot unban user {target_user_id}."),
                INSUFFICIENT_POWER,
            ));
        } else if target_user_id != event.sender() {
            let kick_level = get_named_level(auth_events, "kick", 50)?;

            if user_level < kick_level || user_level <= target_level {
                return Err(AuthFailure::unstable(
                    format!("You cannot kick user {target_user_id}."),
                    INSUFFICIENT_POWER,
                ));
            }
        }
    } else if is(membership, BAN) {
        if user_level < ban_level {
            return Err(AuthFailure::unstable(
                "You don't have permission to ban",
                INSUFFICIENT_POWER,
            ));
        } else if user_level <= target_level {
            return Err(AuthFailure::unstable(
                "You don't have permission to ban this user",
                INSUFFICIENT_POWER,
            ));
        }
    } else if room_version.knock_join_rule && is(membership, KNOCK) {
        if !is(join_rule, JOIN_RULE_KNOCK)
            && (!room_version.knock_restricted_join_rule
                || !is(join_rule, JOIN_RULE_KNOCK_RESTRICTED))
        {
            return Err(AuthFailure::rejected("You don't have permission to knock"));
        } else if target_user_id != event.sender() {
            return Err(AuthFailure::rejected("You cannot knock for other users"));
        } else if target_in_room {
            return Err(AuthFailure::unstable(
                "You cannot knock on a room you are already in",
                ALREADY_JOINED,
            ));
        } else if caller_invited {
            return Err(AuthFailure::rejected(
                "You are already invited to this room",
            ));
        } else if target_banned {
            return Err(AuthFailure::rejected("You are banned from this room"));
        }
    } else {
        return Err(AuthFailure::Rejected {
            code: 500,
            msg: format!("Unknown membership {membership}"),
            errcode: None,
        });
    }

    Ok(())
}
