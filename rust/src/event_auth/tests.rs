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

use serde_json::{json, Value};

use super::*;
use crate::events::internal_metadata::EventInternalMetadata;

const CREATOR: &str = "@creator:example.com";
const JOINER: &str = "@joiner:example.com";
const OTHER: &str = "@other:example.com";
const ROOM: &str = "!room:example.com";

/// Build an event for `room_version` from partial JSON, filling in the
/// fields the format requires.
pub(crate) fn make_event(room_version: &'static RoomVersion, partial: Value) -> Event {
    let mut obj = partial.as_object().expect("object").clone();
    obj.entry("auth_events").or_insert(json!([]));
    obj.entry("prev_events").or_insert(json!([]));
    obj.entry("depth").or_insert(json!(1));
    obj.entry("origin_server_ts").or_insert(json!(0));
    obj.entry("hashes").or_insert(json!({"sha256": "aaa"}));
    obj.entry("signatures").or_insert(json!({}));
    obj.entry("content").or_insert(json!({}));
    if !room_version.msc4291_room_ids_as_hashes {
        obj.entry("room_id").or_insert(json!(ROOM));
    }
    if room_version.event_format == EventFormatVersions::ROOM_V1_V2 {
        assert!(obj.contains_key("event_id"), "v1 events need an event_id");
    } else {
        obj.remove("event_id");
    }
    let json = serde_json::to_string(&obj).unwrap();
    Event::from_json_str(
        &json,
        room_version,
        EventInternalMetadata::from_json_str("{}").unwrap(),
        None,
    )
    .unwrap()
}

fn create(room_version: &'static RoomVersion) -> Event {
    let mut content = json!({});
    if !room_version.implicit_room_creator {
        content["creator"] = json!(CREATOR);
    }
    make_event(
        room_version,
        json!({"type": "m.room.create", "state_key": "", "sender": CREATOR,
               "content": content, "event_id": "$create:example.com"}),
    )
}

fn member(room_version: &'static RoomVersion, user: &str, membership: &str) -> Event {
    make_event(
        room_version,
        json!({"type": "m.room.member", "state_key": user, "sender": user,
               "content": {"membership": membership},
               "event_id": format!("${membership}-{user}")}),
    )
}

fn power_levels(room_version: &'static RoomVersion, sender: &str, content: Value) -> Event {
    make_event(
        room_version,
        json!({"type": "m.room.power_levels", "state_key": "", "sender": sender,
               "content": content, "event_id": "$pl:example.com"}),
    )
}

fn join_rules(room_version: &'static RoomVersion, rule: &str) -> Event {
    make_event(
        room_version,
        json!({"type": "m.room.join_rules", "state_key": "", "sender": CREATOR,
               "content": {"join_rule": rule}, "event_id": "$jr:example.com"}),
    )
}

fn check(event: &Event, auth: &[&Event]) -> Result<(), AuthFailure> {
    check_state_dependent_auth_rules(event, auth.iter().copied())
}

fn assert_rejected(result: Result<(), AuthFailure>) {
    match result {
        Err(AuthFailure::Rejected { .. }) => {}
        other => panic!("expected rejection, got {other:?}"),
    }
}

fn assert_invalid(result: Result<(), AuthFailure>) {
    match result {
        Err(AuthFailure::Invalid { .. }) => {}
        other => panic!("expected invalid, got {other:?}"),
    }
}

const V6: &RoomVersion = &RoomVersion::V6;

#[test]
fn create_event_is_always_allowed() {
    let create = create(V6);
    check(&create, &[]).unwrap();
}

#[test]
fn missing_create_event_is_rejected() {
    let msg = make_event(
        V6,
        json!({"type": "m.room.message", "sender": CREATOR, "content": {}}),
    );
    assert_rejected(check(&msg, &[&member(V6, CREATOR, "join")]));
}

#[test]
fn messages_need_membership_and_power() {
    let create = create(V6);
    let creator = member(V6, CREATOR, "join");
    let joiner = member(V6, JOINER, "join");
    let msg = |sender: &str| {
        make_event(
            V6,
            json!({"type": "m.room.message", "sender": sender, "content": {"body": "hi"}}),
        )
    };

    // Joined users can send messages; others cannot.
    check(&msg(JOINER), &[&create, &joiner]).unwrap();
    assert_rejected(check(&msg(OTHER), &[&create, &joiner]));

    // A higher events_default shuts out the joiner but not the creator.
    let pl = power_levels(
        V6,
        CREATOR,
        json!({"users": {CREATOR: 100}, "events_default": 50}),
    );
    assert_rejected(check(&msg(JOINER), &[&create, &joiner, &pl]));
    check(&msg(CREATOR), &[&create, &creator, &pl]).unwrap();

    // Per-type levels override events_default.
    let pl = power_levels(
        V6,
        CREATOR,
        json!({"users": {CREATOR: 100}, "events_default": 50, "events": {"m.room.message": 0}}),
    );
    check(&msg(JOINER), &[&create, &joiner, &pl]).unwrap();
}

#[test]
fn unfederatable_room() {
    let create = make_event(
        V6,
        json!({"type": "m.room.create", "state_key": "", "sender": CREATOR,
               "content": {"creator": CREATOR, "m.federate": false}}),
    );
    let remote = "@remote:other.org";
    let remote_member = member(V6, remote, "join");
    let msg = make_event(V6, json!({"type": "m.room.message", "sender": remote}));
    assert_rejected(check(&msg, &[&create, &remote_member]));
}

#[test]
fn creator_join_after_create() {
    let create = create(V6);
    let join = make_event(
        V6,
        json!({"type": "m.room.member", "state_key": CREATOR, "sender": CREATOR,
               "content": {"membership": "join"}, "prev_events": [create.event_id()]}),
    );
    check(&join, &[&create]).unwrap();

    // Anyone else needs a join rule.
    let join = make_event(
        V6,
        json!({"type": "m.room.member", "state_key": JOINER, "sender": JOINER,
               "content": {"membership": "join"}, "prev_events": [create.event_id()]}),
    );
    assert_rejected(check(&join, &[&create]));
}

#[test]
fn join_rule_checks() {
    let create = create(V6);
    let creator = member(V6, CREATOR, "join");
    let join = member(V6, JOINER, "join");

    check(&join, &[&create, &creator, &join_rules(V6, "public")]).unwrap();
    assert_rejected(check(
        &join,
        &[&create, &creator, &join_rules(V6, "invite")],
    ));

    // An invited user may join an invite-only room.
    let invite = make_event(
        V6,
        json!({"type": "m.room.member", "state_key": JOINER, "sender": CREATOR,
               "content": {"membership": "invite"}}),
    );
    check(
        &join,
        &[&create, &creator, &join_rules(V6, "invite"), &invite],
    )
    .unwrap();

    // Banned users may not join, even a public room.
    let ban = make_event(
        V6,
        json!({"type": "m.room.member", "state_key": JOINER, "sender": CREATOR,
               "content": {"membership": "ban"}}),
    );
    assert_rejected(check(
        &join,
        &[&create, &creator, &join_rules(V6, "public"), &ban],
    ));

    // Knock is not a join rule in v6.
    assert_rejected(check(&join, &[&create, &creator, &join_rules(V6, "knock")]));
}

#[test]
fn knocking() {
    const V7: &RoomVersion = &RoomVersion::V7;
    let create = create(V7);
    let creator = member(V7, CREATOR, "join");
    let knock = member(V7, JOINER, "knock");

    check(&knock, &[&create, &creator, &join_rules(V7, "knock")]).unwrap();
    assert_rejected(check(
        &knock,
        &[&create, &creator, &join_rules(V7, "invite")],
    ));

    // Knock is not a membership in v6.
    let knock = member(V6, JOINER, "knock");
    let create = self::create(V6);
    let creator = member(V6, CREATOR, "join");
    assert_rejected(check(
        &knock,
        &[&create, &creator, &join_rules(V6, "knock")],
    ));
}

#[test]
fn restricted_join() {
    const V8: &RoomVersion = &RoomVersion::V8;
    let create = create(V8);
    let creator = member(V8, CREATOR, "join");
    let rules = join_rules(V8, "restricted");
    let pl = power_levels(V8, CREATOR, json!({"users": {CREATOR: 100}}));

    let join = |authoriser: Option<&str>| {
        let mut content = json!({"membership": "join"});
        if let Some(authoriser) = authoriser {
            content["join_authorised_via_users_server"] = json!(authoriser);
        }
        make_event(
            V8,
            json!({"type": "m.room.member", "state_key": JOINER, "sender": JOINER,
                   "content": content}),
        )
    };

    check(&join(Some(CREATOR)), &[&create, &creator, &rules, &pl]).unwrap();
    assert_rejected(check(&join(None), &[&create, &creator, &rules, &pl]));
    // The authoriser must be in the room.
    assert_rejected(check(&join(Some(OTHER)), &[&create, &creator, &rules, &pl]));

    // ...and able to invite.
    let other = member(V8, OTHER, "join");
    let pl = power_levels(V8, CREATOR, json!({"users": {CREATOR: 100}, "invite": 50}));
    assert_rejected(check(
        &join(Some(OTHER)),
        &[&create, &creator, &other, &rules, &pl],
    ));

    // auth_types_for_event asks for the authoriser's membership.
    let event = join(Some(CREATOR));
    let types = auth_types_for_event(V8, &event).unwrap();
    assert!(types.contains(&(M_ROOM_MEMBER, CREATOR)));
}

#[test]
fn kick_and_ban_levels() {
    let create = create(V6);
    let creator = member(V6, CREATOR, "join");
    let joiner = member(V6, JOINER, "join");
    let pl = power_levels(V6, CREATOR, json!({"users": {CREATOR: 100}}));

    let change = |sender: &str, target: &str, membership: &str| {
        make_event(
            V6,
            json!({"type": "m.room.member", "state_key": target, "sender": sender,
                   "content": {"membership": membership}}),
        )
    };

    let auth = [&create, &creator, &joiner, &pl];
    check(&change(CREATOR, JOINER, "leave"), &auth).unwrap();
    check(&change(CREATOR, JOINER, "ban"), &auth).unwrap();
    assert_rejected(check(&change(JOINER, CREATOR, "leave"), &auth));
    assert_rejected(check(&change(JOINER, CREATOR, "ban"), &auth));

    // Leaving yourself is always fine.
    check(&change(JOINER, JOINER, "leave"), &auth).unwrap();

    // An unknown membership is a 500 AuthError.
    match check(&change(CREATOR, JOINER, "dance"), &auth) {
        Err(AuthFailure::Rejected { code: 500, .. }) => {}
        other => panic!("expected a 500 rejection, got {other:?}"),
    }
}

#[test]
fn power_level_changes() {
    let create = create(V6);
    let creator = member(V6, CREATOR, "join");
    let joiner = member(V6, JOINER, "join");
    let old_pl = power_levels(
        V6,
        CREATOR,
        json!({"users": {CREATOR: 100, JOINER: 50}, "state_default": 50}),
    );
    let auth = [&create, &creator, &joiner, &old_pl];

    // The joiner may not raise themself above their own level...
    let raise = power_levels(
        V6,
        JOINER,
        json!({"users": {CREATOR: 100, JOINER: 75}, "state_default": 50}),
    );
    assert_rejected(check(&raise, &auth));

    // ...nor demote the creator.
    let demote = power_levels(
        V6,
        JOINER,
        json!({"users": {CREATOR: 0, JOINER: 50}, "state_default": 50}),
    );
    assert_rejected(check(&demote, &auth));

    // The creator can do both.
    let ok = power_levels(
        V6,
        CREATOR,
        json!({"users": {CREATOR: 100, JOINER: 75}, "state_default": 50}),
    );
    check(&ok, &auth).unwrap();

    // Stringy levels are accepted (and coerced) before v10.
    let stringy = power_levels(
        V6,
        CREATOR,
        json!({"users": {CREATOR: "100", JOINER: "50"}, "state_default": "50"}),
    );
    check(&stringy, &auth).unwrap();

    // A malformed user ID or level is a 400.
    let bad_user = power_levels(V6, CREATOR, json!({"users": {"notauser": 10}}));
    assert_invalid(check(&bad_user, &auth));
    let bad_level = power_levels(V6, CREATOR, json!({"users": {JOINER: "lots"}}));
    assert_invalid(check(&bad_level, &auth));
}

#[test]
fn integer_power_levels_from_v10() {
    const V10: &RoomVersion = &RoomVersion::V10;
    let create = create(V10);
    let creator = member(V10, CREATOR, "join");
    let auth = [&create, &creator];

    let stringy = power_levels(V10, CREATOR, json!({"users": {CREATOR: "100"}}));
    assert_invalid(check(&stringy, &auth));
    let float = power_levels(V10, CREATOR, json!({"users_default": 1.0}));
    assert_invalid(check(&float, &auth));
    let ok = power_levels(V10, CREATOR, json!({"users": {CREATOR: 100}, "ban": 50}));
    check(&ok, &auth).unwrap();
}

#[test]
fn user_power_level_lookup() {
    let create = create(V6);
    let pl = power_levels(
        V6,
        CREATOR,
        json!({"users": {CREATOR: 100, JOINER: null}, "users_default": "7"}),
    );

    let map = build_auth_map([&create]).unwrap();
    // Without a power levels event, the creator has 100 and everyone else 0.
    assert_eq!(get_user_power_level(CREATOR, &map).unwrap(), 100);
    assert_eq!(get_user_power_level(JOINER, &map).unwrap(), 0);

    let map = build_auth_map([&create, &pl]).unwrap();
    assert_eq!(get_user_power_level(CREATOR, &map).unwrap(), 100);
    // A null level falls back to users_default.
    assert_eq!(get_user_power_level(JOINER, &map).unwrap(), 7);
    assert_eq!(get_user_power_level(OTHER, &map).unwrap(), 7);
    assert_eq!(get_named_level(&map, "ban", 50).unwrap(), 50);
}

#[test]
fn creator_power_from_msc4289() {
    const V12: &RoomVersion = &RoomVersion::V12;
    let create = make_event(
        V12,
        json!({"type": "m.room.create", "state_key": "", "sender": CREATOR,
               "content": {"room_version": "12", "additional_creators": [OTHER]}}),
    );
    let room_id = create.room_id().to_owned();
    let pl = make_event(
        V12,
        json!({"type": "m.room.power_levels", "state_key": "", "sender": CREATOR,
               "room_id": room_id, "content": {"users": {JOINER: 50}}}),
    );

    let map = build_auth_map([&create, &pl]).unwrap();
    assert_eq!(
        get_user_power_level(CREATOR, &map).unwrap(),
        CREATOR_POWER_LEVEL
    );
    assert_eq!(
        get_user_power_level(OTHER, &map).unwrap(),
        CREATOR_POWER_LEVEL
    );
    assert_eq!(get_user_power_level(JOINER, &map).unwrap(), 50);

    // Creators may not appear in the users map.
    let creator_member = make_event(
        V12,
        json!({"type": "m.room.member", "state_key": CREATOR, "sender": CREATOR,
               "room_id": room_id, "content": {"membership": "join"}}),
    );
    for user in [CREATOR, OTHER] {
        let bad = make_event(
            V12,
            json!({"type": "m.room.power_levels", "state_key": "", "sender": CREATOR,
                   "room_id": room_id, "content": {"users": {user: 100}}}),
        );
        assert_invalid(check(&bad, &[&create, &creator_member]));
    }
}

#[test]
fn aliases_in_v1() {
    const V1: &RoomVersion = &RoomVersion::V1;
    let create = create(V1);
    let alias = |state_key: &str| {
        make_event(
            V1,
            json!({"type": "m.room.aliases", "state_key": state_key, "sender": JOINER,
                   "event_id": "$alias:example.com"}),
        )
    };
    // The sender need not even be in the room.
    check(&alias("example.com"), &[&create]).unwrap();
    assert_rejected(check(&alias("other.org"), &[&create]));
    assert_rejected(check(&alias(""), &[&create]));
}

#[test]
fn redactions() {
    const V1: &RoomVersion = &RoomVersion::V1;
    let create = create(V1);
    let joiner = member(V1, JOINER, "join");
    let redact = |redacts: &str| {
        make_event(
            V1,
            json!({"type": "m.room.redaction", "sender": JOINER, "redacts": redacts,
                   "event_id": "$redaction:example.com"}),
        )
    };
    // In v1, a redaction from the same domain is allowed to be checked later.
    check(&redact("$target:example.com"), &[&create, &joiner]).unwrap();
    assert_rejected(check(&redact("$target:other.org"), &[&create, &joiner]));

    // From v3, the redaction is allowed and marked for a recheck.
    let create = self::create(V6);
    let joiner = member(V6, JOINER, "join");
    let redaction = make_event(
        V6,
        json!({"type": "m.room.redaction", "sender": JOINER, "redacts": "$target"}),
    );
    assert!(!redaction
        .internal_metadata()
        .need_to_check_redaction()
        .unwrap());
    check(&redaction, &[&create, &joiner]).unwrap();
    assert!(redaction
        .internal_metadata()
        .need_to_check_redaction()
        .unwrap());
}

#[test]
fn others_state_with_msc3757() {
    const MSC3757: &RoomVersion = &RoomVersion::MSC3757V10;
    let create = create(MSC3757);
    let creator = member(MSC3757, CREATOR, "join");
    let joiner = member(MSC3757, JOINER, "join");
    let pl = power_levels(
        MSC3757,
        CREATOR,
        json!({"users": {CREATOR: 100}, "state_default": 0}),
    );
    let auth = [&create, &creator, &joiner, &pl];
    let state = |sender: &str, state_key: &str| {
        make_event(
            MSC3757,
            json!({"type": "m.custom", "state_key": state_key, "sender": sender}),
        )
    };

    check(&state(JOINER, JOINER), &auth).unwrap();
    check(&state(JOINER, &format!("{JOINER}_device")), &auth).unwrap();
    assert_rejected(check(&state(JOINER, CREATOR), &auth));
    // A higher power level may set a lower user's state.
    check(&state(CREATOR, &format!("{JOINER}_device")), &auth).unwrap();
    // A state key that starts with '@' but is not a user ID is a 400.
    assert_invalid(check(&state(JOINER, "@nocolon"), &auth));

    // Without MSC3757, nobody may set state keyed to another user.
    let create = self::create(V6);
    let creator = member(V6, CREATOR, "join");
    let pl = power_levels(
        V6,
        CREATOR,
        json!({"users": {CREATOR: 100}, "state_default": 0}),
    );
    let event = make_event(
        V6,
        json!({"type": "m.custom", "state_key": JOINER, "sender": CREATOR}),
    );
    assert_rejected(check(&event, &[&create, &creator, &pl]));
}

#[test]
fn auth_types() {
    let invite = make_event(
        V6,
        json!({"type": "m.room.member", "state_key": JOINER, "sender": CREATOR,
               "content": {"membership": "invite",
                           "third_party_invite": {"signed": {"token": "tok"}}}}),
    );
    // In the order the Python adds them.
    let types = auth_types_for_event(V6, &invite).unwrap();
    let expected = vec![
        POWER_LEVELS_KEY,
        (M_ROOM_MEMBER, CREATOR),
        CREATE_KEY,
        JOIN_RULES_KEY,
        (M_ROOM_MEMBER, JOINER),
        (M_ROOM_THIRD_PARTY_INVITE, "tok"),
    ];
    assert_eq!(types, expected);

    // Pairs are distinct: a self-invite names the sender twice.
    let self_join = member(V6, JOINER, "join");
    let types = auth_types_for_event(V6, &self_join).unwrap();
    assert_eq!(
        types,
        vec![
            POWER_LEVELS_KEY,
            (M_ROOM_MEMBER, JOINER),
            CREATE_KEY,
            JOIN_RULES_KEY
        ]
    );

    assert!(auth_types_for_event(V6, &create(V6)).unwrap().is_empty());
}

#[test]
fn server_name_validation() {
    assert!(user_id_is_valid("@a:example.com"));
    assert!(user_id_is_valid("@a:example.com:8448"));
    assert!(user_id_is_valid("@a:[::1]"));
    assert!(user_id_is_valid("@a:[::1]:8448"));
    assert!(!user_id_is_valid("@a:[nope]"));
    assert!(!user_id_is_valid("@a:exa_mple.com"));
    assert!(!user_id_is_valid("@a:example.com:port"));
    assert!(!user_id_is_valid("@a"));
    assert!(!user_id_is_valid("a:example.com"));
}
