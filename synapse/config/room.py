#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from typing import Any

from synapse.api.constants import RoomCreationPreset
from synapse.types import JsonDict

from ._base import Config, ConfigError

logger = logging.Logger(__name__)


class RoomDefaultEncryptionTypes:
    """Possible values for the encryption_enabled_by_default_for_room_type config option"""

    ALL = "all"
    INVITE = "invite"
    OFF = "off"


class RoomConfig(Config):
    section = "room"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        # Whether new, locally-created rooms should have encryption enabled
        encryption_for_room_type = config.get(
            "encryption_enabled_by_default_for_room_type",
            RoomDefaultEncryptionTypes.OFF,
        )
        if encryption_for_room_type == RoomDefaultEncryptionTypes.ALL:
            self.encryption_enabled_by_default_for_room_presets = [
                RoomCreationPreset.PRIVATE_CHAT,
                RoomCreationPreset.TRUSTED_PRIVATE_CHAT,
                RoomCreationPreset.PUBLIC_CHAT,
            ]
        elif encryption_for_room_type == RoomDefaultEncryptionTypes.INVITE:
            self.encryption_enabled_by_default_for_room_presets = [
                RoomCreationPreset.PRIVATE_CHAT,
                RoomCreationPreset.TRUSTED_PRIVATE_CHAT,
            ]
        elif (
            encryption_for_room_type == RoomDefaultEncryptionTypes.OFF
            or encryption_for_room_type is False
        ):
            # PyYAML translates "off" into False if it's unquoted, so we also need to
            # check for encryption_for_room_type being False.
            self.encryption_enabled_by_default_for_room_presets = []
        else:
            raise ConfigError(
                "Invalid value for encryption_enabled_by_default_for_room_type"
            )

        self.default_power_level_content_override = config.get(
            "default_power_level_content_override",
            None,
        )
        if self.default_power_level_content_override is not None:
            for preset in self.default_power_level_content_override:
                if preset not in vars(RoomCreationPreset).values():
                    raise ConfigError(
                        "Unrecognised room preset %s in default_power_level_content_override"
                        % preset
                    )
                # We validate the actual overrides when we try to apply them.

        # When enabled, users will forget rooms when they leave them, either via a
        # leave, kick or ban.
        self.forget_on_leave = config.get("forget_rooms_on_leave", False)
