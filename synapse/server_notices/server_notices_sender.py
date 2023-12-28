#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
from typing import TYPE_CHECKING, Iterable, Union

from synapse.server_notices.consent_server_notices import ConsentServerNotices
from synapse.server_notices.resource_limits_server_notices import (
    ResourceLimitsServerNotices,
)
from synapse.server_notices.worker_server_notices_sender import (
    WorkerServerNoticesSender,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer


class ServerNoticesSender(WorkerServerNoticesSender):
    """A centralised place which sends server notices automatically when
    Certain Events take place
    """

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self._server_notices: Iterable[
            Union[ConsentServerNotices, ResourceLimitsServerNotices]
        ] = (
            ConsentServerNotices(hs),
            ResourceLimitsServerNotices(hs),
        )

    async def on_user_syncing(self, user_id: str) -> None:
        """Called when the user performs a sync operation.

        Args:
            user_id: mxid of user who synced
        """
        for sn in self._server_notices:
            await sn.maybe_send_server_notice_to_user(user_id)

    async def on_user_ip(self, user_id: str) -> None:
        """Called on the master when a worker process saw a client request.

        Args:
            user_id: mxid
        """
        # The synchrotrons use a stubbed version of ServerNoticesSender, so
        # we check for notices to send to the user in on_user_ip as well as
        # in on_user_syncing
        for sn in self._server_notices:
            await sn.maybe_send_server_notice_to_user(user_id)
