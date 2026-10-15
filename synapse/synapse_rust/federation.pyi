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

from typing import Mapping, Sequence

from signedjson.types import SigningKey

from synapse.server import HomeServer
from synapse.types import JsonDict

def build_auth_header_for_test(
    signing_key: SigningKey,
    origin: str,
    method: str,
    uri: str,
    destination: str | None,
    destination_is: str | None = None,
    content: JsonDict | None = None,
) -> str:
    """Build the `X-Matrix` `Authorization` header the Rust request layer
    sends. Tests compare it with `MatrixFederationHttpClient.build_auth_headers`.
    """

class RustFederationAgentForTest:
    """The Rust federation agent, built from the homeserver's config, resolving
    names from fixed tables. For tests only.

    `request_blocking` blocks the calling thread until the response arrives, so
    the server it talks to must run on another thread.
    """

    def __init__(
        self,
        hs: HomeServer,
        srv_records: Mapping[str, Sequence[tuple[int, int, int, str]]],
        host_records: Mapping[str, Sequence[str]],
        port_overrides: Mapping[int, int],
        user_agent: str = "Synapse-test",
    ) -> None:
        """
        Args:
            hs: The homeserver whose config and Rust runtime to use.
            srv_records: SRV name to `(priority, weight, port, target)` tuples.
            host_records: Host name to IP addresses.
            port_overrides: Ports that resolution picks, mapped to the ports to
                really connect to.
            user_agent: The `User-Agent` header to send.
        """

    def request_blocking(
        self,
        method: str,
        uri: str,
        headers: Sequence[tuple[str, str]] = (),
        body: bytes = b"",
    ) -> tuple[int, list[tuple[str, bytes]], bytes]:
        """Send a request and return `(status, headers, body)`.

        Raises:
            RuntimeError: `"<kind>: <message>"` if no response arrived, where
                `kind` is e.g. `dns`, `connect`, `tls` or `service_unavailable`.
        """
