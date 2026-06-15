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

-- Backfill last_receipt_stream_ordering for event_push_summary rows created
-- with last_receipt_stream_ordering=NULL by the rotation job before
-- the fix in https://github.com/element-hq/synapse/pull/19785.
--
-- This can touch very large event_push_summary and receipts_linearized tables,
-- so run it as a background update rather than a foreground schema migration.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
  (9405, 'event_push_summary_last_receipt_stream_ordering_backfill', '{}');
