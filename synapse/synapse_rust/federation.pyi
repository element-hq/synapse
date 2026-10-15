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

from signedjson.types import SigningKey

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
