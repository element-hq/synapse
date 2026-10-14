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

//! The set of tables the generator writes, split by load pass.
//!
//! Pass A tables have no foreign key onto `events`; pass B tables do and
//! can only be loaded once pass A has committed. Rows for a table whose
//! sink is not open in the current pass are silently dropped, so the
//! generation code writes every row unconditionally and the pass decides
//! what lands.

use anyhow::Result;
use deadpool_postgres::Pool;
use tracing::info;

use super::sink::{Sink, F};

macro_rules! sinks {
    ($( $pass:ident : $name:ident => $table:literal ( $cols:literal ) ),* $(,)?) => {
        #[derive(Default)]
        pub struct Sinks {
            $( pub $name: Option<Sink>, )*
        }

        impl Sinks {
            pub async fn open(pool: &Pool, pass: Pass) -> Result<Self> {
                let mut s = Self::default();
                $(
                    if pass == Pass::$pass {
                        s.$name = Some(Sink::open(pool, $table, $cols).await?);
                    }
                )*
                Ok(s)
            }

            pub async fn close(self) -> Result<u64> {
                let mut total = 0;
                $(
                    if let Some(sink) = self.$name {
                        let table = sink.table();
                        let rows = sink.close().await?;
                        info!(table, rows, "loaded");
                        total += rows;
                    }
                )*
                Ok(total)
            }
        }
    };
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Pass {
    /// Planning only: no sinks.
    Plan,
    A,
    B,
}

sinks! {
    A: rooms => "rooms"("room_id, is_public, creator, room_version, has_auth_chain_index"),
    A: events => "events"("stream_ordering, topological_ordering, event_id, type, room_id, processed, outlier, depth, origin_server_ts, received_ts, sender, contains_url, instance_name, state_key"),
    A: event_json => "event_json"("event_id, room_id, internal_metadata, json, format_version"),
    A: event_auth => "event_auth"("event_id, auth_id, room_id"),
    A: state_groups => "state_groups"("id, room_id, event_id"),
    A: state_groups_state => "state_groups_state"("state_group, room_id, type, state_key, event_id"),
    A: state_group_edges => "state_group_edges"("state_group, prev_state_group"),
    A: event_to_state_groups => "event_to_state_groups"("event_id, state_group"),
    A: current_state_delta_stream => "current_state_delta_stream"("stream_id, room_id, type, state_key, event_id, prev_event_id, instance_name"),
    A: stream_ordering_to_exterm => "stream_ordering_to_exterm"("stream_ordering, room_id, event_id"),
    A: room_depth => "room_depth"("room_id, min_depth"),
    A: room_stats_current => "room_stats_current"("room_id, current_state_events, joined_members, invited_members, left_members, banned_members, knocked_members, local_users_in_room, completed_delta_stream_id"),
    A: room_stats_state => "room_stats_state"("room_id, name, canonical_alias, join_rules, history_visibility, encryption, avatar, guest_access, is_federatable, topic, room_type"),
    A: room_aliases => "room_aliases"("room_alias, room_id, creator"),
    A: event_push_summary => "event_push_summary"("user_id, room_id, notif_count, stream_ordering, unread_count, last_receipt_stream_ordering, thread_id"),
    A: receipts_linearized => "receipts_linearized"("stream_id, room_id, receipt_type, user_id, event_id, data, instance_name, event_stream_ordering, thread_id"),
    A: receipts_graph => "receipts_graph"("room_id, receipt_type, user_id, event_ids, data, thread_id"),
    A: room_account_data => "room_account_data"("user_id, room_id, account_data_type, stream_id, content, instance_name"),
    A: room_tags => "room_tags"("user_id, room_id, tag, content"),
    A: room_tags_revisions => "room_tags_revisions"("user_id, room_id, stream_id, instance_name"),
    A: threads => "threads"("room_id, thread_id, latest_event_id, topological_ordering, stream_ordering"),
    A: event_relations => "event_relations"("event_id, relates_to_id, relation_type, aggregation_key"),
    A: users_in_public_rooms => "users_in_public_rooms"("user_id, room_id"),
    A: users_who_share_private_rooms => "users_who_share_private_rooms"("user_id, other_user_id, room_id"),
    A: users => "users"("name, password_hash, creation_ts, admin, is_guest, appservice_id, user_type, deactivated, shadow_banned, approved, locked, suspended"),
    A: profiles => "profiles"("user_id, displayname, avatar_url, full_user_id"),
    A: access_tokens => "access_tokens"("id, user_id, device_id, token, valid_until_ms, puppets_user_id, last_validated, refresh_token_id, used"),
    A: devices => "devices"("user_id, device_id, display_name, last_seen, ip, user_agent, hidden"),
    A: e2e_device_keys_json => "e2e_device_keys_json"("user_id, device_id, ts_added_ms, key_json"),
    A: e2e_one_time_keys_json => "e2e_one_time_keys_json"("user_id, device_id, algorithm, key_id, ts_added_ms, key_json"),
    A: e2e_fallback_keys_json => "e2e_fallback_keys_json"("user_id, device_id, algorithm, key_id, key_json, used"),
    A: e2e_cross_signing_keys => "e2e_cross_signing_keys"("user_id, keytype, keydata, stream_id, instance_name"),
    A: account_data => "account_data"("user_id, account_data_type, stream_id, content, instance_name"),
    A: push_rules => "push_rules"("id, user_name, rule_id, priority_class, priority, conditions, actions"),
    A: user_filters => "user_filters"("user_id, filter_id, filter_json, full_user_id"),
    A: ignored_users => "ignored_users"("ignorer_user_id, ignored_user_id"),
    A: presence_stream => "presence_stream"("stream_id, user_id, state, last_active_ts, last_federation_update_ts, last_user_sync_ts, status_msg, currently_active, instance_name"),
    A: user_stats_current => "user_stats_current"("user_id, joined_rooms, completed_delta_stream_id"),
    A: user_directory => "user_directory"("user_id, room_id, display_name, avatar_url"),
    B: current_state_events => "current_state_events"("event_id, room_id, type, state_key, membership, event_stream_ordering"),
    B: room_memberships => "room_memberships"("event_id, user_id, sender, room_id, membership, forgotten, event_stream_ordering, participant"),
    B: local_current_membership => "local_current_membership"("room_id, user_id, event_id, membership, event_stream_ordering"),
    B: event_edges => "event_edges"("event_id, prev_event_id, room_id, is_state"),
    B: event_forward_extremities => "event_forward_extremities"("event_id, room_id"),
    B: sliding_sync_joined_rooms => "sliding_sync_joined_rooms"("room_id, event_stream_ordering, bump_stamp, room_type, room_name, is_encrypted, tombstone_successor_room_id"),
    B: sliding_sync_membership_snapshots => "sliding_sync_membership_snapshots"("room_id, user_id, sender, membership_event_id, membership, forgotten, event_stream_ordering, event_instance_name, has_known_state, room_type, room_name, is_encrypted, tombstone_successor_room_id"),
}

/// Write a row to a sink if it is open in this pass.
pub async fn put(sink: &mut Option<Sink>, fields: &[F<'_>]) -> Result<()> {
    match sink {
        Some(s) => s.row(fields).await,
        None => Ok(()),
    }
}
