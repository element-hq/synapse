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

use std::{
    collections::{HashMap, VecDeque},
    sync::Mutex,
};

use async_trait::async_trait;
use http::Response;
use http_body_util::{BodyExt, Full};
use serde_json::json;
use tokio::time::Instant;

use super::{
    limiter::{RetryTimings, TimingsUpdate},
    *,
};
use crate::clock::TokioSleeper;

/// What the scripted transport does with the next request.
pub(crate) enum Step {
    Respond(u16, &'static str),
    Fail(fn() -> TransportError),
    /// Never answer.
    Hang,
}

#[derive(Default)]
pub(crate) struct ScriptedTransport {
    steps: Mutex<VecDeque<Step>>,
    seen: Mutex<Vec<(Request<Bytes>, Instant)>>,
}

impl ScriptedTransport {
    pub(crate) fn new(steps: impl IntoIterator<Item = Step>) -> Arc<Self> {
        Arc::new(ScriptedTransport {
            steps: Mutex::new(steps.into_iter().collect()),
            seen: Mutex::default(),
        })
    }

    pub(crate) fn seen(&self) -> Vec<Request<Bytes>> {
        self.seen
            .lock()
            .unwrap()
            .iter()
            .map(|(r, _)| r.clone())
            .collect()
    }

    /// When each request arrived, relative to `start`.
    fn times(&self, start: Instant) -> Vec<Duration> {
        self.seen
            .lock()
            .unwrap()
            .iter()
            .map(|(_, at)| *at - start)
            .collect()
    }
}

#[async_trait]
impl FederationTransport for ScriptedTransport {
    async fn send(
        &self,
        request: Request<Bytes>,
    ) -> Result<Response<ResponseBody>, TransportError> {
        self.seen.lock().unwrap().push((request, Instant::now()));
        let step = self
            .steps
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected request");
        match step {
            Step::Respond(status, body) => {
                let mut response = Response::new(
                    Full::new(Bytes::from_static(body.as_bytes()))
                        .map_err(|never| match never {})
                        .boxed_unsync(),
                );
                *response.status_mut() = StatusCode::from_u16(status).unwrap();
                response.headers_mut().insert(
                    header::CONTENT_TYPE,
                    HeaderValue::from_static("application/json"),
                );
                Ok(response)
            }
            Step::Fail(err) => Err(err()),
            Step::Hang => std::future::pending().await,
        }
    }
}

#[derive(Default)]
pub(crate) struct MemoryTimings {
    rows: Mutex<HashMap<String, RetryTimings>>,
    pub(crate) updates: Mutex<Vec<(String, TimingsUpdate)>>,
}

#[async_trait]
impl RetryTimingsStore for MemoryTimings {
    async fn get(&self, destination: &str) -> anyhow::Result<Option<RetryTimings>> {
        Ok(self.rows.lock().unwrap().get(destination).copied())
    }

    fn set(&self, destination: &str, update: TimingsUpdate) {
        self.updates
            .lock()
            .unwrap()
            .push((destination.to_owned(), update));
    }
}

pub(crate) const ORIGIN: &str = "origin.example";
pub(crate) const DEST: &str = "remote.example";

fn signing_key() -> FederationSigningKey {
    FederationSigningKey::new("ed25519", "k1", &[7u8; 32]).unwrap()
}

fn config() -> ClientConfig {
    ClientConfig {
        server_name: ORIGIN.to_owned(),
        signing_key: signing_key(),
        user_agent: HeaderValue::from_static("Synapse/test"),
        default_timeout: Duration::from_secs(60),
        max_long_retries: 10,
        max_short_retries: 3,
        max_long_retry_delay: Duration::from_secs(60),
        max_short_retry_delay: Duration::from_secs(2),
        domain_whitelist: None,
        backoff: BackoffConfig {
            min_retry_interval_ms: 600_000,
            retry_multiplier: 2.0,
            max_retry_interval_ms: 7 * 24 * 3_600_000,
        },
    }
}

/// The virtual epoch the client's clock starts at.
pub(crate) const EPOCH_MS: u64 = 1_000_000;

fn client_with(
    config: ClientConfig,
    transport: Arc<ScriptedTransport>,
    timings: Arc<MemoryTimings>,
) -> FederationClient {
    let start = Instant::now();
    FederationClient::new(
        config,
        transport,
        timings,
        Arc::new(TokioSleeper),
        Arc::new(move || EPOCH_MS + start.elapsed().as_millis() as u64),
    )
    .with_jitter(1.0)
}

pub(crate) fn new_client(
    transport: Arc<ScriptedTransport>,
) -> (FederationClient, Arc<MemoryTimings>) {
    let timings = Arc::new(MemoryTimings::default());
    (
        client_with(config(), transport, Arc::clone(&timings)),
        timings,
    )
}

fn get(path: &str) -> FederationRequest {
    FederationRequest {
        method: Method::GET,
        destination: DEST.to_owned(),
        path: path.to_owned(),
        query: String::new(),
        body: None,
        rewrite_age_ts: false,
        extra_headers: Vec::new(),
    }
}

fn put(path: &str, body: Value) -> FederationRequest {
    FederationRequest {
        method: Method::PUT,
        body: Some(body),
        ..get(path)
    }
}

async fn send(
    client: &FederationClient,
    request: &FederationRequest,
    options: &RequestOptions,
) -> Result<SentResponse, ClientError> {
    client
        .send(request, options, &mut RequestStats::default())
        .await
}

async fn body_of(client: &FederationClient, response: SentResponse) -> Bytes {
    let mut body = BytesMut::new();
    client
        .read_body(response.body, None, Duration::from_secs(60), |chunk| {
            body.extend_from_slice(&chunk);
            Ok(())
        })
        .await
        .unwrap();
    body.freeze()
}

#[tokio::test(start_paused = true)]
async fn signs_and_sends() {
    let transport = ScriptedTransport::new([Step::Respond(200, r#"{"ok":true}"#)]);
    let (client, timings) = new_client(Arc::clone(&transport));

    let mut request = put("/_matrix/federation/v1/send/1", json!({"b": 1, "a": "é"}));
    request.query = "x=1&y=2".to_owned();
    let response = send(&client, &request, &RequestOptions::default())
        .await
        .unwrap();
    assert_eq!(response.status, StatusCode::OK);
    assert_eq!(&body_of(&client, response).await[..], br#"{"ok":true}"#);

    let [sent] = &transport.seen()[..] else {
        panic!("expected one request");
    };
    assert_eq!(sent.method(), Method::PUT);
    assert_eq!(
        sent.uri().to_string(),
        "matrix-federation://remote.example/_matrix/federation/v1/send/1?x=1&y=2"
    );
    assert_eq!(sent.headers()[header::USER_AGENT], "Synapse/test");
    assert_eq!(sent.headers()[header::CONTENT_TYPE], "application/json");
    // Canonical JSON: sorted keys, UTF-8 as is.
    assert_eq!(&sent.body()[..], "{\"a\":\"é\",\"b\":1}".as_bytes());

    let body = CanonicalBody::new(&json!({"b": 1, "a": "é"})).unwrap();
    let expected = make_auth_header(
        &signing_key(),
        ORIGIN,
        "PUT",
        "/_matrix/federation/v1/send/1?x=1&y=2",
        Destination::Server(DEST),
        Some(&body),
    )
    .unwrap();
    assert_eq!(sent.headers()[header::AUTHORIZATION], expected.as_str());

    // No backoff to clear.
    assert!(timings.updates.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn empty_body_is_no_body() {
    let transport = ScriptedTransport::new([Step::Respond(200, "{}")]);
    let (client, _) = new_client(Arc::clone(&transport));

    send(&client, &put("/p", json!({})), &RequestOptions::default())
        .await
        .unwrap();

    let sent = &transport.seen()[0];
    assert!(sent.body().is_empty());
    assert!(!sent.headers().contains_key(header::CONTENT_TYPE));
    let expected = make_auth_header(
        &signing_key(),
        ORIGIN,
        "PUT",
        "/p",
        Destination::Server(DEST),
        None,
    )
    .unwrap();
    assert_eq!(sent.headers()[header::AUTHORIZATION], expected.as_str());
}

#[tokio::test(start_paused = true)]
async fn short_retries_on_5xx() {
    let transport = ScriptedTransport::new([
        Step::Respond(502, ""),
        Step::Respond(503, ""),
        Step::Respond(500, ""),
        Step::Respond(500, r#"{"errcode":"M_UNKNOWN"}"#),
    ]);
    let (client, timings) = new_client(Arc::clone(&transport));
    let start = Instant::now();

    let mut stats = RequestStats::default();
    let err = client
        .send(&get("/p"), &RequestOptions::default(), &mut stats)
        .await
        .err()
        .unwrap();

    let ClientError::SendFailed {
        cause: FailureCause::Http(http),
        can_retry: true,
    } = err
    else {
        panic!("unexpected error {err:?}");
    };
    assert_eq!(http.code, 500);
    assert_eq!(&http.body[..], br#"{"errcode":"M_UNKNOWN"}"#);

    // 0.5 s, 1 s, 2 s apart.
    assert_eq!(
        transport.times(start),
        [0, 500, 1500, 3500].map(Duration::from_millis)
    );
    assert_eq!(stats.attempts, 4);
    assert_eq!(stats.responses, [502, 503, 500, 500]);

    // The failure starts the backoff.
    let updates = timings.updates.lock().unwrap();
    let [(dest, update)] = &updates[..] else {
        panic!("expected one update");
    };
    assert_eq!(dest, DEST);
    assert_eq!(update.retry_interval, 600_000);
    assert_eq!(update.failure_ts, Some(EPOCH_MS + 3500));
}

#[tokio::test(start_paused = true)]
async fn long_retries_grow_to_the_cap() {
    let transport =
        ScriptedTransport::new(std::iter::repeat_with(|| Step::Respond(500, "")).take(11));
    let (client, _) = new_client(Arc::clone(&transport));
    let start = Instant::now();

    let options = RequestOptions {
        long_retries: true,
        ..Default::default()
    };
    send(&client, &get("/p"), &options).await.err().unwrap();

    let times = transport.times(start);
    let gaps: Vec<u64> = times.windows(2).map(|w| (w[1] - w[0]).as_secs()).collect();
    assert_eq!(gaps, [4, 16, 60, 60, 60, 60, 60, 60, 60, 60]);
}

#[tokio::test(start_paused = true)]
async fn explicit_timeout_means_no_retries() {
    let transport = ScriptedTransport::new([Step::Respond(500, "")]);
    let (client, _) = new_client(Arc::clone(&transport));

    let options = RequestOptions {
        timeout: Some(Duration::from_secs(10)),
        ..Default::default()
    };
    send(&client, &get("/p"), &options).await.err().unwrap();
    assert_eq!(transport.seen().len(), 1);
}

#[tokio::test(start_paused = true)]
async fn timeout_fails_the_attempt_and_retries() {
    let transport = ScriptedTransport::new([Step::Hang, Step::Respond(200, "{}")]);
    let (client, _) = new_client(Arc::clone(&transport));
    let start = Instant::now();

    send(&client, &get("/p"), &RequestOptions::default())
        .await
        .unwrap();
    assert_eq!(
        transport.times(start),
        [Duration::ZERO, Duration::from_millis(60_500)]
    );
}

#[tokio::test(start_paused = true)]
async fn client_errors_are_not_retried() {
    let transport = ScriptedTransport::new([Step::Respond(403, r#"{"errcode":"M_FORBIDDEN"}"#)]);
    let (client, timings) = new_client(Arc::clone(&transport));

    let err = send(&client, &get("/p"), &RequestOptions::default())
        .await
        .err()
        .unwrap();
    let ClientError::Http(http) = err else {
        panic!("unexpected error {err:?}");
    };
    assert_eq!(http.code, 403);
    assert_eq!(http.phrase, "Forbidden");
    assert_eq!(http.errcode().as_deref(), Some("M_FORBIDDEN"));
    // A 403 does not start a backoff.
    assert!(timings.updates.lock().unwrap().is_empty());

    // Unless asked to back off on all errors.
    let transport = ScriptedTransport::new([Step::Respond(403, "")]);
    let (client, timings) = new_client(Arc::clone(&transport));
    let options = RequestOptions {
        backoff_on_all_error_codes: true,
        ..Default::default()
    };
    send(&client, &get("/p"), &options).await.err().unwrap();
    assert_eq!(timings.updates.lock().unwrap().len(), 1);
}

#[tokio::test(start_paused = true)]
async fn dns_failure() {
    let dns = || TransportError::Dns(anyhow::anyhow!("no such host"));

    // Retried by default.
    let transport = ScriptedTransport::new([Step::Fail(dns), Step::Respond(200, "{}")]);
    let (client, _) = new_client(Arc::clone(&transport));
    send(&client, &get("/p"), &RequestOptions::default())
        .await
        .unwrap();

    // Not when the caller says not to.
    let transport = ScriptedTransport::new([Step::Fail(dns)]);
    let (client, _) = new_client(Arc::clone(&transport));
    let options = RequestOptions {
        retry_on_dns_fail: false,
        ..Default::default()
    };
    let err = send(&client, &get("/p"), &options).await.err().unwrap();
    assert!(
        matches!(
            err,
            ClientError::SendFailed {
                cause: FailureCause::Transport(TransportError::Dns(_)),
                can_retry: false
            }
        ),
        "{err:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn trailing_slash_on_400_unrecognized() {
    let transport = ScriptedTransport::new([
        Step::Respond(400, r#"{"errcode":"M_UNRECOGNIZED"}"#),
        Step::Respond(200, "{}"),
    ]);
    let (client, _) = new_client(Arc::clone(&transport));

    let options = RequestOptions {
        try_trailing_slash_on_400: true,
        ..Default::default()
    };
    send(&client, &get("/p"), &options).await.unwrap();
    let paths: Vec<String> = transport
        .seen()
        .iter()
        .map(|r| r.uri().path().to_owned())
        .collect();
    assert_eq!(paths, ["/p", "/p/"]);

    // Only for that errcode.
    let transport = ScriptedTransport::new([Step::Respond(400, r#"{"errcode":"M_BAD_JSON"}"#)]);
    let (client, _) = new_client(Arc::clone(&transport));
    send(&client, &get("/p"), &options).await.err().unwrap();
    assert_eq!(transport.seen().len(), 1);
}

#[tokio::test(start_paused = true)]
async fn backing_off_refuses_the_request() {
    let transport = ScriptedTransport::new([]);
    let timings = Arc::new(MemoryTimings::default());
    timings.rows.lock().unwrap().insert(
        DEST.to_owned(),
        RetryTimings {
            failure_ts: Some(EPOCH_MS - 1000),
            retry_last_ts: EPOCH_MS - 1000,
            retry_interval: 600_000,
        },
    );
    let client = client_with(config(), transport, timings);

    let err = send(&client, &get("/p"), &RequestOptions::default())
        .await
        .err()
        .unwrap();
    assert!(
        matches!(
            err,
            ClientError::NotRetrying(NotRetrying {
                retry_interval: 600_000,
                ..
            })
        ),
        "{err:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn success_after_failures_clears_the_backoff() {
    let transport = ScriptedTransport::new([Step::Respond(200, "{}")]);
    let timings = Arc::new(MemoryTimings::default());
    timings.rows.lock().unwrap().insert(
        DEST.to_owned(),
        RetryTimings {
            failure_ts: Some(1),
            retry_last_ts: 1,
            retry_interval: 600_000,
        },
    );
    let client = client_with(config(), transport, Arc::clone(&timings));

    send(&client, &get("/p"), &RequestOptions::default())
        .await
        .unwrap();
    let updates = timings.updates.lock().unwrap();
    assert_eq!(updates.len(), 1);
    assert!(updates[0].1.server_up);
    assert_eq!(updates[0].1.retry_interval, 0);
}

#[tokio::test(start_paused = true)]
async fn denied_destinations() {
    let transport = ScriptedTransport::new([]);
    let (client, _) = new_client(Arc::clone(&transport));
    let mut request = get("/p");
    request.destination = "bad name".to_owned();
    let err = send(&client, &request, &RequestOptions::default())
        .await
        .err()
        .unwrap();
    assert!(matches!(err, ClientError::Denied), "{err:?}");

    let mut config = config();
    config.domain_whitelist = Some(HashSet::from(["other.example".to_owned()]));
    let client = client_with(config, transport, Arc::default());
    let err = send(&client, &get("/p"), &RequestOptions::default())
        .await
        .err()
        .unwrap();
    assert!(matches!(err, ClientError::Denied), "{err:?}");
}

#[tokio::test(start_paused = true)]
async fn wake_destination_ends_the_retry_sleep() {
    let transport = ScriptedTransport::new([
        Step::Respond(500, ""),
        Step::Respond(500, ""),
        Step::Respond(200, "{}"),
    ]);
    let (client, _) = new_client(Arc::clone(&transport));
    let client = Arc::new(client);
    let start = Instant::now();

    let options = RequestOptions {
        long_retries: true,
        ..Default::default()
    };
    let task = tokio::spawn({
        let client = Arc::clone(&client);
        async move { send(&client, &get("/p"), &options).await.map(|_| ()) }
    });

    // The first retry would wait 4 s. Wake it after 1 s.
    tokio::time::sleep(Duration::from_secs(1)).await;
    client.wake_destination(DEST);
    task.await.unwrap().unwrap();

    // The second retry sleeps its full 16 s.
    assert_eq!(transport.times(start), [0, 1, 17].map(Duration::from_secs));
}

#[tokio::test(start_paused = true)]
async fn rewrites_age_ts_on_each_attempt() {
    let transport = ScriptedTransport::new([Step::Respond(500, ""), Step::Respond(200, "{}")]);
    let (client, _) = new_client(Arc::clone(&transport));

    let mut request = put(
        "/send/1",
        json!({
            "pdus": [
                {"event_id": "$a", "age_ts": EPOCH_MS - 100},
                {"event_id": "$b", "unsigned": {"x": 1}, "age_ts": EPOCH_MS - 200},
                {"event_id": "$c"},
            ],
        }),
    );
    request.rewrite_age_ts = true;
    send(&client, &request, &RequestOptions::default())
        .await
        .unwrap();

    let bodies: Vec<Value> = transport
        .seen()
        .iter()
        .map(|r| serde_json::from_slice(r.body()).unwrap())
        .collect();
    assert_eq!(
        bodies[0]["pdus"],
        json!([
            {"event_id": "$a", "unsigned": {"age": 100}},
            {"event_id": "$b", "unsigned": {"x": 1, "age": 200}},
            {"event_id": "$c"},
        ])
    );
    // 500 ms later, on the retry.
    assert_eq!(bodies[1]["pdus"][0]["unsigned"]["age"], 600);
}

#[tokio::test(start_paused = true)]
async fn shutdown_stops_retries() {
    let transport = ScriptedTransport::new([Step::Respond(500, "")]);
    let (client, timings) = new_client(Arc::clone(&transport));
    let client = Arc::new(client);

    let options = RequestOptions {
        long_retries: true,
        ..Default::default()
    };
    let task = tokio::spawn({
        let client = Arc::clone(&client);
        async move { send(&client, &get("/p"), &options).await.map(|_| ()) }
    });
    tokio::time::sleep(Duration::from_secs(1)).await;
    client.shutdown();

    // The sleep in progress runs out, then the loop stops.
    let err = task.await.unwrap().unwrap_err();
    assert!(matches!(err, ClientError::Shutdown), "{err:?}");
    assert_eq!(transport.seen().len(), 1);
    assert!(timings.updates.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn read_body_limit_and_timeout() {
    let transport = ScriptedTransport::new([Step::Respond(200, "0123456789")]);
    let (client, _) = new_client(Arc::clone(&transport));
    let response = send(&client, &get("/p"), &RequestOptions::default())
        .await
        .unwrap();

    // A body that reaches the limit fails, as in Python.
    let err = client
        .read_body(response.body, Some(10), Duration::from_secs(1), |_| Ok(()))
        .await
        .unwrap_err();
    assert!(
        matches!(
            err,
            ClientError::SendFailed {
                cause: FailureCause::TooLarge { limit: 10 },
                can_retry: false
            }
        ),
        "{err:?}"
    );

    let stalled = http_body_util::StreamBody::new(futures::stream::pending::<
        Result<hyper::body::Frame<Bytes>, crate::federation::transport::BoxError>,
    >())
    .boxed_unsync();
    let err = client
        .read_body(stalled, None, Duration::from_secs(5), |_| Ok(()))
        .await
        .unwrap_err();
    assert!(
        matches!(
            err,
            ClientError::SendFailed {
                cause: FailureCause::BodyTimeout,
                can_retry: true
            }
        ),
        "{err:?}"
    );
}
