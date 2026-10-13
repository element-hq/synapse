--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- MSC4430 (member keys): room members are identified by a per-user, per-room
-- Ed25519 key rather than by their user ID. Two tables: the keys this server
-- holds for its own users, and what it has learned about everyone else's.

-- The member keys we hold: one per (local user, room). `member_key` is the
-- `@<unpadded urlsafe base64 public key>` that appears as `sender` and
-- `state_key` in the room; `signing_key` is the unpadded base64 seed of the
-- private half. Rows are never deleted or replaced, and survive a purge of the
-- room: a user's key in a room must not change when they leave and re-join.
--
-- `signing_key` is a secret, and unlike an access token it cannot be revoked:
-- MSC4430 has no member key rotation, so a leaked seed impersonates that user
-- in that room until this server's signing key is rotated. (It is also in the
-- parameters of the INSERT, which the SQL logger prints at DEBUG level.)
CREATE TABLE msc4430_member_keys (
  room_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  member_key TEXT NOT NULL,
  signing_key TEXT NOT NULL,
  created_ts BIGINT NOT NULL,
  PRIMARY KEY (room_id, user_id)
);

-- Keys are random 32 byte public keys and so unique across rooms. Lookups by key
-- are always made within a room (by room ID and member key), for which this
-- index is both the uniqueness guarantee and the lookup path. (Note: delta
-- scripts must not contain a question mark, even in a comment: it is treated as
-- a parameter placeholder on Postgres.)
CREATE UNIQUE INDEX msc4430_member_keys_member_key ON msc4430_member_keys(member_key);

-- All of a user's keys (serialising their own events, erasure).
CREATE INDEX msc4430_member_keys_user_id ON msc4430_member_keys(user_id);


-- What we know about member keys we do not hold: the linkage information
-- (`content.localpart`, `content.domain`) that a key's `m.room.member` event
-- claimed, and whether we have verified it. Cleared when the room is purged.
--
--  * `domain`: the claimed domain. Used for routing whether verified or not. A
--    key whose events carried no linkage information has no row.
--  * `localpart`: the claimed localpart; NULL once the event that carried it
--    has been redacted (erasure), after which the key maps to no user.
--  * `verified`: whether the server signing key `key_id` of `domain` co-signed
--    the `m.room.member` event `event_id`, and was that domain's current key
--    when we last checked. A verified link always names the event and key that
--    proved it (there is no other way to be verified). When the key stops being
--    current the link is downgraded but keeps both, so that it can be found
--    again if the event is re-signed. A key ID alone does not identify a key
--    (different material can be published under a reused ID), so re-checking
--    a link means re-checking the co-signature on `event_id` against the
--    domain's current key material.
--
-- The keys this server holds never have a link row: a row claiming our own
-- domain for a key we do not hold is somebody else's claim.
CREATE TABLE msc4430_member_links (
  room_id TEXT NOT NULL,
  member_key TEXT NOT NULL,
  domain TEXT NOT NULL,
  localpart TEXT,
  verified BOOLEAN NOT NULL,
  event_id TEXT,
  key_id TEXT,
  PRIMARY KEY (room_id, member_key),
  CHECK (NOT verified OR (event_id IS NOT NULL AND key_id IS NOT NULL))
);

-- The keys in a room claiming a given domain (hosts in room, routing).
CREATE INDEX msc4430_member_links_room_domain ON msc4430_member_links(room_id, domain);

-- The links a given server signing key vouches for (re-verification after key
-- rotation).
CREATE INDEX msc4430_member_links_domain_key ON msc4430_member_links(domain, key_id);

-- Forgetting a localpart when the event that carried it is redacted.
CREATE INDEX msc4430_member_links_event_id ON msc4430_member_links(event_id);
