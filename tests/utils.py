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

import atexit
import os
import signal
from types import FrameType, TracebackType
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    overload,
)

import attr
from typing_extensions import Literal, ParamSpec

from synapse.api.constants import EventTypes
from synapse.api.room_versions import RoomVersions
from synapse.config.homeserver import HomeServerConfig
from synapse.config.server import DEFAULT_ROOM_VERSION
from synapse.logging.context import current_context, set_current_context
from synapse.server import HomeServer
from synapse.storage.database import LoggingDatabaseConnection
from synapse.storage.engines import create_engine
from synapse.storage.prepare_database import prepare_database

try:
    import authlib  # noqa: F401

    HAS_AUTHLIB = True
except ImportError:
    HAS_AUTHLIB = False

# set this to True to run the tests against postgres instead of sqlite.
#
# When running under postgres, we first create a base database with the name
# POSTGRES_BASE_DB and update it to the current schema. Then, for each test case, we
# create another unique database, using the base database as a template.
USE_POSTGRES_FOR_TESTS = os.environ.get("SYNAPSE_POSTGRES", False)
LEAVE_DB = os.environ.get("SYNAPSE_LEAVE_DB", False)
POSTGRES_USER = os.environ.get("SYNAPSE_POSTGRES_USER", None)
POSTGRES_HOST = os.environ.get("SYNAPSE_POSTGRES_HOST", None)
POSTGRES_PASSWORD = os.environ.get("SYNAPSE_POSTGRES_PASSWORD", None)
POSTGRES_PORT = (
    int(os.environ["SYNAPSE_POSTGRES_PORT"])
    if "SYNAPSE_POSTGRES_PORT" in os.environ
    else None
)
POSTGRES_BASE_DB = "_synapse_unit_tests_base_%s" % (os.getpid(),)

# When debugging a specific test, it's occasionally useful to write the
# DB to disk and query it with the sqlite CLI.
SQLITE_PERSIST_DB = os.environ.get("SYNAPSE_TEST_PERSIST_SQLITE_DB") is not None

# the dbname we will connect to in order to create the base database.
POSTGRES_DBNAME_FOR_INITIAL_CREATE = "postgres"


def setupdb() -> None:
    # If we're using PostgreSQL, set up the db once
    if USE_POSTGRES_FOR_TESTS:
        # create a PostgresEngine
        db_engine = create_engine({"name": "psycopg2", "args": {}})
        # connect to postgres to create the base database.
        db_conn = db_engine.module.connect(
            user=POSTGRES_USER,
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            password=POSTGRES_PASSWORD,
            dbname=POSTGRES_DBNAME_FOR_INITIAL_CREATE,
        )
        db_engine.attempt_to_set_autocommit(db_conn, autocommit=True)
        cur = db_conn.cursor()
        cur.execute("DROP DATABASE IF EXISTS %s;" % (POSTGRES_BASE_DB,))
        cur.execute(
            "CREATE DATABASE %s ENCODING 'UTF8' LC_COLLATE='C' LC_CTYPE='C' "
            "template=template0;" % (POSTGRES_BASE_DB,)
        )
        cur.close()
        db_conn.close()

        # Set up in the db
        db_conn = db_engine.module.connect(
            dbname=POSTGRES_BASE_DB,
            user=POSTGRES_USER,
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            password=POSTGRES_PASSWORD,
        )
        logging_conn = LoggingDatabaseConnection(db_conn, db_engine, "tests")
        prepare_database(logging_conn, db_engine, None)
        logging_conn.close()

        def _cleanup() -> None:
            db_conn = db_engine.module.connect(
                user=POSTGRES_USER,
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                password=POSTGRES_PASSWORD,
                dbname=POSTGRES_DBNAME_FOR_INITIAL_CREATE,
            )
            db_engine.attempt_to_set_autocommit(db_conn, autocommit=True)
            cur = db_conn.cursor()
            cur.execute("DROP DATABASE IF EXISTS %s;" % (POSTGRES_BASE_DB,))
            cur.close()
            db_conn.close()

        atexit.register(_cleanup)


@overload
def default_config(name: str, parse: Literal[False] = ...) -> Dict[str, object]: ...


@overload
def default_config(name: str, parse: Literal[True]) -> HomeServerConfig: ...


def default_config(
    name: str, parse: bool = False
) -> Union[Dict[str, object], HomeServerConfig]:
    """
    Create a reasonable test config.
    """
    config_dict = {
        "server_name": name,
        # Setting this to an empty list turns off federation sending.
        "federation_sender_instances": [],
        "media_store_path": "media",
        # the test signing key is just an arbitrary ed25519 key to keep the config
        # parser happy
        "signing_key": "ed25519 a_lPym qvioDNmfExFBRPgdTU+wtFYKq4JfwFRv7sYVgWvmgJg",
        # Disable trusted key servers, otherwise unit tests might try to actually
        # reach out to matrix.org.
        "trusted_key_servers": [],
        "event_cache_size": 1,
        "enable_registration": True,
        "enable_registration_captcha": False,
        "macaroon_secret_key": "not even a little secret",
        "password_providers": [],
        "worker_app": None,
        "block_non_admin_invites": False,
        "federation_domain_whitelist": None,
        "filter_timeline_limit": 5000,
        "user_directory_search_all_users": False,
        "user_consent_server_notice_content": None,
        "block_events_without_consent_error": None,
        "user_consent_at_registration": False,
        "user_consent_policy_name": "Privacy Policy",
        "media_storage_providers": [],
        "autocreate_auto_join_rooms": True,
        "auto_join_rooms": [],
        "limit_usage_by_mau": False,
        "hs_disabled": False,
        "hs_disabled_message": "",
        "max_mau_value": 50,
        "mau_trial_days": 0,
        "mau_stats_only": False,
        "mau_limits_reserved_threepids": [],
        "admin_contact": None,
        "rc_message": {"per_second": 10000, "burst_count": 10000},
        "rc_registration": {"per_second": 10000, "burst_count": 10000},
        "rc_login": {
            "address": {"per_second": 10000, "burst_count": 10000},
            "account": {"per_second": 10000, "burst_count": 10000},
            "failed_attempts": {"per_second": 10000, "burst_count": 10000},
        },
        "rc_joins": {
            "local": {"per_second": 10000, "burst_count": 10000},
            "remote": {"per_second": 10000, "burst_count": 10000},
        },
        "rc_joins_per_room": {"per_second": 10000, "burst_count": 10000},
        "rc_invites": {
            "per_room": {"per_second": 10000, "burst_count": 10000},
            "per_user": {"per_second": 10000, "burst_count": 10000},
        },
        "rc_3pid_validation": {"per_second": 10000, "burst_count": 10000},
        "saml2_enabled": False,
        "public_baseurl": None,
        "default_identity_server": None,
        "key_refresh_interval": 24 * 60 * 60 * 1000,
        "old_signing_keys": {},
        "tls_fingerprints": [],
        "use_frozen_dicts": False,
        # We need a sane default_room_version, otherwise attempts to create
        # rooms will fail.
        "default_room_version": DEFAULT_ROOM_VERSION,
        # disable user directory updates, because they get done in the
        # background, which upsets the test runner. Setting this to an
        # (obviously) fake worker name disables updating the user directory.
        "update_user_directory_from_worker": "does_not_exist_worker_name",
        "caches": {"global_factor": 1, "sync_response_cache_duration": 0},
        "listeners": [{"port": 0, "type": "http"}],
    }

    if parse:
        config = HomeServerConfig()
        config.parse_config_dict(config_dict, "", "")
        return config

    return config_dict


def mock_getRawHeaders(headers=None):  # type: ignore[no-untyped-def]
    headers = headers if headers is not None else {}

    def getRawHeaders(name, default=None):  # type: ignore[no-untyped-def]
        # If the requested header is present, the real twisted function returns
        # List[str] if name is a str and List[bytes] if name is a bytes.
        # This mock doesn't support that behaviour.
        # Fortunately, none of the current callers of mock_getRawHeaders() provide a
        # headers dict, so we don't encounter this discrepancy in practice.
        return headers.get(name, default)

    return getRawHeaders


P = ParamSpec("P")


@attr.s(slots=True, auto_attribs=True)
class Timer:
    absolute_time: float
    callback: Callable[[], None]
    expired: bool


# TODO: Make this generic over a ParamSpec?
@attr.s(slots=True, auto_attribs=True)
class Looper:
    func: Callable[..., Any]
    interval: float  # seconds
    last: float
    args: Tuple[object, ...]
    kwargs: Dict[str, object]


class MockClock:
    now = 1000.0

    def __init__(self) -> None:
        # Timers in no particular order
        self.timers: List[Timer] = []
        self.loopers: List[Looper] = []

    def time(self) -> float:
        return self.now

    def time_msec(self) -> int:
        return int(self.time() * 1000)

    def call_later(
        self,
        delay: float,
        callback: Callable[P, object],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Timer:
        ctx = current_context()

        def wrapped_callback() -> None:
            set_current_context(ctx)
            callback(*args, **kwargs)

        t = Timer(self.now + delay, wrapped_callback, False)
        self.timers.append(t)

        return t

    def looping_call(
        self,
        function: Callable[P, object],
        interval: float,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        self.loopers.append(Looper(function, interval / 1000.0, self.now, args, kwargs))

    def cancel_call_later(self, timer: Timer, ignore_errs: bool = False) -> None:
        if timer.expired:
            if not ignore_errs:
                raise Exception("Cannot cancel an expired timer")

        timer.expired = True
        self.timers = [t for t in self.timers if t != timer]

    # For unit testing
    def advance_time(self, secs: float) -> None:
        self.now += secs

        timers = self.timers
        self.timers = []

        for t in timers:
            if t.expired:
                raise Exception("Timer already expired")

            if self.now >= t.absolute_time:
                t.expired = True
                t.callback()
            else:
                self.timers.append(t)

        for looped in self.loopers:
            if looped.last + looped.interval < self.now:
                looped.func(*looped.args, **looped.kwargs)
                looped.last = self.now

    def advance_time_msec(self, ms: float) -> None:
        self.advance_time(ms / 1000.0)


async def create_room(hs: HomeServer, room_id: str, creator_id: str) -> None:
    """Creates and persist a creation event for the given room"""

    persistence_store = hs.get_storage_controllers().persistence
    assert persistence_store is not None
    store = hs.get_datastores().main
    event_builder_factory = hs.get_event_builder_factory()
    event_creation_handler = hs.get_event_creation_handler()

    await store.store_room(
        room_id=room_id,
        room_creator_user_id=creator_id,
        is_public=False,
        room_version=RoomVersions.V1,
    )

    builder = event_builder_factory.for_room_version(
        RoomVersions.V1,
        {
            "type": EventTypes.Create,
            "state_key": "",
            "sender": creator_id,
            "room_id": room_id,
            "content": {},
        },
    )

    event, unpersisted_context = await event_creation_handler.create_new_client_event(
        builder
    )
    context = await unpersisted_context.persist(event)

    await persistence_store.persist_event(event, context)


T = TypeVar("T")


def checked_cast(type: Type[T], x: object) -> T:
    """A version of typing.cast that is checked at runtime.

    We have our own function for this for two reasons:

    1. typing.cast itself is deliberately a no-op at runtime, see
       https://docs.python.org/3/library/typing.html#typing.cast
    2. To help workaround a mypy-zope bug https://github.com/Shoobx/mypy-zope/issues/91
       where mypy would erroneously consider `isinstance(x, type)` to be false in all
       circumstances.

    For this to make sense, `T` needs to be something that `isinstance` can check; see
        https://docs.python.org/3/library/functions.html?highlight=isinstance#isinstance
        https://docs.python.org/3/glossary.html#term-abstract-base-class
        https://docs.python.org/3/library/typing.html#typing.runtime_checkable
    for more details.
    """
    assert isinstance(x, type)
    return x


class TestTimeout(Exception):
    pass


class test_timeout:
    def __init__(self, seconds: int, error_message: Optional[str] = None) -> None:
        if error_message is None:
            error_message = "test timed out after {}s.".format(seconds)
        self.seconds = seconds
        self.error_message = error_message

    def handle_timeout(self, signum: int, frame: Optional[FrameType]) -> None:
        raise TestTimeout(self.error_message)

    def __enter__(self) -> None:
        signal.signal(signal.SIGALRM, self.handle_timeout)
        signal.alarm(self.seconds)

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        signal.alarm(0)
