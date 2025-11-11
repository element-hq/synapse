--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2025 Element Creations, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Set delayed events to be uniquely identifiable by their delay_id.

-- In practice, delay_ids are already unique because they are generated
-- from cryptographically strong random strings.
-- Therefore, adding this constraint is not expected to ever fail,
-- despite the current pkey technically allowing non-unique delay_ids.
CREATE UNIQUE INDEX delayed_events_idx ON delayed_events (delay_id);