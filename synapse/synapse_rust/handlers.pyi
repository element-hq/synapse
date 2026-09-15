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

from typing import TYPE_CHECKING, Optional

from twisted.internet.defer import Deferred

from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

class VersionsHandler:
    def get_versions(self, user_id: Optional[str] = None) -> Deferred[JsonDict]:
        """
        Assemble a `/versions` response.

        The returned deferred follows Synapse logcontext rules.
        """

class RustHandlers:
    """The collection of Rust-implemented request handlers."""

    def __init__(self, homeserver: "HomeServer") -> None: ...
    @property
    def versions(self) -> VersionsHandler: ...
