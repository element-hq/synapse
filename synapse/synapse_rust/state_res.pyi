# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from collections.abc import Iterable
from typing import Any

def get_auth_chain_difference_from_event_graph(
    state_sets: Iterable[Any],
    event_map: dict[str, Any],
) -> set[str]: ...
def resolve_v2_via_lattice_fold(
    unconflicted_state: dict[tuple[str, str], str],
    conflicted_event_ids: Iterable[str],
    event_map: dict[str, Any],
) -> dict[tuple[str, str], str]: ...
