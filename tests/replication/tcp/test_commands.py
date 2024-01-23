#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from synapse.replication.tcp.commands import (
    RdataCommand,
    ReplicateCommand,
    parse_command_from_line,
)

from tests.unittest import TestCase


class ParseCommandTestCase(TestCase):
    def test_parse_one_word_command(self) -> None:
        line = "REPLICATE"
        cmd = parse_command_from_line(line)
        self.assertIsInstance(cmd, ReplicateCommand)

    def test_parse_rdata(self) -> None:
        line = 'RDATA events master 6287863 ["ev", ["$eventid", "!roomid", "type", null, null, null]]'
        cmd = parse_command_from_line(line)
        assert isinstance(cmd, RdataCommand)
        self.assertEqual(cmd.stream_name, "events")
        self.assertEqual(cmd.instance_name, "master")
        self.assertEqual(cmd.token, 6287863)

    def test_parse_rdata_batch(self) -> None:
        line = 'RDATA presence master batch ["@foo:example.com", "online"]'
        cmd = parse_command_from_line(line)
        assert isinstance(cmd, RdataCommand)
        self.assertEqual(cmd.stream_name, "presence")
        self.assertEqual(cmd.instance_name, "master")
        self.assertIsNone(cmd.token)
