#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
"""Tests for the MSC4430 identity module (`synapse.handlers.member_keys`)."""

from prometheus_client import REGISTRY
from signedjson.key import generate_signing_key

from synapse.api.constants import MemberKeys
from synapse.handlers import member_keys
from synapse.handlers.member_keys import record_unsupported_path
from synapse.metrics import SERVER_NAME_LABEL

from tests import unittest

# The example member key from MSC4430.
EXAMPLE_MEMBER_KEY = "@l8Hft5qXKn1vfHrg3p4-W8gELQVo8N13JkluMfmn2sQ"


class IsMineIdentityTestCase(unittest.HomeserverTestCase):
    def test_user_ids(self) -> None:
        self.assertTrue(
            self.get_success(self.hs.is_mine_identity("!room:test", "@alice:test"))
        )
        self.assertFalse(
            self.get_success(self.hs.is_mine_identity("!room:test", "@bob:other"))
        )

    def test_member_keys(self) -> None:
        """A member key is ours in the room we hold it for, and nowhere else."""
        store = self.hs.get_datastores().main
        self.assertFalse(
            self.get_success(self.hs.is_mine_identity("!room:test", EXAMPLE_MEMBER_KEY))
        )

        ours = self.get_success(
            store.create_local_member_key(
                "!room:test",
                "@alice:test",
                generate_signing_key(MemberKeys.KEY_VERSION),
            )
        )
        self.assertTrue(
            self.get_success(self.hs.is_mine_identity("!room:test", ours.member_key))
        )
        self.assertFalse(
            self.get_success(self.hs.is_mine_identity("!other:test", ours.member_key))
        )


class RecordUnsupportedPathTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # The warn-once set is process global.
        member_keys._warned_paths.discard("tests.example")

    def test_counter(self) -> None:
        labels = {SERVER_NAME_LABEL: "test", "path": "tests.example"}

        def value() -> float:
            return (
                REGISTRY.get_sample_value(
                    "synapse_msc4430_unsupported_path_total", labels
                )
                or 0.0
            )

        before = value()
        with self.assertLogs("synapse.handlers.member_keys", level="WARNING") as cm:
            record_unsupported_path("test", "tests.example")
            # Only the first occurrence of a path is logged.
            record_unsupported_path("test", "tests.example")
        self.assertEqual(len(cm.output), 1)
        self.assertIn("tests.example", cm.output[0])
        self.assertEqual(value(), before + 2)
