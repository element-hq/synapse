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

//! The per-destination backoff: Python's `get_retry_limiter` and
//! `RetryDestinationLimiter`.
//!
//! [`RetryLimiter::check`] refuses a destination we are backing off from.
//! [`RetryLimiter::finish`] works out the new timings from how the request
//! went, as the limiter's `__exit__` does, and hands them to the
//! [`RetryTimingsStore`] to persist in the background.

use async_trait::async_trait;

/// A row of the `destinations` table, as `get_destination_retry_timings`
/// returns it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RetryTimings {
    pub failure_ts: Option<u64>,
    pub retry_last_ts: u64,
    pub retry_interval: u64,
}

/// New timings to write, as `set_destination_retry_timings` takes them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TimingsUpdate {
    pub failure_ts: Option<u64>,
    pub retry_last_ts: u64,
    pub retry_interval: u64,
    /// The destination was failing and now is not. The writer tells the
    /// notifier and the other workers, as the Python limiter does.
    pub server_up: bool,
}

/// Where the retry timings live.
#[async_trait]
pub trait RetryTimingsStore: Send + Sync + 'static {
    /// The destination's timings, or `None` if we are not backing off from
    /// it.
    async fn get(&self, destination: &str) -> anyhow::Result<Option<RetryTimings>>;

    /// Persist new timings. Python does this in the background and logs a
    /// failure, so this returns nothing.
    fn set(&self, destination: &str, update: TimingsUpdate);
}

/// The backoff settings from the federation config.
#[derive(Clone, Copy, Debug)]
pub struct BackoffConfig {
    pub min_retry_interval_ms: u64,
    pub retry_multiplier: f64,
    pub max_retry_interval_ms: u64,
}

/// How to treat errors, from the request's options.
#[derive(Clone, Copy, Debug, Default)]
pub struct LimiterOptions {
    pub ignore_backoff: bool,
    pub backoff_on_404: bool,
    pub backoff_on_all_error_codes: bool,
}

/// We are backing off from the destination.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NotRetrying {
    pub retry_last_ts: u64,
    pub retry_interval: u64,
}

/// How the request went, as far as the limiter cares.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// A 2xx response.
    Success,
    /// The remote answered with this error status, raised as an
    /// `HttpResponseException`. Python's limiter sees a
    /// `CodeMessageException`.
    HttpError(u16),
    /// Anything else: no response, a 5xx or 429 after the retries (which
    /// Python wraps in `RequestSendFailed`), or a bug.
    Failure,
}

/// The limiter for one request to one destination.
#[derive(Debug)]
pub struct RetryLimiter {
    failure_ts: Option<u64>,
    retry_interval: u64,
    options: LimiterOptions,
}

impl RetryLimiter {
    /// Read the destination's timings, and refuse it if we are backing off
    /// from it and the request does not ignore that.
    pub async fn check(
        store: &dyn RetryTimingsStore,
        destination: &str,
        now_ms: u64,
        options: LimiterOptions,
    ) -> anyhow::Result<Result<RetryLimiter, NotRetrying>> {
        let timings = store.get(destination).await?;

        let (failure_ts, retry_interval) = match timings {
            Some(timings) => {
                if !options.ignore_backoff
                    && timings.retry_last_ts.saturating_add(timings.retry_interval) > now_ms
                {
                    return Ok(Err(NotRetrying {
                        retry_last_ts: timings.retry_last_ts,
                        retry_interval: timings.retry_interval,
                    }));
                }
                (timings.failure_ts, timings.retry_interval)
            }
            None => (None, 0),
        };

        Ok(Ok(RetryLimiter {
            failure_ts,
            retry_interval,
            options,
        }))
    }

    /// The new timings after a request that ended with `outcome`, if they
    /// change. `jitter` is drawn from U(0.8, 1.4) in production.
    pub fn finish(
        self,
        outcome: Outcome,
        now_ms: u64,
        config: &BackoffConfig,
        jitter: f64,
    ) -> Option<TimingsUpdate> {
        let previously_failing = self.failure_ts.is_some_and(|ts| ts != 0);

        let (failure_ts, retry_last_ts, retry_interval) = match outcome {
            Outcome::Success => {
                if self.retry_interval == 0 {
                    return None;
                }
                (None, 0, 0)
            }
            Outcome::HttpError(code) if self.is_valid_error_code(code) => {
                // A response that might be valid. We leave the timings
                // alone, since a deprovisioned server may answer every
                // request with a 404 or 403.
                return None;
            }
            Outcome::HttpError(_) | Outcome::Failure => {
                // Ignoring the backoff also means not growing it, or a server
                // that was briefly down could quickly reach the maximum.
                if self.options.ignore_backoff {
                    return None;
                }

                let retry_interval = if self.retry_interval != 0 {
                    let grown =
                        (self.retry_interval as f64 * config.retry_multiplier * jitter) as u64;
                    grown.min(config.max_retry_interval_ms)
                } else {
                    config.min_retry_interval_ms
                };

                // Python only sets `failure_ts` when it is `None`, so a zero
                // from the database stays zero.
                let failure_ts = self.failure_ts.or(Some(now_ms));
                (failure_ts, now_ms, retry_interval)
            }
        };

        let currently_failing = failure_ts.is_some_and(|ts| ts != 0);

        Some(TimingsUpdate {
            failure_ts,
            retry_last_ts,
            retry_interval,
            server_up: previously_failing && !currently_failing,
        })
    }

    /// Whether an error response leaves the backoff alone.
    fn is_valid_error_code(&self, code: u16) -> bool {
        let backs_off = self.options.backoff_on_all_error_codes
            || (code == 404 && self.options.backoff_on_404)
            || code == 401
            || code == 429
            || code >= 500;
        !backs_off
    }
}

#[cfg(test)]
mod tests {
    use std::{collections::HashMap, sync::Mutex};

    use super::*;

    const CONFIG: BackoffConfig = BackoffConfig {
        min_retry_interval_ms: 10 * 60 * 1000,
        retry_multiplier: 2.0,
        max_retry_interval_ms: 7 * 24 * 60 * 60 * 1000,
    };

    #[derive(Default)]
    struct MemoryStore {
        rows: Mutex<HashMap<String, RetryTimings>>,
    }

    #[async_trait]
    impl RetryTimingsStore for MemoryStore {
        async fn get(&self, destination: &str) -> anyhow::Result<Option<RetryTimings>> {
            Ok(self.rows.lock().unwrap().get(destination).copied())
        }

        fn set(&self, _destination: &str, _update: TimingsUpdate) {}
    }

    fn store_with(timings: RetryTimings) -> MemoryStore {
        let store = MemoryStore::default();
        store.rows.lock().unwrap().insert("d".to_owned(), timings);
        store
    }

    async fn limiter(store: &MemoryStore, now: u64, options: LimiterOptions) -> RetryLimiter {
        RetryLimiter::check(store, "d", now, options)
            .await
            .unwrap()
            .unwrap()
    }

    #[tokio::test]
    async fn refuses_while_backing_off() {
        let store = store_with(RetryTimings {
            failure_ts: Some(1000),
            retry_last_ts: 1000,
            retry_interval: 500,
        });

        let refused = RetryLimiter::check(&store, "d", 1499, LimiterOptions::default())
            .await
            .unwrap()
            .unwrap_err();
        assert_eq!(
            refused,
            NotRetrying {
                retry_last_ts: 1000,
                retry_interval: 500
            }
        );

        // Due now.
        limiter(&store, 1500, LimiterOptions::default()).await;

        // Or ignoring the backoff.
        let options = LimiterOptions {
            ignore_backoff: true,
            ..Default::default()
        };
        limiter(&store, 1499, options).await;
    }

    #[tokio::test]
    async fn first_failure_sets_the_minimum() {
        let store = MemoryStore::default();
        let update = limiter(&store, 5000, LimiterOptions::default())
            .await
            .finish(Outcome::Failure, 5000, &CONFIG, 1.0)
            .unwrap();
        assert_eq!(
            update,
            TimingsUpdate {
                failure_ts: Some(5000),
                retry_last_ts: 5000,
                retry_interval: CONFIG.min_retry_interval_ms,
                server_up: false,
            }
        );
    }

    #[tokio::test]
    async fn later_failures_grow_up_to_the_maximum() {
        let store = store_with(RetryTimings {
            failure_ts: Some(1000),
            retry_last_ts: 1000,
            retry_interval: 1000,
        });
        let update = limiter(&store, 9000, LimiterOptions::default())
            .await
            .finish(Outcome::Failure, 9000, &CONFIG, 1.25)
            .unwrap();
        assert_eq!(update.retry_interval, 2500);
        // The original failure time is kept.
        assert_eq!(update.failure_ts, Some(1000));
        assert_eq!(update.retry_last_ts, 9000);

        let store = store_with(RetryTimings {
            failure_ts: Some(1000),
            retry_last_ts: 1000,
            retry_interval: CONFIG.max_retry_interval_ms,
        });
        let update = limiter(&store, u64::MAX / 2, LimiterOptions::default())
            .await
            .finish(Outcome::Failure, 9000, &CONFIG, 1.4)
            .unwrap();
        assert_eq!(update.retry_interval, CONFIG.max_retry_interval_ms);
    }

    #[tokio::test]
    async fn success_clears_the_backoff_and_reports_the_server_up() {
        let store = store_with(RetryTimings {
            failure_ts: Some(1000),
            retry_last_ts: 1000,
            retry_interval: 500,
        });
        let update = limiter(&store, 2000, LimiterOptions::default())
            .await
            .finish(Outcome::Success, 2000, &CONFIG, 1.0)
            .unwrap();
        assert_eq!(
            update,
            TimingsUpdate {
                failure_ts: None,
                retry_last_ts: 0,
                retry_interval: 0,
                server_up: true,
            }
        );

        // Nothing to clear.
        let store = MemoryStore::default();
        let update = limiter(&store, 2000, LimiterOptions::default())
            .await
            .finish(Outcome::Success, 2000, &CONFIG, 1.0);
        assert_eq!(update, None);
    }

    #[tokio::test]
    async fn error_codes() {
        let store = &MemoryStore::default();
        let finish = |options, code| async move {
            limiter(store, 0, options)
                .await
                .finish(Outcome::HttpError(code), 0, &CONFIG, 1.0)
                .is_some()
        };

        let default = LimiterOptions::default();
        assert!(!finish(default, 400).await);
        assert!(!finish(default, 403).await);
        assert!(!finish(default, 404).await);
        assert!(finish(default, 401).await);
        assert!(finish(default, 429).await);
        assert!(finish(default, 500).await);

        let on_404 = LimiterOptions {
            backoff_on_404: true,
            ..default
        };
        assert!(finish(on_404, 404).await);
        assert!(!finish(on_404, 403).await);

        let on_all = LimiterOptions {
            backoff_on_all_error_codes: true,
            ..default
        };
        assert!(finish(on_all, 400).await);
    }

    #[tokio::test]
    async fn ignoring_the_backoff_does_not_grow_it() {
        let store = MemoryStore::default();
        let options = LimiterOptions {
            ignore_backoff: true,
            ..Default::default()
        };
        let update = limiter(&store, 0, options)
            .await
            .finish(Outcome::Failure, 0, &CONFIG, 1.0);
        assert_eq!(update, None);
    }
}
