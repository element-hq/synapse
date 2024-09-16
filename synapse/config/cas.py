#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from typing import Any, List

from synapse.config.sso import SsoAttributeRequirement
from synapse.types import JsonDict

from ._base import Config, ConfigError
from ._util import validate_config


class CasConfig(Config):
    """Cas Configuration

    cas_server_url: URL of CAS server
    """

    section = "cas"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        cas_config = config.get("cas_config", None)
        self.cas_enabled = cas_config and cas_config.get("enabled", True)

        if self.cas_enabled:
            self.cas_server_url = cas_config["server_url"]

            # TODO Update this to a _synapse URL.
            public_baseurl = self.root.server.public_baseurl
            self.cas_service_url = public_baseurl + "_matrix/client/r0/login/cas/ticket"

            self.cas_protocol_version = cas_config.get("protocol_version")
            if (
                self.cas_protocol_version is not None
                and self.cas_protocol_version not in [1, 2, 3]
            ):
                raise ConfigError(
                    "Unsupported CAS protocol version %s (only versions 1, 2, 3 are supported)"
                    % (self.cas_protocol_version,),
                    ("cas_config", "protocol_version"),
                )
            self.cas_displayname_attribute = cas_config.get("displayname_attribute")
            required_attributes = cas_config.get("required_attributes") or {}
            self.cas_required_attributes = _parsed_required_attributes_def(
                required_attributes
            )

            self.cas_enable_registration = cas_config.get("enable_registration", True)

            self.cas_allow_numeric_ids = cas_config.get("allow_numeric_ids")
            self.cas_numeric_ids_prefix = cas_config.get("numeric_ids_prefix")
            if (
                self.cas_numeric_ids_prefix is not None
                and self.cas_numeric_ids_prefix.isalnum() is False
            ):
                raise ConfigError(
                    "Only alphanumeric characters are allowed for numeric IDs prefix",
                    ("cas_config", "numeric_ids_prefix"),
                )

            self.idp_name = cas_config.get("idp_name", "CAS")
            self.idp_icon = cas_config.get("idp_icon")
            self.idp_brand = cas_config.get("idp_brand")

        else:
            self.cas_server_url = None
            self.cas_service_url = None
            self.cas_protocol_version = None
            self.cas_displayname_attribute = None
            self.cas_required_attributes = []
            self.cas_enable_registration = False
            self.cas_allow_numeric_ids = False
            self.cas_numeric_ids_prefix = "u"


# CAS uses a legacy required attributes mapping, not the one provided by
# SsoAttributeRequirement.
REQUIRED_ATTRIBUTES_SCHEMA = {
    "type": "object",
    "additionalProperties": {"anyOf": [{"type": "string"}, {"type": "null"}]},
}


def _parsed_required_attributes_def(
    required_attributes: Any,
) -> List[SsoAttributeRequirement]:
    validate_config(
        REQUIRED_ATTRIBUTES_SCHEMA,
        required_attributes,
        config_path=("cas_config", "required_attributes"),
    )
    return [SsoAttributeRequirement(k, v) for k, v in required_attributes.items()]
