import argparse
from typing import (
    Any,
    Collection,
    Iterable,
    Iterator,
    Literal,
    MutableMapping,
    TypeVar,
    overload,
)

import jinja2

from synapse.config import (  # noqa: F401
    account_validity,
    api,
    appservice,
    auth,
    auto_accept_invites,
    background_updates,
    cache,
    captcha,
    cas,
    consent,
    database,
    emailconfig,
    experimental,
    federation,
    jwt,
    key,
    logger,
    mas,
    matrixrtc,
    metrics,
    modules,
    oembed,
    oidc,
    password_auth_providers,
    push,
    ratelimiting,
    redis,
    registration,
    repository,
    retention,
    room,
    room_directory,
    saml2,
    server,
    server_notices,
    spam_checker,
    sso,
    stats,
    third_party_event_rules,
    tls,
    tracer,
    user_directory,
    user_types,
    voip,
    workers,
)
from synapse.types import StrSequence

class ConfigError(Exception):
    def __init__(self, msg: str, path: StrSequence | None = None):
        self.msg = msg
        self.path = path

def format_config_error(e: ConfigError) -> Iterator[str]: ...

MISSING_REPORT_STATS_CONFIG_INSTRUCTIONS: str
MISSING_REPORT_STATS_SPIEL: str
MISSING_SERVER_NAME: str

def path_exists(file_path: str) -> bool: ...

TRootConfig = TypeVar("TRootConfig", bound="RootConfig")

class RootConfig:
    server: server.ServerConfig
    experimental: experimental.ExperimentalConfig
    tls: tls.TlsConfig
    database: database.DatabaseConfig
    logging: logger.LoggingConfig
    ratelimiting: ratelimiting.RatelimitConfig
    media: repository.ContentRepositoryConfig
    oembed: oembed.OembedConfig
    captcha: captcha.CaptchaConfig
    voip: voip.VoipConfig
    registration: registration.RegistrationConfig
    account_validity: account_validity.AccountValidityConfig
    metrics: metrics.MetricsConfig
    api: api.ApiConfig
    appservice: appservice.AppServiceConfig
    key: key.KeyConfig
    saml2: saml2.SAML2Config
    cas: cas.CasConfig
    sso: sso.SSOConfig
    oidc: oidc.OIDCConfig
    jwt: jwt.JWTConfig
    auth: auth.AuthConfig
    email: emailconfig.EmailConfig
    worker: workers.WorkerConfig
    authproviders: password_auth_providers.PasswordAuthProviderConfig
    push: push.PushConfig
    spamchecker: spam_checker.SpamCheckerConfig
    room: room.RoomConfig
    userdirectory: user_directory.UserDirectoryConfig
    consent: consent.ConsentConfig
    stats: stats.StatsConfig
    servernotices: server_notices.ServerNoticesConfig
    roomdirectory: room_directory.RoomDirectoryConfig
    thirdpartyrules: third_party_event_rules.ThirdPartyRulesConfig
    tracing: tracer.TracerConfig
    redis: redis.RedisConfig
    modules: modules.ModulesConfig
    caches: cache.CacheConfig
    federation: federation.FederationConfig
    retention: retention.RetentionConfig
    background_updates: background_updates.BackgroundUpdateConfig
    auto_accept_invites: auto_accept_invites.AutoAcceptInvitesConfig
    user_types: user_types.UserTypesConfig
    mas: mas.MasConfig
    matrix_rtc: matrixrtc.MatrixRtcConfig

    config_classes: list[type["Config"]] = ...
    config_files: list[str]
    def __init__(self, config_files: Collection[str] = ...) -> None: ...
    def invoke_all(
        self, func_name: str, *args: Any, **kwargs: Any
    ) -> MutableMapping[str, Any]: ...
    @classmethod
    def invoke_all_static(cls, func_name: str, *args: Any, **kwargs: Any) -> None: ...
    def parse_config_dict(
        self,
        config_dict: dict[str, Any],
        config_dir_path: str,
        data_dir_path: str,
        allow_secrets_in_config: bool = ...,
    ) -> None: ...
    def generate_config(
        self,
        config_dir_path: str,
        data_dir_path: str,
        server_name: str,
        generate_secrets: bool = ...,
        report_stats: bool | None = ...,
        open_private_ports: bool = ...,
        listeners: Any | None = ...,
        tls_certificate_path: str | None = ...,
        tls_private_key_path: str | None = ...,
    ) -> str: ...
    @classmethod
    def load_or_generate_config(
        cls: type[TRootConfig], description: str, argv_options: list[str]
    ) -> TRootConfig | None: ...
    @classmethod
    def load_config(
        cls: type[TRootConfig], description: str, argv_options: list[str]
    ) -> TRootConfig: ...
    @classmethod
    def add_arguments_to_parser(
        cls, config_parser: argparse.ArgumentParser
    ) -> None: ...
    @classmethod
    def load_config_with_parser(
        cls: type[TRootConfig], parser: argparse.ArgumentParser, argv_options: list[str]
    ) -> tuple[TRootConfig, argparse.Namespace]: ...
    def generate_missing_files(
        self, config_dict: dict, config_dir_path: str
    ) -> None: ...
    @overload
    def reload_config_section(
        self, section_name: Literal["caches"]
    ) -> cache.CacheConfig: ...
    @overload
    def reload_config_section(self, section_name: str) -> "Config": ...

class Config:
    root: RootConfig
    default_template_dir: str
    def __init__(self, root_config: RootConfig = ...) -> None: ...
    @staticmethod
    def parse_size(value: str | int) -> int: ...
    @staticmethod
    def parse_duration(value: str | int) -> int: ...
    @staticmethod
    def abspath(file_path: str | None) -> str: ...
    @classmethod
    def path_exists(cls, file_path: str) -> bool: ...
    @classmethod
    def check_file(cls, file_path: str, config_name: str) -> str: ...
    @classmethod
    def ensure_directory(cls, dir_path: str) -> str: ...
    @classmethod
    def read_file(cls, file_path: str, config_name: str) -> str: ...
    def read_template(self, filenames: str) -> jinja2.Template: ...
    def read_templates(
        self,
        filenames: list[str],
        custom_template_directories: Iterable[str] | None = None,
    ) -> list[jinja2.Template]: ...

def read_config_files(config_files: Iterable[str]) -> dict[str, Any]: ...
def find_config_files(search_paths: list[str]) -> list[str]: ...

class ShardedWorkerHandlingConfig:
    instances: list[str]
    def __init__(self, instances: list[str]) -> None: ...
    def should_handle(self, instance_name: str, key: str) -> bool: ...  # noqa: F811

class RoutableShardedWorkerHandlingConfig(ShardedWorkerHandlingConfig):
    def get_instance(self, key: str) -> str: ...  # noqa: F811

def read_file(file_path: Any, config_path: StrSequence) -> str: ...
