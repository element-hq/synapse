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

//! `.well-known/matrix/server` delegation, as Python's `WellKnownResolver`.
//!
//! The caching rules are Python's, constant for constant. Two things are
//! new: each fetch attempt has a timeout (Python's has none), and
//! concurrent lookups of one server name share a single fetch. That fetch
//! runs as its own task, so it finishes and caches its result even if every
//! request waiting on it gives up.

use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{Duration, UNIX_EPOCH},
};

use anyhow::anyhow;
use async_trait::async_trait;
use bytes::Bytes;
use futures::{
    future::{BoxFuture, Shared},
    FutureExt,
};
use http::{header, HeaderMap, StatusCode, Uri};
use log::{info, warn};
use rand::RngExt;

use super::Now;
use crate::clock::Sleeper;

/// How long to cache a `.well-known` result by default.
const DEFAULT_CACHE_PERIOD: f64 = 24.0 * 3600.0;
/// Jitter on the default and failure cache periods.
const CACHE_PERIOD_JITTER: f64 = 0.1;
/// How long to cache a failure to fetch.
const INVALID_CACHE_PERIOD: f64 = 3600.0;
/// How long to cache a failure to fetch if there was recently a valid result.
const DOWN_CACHE_PERIOD: f64 = 2.0 * 60.0;
/// How long to remember there was a valid result, after it expires.
const REMEMBER_DOMAIN_HAD_VALID: f64 = 2.0 * 3600.0;
/// Bounds on a cache period taken from the response headers.
const MAX_CACHE_PERIOD: f64 = 48.0 * 3600.0;
const MIN_CACHE_PERIOD: f64 = 5.0 * 60.0;
/// The largest `.well-known` body we accept.
pub const MAX_SIZE: usize = 50 * 1024;
/// Refetch this fraction of the TTL before a cached result expires.
const GRACE_PERIOD_FACTOR: f64 = 0.2;
/// Attempts to fetch for a server that recently had a valid result.
const RETRY_ATTEMPTS: u32 = 3;
/// The pause between those attempts.
const RETRY_DELAY: Duration = Duration::from_millis(500);
/// The most redirects we follow, as Twisted's `RedirectAgent`.
const MAX_REDIRECTS: usize = 20;

/// One HTTP response, with the body read in full.
#[derive(Clone, Debug)]
pub struct FetchResponse {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: Bytes,
}

#[derive(Debug)]
pub enum FetchError {
    /// The body was longer than [`MAX_SIZE`].
    TooLarge,
    Other(anyhow::Error),
}

/// Makes one `GET` request, without following redirects. The agent's
/// implementation goes through the agent's own connection pool.
#[async_trait]
pub trait WellKnownFetcher: Send + Sync + 'static {
    async fn fetch(&self, uri: &Uri) -> Result<FetchResponse, FetchError>;
}

#[derive(Clone, Debug)]
struct CachedResult {
    /// The delegated server name, if any.
    delegated: Option<String>,
    expiry: f64,
    ttl: f64,
}

#[derive(Default)]
struct CacheState {
    results: HashMap<String, CachedResult>,
    /// Whether each server recently had a valid `.well-known`, and until
    /// when we remember that.
    had_valid: HashMap<String, (bool, f64)>,
}

impl CacheState {
    fn result(&self, server_name: &str, now: f64) -> Option<&CachedResult> {
        self.results.get(server_name).filter(|r| r.expiry > now)
    }

    fn had_valid(&self, server_name: &str, now: f64) -> bool {
        self.had_valid
            .get(server_name)
            .is_some_and(|&(valid, expiry)| valid && expiry > now)
    }

    fn insert_result(&mut self, server_name: &str, result: CachedResult, now: f64) {
        // Drop expired entries, as Python's `TTLCache` does on every access,
        // so the maps only hold live entries.
        self.results.retain(|_, r| r.expiry > now);
        self.results.insert(server_name.to_owned(), result);
    }

    fn insert_had_valid(&mut self, server_name: &str, valid: bool, expiry: f64, now: f64) {
        self.had_valid.retain(|_, &mut (_, e)| e > now);
        self.had_valid
            .insert(server_name.to_owned(), (valid, expiry));
    }
}

/// Why a fetch failed. A temporary failure (no response, or a 5xx) keeps
/// serving a cached result. A permanent one (a bad response) does not.
#[derive(Debug)]
struct FetchFailure {
    temporary: bool,
}

pub struct WellKnownResolver {
    fetcher: Arc<dyn WellKnownFetcher>,
    sleeper: Arc<dyn Sleeper>,
    now: Now,
    attempt_timeout: Duration,
    state: Mutex<CacheState>,
    in_flight: Mutex<HashMap<String, Shared<BoxFuture<'static, Option<String>>>>>,
}

impl WellKnownResolver {
    pub fn new(
        fetcher: Arc<dyn WellKnownFetcher>,
        sleeper: Arc<dyn Sleeper>,
        now: Now,
        attempt_timeout: Duration,
    ) -> Self {
        WellKnownResolver {
            fetcher,
            sleeper,
            now,
            attempt_timeout,
            state: Mutex::new(CacheState::default()),
            in_flight: Mutex::new(HashMap::new()),
        }
    }

    fn now_secs(&self) -> f64 {
        (self.now)() as f64 / 1000.0
    }

    /// The server `server_name` delegates federation to, if any.
    pub async fn get_well_known(self: &Arc<Self>, server_name: &str) -> Option<String> {
        let now = self.now_secs();
        let prev = {
            let state = self.state.lock().expect("poisoned");
            match state.result(server_name, now) {
                Some(cached) if now < cached.expiry - GRACE_PERIOD_FACTOR * cached.ttl => {
                    return cached.delegated.clone();
                }
                Some(cached) => cached.delegated.clone(),
                None => None,
            }
        };

        let lookup = {
            let mut in_flight = self.in_flight.lock().expect("poisoned");
            in_flight
                .entry(server_name.to_owned())
                .or_insert_with(|| {
                    let this = self.clone();
                    let server_name = server_name.to_owned();
                    // A future only its callers poll would stall when they
                    // all go away, and resume on the next request with its
                    // fetch timeout long expired.
                    let task = tokio::spawn(async move {
                        let _done = InFlightGuard {
                            resolver: &this,
                            server_name: &server_name,
                        };
                        this.lookup(&server_name, prev).await
                    });
                    task.map(|joined| {
                        joined.unwrap_or_else(|err| {
                            warn!("Well-known lookup failed: {err}");
                            None
                        })
                    })
                    .boxed()
                    .shared()
                })
                .clone()
        };

        lookup.await
    }

    /// Fetch, and cache what we learn. `prev` is the cached delegation we
    /// are refreshing, if any.
    async fn lookup(&self, server_name: &str, prev: Option<String>) -> Option<String> {
        let (result, cache_period) = match self.fetch_well_known(server_name).await {
            Ok(fetched) => fetched,
            Err(failure) => {
                if failure.temporary && prev.is_some() {
                    // Keep serving the cached result; hopefully the server
                    // is back next time we ask.
                    return prev;
                }

                let had_valid = self
                    .state
                    .lock()
                    .expect("poisoned")
                    .had_valid(server_name, self.now_secs());
                let period = if had_valid {
                    DOWN_CACHE_PERIOD
                } else {
                    INVALID_CACHE_PERIOD
                };
                (None, period * jitter())
            }
        };

        if cache_period > 0.0 {
            let now = self.now_secs();
            self.state.lock().expect("poisoned").insert_result(
                server_name,
                CachedResult {
                    delegated: result.clone(),
                    expiry: now + cache_period,
                    ttl: cache_period,
                },
                now,
            );
        }

        result
    }

    /// Fetch and parse, without consulting the result cache. Returns the
    /// delegated server (if any) and how long to cache that for.
    async fn fetch_well_known(
        &self,
        server_name: &str,
    ) -> Result<(Option<String>, f64), FetchFailure> {
        let had_valid = self
            .state
            .lock()
            .expect("poisoned")
            .had_valid(server_name, self.now_secs());

        let response = self.make_request(server_name, had_valid).await?;

        let delegated = parse_response(&response).map_err(|err| {
            info!("Error parsing well-known for {server_name}: {err:#}");
            FetchFailure { temporary: false }
        })?;
        info!("Response from .well-known for {server_name}: {delegated:?}");

        let now = self.now_secs();
        let cache_period = match cache_period_from_headers(&response.headers, now) {
            None => DEFAULT_CACHE_PERIOD * jitter(),
            Some(period) => period.clamp(MIN_CACHE_PERIOD, MAX_CACHE_PERIOD),
        };

        self.state.lock().expect("poisoned").insert_had_valid(
            server_name,
            delegated.is_some(),
            now + cache_period + REMEMBER_DOMAIN_HAD_VALID,
            now,
        );

        Ok((delegated, cache_period))
    }

    /// Fetch the file, retrying on failure (no response or a 5xx) only if
    /// `retry`. A non-5xx response is returned whatever its status.
    async fn make_request(
        &self,
        server_name: &str,
        retry: bool,
    ) -> Result<FetchResponse, FetchFailure> {
        let uri: Uri = format!("https://{server_name}/.well-known/matrix/server")
            .parse()
            .map_err(|err| {
                info!("Invalid well-known URI for {server_name}: {err}");
                FetchFailure { temporary: true }
            })?;

        let mut attempt = 0;
        loop {
            attempt += 1;
            info!("Fetching {uri}");

            let error = match tokio::time::timeout(
                self.attempt_timeout,
                self.fetch_following_redirects(&uri),
            )
            .await
            {
                Ok(Ok(response)) if !response.status.is_server_error() => return Ok(response),
                Ok(Ok(response)) => anyhow!("Non-200 response {}", response.status.as_u16()),
                Ok(Err(FetchError::TooLarge)) => {
                    // Not worth fetching again, but may be temporary.
                    warn!("Requested .well-known file for {server_name} is too large > {MAX_SIZE} bytes");
                    return Err(FetchFailure { temporary: true });
                }
                Ok(Err(FetchError::Other(err))) => err,
                Err(_) => anyhow!("timed out after {:?}", self.attempt_timeout),
            };

            if !retry || attempt >= RETRY_ATTEMPTS {
                info!("Error fetching {uri}: {error:#}");
                return Err(FetchFailure { temporary: true });
            }
            info!("Error fetching {uri}: {error:#}. Retrying");

            // Pause briefly in the hope that it comes back up.
            self.sleeper.sleep(RETRY_DELAY).await;
        }
    }

    /// One `GET`, following redirects as Twisted's `RedirectAgent` does.
    async fn fetch_following_redirects(&self, uri: &Uri) -> Result<FetchResponse, FetchError> {
        let mut uri = uri.clone();
        for _ in 0..=MAX_REDIRECTS {
            let response = self.fetcher.fetch(&uri).await?;

            if !matches!(response.status.as_u16(), 301 | 302 | 303 | 307 | 308) {
                return Ok(response);
            }

            let location = response
                .headers
                .get(header::LOCATION)
                .and_then(|l| l.to_str().ok())
                .ok_or_else(|| FetchError::Other(anyhow!("redirect with no Location")))?;
            let next = url::Url::parse(&uri.to_string())
                .and_then(|base| base.join(location))
                .map_err(|err| FetchError::Other(anyhow!("bad redirect {location:?}: {err}")))?;
            uri = next
                .as_str()
                .parse()
                .map_err(|err| FetchError::Other(anyhow!("bad redirect {location:?}: {err}")))?;
        }

        Err(FetchError::Other(anyhow!("too many redirects")))
    }
}

/// Removes a lookup from `in_flight` when it finishes, or panics.
struct InFlightGuard<'a> {
    resolver: &'a WellKnownResolver,
    server_name: &'a str,
}

impl Drop for InFlightGuard<'_> {
    fn drop(&mut self) {
        if let Ok(mut in_flight) = self.resolver.in_flight.lock() {
            in_flight.remove(self.server_name);
        }
    }
}

fn jitter() -> f64 {
    rand::rng().random_range((1.0 - CACHE_PERIOD_JITTER)..=(1.0 + CACHE_PERIOD_JITTER))
}

/// The delegated server name in a `.well-known` response.
///
/// Python also rejects a non-200 status and a missing, non-string or
/// non-ASCII `m.server`. We also reject a value that is not a valid URI
/// authority, which Python would only discover when the request built from
/// it failed. An empty `m.server` means no delegation.
fn parse_response(response: &FetchResponse) -> anyhow::Result<Option<String>> {
    if response.status != StatusCode::OK {
        anyhow::bail!("Non-200 response {}", response.status.as_u16());
    }

    let body: serde_json::Value = serde_json::from_slice(&response.body)?;
    let server = body
        .get("m.server")
        .and_then(|s| s.as_str())
        .ok_or_else(|| anyhow!("no string m.server"))?;

    if server.is_empty() {
        return Ok(None);
    }
    if !server.is_ascii() {
        anyhow::bail!("non-ASCII m.server {server:?}");
    }
    server
        .parse::<http::uri::Authority>()
        .map_err(|err| anyhow!("invalid m.server {server:?}: {err}"))?;

    Ok(Some(server.to_owned()))
}

/// The cache period the response headers ask for, in seconds: `no-store`
/// is zero, then `max-age`, then `Expires`. As Python's
/// `_cache_period_from_headers`.
fn cache_period_from_headers(headers: &HeaderMap, now_secs: f64) -> Option<f64> {
    let cache_control = parse_cache_control(headers);

    if cache_control.contains_key("no-store") {
        return Some(0.0);
    }

    if let Some(Some(max_age)) = cache_control.get("max-age") {
        if let Ok(max_age) = max_age.parse::<i64>() {
            return Some(max_age as f64);
        }
    }

    if let Some(expires) = headers.get_all(header::EXPIRES).iter().next_back() {
        // An invalid date (notably "0") means already expired (RFC 7234).
        return Some(
            expires
                .to_str()
                .ok()
                .and_then(|e| httpdate::parse_http_date(e).ok())
                .map(|expires| {
                    let expires = expires
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs_f64();
                    expires - now_secs
                })
                .unwrap_or(0.0),
        );
    }

    None
}

fn parse_cache_control(headers: &HeaderMap) -> HashMap<String, Option<String>> {
    let mut directives = HashMap::new();
    for value in headers.get_all(header::CACHE_CONTROL) {
        let Ok(value) = value.to_str() else { continue };
        for directive in value.split(',') {
            let mut parts = directive.splitn(2, '=').map(str::trim);
            let key = parts.next().unwrap_or("").to_lowercase();
            let value = parts.next().map(str::to_owned);
            directives.insert(key, value);
        }
    }
    directives
}

/// For tests, which need a fixed `SystemTime` for `Expires` headers.
#[cfg(test)]
fn http_date(secs: u64) -> String {
    httpdate::fmt_http_date(UNIX_EPOCH + Duration::from_secs(secs))
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};

    use http::HeaderValue;

    use super::*;
    use crate::clock::TokioSleeper;

    type Handler = dyn Fn(&Uri) -> Result<FetchResponse, FetchError> + Send + Sync;

    struct FakeFetcher {
        handler: Mutex<Box<Handler>>,
        calls: AtomicUsize,
    }

    impl FakeFetcher {
        fn new(
            handler: impl Fn(&Uri) -> Result<FetchResponse, FetchError> + Send + Sync + 'static,
        ) -> Arc<Self> {
            Arc::new(FakeFetcher {
                handler: Mutex::new(Box::new(handler)),
                calls: AtomicUsize::new(0),
            })
        }

        fn set(
            &self,
            handler: impl Fn(&Uri) -> Result<FetchResponse, FetchError> + Send + Sync + 'static,
        ) {
            *self.handler.lock().unwrap() = Box::new(handler);
        }

        fn calls(&self) -> usize {
            self.calls.swap(0, Ordering::SeqCst)
        }
    }

    #[async_trait]
    impl WellKnownFetcher for FakeFetcher {
        async fn fetch(&self, uri: &Uri) -> Result<FetchResponse, FetchError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            // Yield, so concurrent lookups overlap.
            tokio::task::yield_now().await;
            (self.handler.lock().unwrap())(uri)
        }
    }

    fn response(
        status: u16,
        body: &str,
        headers: &[(&str, &str)],
    ) -> Result<FetchResponse, FetchError> {
        let mut map = HeaderMap::new();
        for (name, value) in headers {
            map.append(
                http::HeaderName::from_bytes(name.as_bytes()).unwrap(),
                HeaderValue::from_str(value).unwrap(),
            );
        }
        Ok(FetchResponse {
            status: StatusCode::from_u16(status).unwrap(),
            headers: map,
            body: Bytes::from(body.to_owned()),
        })
    }

    /// A response delegating to `to`, cached for exactly two hours.
    fn delegate(to: &'static str) -> impl Fn(&Uri) -> Result<FetchResponse, FetchError> {
        move |_| {
            response(
                200,
                &format!(r#"{{"m.server": "{to}"}}"#),
                &[("cache-control", "max-age=7200")],
            )
        }
    }

    /// The same without cache headers, so cached for the jittered default.
    fn delegate_default(to: &'static str) -> impl Fn(&Uri) -> Result<FetchResponse, FetchError> {
        move |_| response(200, &format!(r#"{{"m.server": "{to}"}}"#), &[])
    }

    fn make_resolver(fetcher: Arc<FakeFetcher>) -> (Arc<WellKnownResolver>, Arc<AtomicU64>) {
        let clock = Arc::new(AtomicU64::new(1_000_000_000));
        let now_clock = clock.clone();
        let resolver = Arc::new(WellKnownResolver::new(
            fetcher,
            Arc::new(TokioSleeper),
            Arc::new(move || now_clock.load(Ordering::SeqCst)),
            Duration::from_secs(60),
        ));
        (resolver, clock)
    }

    fn advance(clock: &AtomicU64, secs: f64) {
        clock.fetch_add((secs * 1000.0) as u64, Ordering::SeqCst);
    }

    #[tokio::test(start_paused = true)]
    async fn valid_result_is_cached_with_jitter() {
        let fetcher = FakeFetcher::new(delegate_default("target:1234"));
        let (resolver, clock) = make_resolver(fetcher.clone());

        let uri_seen = Arc::new(Mutex::new(None));
        let seen = uri_seen.clone();
        fetcher.set(move |uri| {
            *seen.lock().unwrap() = Some(uri.to_string());
            delegate_default("target:1234")(uri)
        });

        assert_eq!(
            resolver.get_well_known("example.com").await.as_deref(),
            Some("target:1234")
        );
        assert_eq!(fetcher.calls(), 1);
        assert_eq!(
            uri_seen.lock().unwrap().as_deref(),
            Some("https://example.com/.well-known/matrix/server")
        );

        // The default period is 24h ±10%, refetched in its last 20%: so no
        // refetch at 24h × 0.9 × 0.8, and one by 24h × 1.1.
        advance(&clock, 24.0 * 3600.0 * 0.9 * 0.8 - 1.0);
        resolver.get_well_known("example.com").await;
        assert_eq!(fetcher.calls(), 0);

        advance(&clock, 24.0 * 3600.0 * 0.4);
        resolver.get_well_known("example.com").await;
        assert_eq!(fetcher.calls(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn missing_well_known_is_cached_for_an_hour() {
        let fetcher = FakeFetcher::new(|_| response(404, "", &[]));
        let (resolver, clock) = make_resolver(fetcher.clone());

        assert_eq!(resolver.get_well_known("example.com").await, None);
        assert_eq!(fetcher.calls(), 1);

        // 1h ±10%, refetched in the last 20%.
        advance(&clock, 3600.0 * 0.9 * 0.8 - 1.0);
        assert_eq!(resolver.get_well_known("example.com").await, None);
        assert_eq!(fetcher.calls(), 0);

        advance(&clock, 3600.0 * 0.4);
        assert_eq!(resolver.get_well_known("example.com").await, None);
        assert_eq!(fetcher.calls(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn headers_set_the_cache_period() {
        let fetcher = FakeFetcher::new(|_| {
            response(
                200,
                r#"{"m.server": "t"}"#,
                &[("cache-control", "max-age=7200")],
            )
        });
        let (resolver, clock) = make_resolver(fetcher.clone());

        resolver.get_well_known("example.com").await;
        assert_eq!(fetcher.calls(), 1);
        // Refetch starts at 80% of 2h.
        advance(&clock, 7200.0 * 0.8 - 1.0);
        resolver.get_well_known("example.com").await;
        assert_eq!(fetcher.calls(), 0);
        advance(&clock, 2.0);
        resolver.get_well_known("example.com").await;
        assert_eq!(fetcher.calls(), 1);
    }

    #[test]
    fn cache_period_parsing() {
        let headers = |pairs: &[(&str, &str)]| response(200, "", pairs).unwrap().headers;

        assert_eq!(cache_period_from_headers(&headers(&[]), 0.0), None);
        assert_eq!(
            cache_period_from_headers(&headers(&[("cache-control", "max-age=60, public")]), 0.0),
            Some(60.0)
        );
        assert_eq!(
            cache_period_from_headers(&headers(&[("Cache-Control", "No-Store, max-age=60")]), 0.0),
            Some(0.0)
        );
        // A bad max-age falls through to Expires.
        assert_eq!(
            cache_period_from_headers(
                &headers(&[
                    ("cache-control", "max-age=abc"),
                    ("expires", &http_date(1000))
                ]),
                400.0
            ),
            Some(600.0)
        );
        assert_eq!(
            cache_period_from_headers(&headers(&[("expires", "0")]), 400.0),
            Some(0.0)
        );
    }

    /// A short (or zero) period from the headers is raised to the minimum.
    #[tokio::test(start_paused = true)]
    async fn short_periods_are_clamped() {
        let fetcher = FakeFetcher::new(|_| {
            response(
                200,
                r#"{"m.server": "t"}"#,
                &[("cache-control", "no-store")],
            )
        });
        let (resolver, clock) = make_resolver(fetcher.clone());

        resolver.get_well_known("example.com").await;
        advance(&clock, 5.0 * 60.0 * 0.8 - 1.0);
        resolver.get_well_known("example.com").await;
        assert_eq!(fetcher.calls(), 1);
    }

    /// Once a server has had a valid `.well-known`, a failure is cached for
    /// only two minutes, fetching is retried, and a temporary failure keeps
    /// the cached result.
    #[tokio::test(start_paused = true)]
    async fn failures_after_a_valid_result() {
        let fetcher = FakeFetcher::new(delegate("target"));
        let (resolver, clock) = make_resolver(fetcher.clone());

        assert_eq!(
            resolver.get_well_known("example.com").await.as_deref(),
            Some("target")
        );
        assert_eq!(fetcher.calls(), 1);

        // In the grace period, the server is down: three attempts, and the
        // cached result is kept.
        fetcher.set(|_| response(503, "", &[]));
        advance(&clock, 7200.0 * 0.9);
        assert_eq!(
            resolver.get_well_known("example.com").await.as_deref(),
            Some("target")
        );
        assert_eq!(fetcher.calls(), 3);

        // After the result expires it is gone. We remember for another two
        // hours that it was valid, so we retry, and cache the failure for
        // 2m (±10%) rather than 1h.
        advance(&clock, 7200.0 * 0.1 + 1.0);
        assert_eq!(resolver.get_well_known("example.com").await, None);
        assert_eq!(fetcher.calls(), 3);

        fetcher.set(delegate("target"));
        advance(&clock, 2.0 * 60.0 * 0.9 * 0.8 - 1.0);
        assert_eq!(resolver.get_well_known("example.com").await, None);
        assert_eq!(fetcher.calls(), 0);
        advance(&clock, 2.0 * 60.0 * 0.4);
        assert_eq!(
            resolver.get_well_known("example.com").await.as_deref(),
            Some("target")
        );
        assert_eq!(fetcher.calls(), 1);
    }

    /// Without a recent valid result, a failure is tried once.
    #[tokio::test(start_paused = true)]
    async fn failures_without_a_valid_result_are_not_retried() {
        let fetcher = FakeFetcher::new(|_| response(503, "", &[]));
        let (resolver, _) = make_resolver(fetcher.clone());
        assert_eq!(resolver.get_well_known("example.com").await, None);
        assert_eq!(fetcher.calls(), 1);
    }

    /// A bad response is a permanent failure: the cached result is dropped.
    #[tokio::test(start_paused = true)]
    async fn invalid_response_replaces_cached_result() {
        let fetcher = FakeFetcher::new(delegate("target"));
        let (resolver, clock) = make_resolver(fetcher.clone());
        resolver.get_well_known("example.com").await;

        fetcher.set(|_| response(200, "not json", &[]));
        advance(&clock, 7200.0 * 0.9);
        assert_eq!(resolver.get_well_known("example.com").await, None);
        assert_eq!(fetcher.calls(), 2);
    }

    #[tokio::test(start_paused = true)]
    async fn bad_bodies() {
        for body in [
            r#"{}"#,
            r#"{"m.server": 1}"#,
            r#"{"m.server": "bad host/name"}"#,
            r#"{"m.server": "é.example"}"#,
            r#"{"m.server": ""}"#,
        ] {
            let fetcher = FakeFetcher::new(move |_| response(200, body, &[]));
            let (resolver, _) = make_resolver(fetcher);
            assert_eq!(resolver.get_well_known("example.com").await, None, "{body}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn too_large_is_not_retried() {
        let fetcher = FakeFetcher::new(delegate("target"));
        let (resolver, clock) = make_resolver(fetcher.clone());
        resolver.get_well_known("example.com").await;
        fetcher.calls();

        // Even though the server had a valid result.
        fetcher.set(|_| Err(FetchError::TooLarge));
        advance(&clock, 7200.0 * 0.9);
        assert_eq!(
            resolver.get_well_known("example.com").await.as_deref(),
            Some("target")
        );
        assert_eq!(fetcher.calls(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn redirects_are_followed() {
        let fetcher = FakeFetcher::new(|uri| match uri.path() {
            "/.well-known/matrix/server" => response(302, "", &[("location", "/elsewhere")]),
            "/elsewhere" => response(301, "", &[("location", "https://other.example/final")]),
            "/final" => {
                assert_eq!(uri.host(), Some("other.example"));
                response(200, r#"{"m.server": "target"}"#, &[])
            }
            other => panic!("unexpected {other}"),
        });
        let (resolver, _) = make_resolver(fetcher.clone());

        assert_eq!(
            resolver.get_well_known("example.com").await.as_deref(),
            Some("target")
        );
        assert_eq!(fetcher.calls(), 3);

        // A redirect loop gives up.
        let fetcher = FakeFetcher::new(|_| response(302, "", &[("location", "/loop")]));
        let (resolver, _) = make_resolver(fetcher.clone());
        assert_eq!(resolver.get_well_known("example.com").await, None);
        assert_eq!(fetcher.calls(), MAX_REDIRECTS + 1);
    }

    #[tokio::test(start_paused = true)]
    async fn concurrent_lookups_share_a_fetch() {
        let fetcher = FakeFetcher::new(delegate("target"));
        let (resolver, _) = make_resolver(fetcher.clone());

        let (a, b) = tokio::join!(
            resolver.get_well_known("example.com"),
            resolver.get_well_known("example.com")
        );
        assert_eq!(a.as_deref(), Some("target"));
        assert_eq!(b.as_deref(), Some("target"));
        assert_eq!(fetcher.calls(), 1);
        assert!(resolver.in_flight.lock().unwrap().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn abandoned_lookup_still_completes() {
        let fetcher = FakeFetcher::new(delegate("target"));
        let (resolver, _) = make_resolver(fetcher.clone());

        // Start a lookup, then give up on it before the fetch returns.
        {
            let lookup = std::pin::pin!(resolver.get_well_known("example.com"));
            assert!(futures::poll!(lookup).is_pending());
        }

        // The fetch carries on without us, and its result is cached.
        tokio::time::sleep(Duration::from_secs(1)).await;
        assert!(resolver.in_flight.lock().unwrap().is_empty());
        assert_eq!(fetcher.calls(), 1);
        assert_eq!(
            resolver.get_well_known("example.com").await.as_deref(),
            Some("target")
        );
        assert_eq!(fetcher.calls(), 0);
    }
}
