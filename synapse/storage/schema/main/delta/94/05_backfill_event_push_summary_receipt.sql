--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 New Vector Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Backfill last_receipt_stream_ordering for event_push_summary rows created
-- with last_receipt_stream_ordering=NULL by the rotation job before the code
-- fix in _handle_new_receipts_for_notifs_txn was applied.
--
-- NULL has a dual meaning in this column (see the schema delta that added it):
--   1. Legacy rows from old Synapse that maintained counts synchronously.
--   2. Bug-affected rows where the receipt UPDATE was a silent no-op.
--
-- For a given event_push_summary row, the relevant receipts are unthreaded
-- receipts (cover all threads) and the threaded receipt for that thread.
--
-- For both kinds of stale row, if stream_ordering <= the max relevant receipt
-- then every event in the summary predates the receipt and the counts should
-- be zero.  If stream_ordering > the max receipt, some events after the
-- receipt are included; we set last_receipt_stream_ordering but leave the
-- count (it may be inflated, but will self-correct when the user next reads
-- the room).
UPDATE event_push_summary
SET last_receipt_stream_ordering = (
        SELECT MAX(r.event_stream_ordering)
        FROM receipts_linearized AS r
        WHERE r.user_id      = event_push_summary.user_id
          AND r.room_id      = event_push_summary.room_id
          AND r.receipt_type IN ('m.read', 'm.read.private')
          AND (r.thread_id IS NULL OR r.thread_id = event_push_summary.thread_id)
    ),
    notif_count = CASE
        WHEN stream_ordering <= (
            SELECT MAX(r.event_stream_ordering)
            FROM receipts_linearized AS r
            WHERE r.user_id      = event_push_summary.user_id
              AND r.room_id      = event_push_summary.room_id
              AND r.receipt_type IN ('m.read', 'm.read.private')
              AND (r.thread_id IS NULL OR r.thread_id = event_push_summary.thread_id)
        ) THEN 0
        ELSE notif_count
    END,
    unread_count = CASE
        WHEN stream_ordering <= (
            SELECT MAX(r.event_stream_ordering)
            FROM receipts_linearized AS r
            WHERE r.user_id      = event_push_summary.user_id
              AND r.room_id      = event_push_summary.room_id
              AND r.receipt_type IN ('m.read', 'm.read.private')
              AND (r.thread_id IS NULL OR r.thread_id = event_push_summary.thread_id)
        ) THEN 0
        ELSE unread_count
    END
WHERE last_receipt_stream_ordering IS NULL
  AND EXISTS (
      SELECT 1 FROM receipts_linearized AS r
      WHERE r.user_id      = event_push_summary.user_id
        AND r.room_id      = event_push_summary.room_id
        AND r.receipt_type IN ('m.read', 'm.read.private')
        AND (r.thread_id IS NULL OR r.thread_id = event_push_summary.thread_id)
  );
