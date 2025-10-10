# Copyright 2015, 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import Any, Mapping, NoReturn, Optional

import psycopg2.extensions

from synapse.storage.engines import PostgresEngine
from synapse.storage.engines._base import IsolationLevel

logger = logging.getLogger(__name__)


class Psycopg2Engine(
    PostgresEngine[psycopg2.extensions.connection, psycopg2.extensions.cursor, int]
):
    def __init__(self, database_config: Mapping[str, Any]):
        super().__init__(psycopg2, database_config)
        psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)

        # Disables passing `bytes` to txn.execute, c.f.
        # https://github.com/matrix-org/synapse/issues/6186. If you do
        # actually want to use bytes than wrap it in `bytearray`.
        def _disable_bytes_adapter(_: bytes) -> NoReturn:
            raise Exception("Passing bytes to DB is disabled.")

        psycopg2.extensions.register_adapter(bytes, _disable_bytes_adapter)

        self.isolation_level_map = {
            IsolationLevel.READ_COMMITTED: psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED,
            IsolationLevel.REPEATABLE_READ: psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ,
            IsolationLevel.SERIALIZABLE: psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE,
        }
        self.default_isolation_level = (
            psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ
        )
        self.config = database_config

    def get_server_version(self, db_conn: psycopg2.extensions.connection) -> int:
        return db_conn.server_version

    def set_statement_timeout(
        self, cursor: psycopg2.extensions.cursor, statement_timeout: int
    ) -> None:
        cursor.execute("SET statement_timeout TO %s", (statement_timeout,))

    def convert_param_style(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def is_deadlock(self, error: Exception) -> bool:
        if isinstance(error, psycopg2.DatabaseError):
            # https://www.postgresql.org/docs/current/static/errcodes-appendix.html
            # "40001" serialization_failure
            # "40P01" deadlock_detected
            return error.pgcode in ["40001", "40P01"]
        return False

    def in_transaction(self, conn: psycopg2.extensions.connection) -> bool:
        return conn.status != psycopg2.extensions.STATUS_READY

    def attempt_to_set_autocommit(
        self, conn: psycopg2.extensions.connection, autocommit: bool
    ) -> None:
        return conn.set_session(autocommit=autocommit)

    def attempt_to_set_isolation_level(
        self,
        conn: psycopg2.extensions.connection,
        isolation_level: Optional[IsolationLevel] = None,
    ) -> None:
        if isolation_level is None:
            pg_isolation_level = self.default_isolation_level
        else:
            pg_isolation_level = self.isolation_level_map[isolation_level]
        return conn.set_isolation_level(pg_isolation_level)
