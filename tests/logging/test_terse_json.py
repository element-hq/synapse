#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
import logging
from io import BytesIO, StringIO
from typing import cast
from unittest.mock import Mock, patch

from twisted.web.http import HTTPChannel
from twisted.web.server import Request

from synapse.http.site import SynapseRequest
from synapse.logging._terse_json import JsonFormatter, TerseJsonFormatter
from synapse.logging.context import LoggingContext, LoggingContextFilter
from synapse.types import JsonDict

from tests.logging import LoggerCleanupMixin
from tests.server import FakeChannel, get_clock
from tests.unittest import TestCase


class TerseJsonTestCase(LoggerCleanupMixin, TestCase):
    def setUp(self) -> None:
        self.output = StringIO()
        self.reactor, _ = get_clock()

    def get_log_line(self) -> JsonDict:
        # One log message, with a single trailing newline.
        data = self.output.getvalue()
        logs = data.splitlines()
        self.assertEqual(len(logs), 1)
        self.assertEqual(data.count("\n"), 1)
        return json.loads(logs[0])

    def test_terse_json_output(self) -> None:
        """
        The Terse JSON formatter converts log messages to JSON.
        """
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(TerseJsonFormatter())
        logger = self.get_logger(handler)

        logger.info("Hello there, %s!", "wally")

        log = self.get_log_line()

        # The terse logger should give us these keys.
        expected_log_keys = [
            "log",
            "time",
            "level",
            "namespace",
        ]
        self.assertCountEqual(log.keys(), expected_log_keys)
        self.assertEqual(log["log"], "Hello there, wally!")

    def test_extra_data(self) -> None:
        """
        Additional information can be included in the structured logging.
        """
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(TerseJsonFormatter())
        logger = self.get_logger(handler)

        logger.info(
            "Hello there, %s!", "wally", extra={"foo": "bar", "int": 3, "bool": True}
        )

        log = self.get_log_line()

        # The terse logger should give us these keys.
        expected_log_keys = [
            "log",
            "time",
            "level",
            "namespace",
            # The additional keys given via extra.
            "foo",
            "int",
            "bool",
        ]
        self.assertCountEqual(log.keys(), expected_log_keys)

        # Check the values of the extra fields.
        self.assertEqual(log["foo"], "bar")
        self.assertEqual(log["int"], 3)
        self.assertIs(log["bool"], True)

    def test_json_output(self) -> None:
        """
        The Terse JSON formatter converts log messages to JSON.
        """
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(JsonFormatter())
        logger = self.get_logger(handler)

        logger.info("Hello there, %s!", "wally")

        log = self.get_log_line()

        # The terse logger should give us these keys.
        expected_log_keys = [
            "log",
            "level",
            "namespace",
        ]
        self.assertCountEqual(log.keys(), expected_log_keys)
        self.assertEqual(log["log"], "Hello there, wally!")

    def test_with_context(self) -> None:
        """
        The logging context should be added to the JSON response.
        """
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(LoggingContextFilter())
        logger = self.get_logger(handler)

        with LoggingContext("name"):
            logger.info("Hello there, %s!", "wally")

        log = self.get_log_line()

        # The terse logger should give us these keys.
        expected_log_keys = [
            "log",
            "level",
            "namespace",
            "request",
        ]
        self.assertCountEqual(log.keys(), expected_log_keys)
        self.assertEqual(log["log"], "Hello there, wally!")
        self.assertEqual(log["request"], "name")

    def test_with_request_context(self) -> None:
        """
        Information from the logging context request should be added to the JSON response.
        """
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(LoggingContextFilter())
        logger = self.get_logger(handler)

        # A full request isn't needed here.
        site = Mock(spec=["site_tag", "server_version_string", "getResourceFor"])
        site.site_tag = "test-site"
        site.server_version_string = "Server v1"
        site.reactor = Mock()
        request = SynapseRequest(
            cast(HTTPChannel, FakeChannel(site, self.reactor)), site
        )
        # Call requestReceived to finish instantiating the object.
        request.content = BytesIO()
        # Partially skip some internal processing of SynapseRequest.
        request._started_processing = Mock()  # type: ignore[method-assign]
        request.request_metrics = Mock(spec=["name"])
        with patch.object(Request, "render"):
            request.requestReceived(b"POST", b"/_matrix/client/versions", b"1.1")

        # Also set the requester to ensure the processing works.
        request.requester = "@foo:test"

        with LoggingContext(
            request.get_request_id(), parent_context=request.logcontext
        ):
            logger.info("Hello there, %s!", "wally")

        log = self.get_log_line()

        # The terse logger includes additional request information, if possible.
        expected_log_keys = [
            "log",
            "level",
            "namespace",
            "request",
            "ip_address",
            "site_tag",
            "requester",
            "authenticated_entity",
            "method",
            "url",
            "protocol",
            "user_agent",
        ]
        self.assertCountEqual(log.keys(), expected_log_keys)
        self.assertEqual(log["log"], "Hello there, wally!")
        self.assertTrue(log["request"].startswith("POST-"))
        self.assertEqual(log["ip_address"], "127.0.0.1")
        self.assertEqual(log["site_tag"], "test-site")
        self.assertEqual(log["requester"], "@foo:test")
        self.assertEqual(log["authenticated_entity"], "@foo:test")
        self.assertEqual(log["method"], "POST")
        self.assertEqual(log["url"], "/_matrix/client/versions")
        self.assertEqual(log["protocol"], "1.1")
        self.assertEqual(log["user_agent"], "")

    def test_with_exception(self) -> None:
        """
        The logging exception type & value should be added to the JSON response.
        """
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(JsonFormatter())
        logger = self.get_logger(handler)

        try:
            raise ValueError("That's wrong, you wally!")
        except ValueError:
            logger.exception("Hello there, %s!", "wally")

        log = self.get_log_line()

        # The terse logger should give us these keys.
        expected_log_keys = [
            "log",
            "level",
            "namespace",
            "exc_type",
            "exc_value",
        ]
        self.assertCountEqual(log.keys(), expected_log_keys)
        self.assertEqual(log["log"], "Hello there, wally!")
        self.assertEqual(log["exc_type"], "ValueError")
        self.assertEqual(log["exc_value"], "That's wrong, you wally!")
