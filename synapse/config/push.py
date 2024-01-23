#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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

from typing import Any

from synapse.types import JsonDict

from ._base import Config


class PushConfig(Config):
    section = "push"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        push_config = config.get("push") or {}
        self.push_include_content = push_config.get("include_content", True)
        self.enable_push = push_config.get("enabled", True)
        self.push_group_unread_count_by_room = push_config.get(
            "group_unread_count_by_room", True
        )

        # There was a a 'redact_content' setting but mistakenly read from the
        # 'email'section'. Check for the flag in the 'push' section, and log,
        # but do not honour it to avoid nasty surprises when people upgrade.
        if push_config.get("redact_content") is not None:
            print(
                "The push.redact_content content option has never worked. "
                "Please set push.include_content if you want this behaviour"
            )

        # Now check for the one in the 'email' section and honour it,
        # with a warning.
        email_push_config = config.get("email") or {}
        redact_content = email_push_config.get("redact_content")
        if redact_content is not None:
            print(
                "The 'email.redact_content' option is deprecated: "
                "please set push.include_content instead"
            )
            self.push_include_content = not redact_content

        # Whether to apply a random delay to outbound push.
        self.push_jitter_delay_ms = None
        push_jitter_delay = push_config.get("jitter_delay", None)
        if push_jitter_delay:
            self.push_jitter_delay_ms = self.parse_duration(push_jitter_delay)
