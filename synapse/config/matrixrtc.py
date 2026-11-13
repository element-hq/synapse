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

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from synapse.config.homeserver import HomeServerConfig


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
    telling clients that the stable MSC4195 LiveKit endpoints are served by this server.
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


def validate_msc4195_stable_endpoints_config(
    config: "HomeServerConfig", appservices: Iterable[ApplicationService]
) -> None:
    """Checks that if the config requests to advertise the stable MSC4195 endpoints
    we also have an application service that covers the `rtc/livekit` namespace.

    This can only happen once the application service registrations have been loaded,
    which is why it isn't done in `MatrixRtcConfig.read_config`.

    Args:
        config: The homeserver config.
        appservices: The loaded application services.

    Raises:
        ConfigError: If the feature flag is enabled but no application service
            proxies the LiveKit endpoints.
    """
    if not config.matrix_rtc.msc4195_stable_endpoints:
        return

    if not config.experimental.msc4512_enabled:
        raise ConfigError(
            "`matrix_rtc.msc4195_stable_endpoints` requires "
            "`experimental_features.msc4512_enabled` to be enabled so "
            "that an application service can proxy the MSC4195 endpoints.",
            ("matrix_rtc", "msc4195_stable_endpoints"),
        )

    if not any(
        appservice.is_path_covered_by_proxy_prefix(
            ApplicationService.LIVEKIT_PROXY_PREFIX
        )
        for appservice in appservices
    ):
        raise ConfigError(
            "`matrix_rtc.msc4195_stable_endpoints` requires an application "
            " service to be configured for proxying requests in the "
            f"`{ApplicationService.LIVEKIT_PROXY_PREFIX}` namespace.",
            ("matrix_rtc", "msc4195_stable_endpoints"),
        )
