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
from typing import TYPE_CHECKING, Callable, Dict, Optional

from synapse.push import Pusher, PusherConfig
from synapse.push.emailpusher import EmailPusher
from synapse.push.httppusher import HttpPusher
from synapse.push.mailer import Mailer

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class PusherFactory:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.config = hs.config

        self.pusher_types: Dict[str, Callable[[HomeServer, PusherConfig], Pusher]] = {
            "http": HttpPusher
        }

        logger.info("email enable notifs: %r", hs.config.email.email_enable_notifs)
        if hs.config.email.email_enable_notifs:
            self.mailers: Dict[str, Mailer] = {}

            self._notif_template_html = hs.config.email.email_notif_template_html
            self._notif_template_text = hs.config.email.email_notif_template_text

            self.pusher_types["email"] = self._create_email_pusher

            logger.info("defined email pusher type")

    def create_pusher(self, pusher_config: PusherConfig) -> Optional[Pusher]:
        kind = pusher_config.kind
        f = self.pusher_types.get(kind, None)
        if not f:
            return None
        logger.debug("creating %s pusher for %r", kind, pusher_config)
        return f(self.hs, pusher_config)

    def _create_email_pusher(
        self, _hs: "HomeServer", pusher_config: PusherConfig
    ) -> EmailPusher:
        app_name = self._app_name_from_pusherdict(pusher_config)
        mailer = self.mailers.get(app_name)
        if not mailer:
            mailer = Mailer(
                hs=self.hs,
                app_name=app_name,
                template_html=self._notif_template_html,
                template_text=self._notif_template_text,
            )
            self.mailers[app_name] = mailer
        return EmailPusher(self.hs, pusher_config, mailer)

    def _app_name_from_pusherdict(self, pusher_config: PusherConfig) -> str:
        data = pusher_config.data

        if isinstance(data, dict):
            brand = data.get("brand")
            if isinstance(brand, str):
                return brand

        return self.config.email.email_app_name
