--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Carry users' customisations of the legacy mention push rules, which Matrix
-- v1.17 (MSC4210) removed from the base rule set, over to the intentional
-- mention rules that replace them.
--
-- This runs ahead of Synapse withholding the legacy rules by default, so that
-- it has completed on every server before they go: until it has, users are
-- notified under the default intentional mention rules regardless of their
-- legacy customisations. The price is that users who had disabled both legacy
-- user mention rules stop being notified of intentional mentions from now on;
-- see `PushRulesWorkerStore._migrate_legacy_mention_push_rules`.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (9412, 'migrate_legacy_mention_push_rules', '{}');
