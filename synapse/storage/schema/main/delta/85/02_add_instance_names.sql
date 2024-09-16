--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2024 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Add `instance_name` columns to stream tables to allow them to be used with
-- `MultiWriterIdGenerator`
ALTER TABLE device_lists_stream ADD COLUMN instance_name TEXT;
ALTER TABLE user_signature_stream ADD COLUMN instance_name TEXT;
ALTER TABLE device_lists_outbound_pokes ADD COLUMN instance_name TEXT;
ALTER TABLE device_lists_changes_in_room ADD COLUMN instance_name TEXT;
ALTER TABLE device_lists_remote_pending ADD COLUMN instance_name TEXT;

ALTER TABLE e2e_cross_signing_keys ADD COLUMN instance_name TEXT;

ALTER TABLE push_rules_stream ADD COLUMN instance_name TEXT;

ALTER TABLE pushers ADD COLUMN instance_name TEXT;
ALTER TABLE deleted_pushers ADD COLUMN instance_name TEXT;
