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

class RustSyncFallback(Exception):
    """The Rust sync handler cannot compute this room's entry. Compute it
    with the Python implementation."""

class SyncHandler:
    """Computes sync responses with the Rust implementation, reading the
    database through the native Rust Postgres driver.

    Get one from `RustHandlers.sync`; it is only built when the main database
    uses that driver.
    """
