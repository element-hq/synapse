#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import hashlib

import unpaddedbase64


def sha256_and_url_safe_base64(input_text: str) -> str:
    """SHA256 hash an input string, encode the digest as url-safe base64, and
    return

    Args:
        input_text: string to hash

    returns:
        A sha256 hashed and url-safe base64 encoded digest
    """
    digest = hashlib.sha256(input_text.encode()).digest()
    return unpaddedbase64.encode_base64(digest, urlsafe=True)
