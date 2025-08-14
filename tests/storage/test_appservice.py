#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import json
import os
import tempfile
from typing import List, cast
from unittest.mock import AsyncMock, Mock

import yaml

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.appservice import ApplicationService, ApplicationServiceState
from synapse.config._base import ConfigError
from synapse.events import EventBase
from synapse.server import HomeServer
from synapse.storage.database import DatabasePool, LoggingDatabaseConnection, make_conn
from synapse.storage.databases.main.appservice import (
    ApplicationServiceStore,
    ApplicationServiceTransactionStore,
)
from synapse.types import DeviceListUpdates
from synapse.util import Clock

from tests import unittest


class ApplicationServiceStoreTestCase(unittest.HomeserverTestCase):
    def setUp(self) -> None:
        super().setUp()

        self.as_yaml_files: List[str] = []

        self.hs.config.appservice.app_service_config_files = self.as_yaml_files
        self.hs.config.caches.event_cache_size = 1

        self.as_token = "token1"
        self.as_url = "some_url"
        self.as_id = "as1"
        self._add_appservice(
            self.as_token, self.as_id, self.as_url, "some_hs_token", "bob"
        )
        self._add_appservice("token2", "as2", "some_url", "some_hs_token", "bob")
        self._add_appservice("token3", "as3", "some_url", "some_hs_token", "bob")
        # must be done after inserts
        database = self.hs.get_datastores().databases[0]
        self.server_name = self.hs.hostname
        self.store = ApplicationServiceStore(
            database,
            make_conn(
                db_config=database._database_config,
                engine=database.engine,
                default_txn_name="test",
                server_name=self.server_name,
            ),
            self.hs,
        )

    def tearDown(self) -> None:
        # TODO: suboptimal that we need to create files for tests!
        for f in self.as_yaml_files:
            try:
                os.remove(f)
            except Exception:
                pass

        super().tearDown()

    def _add_appservice(
        self, as_token: str, id: str, url: str, hs_token: str, sender: str
    ) -> None:
        as_yaml = {
            "url": url,
            "as_token": as_token,
            "hs_token": hs_token,
            "id": id,
            "sender_localpart": sender,
            "namespaces": {},
        }
        # use the token as the filename
        with open(as_token, "w") as outfile:
            outfile.write(yaml.dump(as_yaml))
            self.as_yaml_files.append(as_token)

    def test_retrieve_unknown_service_token(self) -> None:
        service = self.store.get_app_service_by_token("invalid_token")
        self.assertEqual(service, None)

    def test_retrieval_of_service(self) -> None:
        stored_service = self.store.get_app_service_by_token(self.as_token)
        assert stored_service is not None
        self.assertEqual(stored_service.token, self.as_token)
        self.assertEqual(stored_service.id, self.as_id)
        self.assertEqual(stored_service.url, self.as_url)
        self.assertEqual(stored_service.namespaces[ApplicationService.NS_ALIASES], [])
        self.assertEqual(stored_service.namespaces[ApplicationService.NS_ROOMS], [])
        self.assertEqual(stored_service.namespaces[ApplicationService.NS_USERS], [])

    def test_retrieval_of_all_services(self) -> None:
        services = self.store.get_app_services()
        self.assertEqual(len(services), 3)


class ApplicationServiceTransactionStoreTestCase(unittest.HomeserverTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.as_yaml_files: List[str] = []

        self.hs.config.appservice.app_service_config_files = self.as_yaml_files
        self.hs.config.caches.event_cache_size = 1

        self.as_list = [
            {"token": "token1", "url": "https://matrix-as.org", "id": "id_1"},
            {"token": "alpha_tok", "url": "https://alpha.com", "id": "id_alpha"},
            {"token": "beta_tok", "url": "https://beta.com", "id": "id_beta"},
            {"token": "gamma_tok", "url": "https://gamma.com", "id": "id_gamma"},
        ]
        for s in self.as_list:
            self._add_service(s["url"], s["token"], s["id"])

        self.as_yaml_files = []

        # We assume there is only one database in these tests
        database = self.hs.get_datastores().databases[0]
        self.db_pool = database._db_pool
        self.engine = database.engine

        server_name = self.hs.hostname
        db_config = self.hs.config.database.get_single_database()
        self.store = TestTransactionStore(
            database,
            make_conn(
                db_config=db_config,
                engine=self.engine,
                default_txn_name="test",
                server_name=server_name,
            ),
            self.hs,
        )

    def _add_service(self, url: str, as_token: str, id: str) -> None:
        as_yaml = {
            "url": url,
            "as_token": as_token,
            "hs_token": "something",
            "id": id,
            "sender_localpart": "a_sender",
            "namespaces": {},
        }
        # use the token as the filename
        with open(as_token, "w") as outfile:
            outfile.write(yaml.dump(as_yaml))
            self.as_yaml_files.append(as_token)

    def _set_state(self, id: str, state: ApplicationServiceState) -> defer.Deferred:
        return self.db_pool.runOperation(
            self.engine.convert_param_style(
                "INSERT INTO application_services_state(as_id, state) VALUES(?,?)"
            ),
            (id, state.value),
        )

    def _insert_txn(
        self, as_id: str, txn_id: int, events: List[Mock]
    ) -> "defer.Deferred[None]":
        return self.db_pool.runOperation(
            self.engine.convert_param_style(
                "INSERT INTO application_services_txns(as_id, txn_id, event_ids) "
                "VALUES(?,?,?)"
            ),
            (as_id, txn_id, json.dumps([e.event_id for e in events])),
        )

    def test_get_appservice_state_none(
        self,
    ) -> None:
        service = Mock(id="999")
        state = self.get_success(self.store.get_appservice_state(service))
        self.assertEqual(None, state)

    def test_get_appservice_state_up(
        self,
    ) -> None:
        self.get_success(
            self._set_state(self.as_list[0]["id"], ApplicationServiceState.UP)
        )
        service = Mock(id=self.as_list[0]["id"])
        state = self.get_success(
            defer.ensureDeferred(self.store.get_appservice_state(service))
        )
        self.assertEqual(ApplicationServiceState.UP, state)

    def test_get_appservice_state_down(
        self,
    ) -> None:
        self.get_success(
            self._set_state(self.as_list[0]["id"], ApplicationServiceState.UP)
        )
        self.get_success(
            self._set_state(self.as_list[1]["id"], ApplicationServiceState.DOWN)
        )
        self.get_success(
            self._set_state(self.as_list[2]["id"], ApplicationServiceState.DOWN)
        )
        service = Mock(id=self.as_list[1]["id"])
        state = self.get_success(self.store.get_appservice_state(service))
        self.assertEqual(ApplicationServiceState.DOWN, state)

    def test_get_appservices_by_state_none(
        self,
    ) -> None:
        services = self.get_success(
            self.store.get_appservices_by_state(ApplicationServiceState.DOWN)
        )
        self.assertEqual(0, len(services))

    def test_set_appservices_state_down(
        self,
    ) -> None:
        service = Mock(id=self.as_list[1]["id"])
        self.get_success(
            self.store.set_appservice_state(service, ApplicationServiceState.DOWN)
        )
        rows = self.get_success(
            self.db_pool.runQuery(
                self.engine.convert_param_style(
                    "SELECT as_id FROM application_services_state WHERE state=?"
                ),
                (ApplicationServiceState.DOWN.value,),
            )
        )
        self.assertEqual(service.id, rows[0][0])

    def test_set_appservices_state_multiple_up(
        self,
    ) -> None:
        service = Mock(id=self.as_list[1]["id"])
        self.get_success(
            self.store.set_appservice_state(service, ApplicationServiceState.UP)
        )
        self.get_success(
            self.store.set_appservice_state(service, ApplicationServiceState.DOWN)
        )
        self.get_success(
            self.store.set_appservice_state(service, ApplicationServiceState.UP)
        )
        rows = self.get_success(
            self.db_pool.runQuery(
                self.engine.convert_param_style(
                    "SELECT as_id FROM application_services_state WHERE state=?"
                ),
                (ApplicationServiceState.UP.value,),
            )
        )
        self.assertEqual(service.id, rows[0][0])

    def test_create_appservice_txn_first(
        self,
    ) -> None:
        service = Mock(id=self.as_list[0]["id"])
        events = cast(List[EventBase], [Mock(event_id="e1"), Mock(event_id="e2")])
        txn = self.get_success(
            defer.ensureDeferred(
                self.store.create_appservice_txn(
                    service, events, [], [], {}, {}, DeviceListUpdates()
                )
            )
        )
        self.assertEqual(txn.id, 1)
        self.assertEqual(txn.events, events)
        self.assertEqual(txn.service, service)

    def test_complete_appservice_txn_first_txn(
        self,
    ) -> None:
        service = Mock(id=self.as_list[0]["id"])
        events = [Mock(event_id="e1"), Mock(event_id="e2")]
        txn_id = 1

        self.get_success(self._insert_txn(service.id, txn_id, events))
        self.get_success(
            self.store.complete_appservice_txn(txn_id=txn_id, service=service)
        )

        res = self.get_success(
            self.db_pool.runQuery(
                self.engine.convert_param_style(
                    "SELECT * FROM application_services_txns WHERE txn_id=?"
                ),
                (txn_id,),
            )
        )
        self.assertEqual(0, len(res))

    def test_complete_appservice_txn_updates_last_txn_state(
        self,
    ) -> None:
        service = Mock(id=self.as_list[0]["id"])
        events = [Mock(event_id="e1"), Mock(event_id="e2")]
        txn_id = 5
        self._set_state(self.as_list[0]["id"], ApplicationServiceState.UP)
        self.get_success(self._insert_txn(service.id, txn_id, events))
        self.get_success(
            self.store.complete_appservice_txn(txn_id=txn_id, service=service)
        )

        res = self.get_success(
            self.db_pool.runQuery(
                self.engine.convert_param_style(
                    "SELECT state FROM application_services_state WHERE as_id=?"
                ),
                (service.id,),
            )
        )
        self.assertEqual(1, len(res))
        self.assertEqual(ApplicationServiceState.UP.value, res[0][0])

        res = self.get_success(
            self.db_pool.runQuery(
                self.engine.convert_param_style(
                    "SELECT * FROM application_services_txns WHERE txn_id=?"
                ),
                (txn_id,),
            )
        )
        self.assertEqual(0, len(res))

    def test_get_oldest_unsent_txn_none(
        self,
    ) -> None:
        service = Mock(id=self.as_list[0]["id"])

        txn = self.get_success(self.store.get_oldest_unsent_txn(service))
        self.assertEqual(None, txn)

    def test_get_oldest_unsent_txn(self) -> None:
        service = Mock(id=self.as_list[0]["id"])
        events = [Mock(event_id="e1"), Mock(event_id="e2")]
        other_events = [Mock(event_id="e5"), Mock(event_id="e6")]

        # we aren't testing store._base stuff here, so mock this out
        # (ignore needed because Mypy won't allow us to assign to a method otherwise)
        self.store.get_events_as_list = AsyncMock(return_value=events)  # type: ignore[method-assign]

        self.get_success(self._insert_txn(self.as_list[1]["id"], 9, other_events))
        self.get_success(self._insert_txn(service.id, 10, events))
        self.get_success(self._insert_txn(service.id, 11, other_events))
        self.get_success(self._insert_txn(service.id, 12, other_events))

        txn = self.get_success(self.store.get_oldest_unsent_txn(service))
        assert txn is not None
        self.assertEqual(service, txn.service)
        self.assertEqual(10, txn.id)
        self.assertEqual(events, txn.events)

    def test_get_appservices_by_state_single(
        self,
    ) -> None:
        self.get_success(
            self._set_state(self.as_list[0]["id"], ApplicationServiceState.DOWN)
        )
        self.get_success(
            self._set_state(self.as_list[1]["id"], ApplicationServiceState.UP)
        )

        services = self.get_success(
            self.store.get_appservices_by_state(ApplicationServiceState.DOWN)
        )
        self.assertEqual(1, len(services))
        self.assertEqual(self.as_list[0]["id"], services[0].id)

    def test_get_appservices_by_state_multiple(
        self,
    ) -> None:
        self.get_success(
            self._set_state(self.as_list[0]["id"], ApplicationServiceState.DOWN)
        )
        self.get_success(
            self._set_state(self.as_list[1]["id"], ApplicationServiceState.UP)
        )
        self.get_success(
            self._set_state(self.as_list[2]["id"], ApplicationServiceState.DOWN)
        )
        self.get_success(
            self._set_state(self.as_list[3]["id"], ApplicationServiceState.UP)
        )

        services = self.get_success(
            self.store.get_appservices_by_state(ApplicationServiceState.DOWN)
        )
        self.assertEqual(2, len(services))
        self.assertEqual(
            {self.as_list[2]["id"], self.as_list[0]["id"]},
            {services[0].id, services[1].id},
        )


class ApplicationServiceStoreTypeStreamIds(unittest.HomeserverTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.service = Mock(id="foo")
        self.store = self.hs.get_datastores().main
        self.get_success(
            self.store.set_appservice_state(self.service, ApplicationServiceState.UP)
        )

    def test_get_type_stream_id_for_appservice_no_value(self) -> None:
        value = self.get_success(
            self.store.get_type_stream_id_for_appservice(self.service, "read_receipt")
        )
        self.assertEqual(value, 1)

        value = self.get_success(
            self.store.get_type_stream_id_for_appservice(self.service, "presence")
        )
        self.assertEqual(value, 1)

    def test_get_type_stream_id_for_appservice_invalid_type(self) -> None:
        self.get_failure(
            self.store.get_type_stream_id_for_appservice(self.service, "foobar"),
            ValueError,
        )

    def test_set_appservice_stream_type_pos(self) -> None:
        read_receipt_value = 1024
        self.get_success(
            self.store.set_appservice_stream_type_pos(
                self.service, "read_receipt", read_receipt_value
            )
        )
        result = self.get_success(
            self.store.get_type_stream_id_for_appservice(self.service, "read_receipt")
        )
        self.assertEqual(result, read_receipt_value)

        self.get_success(
            self.store.set_appservice_stream_type_pos(
                self.service, "presence", read_receipt_value
            )
        )
        result = self.get_success(
            self.store.get_type_stream_id_for_appservice(self.service, "presence")
        )
        self.assertEqual(result, read_receipt_value)

    def test_set_appservice_stream_type_pos_invalid_type(self) -> None:
        self.get_failure(
            self.store.set_appservice_stream_type_pos(self.service, "foobar", 1024),
            ValueError,
        )


# required for ApplicationServiceTransactionStoreTestCase tests
class TestTransactionStore(ApplicationServiceTransactionStore, ApplicationServiceStore):
    def __init__(
        self, database: DatabasePool, db_conn: LoggingDatabaseConnection, hs: HomeServer
    ) -> None:
        super().__init__(database, db_conn, hs)


class ApplicationServiceStoreConfigTestCase(unittest.HomeserverTestCase):
    def _write_config(self, suffix: str, **kwargs: str) -> str:
        vals = {
            "id": "id" + suffix,
            "url": "url" + suffix,
            "as_token": "as_token" + suffix,
            "hs_token": "hs_token" + suffix,
            "sender_localpart": "sender_localpart" + suffix,
            "namespaces": {},
        }
        vals.update(kwargs)

        _, path = tempfile.mkstemp(prefix="as_config")
        with open(path, "w") as f:
            f.write(yaml.dump(vals))
        return path

    def test_unique_works(self) -> None:
        f1 = self._write_config(suffix="1")
        f2 = self._write_config(suffix="2")

        self.hs.config.appservice.app_service_config_files = [f1, f2]
        self.hs.config.caches.event_cache_size = 1

        server_name = self.hs.hostname
        database = self.hs.get_datastores().databases[0]
        ApplicationServiceStore(
            database,
            make_conn(
                db_config=database._database_config,
                engine=database.engine,
                default_txn_name="test",
                server_name=server_name,
            ),
            self.hs,
        )

    def test_duplicate_ids(self) -> None:
        f1 = self._write_config(id="id", suffix="1")
        f2 = self._write_config(id="id", suffix="2")

        self.hs.config.appservice.app_service_config_files = [f1, f2]
        self.hs.config.caches.event_cache_size = 1

        with self.assertRaises(ConfigError) as cm:
            server_name = self.hs.hostname
            database = self.hs.get_datastores().databases[0]
            ApplicationServiceStore(
                database,
                make_conn(
                    db_config=database._database_config,
                    engine=database.engine,
                    default_txn_name="test",
                    server_name=server_name,
                ),
                self.hs,
            )

        e = cm.exception
        self.assertIn(f1, str(e))
        self.assertIn(f2, str(e))
        self.assertIn("id", str(e))

    def test_duplicate_as_tokens(self) -> None:
        f1 = self._write_config(as_token="as_token", suffix="1")
        f2 = self._write_config(as_token="as_token", suffix="2")

        self.hs.config.appservice.app_service_config_files = [f1, f2]
        self.hs.config.caches.event_cache_size = 1

        with self.assertRaises(ConfigError) as cm:
            server_name = self.hs.hostname
            database = self.hs.get_datastores().databases[0]
            ApplicationServiceStore(
                database,
                make_conn(
                    db_config=database._database_config,
                    engine=database.engine,
                    default_txn_name="test",
                    server_name=server_name,
                ),
                self.hs,
            )

        e = cm.exception
        self.assertIn(f1, str(e))
        self.assertIn(f2, str(e))
        self.assertIn("as_token", str(e))
