#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014 - 2021 The Matrix.org Foundation C.I.C.
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
import importlib.util
import logging
import os
import re
from collections import Counter
from typing import (
    Collection,
    Counter as CounterType,
    Generator,
    Iterable,
    List,
    Optional,
    TextIO,
    Tuple,
)

import attr

from synapse.config.homeserver import HomeServerConfig
from synapse.storage.database import LoggingDatabaseConnection, LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine, Sqlite3Engine
from synapse.storage.schema import SCHEMA_COMPAT_VERSION, SCHEMA_VERSION
from synapse.storage.types import Cursor

logger = logging.getLogger(__name__)


schema_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "schema")


class PrepareDatabaseException(Exception):
    pass


class UpgradeDatabaseException(PrepareDatabaseException):
    pass


OUTDATED_SCHEMA_ON_WORKER_ERROR = (
    "Expected database schema version %i but got %i: run the main synapse process to "
    "upgrade the database schema before starting worker processes."
)

EMPTY_DATABASE_ON_WORKER_ERROR = (
    "Uninitialised database: run the main synapse process to prepare the database "
    "schema before starting worker processes."
)

UNAPPLIED_DELTA_ON_WORKER_ERROR = (
    "Database schema delta %s has not been applied: run the main synapse process to "
    "upgrade the database schema before starting worker processes."
)


@attr.s
class _SchemaState:
    current_version: int = attr.ib()
    """The current schema version of the database"""

    compat_version: Optional[int] = attr.ib()
    """The SCHEMA_VERSION of the oldest version of Synapse for this database

    If this is None, we have an old version of the database without the necessary
    table.
    """

    applied_deltas: Collection[str] = attr.ib(factory=tuple)
    """Any delta files for `current_version` which have already been applied"""

    upgraded: bool = attr.ib(default=False)
    """Whether the current state was reached by applying deltas.

    If False, we have run the full schema for `current_version`, and have applied no
    deltas since. If True, we have run some deltas since the original creation."""


def prepare_database(
    db_conn: LoggingDatabaseConnection,
    database_engine: BaseDatabaseEngine,
    config: Optional[HomeServerConfig],
    databases: Collection[str] = ("main", "state"),
) -> None:
    """Prepares a physical database for usage. Will either create all necessary tables
    or upgrade from an older schema version.

    If `config` is None then prepare_database will assert that no upgrade is
    necessary, *or* will create a fresh database if the database is empty.

    Args:
        db_conn:
        database_engine:
        config :
            application config, or None if we are connecting to an existing
            database which we expect to be configured already
        databases: The name of the databases that will be used
            with this physical database. Defaults to all databases.
    """

    try:
        cur = db_conn.cursor(txn_name="prepare_database")

        # sqlite does not automatically start transactions for DDL / SELECT statements,
        # so we start one before running anything. This ensures that any upgrades
        # are either applied completely, or not at all.
        #
        # psycopg2 does not automatically start transactions when in autocommit mode.
        # While it is technically harmless to nest transactions in postgres, doing so
        # results in a warning in Postgres' logs per query. And we'd rather like to
        # avoid doing that.
        if isinstance(database_engine, Sqlite3Engine) or (
            isinstance(database_engine, PostgresEngine) and db_conn.autocommit
        ):
            cur.execute("BEGIN TRANSACTION")

        logger.info("%r: Checking existing schema version", databases)
        version_info = _get_or_create_schema_state(cur, database_engine)

        if version_info:
            logger.info(
                "%r: Existing schema is %i (+%i deltas)",
                databases,
                version_info.current_version,
                len(version_info.applied_deltas),
            )

            # config should only be None when we are preparing an in-memory SQLite db,
            # which should be empty.
            if config is None:
                raise ValueError(
                    "config==None in prepare_database, but database is not empty"
                )

            # This should be run on all processes, master or worker. The master will
            # apply the deltas, while workers will check if any outstanding deltas
            # exist and raise an PrepareDatabaseException if they do.
            _upgrade_existing_database(
                cur,
                version_info,
                database_engine,
                config,
                databases=databases,
            )

        else:
            logger.info("%r: Initialising new database", databases)

            # if it's a worker app, refuse to upgrade the database, to avoid multiple
            # workers doing it at once.
            if config and config.worker.worker_app is not None:
                raise UpgradeDatabaseException(EMPTY_DATABASE_ON_WORKER_ERROR)

            _setup_new_database(cur, database_engine, databases=databases)

        # check if any of our configured dynamic modules want a database
        if config is not None:
            _apply_module_schemas(cur, database_engine, config)

        cur.close()
        db_conn.commit()
    except Exception:
        db_conn.rollback()
        raise


def _setup_new_database(
    cur: LoggingTransaction,
    database_engine: BaseDatabaseEngine,
    databases: Collection[str],
) -> None:
    """Sets up the physical database by finding a base set of "full schemas" and
    then applying any necessary deltas, including schemas from the given data
    stores.

    The "full_schemas" directory has subdirectories named after versions. This
    function searches for the highest version less than or equal to
    `SCHEMA_VERSION` and executes all .sql files in that directory.

    The function will then apply all deltas for all versions after the base
    version.

    Example directory structure:

    schema/
        common/
            delta/
                ...
            full_schemas/
                11/
                    foo.sql
        main/
            delta/
                ...
            full_schemas/
                3/
                    test.sql
                    ...
                11/
                    bar.sql
                ...

    In the example foo.sql and bar.sql would be run, and then any delta files
    for versions strictly greater than 11.

    Note: we apply the full schemas and deltas from the `schema/common`
    folder as well those in the databases specified.

    Args:
        cur: a database cursor
        database_engine
        databases: The names of the databases to instantiate on the given physical database.
    """

    # We're about to set up a brand new database so we check that its
    # configured to our liking.
    database_engine.check_new_database(cur)

    full_schemas_dir = os.path.join(schema_path, "common", "full_schemas")

    # First we find the highest full schema version we have
    valid_versions = []

    for filename in os.listdir(full_schemas_dir):
        try:
            ver = int(filename)
        except ValueError:
            continue

        if ver <= SCHEMA_VERSION:
            valid_versions.append(ver)

    if not valid_versions:
        raise PrepareDatabaseException(
            "Could not find a suitable base set of full schemas"
        )

    max_current_ver = max(valid_versions)

    logger.debug("Initialising schema v%d", max_current_ver)

    # Now let's find all the full schema files, both in the common schema and
    # in database schemas.
    directories = [os.path.join(full_schemas_dir, str(max_current_ver))]
    directories.extend(
        os.path.join(
            schema_path,
            database,
            "full_schemas",
            str(max_current_ver),
        )
        for database in databases
    )

    directory_entries: List[_DirectoryListing] = []
    for directory in directories:
        directory_entries.extend(
            _DirectoryListing(file_name, os.path.join(directory, file_name))
            for file_name in os.listdir(directory)
        )

    if isinstance(database_engine, PostgresEngine):
        specific = "postgres"
    else:
        specific = "sqlite"

    directory_entries.sort()
    for entry in directory_entries:
        if entry.file_name.endswith(".sql") or entry.file_name.endswith(
            ".sql." + specific
        ):
            logger.debug("Applying schema %s", entry.absolute_path)
            database_engine.execute_script_file(cur, entry.absolute_path)

    cur.execute(
        "INSERT INTO schema_version (version, upgraded) VALUES (?,?)",
        (max_current_ver, False),
    )

    _upgrade_existing_database(
        cur,
        _SchemaState(current_version=max_current_ver, compat_version=None),
        database_engine=database_engine,
        config=None,
        databases=databases,
        is_empty=True,
    )


def _upgrade_existing_database(
    cur: LoggingTransaction,
    current_schema_state: _SchemaState,
    database_engine: BaseDatabaseEngine,
    config: Optional[HomeServerConfig],
    databases: Collection[str],
    is_empty: bool = False,
) -> None:
    """Upgrades an existing physical database.

    Delta files can either be SQL stored in *.sql files, or python modules
    in *.py.

    There can be multiple delta files per version. Synapse will keep track of
    which delta files have been applied, and will apply any that haven't been
    even if there has been no version bump. This is useful for development
    where orthogonal schema changes may happen on separate branches.

    Different delta files for the same version *must* be orthogonal and give
    the same result when applied in any order. No guarantees are made on the
    order of execution of these scripts.

    This is a no-op of current_version == SCHEMA_VERSION.

    Example directory structure:

        schema/
            delta/
                11/
                    foo.sql
                    ...
                12/
                    foo.sql
                    bar.py
                ...
            full_schemas/
                ...

    In the example, if current_version is 11, then foo.sql will be run if and
    only if `upgraded` is True. Then `foo.sql` and `bar.py` would be run in
    some arbitrary order.

    Note: we apply the delta files from the specified data stores as well as
    those in the top-level schema. We apply all delta files across data stores
    for a version before applying those in the next version.

    Args:
        cur
        current_schema_state: The current version of the schema, as
            returned by _get_or_create_schema_state
        database_engine
        config:
            None if we are initialising a blank database, otherwise the application
            config
        databases: The names of the databases to instantiate
            on the given physical database.
        is_empty: Is this a blank database? I.e. do we need to run the
            upgrade portions of the delta scripts.
    """
    if is_empty:
        assert not current_schema_state.applied_deltas
    else:
        assert config

    is_worker = config and config.worker.worker_app is not None

    # If the schema version needs to be updated, and we are on a worker, we immediately
    # know to bail out as workers cannot update the database schema. Only one process
    # must update the database at the time, therefore we delegate this task to the master.
    if is_worker and current_schema_state.current_version < SCHEMA_VERSION:
        # If the DB is on an older version than we expect then we refuse
        # to start the worker (as the main process needs to run first to
        # update the schema).
        raise UpgradeDatabaseException(
            OUTDATED_SCHEMA_ON_WORKER_ERROR
            % (SCHEMA_VERSION, current_schema_state.current_version)
        )

    if (
        current_schema_state.compat_version is not None
        and current_schema_state.compat_version > SCHEMA_VERSION
    ):
        raise ValueError(
            "Cannot use this database as it is too "
            + "new for the server to understand"
        )

    # some of the deltas assume that server_name is set correctly, so now
    # is a good time to run the sanity check.
    if not is_empty and "main" in databases:
        from synapse.storage.databases.main import check_database_before_upgrade

        assert config is not None
        check_database_before_upgrade(cur, database_engine, config)

    # update schema_compat_version before we run any upgrades, so that if synapse
    # gets downgraded again, it won't try to run against the upgraded database.
    if (
        current_schema_state.compat_version is None
        or current_schema_state.compat_version < SCHEMA_COMPAT_VERSION
    ):
        cur.execute("DELETE FROM schema_compat_version")
        cur.execute(
            "INSERT INTO schema_compat_version(compat_version) VALUES (?)",
            (SCHEMA_COMPAT_VERSION,),
        )

    start_ver = current_schema_state.current_version

    # if we got to this schema version by running a full_schema rather than a series
    # of deltas, we should not run the deltas for this version.
    if not current_schema_state.upgraded:
        start_ver += 1

    logger.debug("applied_delta_files: %s", current_schema_state.applied_deltas)

    if isinstance(database_engine, PostgresEngine):
        specific_engine_extension = ".postgres"
    else:
        specific_engine_extension = ".sqlite"

    specific_engine_extensions = (".sqlite", ".postgres")

    for v in range(start_ver, SCHEMA_VERSION + 1):
        if not is_worker:
            logger.info("Applying schema deltas for v%d", v)

            cur.execute("DELETE FROM schema_version")
            cur.execute(
                "INSERT INTO schema_version (version, upgraded) VALUES (?,?)",
                (v, True),
            )
        else:
            logger.info("Checking schema deltas for v%d", v)

        # We need to search both the global and per data store schema
        # directories for schema updates.

        # First we find the directories to search in
        delta_dir = os.path.join(schema_path, "common", "delta", str(v))
        directories = [delta_dir]
        for database in databases:
            directories.append(os.path.join(schema_path, database, "delta", str(v)))

        # Used to check if we have any duplicate file names
        file_name_counter: CounterType[str] = Counter()

        # Now find which directories have anything of interest.
        directory_entries: List[_DirectoryListing] = []
        for directory in directories:
            logger.debug("Looking for schema deltas in %s", directory)
            try:
                file_names = os.listdir(directory)
                directory_entries.extend(
                    _DirectoryListing(file_name, os.path.join(directory, file_name))
                    for file_name in file_names
                )

                for file_name in file_names:
                    file_name_counter[file_name] += 1
            except FileNotFoundError:
                # Data stores can have empty entries for a given version delta.
                pass
            except OSError:
                raise UpgradeDatabaseException(
                    "Could not open delta dir for version %d: %s" % (v, directory)
                )

        duplicates = {
            file_name for file_name, count in file_name_counter.items() if count > 1
        }
        if duplicates:
            # We don't support using the same file name in the same delta version.
            raise PrepareDatabaseException(
                "Found multiple delta files with the same name in v%d: %s"
                % (
                    v,
                    duplicates,
                )
            )

        # We sort to ensure that we apply the delta files in a consistent
        # order (to avoid bugs caused by inconsistent directory listing order)
        directory_entries.sort()
        for entry in directory_entries:
            file_name = entry.file_name
            relative_path = os.path.join(str(v), file_name)
            absolute_path = entry.absolute_path

            logger.debug("Found file: %s (%s)", relative_path, absolute_path)
            if relative_path in current_schema_state.applied_deltas:
                continue

            root_name, ext = os.path.splitext(file_name)

            if ext == ".py":
                # This is a python upgrade module. We need to import into some
                # package and then execute its `run_upgrade` function.
                if is_worker:
                    raise PrepareDatabaseException(
                        UNAPPLIED_DELTA_ON_WORKER_ERROR % relative_path
                    )

                module_name = "synapse.storage.v%d_%s" % (v, root_name)

                spec = importlib.util.spec_from_file_location(
                    module_name, absolute_path
                )
                if spec is None:
                    raise RuntimeError(
                        f"Could not build a module spec for {module_name} at {absolute_path}"
                    )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)  # type: ignore

                if hasattr(module, "run_create"):
                    logger.info("Running %s:run_create", relative_path)
                    module.run_create(cur, database_engine)

                if not is_empty and hasattr(module, "run_upgrade"):
                    logger.info("Running %s:run_upgrade", relative_path)
                    module.run_upgrade(cur, database_engine, config=config)
            elif ext == ".pyc" or file_name == "__pycache__":
                # Sometimes .pyc files turn up anyway even though we've
                # disabled their generation; e.g. from distribution package
                # installers. Silently skip it
                continue
            elif ext == ".sql":
                # A plain old .sql file, just read and execute it
                if is_worker:
                    raise PrepareDatabaseException(
                        UNAPPLIED_DELTA_ON_WORKER_ERROR % relative_path
                    )
                logger.info("Applying schema %s", relative_path)
                database_engine.execute_script_file(cur, absolute_path)
            elif ext == specific_engine_extension and root_name.endswith(".sql"):
                # A .sql file specific to our engine; just read and execute it
                if is_worker:
                    raise PrepareDatabaseException(
                        UNAPPLIED_DELTA_ON_WORKER_ERROR % relative_path
                    )
                logger.info("Applying engine-specific schema %s", relative_path)
                database_engine.execute_script_file(cur, absolute_path)
            elif ext in specific_engine_extensions and root_name.endswith(".sql"):
                # A .sql file for a different engine; skip it.
                continue
            else:
                # Not a valid delta file.
                logger.warning(
                    "Found directory entry that did not end in .py or .sql: %s",
                    relative_path,
                )
                continue

            # Mark as done.
            cur.execute(
                "INSERT INTO applied_schema_deltas (version, file) VALUES (?,?)",
                (v, relative_path),
            )

    logger.info("Schema now up to date")


def _apply_module_schemas(
    txn: Cursor, database_engine: BaseDatabaseEngine, config: HomeServerConfig
) -> None:
    """Apply the module schemas for the dynamic modules, if any

    Args:
        cur: database cursor
        database_engine:
        config: application config
    """
    # This is the old way for password_auth_provider modules to make changes
    # to the database. This should instead be done using the module API
    for mod, _config in config.authproviders.password_providers:
        if not hasattr(mod, "get_db_schema_files"):
            continue
        modname = ".".join((mod.__module__, mod.__name__))
        _apply_module_schema_files(
            txn, database_engine, modname, mod.get_db_schema_files()
        )


def _apply_module_schema_files(
    cur: Cursor,
    database_engine: BaseDatabaseEngine,
    modname: str,
    names_and_streams: Iterable[Tuple[str, TextIO]],
) -> None:
    """Apply the module schemas for a single module

    Args:
        cur: database cursor
        database_engine: synapse database engine class
        modname: fully qualified name of the module
        names_and_streams: the names and streams of schemas to be applied
    """
    cur.execute(
        "SELECT file FROM applied_module_schemas WHERE module_name = ?",
        (modname,),
    )
    applied_deltas = {d for (d,) in cur}
    for name, stream in names_and_streams:
        if name in applied_deltas:
            continue

        root_name, ext = os.path.splitext(name)
        if ext != ".sql":
            raise PrepareDatabaseException(
                "only .sql files are currently supported for module schemas"
            )

        logger.info("applying schema %s for %s", name, modname)
        execute_statements_from_stream(cur, stream)

        # Mark as done.
        cur.execute(
            "INSERT INTO applied_module_schemas (module_name, file) VALUES (?,?)",
            (modname, name),
        )


def get_statements(f: Iterable[str]) -> Generator[str, None, None]:
    statement_buffer = ""
    in_comment = False  # If we're in a /* ... */ style comment

    for line in f:
        line = line.strip()

        if in_comment:
            # Check if this line contains an end to the comment
            comments = line.split("*/", 1)
            if len(comments) == 1:
                continue
            line = comments[1]
            in_comment = False

        # Remove inline block comments
        line = re.sub(r"/\*.*\*/", " ", line)

        # Does this line start a comment?
        comments = line.split("/*", 1)
        if len(comments) > 1:
            line = comments[0]
            in_comment = True

        # Deal with line comments
        line = line.split("--", 1)[0]
        line = line.split("//", 1)[0]

        # Find *all* semicolons. We need to treat first and last entry
        # specially.
        statements = line.split(";")

        # We must prepend statement_buffer to the first statement
        first_statement = "%s %s" % (statement_buffer.strip(), statements[0].strip())
        statements[0] = first_statement

        # Every entry, except the last, is a full statement
        for statement in statements[:-1]:
            yield statement.strip()

        # The last entry did *not* end in a semicolon, so we store it for the
        # next semicolon we find
        statement_buffer = statements[-1].strip()


def executescript(txn: Cursor, schema_path: str) -> None:
    with open(schema_path) as f:
        execute_statements_from_stream(txn, f)


def execute_statements_from_stream(cur: Cursor, f: TextIO) -> None:
    for statement in get_statements(f):
        cur.execute(statement)


def _get_or_create_schema_state(
    txn: Cursor, database_engine: BaseDatabaseEngine
) -> Optional[_SchemaState]:
    # Bluntly try creating the schema_version tables.
    sql_path = os.path.join(schema_path, "common", "schema_version.sql")
    database_engine.execute_script_file(txn, sql_path)

    txn.execute("SELECT version, upgraded FROM schema_version")
    row = txn.fetchone()

    if row is None:
        # new database
        return None

    current_version = int(row[0])
    upgraded = bool(row[1])

    compat_version: Optional[int] = None
    txn.execute("SELECT compat_version FROM schema_compat_version")
    row = txn.fetchone()
    if row is not None:
        compat_version = int(row[0])

    txn.execute(
        "SELECT file FROM applied_schema_deltas WHERE version >= ?",
        (current_version,),
    )
    applied_deltas = tuple(d for (d,) in txn)

    return _SchemaState(
        current_version=current_version,
        compat_version=compat_version,
        applied_deltas=applied_deltas,
        upgraded=upgraded,
    )


@attr.s(slots=True, auto_attribs=True)
class _DirectoryListing:
    """Helper class to store schema file name and the
    absolute path to it.

    These entries get sorted, so for consistency we want to ensure that
    `file_name` attr is kept first.
    """

    file_name: str
    absolute_path: str
