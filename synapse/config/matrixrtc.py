#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# [This file includes modifications made by New Vector Limited]
#
#

from functools import cached_property
from typing import Any

from pydantic import (
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)
from typing_extensions import Self

from synapse.appservice import ApplicationService
from synapse.types import JsonDict
from synapse.util.pydantic_models import ParseModel

from ._base import Config, ConfigError
from .appservice import load_appservices


class TransportConfigModel(ParseModel):
    type: StrictStr

    url: StrictStr | None = Field(default=None)
    """
    An optional WebSocket URL pointing to the LiveKit SFU. If type is "m.livekit", this is required.
    If type is "livekit", either this or livekit_service_url is required.
    """

    livekit_service_url: StrictStr | None = Field(default=None)
    """Deprecated. An optional HTTP URL pointing to the LiveKit authorization service. If type is "livekit", either this or url is required."""

    @model_validator(mode="after")
    def validate_livekit_transport(self) -> Self:
        if self.type == "livekit" and not self.url and not self.livekit_service_url:
            raise ValueError(
                "You must set either `url` or `livekit_service_url` when using the 'livekit' transport."
            )
        if self.type == "m.livekit" and not self.url:
            raise ValueError("You must set `url` when using the 'm.livekit' transport.")
        return self


class MatrixRtcConfigModel(ParseModel):
    transports: list[dict[str, Any]] = []

    msc4195_stable_endpoints: StrictBool = False
    """
    Whether to advertise the `org.matrix.msc4195.stable` feature flag in `/versions`,
    telling clients that the stable MSC4195 endpoints are served by this server, when
    an application service proxies these endpoints.
    """

    @field_validator("transports")
    @classmethod
    def validate_transports(
        cls, transports: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate each transport by attempting to construct a `TransportConfigModel`
        from it, raising a `ValidationError` if construction fails."""
        for transport in transports:
            TransportConfigModel(**transport)
        return transports


class MatrixRtcConfig(Config):
    section = "matrix_rtc"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:
        matrix_rtc = config.get("matrix_rtc", {})
        if matrix_rtc is None:
            matrix_rtc = {}

        try:
            parsed = MatrixRtcConfigModel(**matrix_rtc)
        except ValidationError as e:
            raise ConfigError(
                "Could not validate matrix_rtc config",
                ("matrix_rtc",),
            ) from e

        self.transports = parsed.transports
        self.msc4195_stable_endpoints = parsed.msc4195_stable_endpoints

    @cached_property
    def livekit_proxied_to_appservice(self) -> bool:
        """Whether an application service proxies the MSC4195 LiveKit endpoints
        (MSC4512), in which case Synapse's default implementation is not used."""
        if not self.root.experimental.msc4512_enabled:
            return False
        return any(
            appservice.proxy_prefix is not None
            and appservice.proxy_prefix.path_prefix
            == ApplicationService.LIVEKIT_PROXY_PREFIX
            for appservice in load_appservices(
                self.root.server.server_name,
                self.root.appservice.app_service_config_files,
            )
        )

    @cached_property
    def advertise_msc4195_stable(self) -> bool:
        """Whether to advertise `org.matrix.msc4195.stable` in `/versions`.

        Synapse's default implementation includes all the stable endpoints. If an
        application service proxies the endpoints instead, whether they are
        supported depends on the service, which the admin has to tell us.
        """
        if self.livekit_proxied_to_appservice:
            return self.msc4195_stable_endpoints
        return True
