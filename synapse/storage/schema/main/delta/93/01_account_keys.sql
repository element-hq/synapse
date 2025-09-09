--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2025 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Keeps a record of MSC4243 account key <--> account name mappings for all servers.
-- This mapping is permanent.
CREATE TABLE account_keys (
    account_key_user_id TEXT PRIMARY KEY NOT NULL,
    -- nullable if we cannot talk to the remote server.
    account_name_user_id TEXT,
    -- the private key as urlsafe base64, only for local accounts
    account_key TEXT,
    UNIQUE(account_key_user_id, account_name_user_id)
);

CREATE INDEX account_keys_key_for_name ON account_keys (account_name_user_id) WHERE account_name_user_id IS NOT NULL;
