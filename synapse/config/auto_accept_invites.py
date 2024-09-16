#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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


class AutoAcceptInvitesConfig(Config):
    section = "auto_accept_invites"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        auto_accept_invites_config = config.get("auto_accept_invites") or {}

        self.enabled = auto_accept_invites_config.get("enabled", False)

        self.accept_invites_only_for_direct_messages = auto_accept_invites_config.get(
            "only_for_direct_messages", False
        )

        self.accept_invites_only_from_local_users = auto_accept_invites_config.get(
            "only_from_local_users", False
        )

        self.worker_to_run_on = auto_accept_invites_config.get("worker_to_run_on")
