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

use serde_json::json;

use super::*;
use crate::federation::client::tests::{new_client, ScriptedTransport, Step, DEST, ORIGIN};

fn pdu(event_id: &str, depth: Value) -> Value {
    json!({"event_id": event_id, "depth": depth, "type": "m.room.message"})
}

fn sender(transport: Arc<ScriptedTransport>) -> TransactionSender {
    let (client, _timings) = new_client(transport);
    TransactionSender::new(Arc::new(client))
}

#[test]
fn body_matches_get_dict() {
    let transaction = Transaction {
        transaction_id: "5".to_owned(),
        origin: ORIGIN.to_owned(),
        destination: DEST.to_owned(),
        origin_server_ts: 1234,
        pdus: vec![pdu("$a", json!(1))],
        edus: vec![],
    };
    // No `edus` key when there are none.
    assert_eq!(
        transaction.body(),
        json!({
            "origin": ORIGIN,
            "origin_server_ts": 1234,
            "pdus": [pdu("$a", json!(1))],
        })
    );

    let edu = json!({"edu_type": "m.typing", "content": {"room_id": "!r"}});
    let transaction = Transaction {
        edus: vec![edu.clone()],
        ..transaction
    };
    assert_eq!(transaction.body()["edus"], json!([edu]));
    assert_eq!(transaction.path(), "/_matrix/federation/v1/send/5");
}

#[test]
fn depth_filter() {
    let max = (1i64 << 53) - 1;
    let pdus = vec![
        pdu("$ok", json!(5)),
        pdu("$max", json!(max)),
        pdu("$min", json!(-max)),
        pdu("$too_big", json!(max + 1)),
        pdu("$too_small", json!(-max - 1)),
        pdu("$huge", json!(u64::MAX)),
        pdu("$string", json!("5")),
        json!({"event_id": "$none"}),
    ];
    let kept: Vec<_> = filter_pdus_for_valid_depth(&pdus)
        .iter()
        .map(|p| p["event_id"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(kept, ["$ok", "$max", "$min"]);
}

#[test]
fn acknowledged_skips_errors() {
    let response = json!({"pdus": {
        "$a": {},
        "$b": {"error": "nope"},
        "$c": "not a dict",
        "$d": {"other": 1},
    }});
    let acked = acknowledged_pdus("TX", response.as_object().unwrap());
    assert_eq!(acked, ["$a", "$d"]);

    // A `pdus` that is not a map is logged and ignored.
    let response = json!({"pdus": ["$a"]});
    assert!(acknowledged_pdus("TX", response.as_object().unwrap()).is_empty());
    assert!(acknowledged_pdus("TX", json!({}).as_object().unwrap()).is_empty());
}

#[test]
fn ids_count_up_from_the_clock() {
    let sender = sender(ScriptedTransport::new([]));
    let first = sender.build(DEST, vec![], vec![]);
    let second = sender.build(DEST, vec![], vec![]);
    let first_id: u64 = first.transaction_id.parse().unwrap();
    assert!(first_id >= crate::federation::client::tests::EPOCH_MS);
    assert_eq!(second.transaction_id, (first_id + 1).to_string());
    assert_eq!(first.origin, ORIGIN);
    assert_eq!(first.destination, DEST);
}

#[tokio::test]
async fn send_puts_the_body() {
    let transport = ScriptedTransport::new([Step::Respond(
        200,
        r#"{"pdus": {"$a": {}, "$b": {"error": "bad"}}}"#,
    )]);
    let sender = sender(Arc::clone(&transport));
    let transaction = sender.build(
        DEST,
        vec![pdu("$a", json!(1)), pdu("$b", json!(2))],
        vec![json!({"edu_type": "m.typing", "content": {}})],
    );

    let mut stats = RequestStats::default();
    let response = sender
        .send(transaction.clone(), Vec::new(), &mut stats)
        .await
        .unwrap();
    let TransactionResponse::Acknowledged(acked) = response else {
        panic!("expected a parsed response");
    };
    assert_eq!(acked, ["$a"]);
    assert_eq!(stats.attempts, 1);

    let seen = transport.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].method(), Method::PUT);
    assert_eq!(
        seen[0].uri().to_string(),
        format!(
            "matrix-federation://{DEST}/_matrix/federation/v1/send/{}",
            transaction.transaction_id
        )
    );
    let body: Value = serde_json::from_slice(seen[0].body()).unwrap();
    assert_eq!(body, transaction.body());
}

#[tokio::test(start_paused = true)]
async fn send_retries_server_errors() {
    // Long retries: a 500 is retried, and the second attempt succeeds.
    let transport = ScriptedTransport::new([
        Step::Respond(500, "{}"),
        Step::Respond(200, r#"{"pdus": {}}"#),
    ]);
    let sender = sender(Arc::clone(&transport));
    let transaction = sender.build(DEST, vec![pdu("$a", json!(1))], vec![]);

    let mut stats = RequestStats::default();
    sender
        .send(transaction.clone(), Vec::new(), &mut stats)
        .await
        .unwrap();
    assert_eq!(stats.attempts, 2);
    assert_eq!(stats.responses, [500, 200]);
}

#[tokio::test]
async fn send_fails_on_client_errors() {
    let transport = ScriptedTransport::new([Step::Respond(403, r#"{"errcode": "M_FORBIDDEN"}"#)]);
    let sender = sender(Arc::clone(&transport));
    let transaction = sender.build(DEST, vec![], vec![]);

    let err = sender
        .send(
            transaction.clone(),
            Vec::new(),
            &mut RequestStats::default(),
        )
        .await
        .unwrap_err();
    assert!(
        matches!(err, ClientError::Http(ref http) if http.code == 403),
        "{err}"
    );
}

#[tokio::test]
async fn send_rejects_a_non_object() {
    let transport = ScriptedTransport::new([Step::Respond(200, "[1]")]);
    let sender = sender(transport);
    let transaction = sender.build(DEST, vec![], vec![]);

    let err = sender
        .send(
            transaction.clone(),
            Vec::new(),
            &mut RequestStats::default(),
        )
        .await
        .unwrap_err();
    assert!(
        matches!(
            err,
            ClientError::SendFailed {
                cause: FailureCause::InvalidBody(_),
                can_retry: false
            }
        ),
        "{err}"
    );
}

#[tokio::test]
async fn send_hands_back_what_serde_refuses() {
    let transport = ScriptedTransport::new([Step::Respond(200, r#"{"pdus": {"$a": "\ud800"}}"#)]);
    let sender = sender(transport);
    let transaction = sender.build(DEST, vec![], vec![]);

    let response = sender
        .send(
            transaction.clone(),
            Vec::new(),
            &mut RequestStats::default(),
        )
        .await
        .unwrap();
    assert!(matches!(response, TransactionResponse::Unparsed(_)));
}
