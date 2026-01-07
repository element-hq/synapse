--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 New Vector, Ltd
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
    -- A user ID with the localpart as the ed25519 verify key.
    account_key_user_id TEXT PRIMARY KEY NOT NULL,
    -- A user ID with the localpart as a human-readable account name. Nullable if we cannot talk to the remote server.
    account_name_user_id TEXT,
    -- the claimed domain of the user ID for this account. Used for selecting all unverified keys for a domain.
    account_domain TEXT NOT NULL,
    -- the private key as urlsafe base64, only for local accounts
    signing_key TEXT,

    -- The following timestamps are used for auditing purposes, as the server merely needs a boolean
    -- the unix millis timestamp when the domain was verified
    verified_at_ms BIGINT,
    -- the unix millis timestamp when the account was erased.
    erased_at_ms BIGINT
);

-- Make account key user ID to name lookups faster. Federation uses this to quickly map PDU senders to account names.
CREATE INDEX account_keys_key_to_name ON account_keys (account_key_user_id);
-- Make account name user ID to key lookups faster. Local users use this to quickly map to the account key.
CREATE INDEX account_keys_name_to_key ON account_keys (account_name_user_id) WHERE account_name_user_id IS NOT NULL;
