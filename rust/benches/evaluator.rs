/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright 2022 The Matrix.org Foundation C.I.C.
 * Copyright (C) 2023 New Vector, Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 * Originally licensed under the Apache License, Version 2.0:
 * <http://www.apache.org/licenses/LICENSE-2.0>.
 *
 * [This file includes modifications made by New Vector Limited]
 *
 */

#![feature(test)]

use std::borrow::Cow;

use synapse::push::{
    evaluator::PushRuleEvaluator, Condition, EventMatchCondition, FilteredPushRules, JsonValue,
    PushRules, SimpleJsonValue,
};
use test::Bencher;

extern crate test;

#[bench]
fn bench_match_exact(b: &mut Bencher) {
    let flattened_keys = [
        (
            "type".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("m.text"))),
        ),
        (
            "room_id".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("!room:server"))),
        ),
        (
            "content.body".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("test message"))),
        ),
    ]
    .into_iter()
    .collect();

    let eval = PushRuleEvaluator::py_new(
        flattened_keys,
        false,
        10,
        Some(0),
        Default::default(),
        Default::default(),
        true,
        vec![],
        false,
        false,
    )
    .unwrap();

    let condition = Condition::Known(synapse::push::KnownCondition::EventMatch(
        EventMatchCondition {
            key: "room_id".into(),
            pattern: "!room:server".into(),
        },
    ));

    let matched = eval.match_condition(&condition, None, None).unwrap();
    assert!(matched, "Didn't match");

    b.iter(|| eval.match_condition(&condition, None, None).unwrap());
}

#[bench]
fn bench_match_word(b: &mut Bencher) {
    let flattened_keys = [
        (
            "type".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("m.text"))),
        ),
        (
            "room_id".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("!room:server"))),
        ),
        (
            "content.body".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("test message"))),
        ),
    ]
    .into_iter()
    .collect();

    let eval = PushRuleEvaluator::py_new(
        flattened_keys,
        false,
        10,
        Some(0),
        Default::default(),
        Default::default(),
        true,
        vec![],
        false,
        false,
    )
    .unwrap();

    let condition = Condition::Known(synapse::push::KnownCondition::EventMatch(
        EventMatchCondition {
            key: "content.body".into(),
            pattern: "test".into(),
        },
    ));

    let matched = eval.match_condition(&condition, None, None).unwrap();
    assert!(matched, "Didn't match");

    b.iter(|| eval.match_condition(&condition, None, None).unwrap());
}

#[bench]
fn bench_match_word_miss(b: &mut Bencher) {
    let flattened_keys = [
        (
            "type".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("m.text"))),
        ),
        (
            "room_id".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("!room:server"))),
        ),
        (
            "content.body".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("test message"))),
        ),
    ]
    .into_iter()
    .collect();

    let eval = PushRuleEvaluator::py_new(
        flattened_keys,
        false,
        10,
        Some(0),
        Default::default(),
        Default::default(),
        true,
        vec![],
        false,
        false,
    )
    .unwrap();

    let condition = Condition::Known(synapse::push::KnownCondition::EventMatch(
        EventMatchCondition {
            key: "content.body".into(),
            pattern: "foobar".into(),
        },
    ));

    let matched = eval.match_condition(&condition, None, None).unwrap();
    assert!(!matched, "Didn't match");

    b.iter(|| eval.match_condition(&condition, None, None).unwrap());
}

#[bench]
fn bench_eval_message(b: &mut Bencher) {
    let flattened_keys = [
        (
            "type".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("m.text"))),
        ),
        (
            "room_id".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("!room:server"))),
        ),
        (
            "content.body".to_string(),
            JsonValue::Value(SimpleJsonValue::Str(Cow::Borrowed("test message"))),
        ),
    ]
    .into_iter()
    .collect();

    let eval = PushRuleEvaluator::py_new(
        flattened_keys,
        false,
        10,
        Some(0),
        Default::default(),
        Default::default(),
        true,
        vec![],
        false,
        false,
    )
    .unwrap();

    let rules = FilteredPushRules::py_new(
        PushRules::new(Vec::new()),
        Default::default(),
        false,
        false,
        false,
        false,
        false,
    );

    b.iter(|| eval.run(&rules, Some("bob"), Some("person")));
}
