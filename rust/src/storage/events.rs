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

//! Loading events from the database, redactions included.
//!
//! Port of the read side of `EventsWorkerStore`: [`EventLoader::load`] is
//! `_get_events_from_db` behind `get_unredacted_events_from_cache_or_db`,
//! and [`EventLoader::get_events`] is `get_events_as_list`. Events read
//! from the database go into the [`EventCache`] as a [`CachedEvent`], which
//! holds the event as persisted and, when a valid redaction of it was found,
//! the redacted form, as Python's `EventCacheEntry` does. A reader picks
//! between them with a [`RedactBehaviour`].
//!
//! The loader runs its statements through a [`Querier`], so it works inside
//! the state resolution's long transaction as well as on a connection the
//! sync store has borrowed for one batch.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{bail, Context};
use async_trait::async_trait;
use log::{debug, error, info, warn};
use pyo3::{Py, PyResult, Python};
use tokio_postgres::{types::ToSql, Row};

use crate::event_auth::get_domain_from_id;
use crate::events::{
    constants::event_type::{M_ROOM_CREATE, M_ROOM_MEMBER, M_ROOM_REDACTION},
    internal_metadata::EventInternalMetadata,
    redact_event_py, Event,
};
use crate::room_versions::{EventFormatVersions, RoomVersion};
use crate::storage::caches::EventCache;

/// Runs statements on one database connection.
///
/// Implemented by the state resolution's transaction and by the sync
/// store's borrowed connections, so [`EventLoader`] serves both.
#[async_trait]
pub trait Querier: Send + Sync {
    async fn query(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> anyhow::Result<Vec<Row>>;
}

/// An event as the event cache holds it: the event as persisted, rejected
/// or not, and its redacted form if a valid redaction was known when it
/// was loaded. The auth event IDs are computed once, as state resolution
/// reads them many times.
///
/// An event handed in from Python shares the Python object's data,
/// including its internal metadata and unsigned data.
#[derive(Clone)]
pub struct CachedEvent(Arc<CachedEventInner>);

struct CachedEventInner {
    event: Event,
    /// The event with the redaction applied, when one was found.
    redacted: Option<Event>,
    auth_event_ids: Box<[Arc<str>]>,
}

impl CachedEvent {
    /// An event without a known redaction: one from memory rather than the
    /// database.
    pub fn new(event: Event) -> anyhow::Result<Self> {
        Self::with_redaction(event, None)
    }

    pub fn with_redaction(event: Event, redacted: Option<Event>) -> anyhow::Result<Self> {
        let auth_event_ids = auth_event_ids_of(&event)?;
        Ok(CachedEvent(Arc::new(CachedEventInner {
            event,
            redacted,
            auth_event_ids,
        })))
    }

    /// The event as persisted.
    pub fn event(&self) -> &Event {
        &self.0.event
    }

    /// The event with its redaction applied, if it has a valid one.
    pub fn redacted(&self) -> Option<&Event> {
        self.0.redacted.as_ref()
    }

    pub fn auth_event_ids(&self) -> &[Arc<str>] {
        &self.0.auth_event_ids
    }

    /// The event as `behaviour` wants it: `None` for [`RedactBehaviour::Block`]
    /// on a redacted event.
    pub fn select(&self, behaviour: RedactBehaviour) -> Option<&Event> {
        match (self.redacted(), behaviour) {
            (Some(_), RedactBehaviour::Block) => None,
            (Some(redacted), RedactBehaviour::Redact) => Some(redacted),
            _ => Some(self.event()),
        }
    }

    /// The event as a new Python object sharing this event's data.
    pub fn to_py(&self, py: Python<'_>) -> PyResult<Py<Event>> {
        Py::new(py, self.0.event.shallow_clone())
    }
}

impl std::ops::Deref for CachedEvent {
    type Target = Event;

    fn deref(&self) -> &Event {
        self.event()
    }
}

fn auth_event_ids_of(event: &Event) -> anyhow::Result<Box<[Arc<str>]>> {
    let ids = event
        .auth_event_ids()
        .map_err(|e| anyhow::anyhow!("{e}"))
        .with_context(|| format!("auth event IDs of {}", event.event_id()))?;
    Ok(ids.into_iter().map(Arc::from).collect())
}

/// What to do with a redacted event. `EventRedactBehaviour`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RedactBehaviour {
    /// The full event body.
    AsIs,
    /// The event with its body redacted.
    Redact,
    /// Leave the event out.
    Block,
}

impl RedactBehaviour {
    /// Parse the name of a member of Python's `EventRedactBehaviour`.
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "as_is" => Some(RedactBehaviour::AsIs),
            "redact" => Some(RedactBehaviour::Redact),
            "block" => Some(RedactBehaviour::Block),
            _ => None,
        }
    }
}

/// What an [`EventLoader`] did, for metrics.
#[derive(Debug, Default, Clone, Copy)]
pub struct EventLoadStats {
    /// Events loaded from the database.
    pub fetched: u64,
    /// Events found in the event cache.
    pub cached: u64,
    /// Time spent building events from rows: parsing the JSON and computing
    /// the event IDs.
    pub parse_time: Duration,
}

/// Loads events through `querier`, consulting `cache` first and filling it
/// with what it loads.
pub struct EventLoader<'a> {
    querier: &'a dyn Querier,
    cache: Option<&'a EventCache>,
}

/// How many event IDs to send per statement.
const EVENT_BATCH_SIZE: usize = 500;

const LOAD_EVENTS_SQL: &str = "
    SELECT e.event_id, e.stream_ordering, e.received_ts, e.instance_name,
        ej.internal_metadata, ej.json, ej.format_version, r.room_version,
        rej.reason, e.outlier
    FROM events AS e
    JOIN event_json AS ej USING (event_id)
    LEFT JOIN rooms AS r ON r.room_id = e.room_id
    LEFT JOIN rejections AS rej USING (event_id)
    WHERE e.event_id = ANY($1)
";

const LOAD_REDACTIONS_SQL: &str = "
    SELECT event_id, redacts, recheck FROM redactions WHERE redacts = ANY($1)
";

const LOAD_BAN_REDACTIONS_SQL: &str = "
    SELECT room_id, user_id, redacting_event_id, redact_end_ordering
    FROM room_ban_redactions
    WHERE room_id = ANY($1) AND user_id = ANY($2)
";

/// A row of [`LOAD_EVENTS_SQL`] with its redactions, as `_EventRow`.
struct FetchedEvent {
    event: Event,
    /// Redaction events that claim to redact this one and must be checked
    /// against it: room v3 and later redactions whose sender could not be
    /// verified when they were persisted, and MSC4293 ban redactions.
    unconfirmed_redactions: Vec<Arc<str>>,
    /// Redactions already known to be valid.
    confirmed_redactions: Vec<Arc<str>>,
}

impl<'a> EventLoader<'a> {
    pub fn new(querier: &'a dyn Querier, cache: Option<&'a EventCache>) -> Self {
        EventLoader { querier, cache }
    }

    /// `get_unredacted_events_from_cache_or_db` with `allow_rejected`:
    /// the events by ID, rejected ones included, from the cache or the
    /// database. Events that are not found are left out. The result can
    /// hold events that were not asked for: the redaction events read to
    /// check an unconfirmed redaction.
    pub async fn load(
        &self,
        ids: &[Arc<str>],
        stats: &mut EventLoadStats,
    ) -> anyhow::Result<HashMap<Arc<str>, CachedEvent>> {
        let mut events = HashMap::with_capacity(ids.len());
        let mut missing: Vec<Arc<str>> = Vec::new();
        for id in ids {
            if events.contains_key(id) || missing.contains(id) {
                continue;
            }
            match self.cache.and_then(|cache| cache.get(id)) {
                Some(event) => {
                    events.insert(Arc::clone(id), event);
                }
                None => missing.push(Arc::clone(id)),
            }
        }
        stats.cached += events.len() as u64;

        if !missing.is_empty() {
            let fetched = self.fetch_from_db(missing, stats).await?;
            stats.fetched += fetched.len() as u64;
            for (event_id, event) in fetched {
                if let Some(cache) = self.cache {
                    drop(cache.insert(Arc::clone(&event_id), event.clone()));
                }
                events.insert(event_id, event);
            }
        }

        Ok(events)
    }

    /// `get_events_as_list`: the events in the order of `ids`, each as
    /// `behaviour` wants it, leaving out those that are not found, those
    /// that were rejected unless `allow_rejected`, and redaction events
    /// whose target is missing or which turn out not to be authorised. With
    /// `prev_content`, a state event carries the content and sender of the
    /// state it replaced in its unsigned data.
    pub async fn get_events(
        &self,
        ids: &[Arc<str>],
        behaviour: RedactBehaviour,
        allow_rejected: bool,
        prev_content: bool,
        stats: &mut EventLoadStats,
    ) -> anyhow::Result<Vec<Event>> {
        let events = self
            .get_events_without_prev_content(ids, behaviour, allow_rejected, stats)
            .await?;
        if prev_content {
            self.add_prev_content(&events, stats).await?;
        }
        Ok(events)
    }

    /// [`EventLoader::get_events`] without the previous content, which it
    /// loads through this.
    async fn get_events_without_prev_content(
        &self,
        ids: &[Arc<str>],
        behaviour: RedactBehaviour,
        allow_rejected: bool,
        stats: &mut EventLoadStats,
    ) -> anyhow::Result<Vec<Event>> {
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        let entries = self.load(ids, stats).await?;

        // A redaction is only served once its target is known to exist and
        // the redaction to be authorised, which can mean rechecking it now.
        let mut redacted_ids: Vec<Arc<str>> = Vec::new();
        if !allow_rejected {
            for id in ids {
                let Some(entry) = entries.get(id) else {
                    continue;
                };
                if entry.type_() == M_ROOM_REDACTION {
                    if let Some(redacts) = entry.redacts().and_then(|v| v.as_str()) {
                        redacted_ids.push(Arc::from(redacts));
                    }
                }
            }
        }
        let originals = if redacted_ids.is_empty() {
            HashMap::new()
        } else {
            self.load(&redacted_ids, stats).await?
        };

        let mut events = Vec::with_capacity(ids.len());
        for id in ids {
            let Some(entry) = entries.get(id) else {
                continue;
            };
            if !allow_rejected {
                if entry.rejected_reason().is_some() {
                    continue;
                }
                if entry.type_() == M_ROOM_REDACTION && !redaction_is_servable(entry, &originals)? {
                    continue;
                }
            }
            let Some(event) = entry.select(behaviour) else {
                continue;
            };
            events.push(event.shallow_clone());
        }
        Ok(events)
    }

    /// The `get_prev_content` part of `get_events_as_list`: give each state
    /// event the content and sender of the state it replaced, unless it has
    /// them already. The unsigned data is shared with any Python object of
    /// the event, so this shows wherever the event is held, as in Python.
    async fn add_prev_content(
        &self,
        events: &[Event],
        stats: &mut EventLoadStats,
    ) -> anyhow::Result<()> {
        let mut wanted: Vec<(usize, Arc<str>)> = Vec::new();
        for (i, event) in events.iter().enumerate() {
            let unsigned = event.unsigned_ref();
            if let Some(replaces) = unsigned.replaces_state().map_err(py_err)? {
                if !unsigned.has_prev_content_and_sender().map_err(py_err)? {
                    wanted.push((i, Arc::from(replaces)));
                }
            }
        }
        if wanted.is_empty() {
            return Ok(());
        }
        let ids: Vec<Arc<str>> = wanted.iter().map(|(_, id)| Arc::clone(id)).collect();
        let prevs = self
            .get_events_without_prev_content(&ids, RedactBehaviour::Redact, false, stats)
            .await?;
        let prevs: HashMap<&str, &Event> = prevs.iter().map(|e| (e.event_id(), e)).collect();
        for (i, prev_id) in wanted {
            if let Some(prev) = prevs.get(&*prev_id) {
                let content = serde_json::to_value(prev.content())
                    .context("serializing the previous content")?;
                events[i]
                    .unsigned_ref()
                    .set_prev_content_and_sender(content, prev.sender())
                    .map_err(py_err)?;
            }
        }
        Ok(())
    }

    /// `_get_events_from_db`: load `ids` and whatever redaction events are
    /// needed to check their unconfirmed redactions, and decide each
    /// event's redaction.
    async fn fetch_from_db(
        &self,
        ids: Vec<Arc<str>>,
        stats: &mut EventLoadStats,
    ) -> anyhow::Result<HashMap<Arc<str>, CachedEvent>> {
        let mut fetched: HashMap<Arc<str>, FetchedEvent> = HashMap::with_capacity(ids.len());
        let mut tried: HashSet<Arc<str>> = HashSet::with_capacity(ids.len());
        let mut to_fetch = ids;
        while !to_fetch.is_empty() {
            let mut rows = self.fetch_rows(&to_fetch, stats).await?;
            // A redaction whose sender could not be verified at persist time
            // is checked here, which needs the redaction event itself.
            let mut redaction_ids: HashSet<Arc<str>> = HashSet::new();
            for id in to_fetch.drain(..) {
                if let Some(row) = rows.remove(&id) {
                    if row.confirmed_redactions.is_empty() {
                        redaction_ids.extend(row.unconfirmed_redactions.iter().cloned());
                    }
                    fetched.insert(Arc::clone(&id), row);
                }
                tried.insert(id);
            }
            to_fetch = redaction_ids
                .into_iter()
                .filter(|id| !tried.contains(id))
                .collect();
            if !to_fetch.is_empty() {
                debug!("Also fetching redaction events {to_fetch:?}");
            }
        }

        let mut redactions: Vec<(Arc<str>, Option<Event>)> = Vec::with_capacity(fetched.len());
        for (event_id, row) in &fetched {
            let redacted = maybe_redact(
                &row.event,
                &row.unconfirmed_redactions,
                &row.confirmed_redactions,
                &fetched,
            )?;
            redactions.push((Arc::clone(event_id), redacted));
        }

        let mut events = HashMap::with_capacity(fetched.len());
        for (event_id, redacted) in redactions {
            let row = fetched.remove(&event_id).expect("built from the same map");
            match CachedEvent::with_redaction(row.event, redacted) {
                Ok(event) => {
                    events.insert(event_id, event);
                }
                Err(e) => {
                    error!("Unable to load event {event_id} from the database: {e:#}");
                }
            }
        }
        Ok(events)
    }

    /// `_fetch_event_rows`: the events' rows with their redactions, parsed.
    /// Events that are not found, or whose row cannot be read, are left out.
    async fn fetch_rows(
        &self,
        ids: &[Arc<str>],
        stats: &mut EventLoadStats,
    ) -> anyhow::Result<HashMap<Arc<str>, FetchedEvent>> {
        let mut fetched = HashMap::with_capacity(ids.len());
        for batch in ids.chunks(EVENT_BATCH_SIZE) {
            let batch_ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = self.querier.query(LOAD_EVENTS_SQL, &[&batch_ids]).await?;
            let start = Instant::now();
            let mut batch_events: HashMap<Arc<str>, FetchedEvent> =
                HashMap::with_capacity(rows.len());
            for row in rows {
                if let Some((event_id, event)) = event_from_row(&row)? {
                    batch_events.insert(
                        event_id,
                        FetchedEvent {
                            event,
                            unconfirmed_redactions: Vec::new(),
                            confirmed_redactions: Vec::new(),
                        },
                    );
                }
            }
            stats.parse_time += start.elapsed();

            let rows = self
                .querier
                .query(LOAD_REDACTIONS_SQL, &[&batch_ids])
                .await?;
            for row in rows {
                let redacter: String = row.try_get(0)?;
                let redacted: String = row.try_get(1)?;
                let recheck: Option<bool> = row.try_get(2)?;
                if let Some(event) = batch_events.get_mut(&*redacted) {
                    let list = if recheck.unwrap_or(false) {
                        &mut event.unconfirmed_redactions
                    } else {
                        &mut event.confirmed_redactions
                    };
                    list.push(Arc::from(redacter));
                }
            }

            // MSC4293: a ban or kick can redact the user's events in the
            // room up to it.
            let mut room_ids: Vec<&str> = Vec::new();
            let mut user_ids: Vec<&str> = Vec::new();
            for event in batch_events.values() {
                room_ids.push(event.event.room_id());
                user_ids.push(event.event.sender());
            }
            room_ids.sort_unstable();
            room_ids.dedup();
            user_ids.sort_unstable();
            user_ids.dedup();
            if !room_ids.is_empty() {
                let rows = self
                    .querier
                    .query(LOAD_BAN_REDACTIONS_SQL, &[&room_ids, &user_ids])
                    .await?;
                for row in rows {
                    let room_id: String = row.try_get(0)?;
                    let user_id: String = row.try_get(1)?;
                    let redacting_event_id: String = row.try_get(2)?;
                    let redact_end_ordering: Option<i64> = row.try_get(3)?;
                    let redacting_event_id: Arc<str> = Arc::from(redacting_event_id);
                    for event in batch_events.values_mut() {
                        if event.event.room_id() != room_id || event.event.sender() != user_id {
                            continue;
                        }
                        // The redaction ends at the membership event that
                        // ends it: events from then on are not redacted.
                        // Backfilled events have negative stream orderings,
                        // so they always are.
                        if let Some(end) = redact_end_ordering.filter(|end| *end != 0) {
                            if stream_ordering_of(&event.event) >= end {
                                continue;
                            }
                        }
                        event
                            .unconfirmed_redactions
                            .push(Arc::clone(&redacting_event_id));
                    }
                }
            }

            fetched.extend(batch_events);
        }
        Ok(fetched)
    }
}

fn py_err(e: pyo3::PyErr) -> anyhow::Error {
    anyhow::anyhow!("{e}")
}

fn stream_ordering_of(event: &Event) -> i64 {
    event
        .internal_metadata()
        .stream_ordering()
        .map(i64::from)
        .unwrap_or(0)
}

/// The part of `get_events_as_list` that decides whether a redaction event
/// is served, given `originals`, the events it may redact: its target must
/// be known and not rejected, not the create event, in the same room, and
/// from the redaction's own server when the redaction has not been
/// authorised yet. An authorised redaction is marked as such, so the check
/// is not repeated.
fn redaction_is_servable(
    redaction: &CachedEvent,
    originals: &HashMap<Arc<str>, CachedEvent>,
) -> anyhow::Result<bool> {
    let Some(redacts) = redaction.redacts().and_then(|v| v.as_str()) else {
        // A redacted redaction has no `redacts` key: withhold it.
        debug!(
            "Withholding redaction event {} as we don't have redacts key",
            redaction.event_id()
        );
        return Ok(false);
    };
    let original = originals
        .get(redacts)
        .filter(|original| original.rejected_reason().is_none());
    let Some(original) = original else {
        // The redaction is assumed unauthorised until its target turns up,
        // when it is rechecked.
        debug!(
            "Withholding redaction event {} since we don't (yet) have the original {redacts}",
            redaction.event_id()
        );
        return Ok(false);
    };
    if original.type_() == M_ROOM_CREATE {
        info!(
            "Withholding redaction {} of create event {redacts}",
            redaction.event_id()
        );
        return Ok(false);
    }
    if original.room_id() != redaction.room_id() {
        info!(
            "Withholding redaction {} of event {redacts} from a different room",
            redaction.event_id()
        );
        return Ok(false);
    }
    if redaction
        .internal_metadata()
        .need_to_check_redaction()
        .map_err(py_err)?
    {
        let original_domain = domain_of(original.sender())?;
        let redaction_domain = domain_of(redaction.sender())?;
        if original_domain != redaction_domain {
            info!(
                "Withholding redaction {} whose sender domain {redaction_domain} doesn't match that of redacted event {redacts} {original_domain}",
                redaction.event_id()
            );
            return Ok(false);
        }
        redaction
            .internal_metadata()
            .set_recheck_redaction(false)
            .map_err(py_err)?;
    }
    Ok(true)
}

/// `_maybe_redact_event_row`: the redacted form of `event` if one of the
/// redactions claiming it is valid. A confirmed redaction is taken as it
/// is. An unconfirmed one must be among `fetched`, not rejected, in the
/// same room, and when it has not been authorised yet, from the sender's
/// own server, in which case it is marked authorised.
fn maybe_redact(
    event: &Event,
    unconfirmed_redactions: &[Arc<str>],
    confirmed_redactions: &[Arc<str>],
    fetched: &HashMap<Arc<str>, FetchedEvent>,
) -> anyhow::Result<Option<Event>> {
    if event.type_() == M_ROOM_CREATE {
        // Redactions of create events are ignored.
        return Ok(None);
    }

    if let Some(redaction_id) = confirmed_redactions.first() {
        return Ok(Some(redact(event, redaction_id)?));
    }

    for redaction_id in unconfirmed_redactions {
        let redaction = fetched
            .get(redaction_id)
            .map(|row| &row.event)
            .filter(|redaction| redaction.rejected_reason().is_none());
        let Some(redaction) = redaction else {
            debug!(
                "{} was redacted by {redaction_id} but redaction not found/authed",
                event.event_id()
            );
            continue;
        };
        if redaction.room_id() != event.room_id() {
            debug!(
                "{} was redacted by {redaction_id} but redaction was in a different room!",
                event.event_id()
            );
            continue;
        }
        // From room version 3, a redaction received before its target is
        // checked on read instead.
        if redaction
            .internal_metadata()
            .need_to_check_redaction()
            .map_err(py_err)?
        {
            if domain_of(redaction.sender())? == domain_of(event.sender())? {
                redaction
                    .internal_metadata()
                    .set_recheck_redaction(false)
                    .map_err(py_err)?;
            } else {
                debug!(
                    "{} was redacted by {redaction_id} but the senders don't match",
                    event.event_id()
                );
                continue;
            }
        }
        debug!("Redacting {} due to {redaction_id}", event.event_id());
        return Ok(Some(redact(event, redaction_id)?));
    }

    Ok(None)
}

/// `prune_event` plus `redacted_by`. The `redacted_because` of the
/// unsigned data is added when the event is serialized, as in Python.
fn redact(event: &Event, redaction_id: &str) -> anyhow::Result<Event> {
    let redacted = redact_event_py(event)
        .map_err(py_err)
        .with_context(|| format!("redacting {}", event.event_id()))?;
    redacted
        .internal_metadata()
        .set_redacted_by(Some(redaction_id.to_owned()))
        .map_err(py_err)?;
    Ok(redacted)
}

fn domain_of(user_id: &str) -> anyhow::Result<&str> {
    get_domain_from_id(user_id).map_err(|e| anyhow::anyhow!("{e}"))
}

/// Build an [`Event`] from a row of [`LOAD_EVENTS_SQL`], the way
/// `_get_events_from_db` does.
///
/// Returns `None` for rows the Python skips: an unknown room version, an
/// event in the wrong format for its room, or JSON that does not parse.
fn event_from_row(row: &Row) -> anyhow::Result<Option<(Arc<str>, Event)>> {
    let event_id: String = row.try_get(0)?;
    let stream_ordering: Option<i64> = row.try_get(1)?;
    let received_ts: Option<i64> = row.try_get(2)?;
    let instance_name: Option<String> = row.try_get(3)?;
    let internal_metadata: String = row.try_get(4)?;
    let json: String = row.try_get(5)?;
    let format_version: Option<i32> = row.try_get(6)?;
    let room_version_id: Option<String> = row.try_get(7)?;
    let rejected_reason: Option<String> = row.try_get(8)?;
    let outlier: bool = row.try_get(9)?;

    // Events stored before Synapse had event format versions are v1.
    let format_version = format_version.unwrap_or(EventFormatVersions::ROOM_V1_V2);

    let room_version_id = room_version_id.filter(|id| !id.is_empty());
    let room_version: &'static RoomVersion = match room_version_id.as_deref() {
        // Only out-of-band membership events from before
        // matrix-org/synapse#6983 have no `rooms` entry. Those rooms are v5 or
        // earlier, which all redact the same way.
        None => match format_version {
            EventFormatVersions::ROOM_V1_V2 => &RoomVersion::V1,
            EventFormatVersions::ROOM_V3 => &RoomVersion::V3,
            _ => &RoomVersion::V5,
        },
        Some(id) => match id.parse::<&'static RoomVersion>() {
            Ok(room_version) => room_version,
            Err(_) => {
                warn!("Event {event_id} has unknown room version {id}");
                return Ok(None);
            }
        },
    };
    if room_version_id.is_some() && room_version.event_format != format_version {
        error!(
            "Event {event_id} with room version {room_version} has wrong format: expected {}, was {format_version}",
            room_version.event_format
        );
        return Ok(None);
    }

    let metadata = match EventInternalMetadata::from_json_str(&internal_metadata) {
        Ok(metadata) => metadata,
        Err(e) => {
            error!("Unable to parse internal_metadata from event: {event_id}: {e:#}");
            return Ok(None);
        }
    };
    metadata.set_outlier_flag(outlier).map_err(py_err)?;
    metadata
        .set_persisted_position(
            stream_ordering,
            received_ts,
            // A null instance name is a row from before sharded persisters.
            Some(instance_name.unwrap_or_else(|| "master".to_owned())),
        )
        .map_err(py_err)?;

    let event = match Event::from_json_str(
        &json,
        room_version,
        metadata,
        rejected_reason.map(String::into_boxed_str),
    ) {
        Ok(event) => event,
        Err(e) => {
            error!("Unable to parse event from database {event_id}: {e:#}");
            return Ok(None);
        }
    };

    if room_version_id.is_none() && event.type_() != M_ROOM_MEMBER {
        bail!("Room for event {event_id} is unknown");
    }

    // If the content was modified in the database, the computed event ID
    // will not match.
    if event.event_id() != event_id {
        bail!(
            "Database corruption: event {event_id} in room {} has event ID {}",
            event.room_id(),
            event.event_id()
        );
    }

    Ok(Some((Arc::from(event_id), event)))
}
