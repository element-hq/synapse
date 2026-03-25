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

from typing import Any

from synapse.types import JsonDict, UserID

from ._base import Config


class ServerNoticesConfig(Config):
    """Configuration for the server notices room.

    Attributes:
        server_notices_mxid (str|None):
            The MXID to use for server notices.
            None if server notices are not enabled.

        server_notices_mxid_display_name (str|None):
            The display name to use for the server notices user.
            None if server notices are not enabled.

        server_notices_mxid_avatar_url (str|None):
            The MXC URL for the avatar of the server notices user.
            None if server notices are not enabled.

        server_notices_room_name (str|None):
            The name to use for the server notices room.
            None if server notices are not enabled.

        server_notices_room_avatar_url (str|None):
            The avatar URL to use for the server notices room.
            None if server notices are not enabled.

        server_notices_room_topic (str|None):
            The topic to use for the server notices room.
            None if server notices are not enabled.
    """

    section = "servernotices"

    def __init__(self, *args: Any):
        super().__init__(*args)
        self.server_notices_mxid: str | None = None
        self.server_notices_mxid_display_name: str | None = None
        self.server_notices_mxid_avatar_url: str | None = None
        self.server_notices_room_name: str | None = None
        self.server_notices_room_avatar_url: str | None = None
        self.server_notices_room_topic: str | None = None
        self.server_notices_auto_join: bool = False

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        c = config.get("server_notices")
        if c is None:
            return

        mxid_localpart = c["system_mxid_localpart"]
        self.server_notices_mxid = UserID(
            mxid_localpart, self.root.server.server_name
        ).to_string()
        self.server_notices_mxid_display_name = c.get("system_mxid_display_name", None)
        self.server_notices_mxid_avatar_url = c.get("system_mxid_avatar_url", None)
        # todo: i18n
        self.server_notices_room_name = c.get("room_name", "Server Notices")
        self.server_notices_room_avatar_url = c.get("room_avatar_url", None)
        self.server_notices_room_topic = c.get("room_topic", None)
        self.server_notices_auto_join = c.get("auto_join", False)
