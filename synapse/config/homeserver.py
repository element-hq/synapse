#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from ._base import ConfigError, RootConfig
from .account_validity import AccountValidityConfig
from .api import ApiConfig
from .appservice import AppServiceConfig
from .auth import AuthConfig
from .auto_accept_invites import AutoAcceptInvitesConfig
from .background_updates import BackgroundUpdateConfig
from .cache import CacheConfig
from .captcha import CaptchaConfig
from .cas import CasConfig
from .consent import ConsentConfig
from .database import DatabaseConfig
from .emailconfig import EmailConfig
from .experimental import ExperimentalConfig
from .federation import FederationConfig
from .jwt import JWTConfig
from .key import KeyConfig
from .logger import LoggingConfig
from .mas import MasConfig
from .matrixrtc import MatrixRtcConfig
from .metrics import MetricsConfig
from .modules import ModulesConfig
from .oembed import OembedConfig
from .oidc import OIDCConfig
from .password_auth_providers import PasswordAuthProviderConfig
from .push import PushConfig
from .ratelimiting import RatelimitConfig
from .redis import RedisConfig
from .registration import RegistrationConfig
from .repository import ContentRepositoryConfig
from .retention import RetentionConfig
from .room import RoomConfig
from .room_directory import RoomDirectoryConfig
from .saml2 import SAML2Config
from .server import ServerConfig
from .server_notices import ServerNoticesConfig
from .spam_checker import SpamCheckerConfig
from .sso import SSOConfig
from .stats import StatsConfig
from .third_party_event_rules import ThirdPartyRulesConfig
from .tls import TlsConfig
from .tracer import TracerConfig
from .user_directory import UserDirectoryConfig
from .user_types import UserTypesConfig
from .voip import VoipConfig
from .workers import WorkerConfig


class HomeServerConfig(RootConfig):
    """
    Top-level config object for Synapse homeserver (main process and workers).
    """

    config_classes = [
        ModulesConfig,
        ServerConfig,
        RetentionConfig,
        TlsConfig,
        FederationConfig,
        CacheConfig,
        DatabaseConfig,
        LoggingConfig,
        RatelimitConfig,
        ContentRepositoryConfig,
        OembedConfig,
        CaptchaConfig,
        VoipConfig,
        MatrixRtcConfig,
        RegistrationConfig,
        AccountValidityConfig,
        MetricsConfig,
        ApiConfig,
        AppServiceConfig,
        KeyConfig,
        SAML2Config,
        OIDCConfig,
        CasConfig,
        SSOConfig,
        JWTConfig,
        AuthConfig,
        EmailConfig,
        PasswordAuthProviderConfig,
        PushConfig,
        SpamCheckerConfig,
        RoomConfig,
        UserDirectoryConfig,
        ConsentConfig,
        StatsConfig,
        ServerNoticesConfig,
        RoomDirectoryConfig,
        ThirdPartyRulesConfig,
        TracerConfig,
        WorkerConfig,
        RedisConfig,
        ExperimentalConfig,
        BackgroundUpdateConfig,
        AutoAcceptInvitesConfig,
        UserTypesConfig,
        # This must be last, as it checks for conflicts with other config options.
        MasConfig,
    ]

    def validate_config(
        self,
    ) -> None:
        if (
            self.registration.enable_registration
            and not self.registration.enable_registration_without_verification
        ):
            if (
                not self.captcha.enable_registration_captcha
                and not self.registration.registrations_require_3pid
                and not self.registration.registration_requires_token
            ):
                raise ConfigError(
                    "You have enabled open registration without any verification. This is a known vector for "
                    "spam and abuse. If you would like to allow public registration, please consider adding email, "
                    "captcha, or token-based verification. Otherwise this check can be removed by setting the "
                    "`enable_registration_without_verification` config option to `true`."
                )
