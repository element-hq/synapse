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

//! Python's `AwakenableSleeper`: sleeps between retries that end early when
//! the destination is known to be back.

use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};

use tokio::sync::Notify;

use crate::clock::Sleeper;

/// Sleeps keyed on a destination, which [`AwakenableSleeper::wake`] ends.
pub struct AwakenableSleeper {
    sleeper: Arc<dyn Sleeper>,
    /// One entry per destination with a sleep in progress.
    waiting: Mutex<HashMap<String, Waiting>>,
}

/// The sleeps in progress for one destination.
#[derive(Default)]
struct Waiting {
    notify: Arc<Notify>,
    sleeps: usize,
}

impl AwakenableSleeper {
    pub fn new(sleeper: Arc<dyn Sleeper>) -> Self {
        AwakenableSleeper {
            sleeper,
            waiting: Mutex::new(HashMap::new()),
        }
    }

    /// End every sleep for `destination` now.
    pub fn wake(&self, destination: &str) {
        let waiting = self.waiting.lock().expect("poisoned").remove(destination);
        if let Some(waiting) = waiting {
            waiting.notify.notify_waiters();
        }
    }

    /// Sleep for `duration`, or until `destination` is woken.
    pub async fn sleep(&self, destination: &str, duration: Duration) {
        // Create the `Notified` while holding the lock. `notify_waiters` wakes
        // every `Notified` created before it is called, polled or not, so a
        // wake between here and the select is not lost.
        let (notify, notified) = {
            let mut waiting = self.waiting.lock().expect("poisoned");
            let entry = waiting.entry(destination.to_owned()).or_default();
            entry.sleeps += 1;
            let notify = Arc::clone(&entry.notify);
            let notified = Arc::clone(&notify).notified_owned();
            (notify, notified)
        };
        // Runs when the sleep ends or is cancelled.
        let _forget = Forget {
            waiting: &self.waiting,
            destination,
            notify,
        };

        tokio::select! {
            () = notified => {}
            () = self.sleeper.sleep(duration) => {}
        }
    }
}

/// Forgets a destination when the last sleep on it ends.
struct Forget<'a> {
    waiting: &'a Mutex<HashMap<String, Waiting>>,
    destination: &'a str,
    notify: Arc<Notify>,
}

impl Drop for Forget<'_> {
    fn drop(&mut self) {
        let mut waiting = self.waiting.lock().expect("poisoned");
        // A wake removes the entry, and a later sleep may have added a new
        // one. Only count down our own.
        let Some(entry) = waiting.get_mut(self.destination) else {
            return;
        };
        if !Arc::ptr_eq(&entry.notify, &self.notify) {
            return;
        }
        entry.sleeps -= 1;
        if entry.sleeps == 0 {
            waiting.remove(self.destination);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::TokioSleeper;

    #[tokio::test(start_paused = true)]
    async fn sleeps_for_the_duration() {
        let sleeper = AwakenableSleeper::new(Arc::new(TokioSleeper));
        let start = tokio::time::Instant::now();
        sleeper.sleep("a", Duration::from_secs(5)).await;
        assert_eq!(start.elapsed(), Duration::from_secs(5));
        assert!(sleeper.waiting.lock().unwrap().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn wake_ends_only_that_destination() {
        let sleeper = Arc::new(AwakenableSleeper::new(Arc::new(TokioSleeper)));
        let start = tokio::time::Instant::now();

        let a = tokio::spawn({
            let sleeper = Arc::clone(&sleeper);
            async move { sleeper.sleep("a", Duration::from_secs(60)).await }
        });
        let b = tokio::spawn({
            let sleeper = Arc::clone(&sleeper);
            async move { sleeper.sleep("b", Duration::from_secs(60)).await }
        });

        tokio::time::sleep(Duration::from_secs(1)).await;
        sleeper.wake("a");
        a.await.unwrap();
        assert_eq!(start.elapsed(), Duration::from_secs(1));
        assert!(!b.is_finished());

        b.await.unwrap();
        assert_eq!(start.elapsed(), Duration::from_secs(60));
        assert!(sleeper.waiting.lock().unwrap().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn cancelled_sleep_forgets_the_destination() {
        let sleeper = AwakenableSleeper::new(Arc::new(TokioSleeper));
        let sleep = sleeper.sleep("a", Duration::from_secs(60));
        let timed_out = tokio::time::timeout(Duration::from_secs(1), sleep).await;
        assert!(timed_out.is_err());
        assert!(sleeper.waiting.lock().unwrap().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn entry_outlives_the_first_of_two_sleeps() {
        let sleeper = Arc::new(AwakenableSleeper::new(Arc::new(TokioSleeper)));
        let start = tokio::time::Instant::now();

        let short = tokio::spawn({
            let sleeper = Arc::clone(&sleeper);
            async move { sleeper.sleep("a", Duration::from_secs(1)).await }
        });
        let long = tokio::spawn({
            let sleeper = Arc::clone(&sleeper);
            async move { sleeper.sleep("a", Duration::from_secs(60)).await }
        });

        short.await.unwrap();
        // The second sleep can still be woken.
        sleeper.wake("a");
        long.await.unwrap();
        assert_eq!(start.elapsed(), Duration::from_secs(1));
        assert!(sleeper.waiting.lock().unwrap().is_empty());
    }
}
