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

import logging
from typing import Any
from urllib import parse as urlparse

import yaml
from netaddr import IPSet

from synapse.appservice import ApplicationService
from synapse.types import JsonDict, UserID

from ._base import Config, ConfigError

logger = logging.getLogger(__name__)


class AppServiceConfig(Config):
    section = "appservice"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        self.app_service_config_files = config.get("app_service_config_files", [])
        if not isinstance(self.app_service_config_files, list) or not all(
            isinstance(x, str) for x in self.app_service_config_files
        ):
            raise ConfigError(
                "Expected '%s' to be a list of AS config files:"
                % (self.app_service_config_files),
                ("app_service_config_files",),
            )

        self.track_appservice_user_ips = config.get("track_appservice_user_ips", False)
        self.use_appservice_legacy_authorization = config.get(
            "use_appservice_legacy_authorization", False
        )
        if self.use_appservice_legacy_authorization:
            logger.warning(
                "The use of appservice legacy authorization via query params is deprecated"
                " and should be considered insecure."
            )


def load_appservices(
    hostname: str, config_files: list[str]
) -> list[ApplicationService]:
    """Returns a list of Application Services from the config files."""

    # Dicts of value -> filename
    seen_as_tokens: dict[str, str] = {}
    seen_ids: dict[str, str] = {}

    appservices = []

    for config_file in config_files:
        try:
            with open(config_file) as f:
                appservice = _load_appservice(hostname, yaml.safe_load(f), config_file)
                if appservice.id in seen_ids:
                    raise ConfigError(
                        "Cannot reuse ID across application services: "
                        "%s (files: %s, %s)"
                        % (appservice.id, config_file, seen_ids[appservice.id])
                    )
                seen_ids[appservice.id] = config_file
                if appservice.token in seen_as_tokens:
                    raise ConfigError(
                        "Cannot reuse as_token across application services: "
                        "%s (files: %s, %s)"
                        % (
                            appservice.token,
                            config_file,
                            seen_as_tokens[appservice.token],
                        )
                    )
                seen_as_tokens[appservice.token] = config_file
                logger.info("Loaded application service: %s", appservice)
                appservices.append(appservice)
        except Exception as e:
            logger.error("Failed to load appservice from '%s'", config_file)
            logger.exception(e)
            raise
    return appservices


def _load_appservice(
    hostname: str, as_info: JsonDict, config_filename: str
) -> ApplicationService:
    required_string_fields = ["id", "as_token", "hs_token", "sender_localpart"]
    for field in required_string_fields:
        if not isinstance(as_info.get(field), str):
            raise KeyError(
                "Required string field: '%s' (%s)" % (field, config_filename)
            )

    # 'url' must either be a string or explicitly null, not missing
    # to avoid accidentally turning off push for ASes.
    if not isinstance(as_info.get("url"), str) and as_info.get("url", "") is not None:
        raise KeyError(
            "Required string field or explicit null: 'url' (%s)" % (config_filename,)
        )

    localpart = as_info["sender_localpart"]
    if urlparse.quote(localpart) != localpart:
        raise ValueError("sender_localpart needs characters which are not URL encoded.")
    user_id = UserID(localpart, hostname)

    # Rate limiting for users of this AS is on by default (excludes sender)
    rate_limited = as_info.get("rate_limited")
    if not isinstance(rate_limited, bool):
        rate_limited = True

    # namespace checks
    if not isinstance(as_info.get("namespaces"), dict):
        raise KeyError("Requires 'namespaces' object.")
    for ns in ApplicationService.NS_LIST:
        # specific namespaces are optional
        if ns in as_info["namespaces"]:
            # expect a list of dicts with exclusive and regex keys
            for regex_obj in as_info["namespaces"][ns]:
                if not isinstance(regex_obj, dict):
                    raise ValueError(
                        "Expected namespace entry in %s to be an object, but got %s",
                        ns,
                        regex_obj,
                    )
                if not isinstance(regex_obj.get("regex"), str):
                    raise ValueError("Missing/bad type 'regex' key in %s", regex_obj)
                if not isinstance(regex_obj.get("exclusive"), bool):
                    raise ValueError(
                        "Missing/bad type 'exclusive' key in %s", regex_obj
                    )
    # protocols check
    protocols = as_info.get("protocols")
    if protocols:
        if not isinstance(protocols, list):
            raise KeyError("Optional 'protocols' must be a list if present.")
        for p in protocols:
            if not isinstance(p, str):
                raise KeyError("Bad value for 'protocols' item")

    if as_info["url"] is None:
        logger.info(
            "(%s) Explicitly empty 'url' provided. This application service"
            " will not receive events or queries.",
            config_filename,
        )

    ip_range_whitelist = None
    if as_info.get("ip_range_whitelist"):
        ip_range_whitelist = IPSet(as_info.get("ip_range_whitelist"))

    supports_ephemeral = as_info.get("de.sorunome.msc2409.push_ephemeral", False)

    # Opt-in flag for the MSC3202-specific transactional behaviour.
    # When enabled, appservice transactions contain the following information:
    #  - device One-Time Key counts
    #  - device unused fallback key usage states
    #  - device list changes
    msc3202_transaction_extensions = as_info.get("org.matrix.msc3202", False)
    if not isinstance(msc3202_transaction_extensions, bool):
        raise ValueError(
            "The `org.matrix.msc3202` option should be true or false if specified."
        )

    # Opt-in flag for the MSC4190 behaviours.
    # When enabled, the following C-S API endpoints change for appservices:
    # - POST /register does not return an access token
    # - PUT /devices/{device_id} creates a new device if one does not exist
    # - DELETE /devices/{device_id} no longer requires UIA
    # - POST /delete_devices/{device_id} no longer requires UIA
    msc4190_enabled = as_info.get("io.element.msc4190", False)
    if not isinstance(msc4190_enabled, bool):
        raise ValueError(
            "The `io.element.msc4190` option should be true or false if specified."
        )

    return ApplicationService(
        token=as_info["as_token"],
        url=as_info["url"],
        namespaces=as_info["namespaces"],
        hs_token=as_info["hs_token"],
        sender=user_id,
        id=as_info["id"],
        protocols=protocols,
        rate_limited=rate_limited,
        ip_range_whitelist=ip_range_whitelist,
        supports_ephemeral=supports_ephemeral,
        msc3202_transaction_extensions=msc3202_transaction_extensions,
        msc4190_device_management=msc4190_enabled,
    )
