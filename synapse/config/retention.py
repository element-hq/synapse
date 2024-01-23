#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
#  Copyright 2021 The Matrix.org Foundation C.I.C.
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
from typing import Any, List, Optional

import attr

from synapse.config._base import Config, ConfigError
from synapse.types import JsonDict

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class RetentionPurgeJob:
    """Object describing the configuration of the manhole"""

    interval: int
    shortest_max_lifetime: Optional[int]
    longest_max_lifetime: Optional[int]


class RetentionConfig(Config):
    section = "retention"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        retention_config = config.get("retention")
        if retention_config is None:
            retention_config = {}

        self.retention_enabled = retention_config.get("enabled", False)

        retention_default_policy = retention_config.get("default_policy")

        if retention_default_policy is not None:
            self.retention_default_min_lifetime = retention_default_policy.get(
                "min_lifetime"
            )
            if self.retention_default_min_lifetime is not None:
                self.retention_default_min_lifetime = self.parse_duration(
                    self.retention_default_min_lifetime
                )

            self.retention_default_max_lifetime = retention_default_policy.get(
                "max_lifetime"
            )
            if self.retention_default_max_lifetime is not None:
                self.retention_default_max_lifetime = self.parse_duration(
                    self.retention_default_max_lifetime
                )

            if (
                self.retention_default_min_lifetime is not None
                and self.retention_default_max_lifetime is not None
                and (
                    self.retention_default_min_lifetime
                    > self.retention_default_max_lifetime
                )
            ):
                raise ConfigError(
                    "The default retention policy's 'min_lifetime' can not be greater"
                    " than its 'max_lifetime'"
                )
        else:
            self.retention_default_min_lifetime = None
            self.retention_default_max_lifetime = None

        if self.retention_enabled:
            logger.info(
                "Message retention policies support enabled with the following default"
                " policy: min_lifetime = %s ; max_lifetime = %s",
                self.retention_default_min_lifetime,
                self.retention_default_max_lifetime,
            )

        self.retention_allowed_lifetime_min = retention_config.get(
            "allowed_lifetime_min"
        )
        if self.retention_allowed_lifetime_min is not None:
            self.retention_allowed_lifetime_min = self.parse_duration(
                self.retention_allowed_lifetime_min
            )

        self.retention_allowed_lifetime_max = retention_config.get(
            "allowed_lifetime_max"
        )
        if self.retention_allowed_lifetime_max is not None:
            self.retention_allowed_lifetime_max = self.parse_duration(
                self.retention_allowed_lifetime_max
            )

        if (
            self.retention_allowed_lifetime_min is not None
            and self.retention_allowed_lifetime_max is not None
            and self.retention_allowed_lifetime_min
            > self.retention_allowed_lifetime_max
        ):
            raise ConfigError(
                "Invalid retention policy limits: 'allowed_lifetime_min' can not be"
                " greater than 'allowed_lifetime_max'"
            )

        self.retention_purge_jobs: List[RetentionPurgeJob] = []
        for purge_job_config in retention_config.get("purge_jobs", []):
            interval_config = purge_job_config.get("interval")

            if interval_config is None:
                raise ConfigError(
                    "A retention policy's purge jobs configuration must have the"
                    " 'interval' key set."
                )

            interval = self.parse_duration(interval_config)

            shortest_max_lifetime = purge_job_config.get("shortest_max_lifetime")

            if shortest_max_lifetime is not None:
                shortest_max_lifetime = self.parse_duration(shortest_max_lifetime)

            longest_max_lifetime = purge_job_config.get("longest_max_lifetime")

            if longest_max_lifetime is not None:
                longest_max_lifetime = self.parse_duration(longest_max_lifetime)

            if (
                shortest_max_lifetime is not None
                and longest_max_lifetime is not None
                and shortest_max_lifetime > longest_max_lifetime
            ):
                raise ConfigError(
                    "A retention policy's purge jobs configuration's"
                    " 'shortest_max_lifetime' value can not be greater than its"
                    " 'longest_max_lifetime' value."
                )

            self.retention_purge_jobs.append(
                RetentionPurgeJob(interval, shortest_max_lifetime, longest_max_lifetime)
            )

        if not self.retention_purge_jobs:
            self.retention_purge_jobs = [
                RetentionPurgeJob(self.parse_duration("1d"), None, None)
            ]
