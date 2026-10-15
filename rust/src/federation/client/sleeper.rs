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
    waiting: Mutex<HashMap<String, Arc<Notify>>>,
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
        let notify = self.waiting.lock().expect("poisoned").remove(destination);
        if let Some(notify) = notify {
            notify.notify_waiters();
        }
    }

    /// Sleep for `duration`, or until `destination` is woken.
    pub async fn sleep(&self, destination: &str, duration: Duration) {
        let notify = Arc::clone(
            self.waiting
                .lock()
                .expect("poisoned")
                .entry(destination.to_owned())
                .or_default(),
        );

        {
            // Register before sleeping, so a wake between here and the
            // select is not lost.
            let notified = notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();

            tokio::select! {
                () = notified => {}
                () = self.sleeper.sleep(duration) => {}
            }
        }

        // Forget the destination if no one else is sleeping on it. The
        // map's reference and ours make two.
        let mut waiting = self.waiting.lock().expect("poisoned");
        if let Some(current) = waiting.get(destination) {
            if Arc::ptr_eq(current, &notify) && Arc::strong_count(&notify) == 2 {
                waiting.remove(destination);
            }
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
}
