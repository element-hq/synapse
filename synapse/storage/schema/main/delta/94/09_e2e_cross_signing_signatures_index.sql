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

-- Remove any rows `e2e_cross_signing_signatures` that have duplicate cross-signing signatures.
-- Ensures that rows are unique across `(user_id, target_user_id, target_device_id, key_id)`, so that
-- we can create an index on those columns. See
-- `./10_e2e_cross_signing_signatures_add_key_id_to_index.sql`, which adds said
-- index.
        
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
  (9409, 'e2e_cross_signing_signatures_remove_duplicates', '{}');
