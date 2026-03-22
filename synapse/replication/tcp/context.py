#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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
import ssl

from synapse.config.redis import RedisConfig


class ClientContextFactory:
    """Creates SSL contexts for Redis connections."""

    def __init__(self, redis_config: RedisConfig):
        self.redis_config = redis_config

    def getContext(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self.redis_config.redis_certificate:
            ctx.load_cert_chain(
                certfile=self.redis_config.redis_certificate,
                keyfile=self.redis_config.redis_private_key,
            )
        if self.redis_config.redis_ca_file:
            ctx.load_verify_locations(cafile=self.redis_config.redis_ca_file)
        elif self.redis_config.redis_ca_path:
            ctx.load_verify_locations(capath=self.redis_config.redis_ca_path)
        return ctx
