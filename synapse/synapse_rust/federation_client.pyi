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

class RequestSigner:
    """Builds the X-Matrix `Authorization` header for outbound federation requests."""

    def __init__(self, server_name: str, key_id: str, seed: bytes) -> None:
        """`seed` is the Ed25519 signing key's 32-byte seed; ValueError if not."""

    def authorization(
        self,
        method: bytes,
        uri: bytes,
        destination: str | None,
        destination_is: str | None,
        content: bytes | None,
    ) -> bytes:
        """The header value. `content` is canonical JSON and is signed as is.

        Raises:
            ValueError: if neither destination is non-empty, `method` or `uri` is
                not ASCII, or the header would hold a byte that is not printable
                ASCII.
        """

def _ip_blocked(cidrs: list[str], ip: str) -> bool:
    """Test-only: whether `ip` is in the blocklist built from `cidrs`."""
