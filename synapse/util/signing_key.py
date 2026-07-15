#
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
#
import hashlib

from signedjson.key import SigningKey, generate_signing_key
from unpaddedbase64 import encode_base64

PLACEHOLDER_SIGNING_KEY_ID = "PLACEHOLDER_SIGNING_KEY_ID"


def derive_signing_key_version(signing_key: SigningKey) -> str:
    digest = hashlib.sha256(signing_key.verify_key.encode()).digest()
    # Matrix key ids do not allow "-" (so, normalize b64url alphabet).
    # NOTE: "version" is the term used in the codebase, not suffix or ID.
    return encode_base64(digest[:16], urlsafe=True).replace("-", "_")


def generate_content_derived_signing_key() -> SigningKey:
    signing_key = generate_signing_key(PLACEHOLDER_SIGNING_KEY_ID)
    signing_key.version = derive_signing_key_version(signing_key)
    return signing_key
