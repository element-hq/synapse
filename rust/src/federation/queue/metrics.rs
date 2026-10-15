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

//! The counters the transmission loops bump.
//!
//! They are Python's Prometheus metrics. Bumping those from tokio would take
//! the GIL for every transaction, so the loops count here, and Python adds
//! the counts to its metrics when they are collected.

use std::{
    collections::{HashMap, HashSet},
    sync::Mutex,
};

use super::Sent;
use crate::federation::client::RequestStats;

/// What happened since the counts were last taken.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Counts {
    /// `synapse_federation_client_sent_transactions`.
    pub sent_transactions: u64,
    /// `synapse_federation_client_sent_edus_by_type`. Their sum is
    /// `synapse_federation_client_sent_edus`.
    pub sent_edus_by_type: HashMap<String, u64>,
    /// `synapse_http_matrixfederationclient_requests` for `PUT`.
    pub requests: u64,
    /// `synapse_http_matrixfederationclient_responses` for `PUT`, by code.
    pub responses: HashMap<u16, u64>,
    /// `synapse_federation_last_sent_pdu_time`, in milliseconds, for the
    /// destinations in `federation_metrics_domains`.
    pub last_sent_pdu_ts: HashMap<String, i64>,
}

/// The counts, shared by the queues of a sender.
pub struct QueueMetrics {
    counts: Mutex<Counts>,
    /// `federation_metrics_domains`.
    metrics_domains: HashSet<String>,
}

impl QueueMetrics {
    pub fn new(metrics_domains: HashSet<String>) -> Self {
        QueueMetrics {
            counts: Mutex::new(Counts::default()),
            metrics_domains,
        }
    }

    /// Count the requests a transaction took, sent or not.
    pub fn record_requests(&self, stats: &RequestStats) {
        let mut counts = self.counts.lock().expect("poisoned");
        add_requests(&mut counts, stats);
    }

    /// Count a sent transaction and its requests.
    pub fn record_sent(&self, destination: &str, sent: &Sent) {
        let mut counts = self.counts.lock().expect("poisoned");
        add_requests(&mut counts, &sent.stats);
        counts.sent_transactions += 1;
        for edu_type in &sent.edu_types {
            *counts
                .sent_edus_by_type
                .entry(edu_type.clone())
                .or_default() += 1;
        }
        if let Some(last) = sent.pdus.last() {
            if self.metrics_domains.contains(destination) {
                counts
                    .last_sent_pdu_ts
                    .insert(destination.to_owned(), last.origin_server_ts);
            }
        }
    }

    /// The counts since the last call.
    pub fn take(&self) -> Counts {
        std::mem::take(&mut *self.counts.lock().expect("poisoned"))
    }
}

fn add_requests(counts: &mut Counts, stats: &RequestStats) {
    counts.requests += u64::from(stats.attempts);
    for code in &stats.responses {
        *counts.responses.entry(*code).or_default() += 1;
    }
}
