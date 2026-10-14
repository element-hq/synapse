# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from collections.abc import Collection, Mapping

from synapse.synapse_rust.events import Event

def check_event_for_client_for_test(
    user_id: str,
    event: Event,
    state: Mapping[tuple[str, str], Event] | None,
    *,
    now_ms: int,
    msc4354_enabled: bool,
    filter_send_to_client: bool,
    is_peeking: bool,
    always_include_ids: Collection[str],
    sender_ignored: bool,
    retention_max_lifetime: int | None,
    sender_erased: bool,
) -> tuple[Event, str] | None:
    """The Rust port of `synapse.visibility._check_event_for_client`: the
    event the user may see (pruned if that is all they may see) and their
    membership at it, or None. `state` is None for an outlier."""
