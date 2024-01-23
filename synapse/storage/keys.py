#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

import logging

import attr
from signedjson.types import VerifyKey

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class FetchKeyResult:
    verify_key: VerifyKey  # the key itself
    valid_until_ts: int  # how long we can use this key for


@attr.s(slots=True, frozen=True, auto_attribs=True)
class FetchKeyResultForRemote:
    key_json: bytes  # the full key JSON
    valid_until_ts: int  # how long we can use this key for, in milliseconds.
    added_ts: int  # When we added this key, in milliseconds.
