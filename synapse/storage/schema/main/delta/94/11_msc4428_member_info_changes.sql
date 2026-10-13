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

-- MSC4428: changes to a room member's `member_info` entry that no event in the
-- room anchors (today: erasure). Each row says "the entry for `stable_id` in
-- `room_id` changed", positioned on its own multi-writer stream so that a
-- syncing client is woken and told about it.
--
-- Rows are only kept for a retention period (see `MemberInfoWorkerStore`); the
-- `_max_pruned_stream_id` table below records how far they have been pruned.
CREATE TABLE msc4428_member_info_changes (
  -- Position in the member info changes stream.
  stream_id BIGINT NOT NULL PRIMARY KEY,

  -- Name of the worker that wrote this row (any worker may write to this
  -- stream, as for cache invalidations).
  instance_name TEXT NOT NULL,

  room_id TEXT NOT NULL,

  -- The stable ID (today: user ID) whose entry changed.
  stable_id TEXT NOT NULL,

  -- When the change was recorded, in milliseconds since the Unix epoch. Used
  -- to prune old rows.
  changed_ts BIGINT NOT NULL
);

CREATE INDEX msc4428_member_info_changes_ts ON msc4428_member_info_changes (changed_ts);

-- The maximum stream_id that has been pruned from `msc4428_member_info_changes`.
-- A sync token at or before this position cannot be answered from the table,
-- and the server falls back to sending the full `member_info` map. A separate
-- table rather than MIN(stream_id), as the device list changes table does, so
-- that an empty table is not mistaken for "nothing pruned".
CREATE TABLE msc4428_member_info_changes_max_pruned_stream_id (
  Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,
  stream_id BIGINT NOT NULL
);

INSERT INTO msc4428_member_info_changes_max_pruned_stream_id (stream_id) VALUES (0);
