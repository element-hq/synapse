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

-- Adds the `key_id` to the e2e_cross_signing_signatures index, since the
-- ("user_id", "key_id", "target_user_id", "target_device_id") should be
-- unique.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
  (9205, 'e2e_cross_signing_signatures_add_key_id_to_index', '{}');
