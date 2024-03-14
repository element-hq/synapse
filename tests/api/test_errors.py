#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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

from synapse.api.errors import LimitExceededError

from tests import unittest


class LimitExceededErrorTestCase(unittest.TestCase):
    def test_key_appears_in_context_but_not_error_dict(self) -> None:
        err = LimitExceededError("needle")
        serialised = json.dumps(err.error_dict(None))
        self.assertIn("needle", err.debug_context)
        self.assertNotIn("needle", serialised)

    def test_limit_exceeded_header(self) -> None:
        err = LimitExceededError(limiter_name="test", retry_after_ms=100)
        self.assertEqual(err.error_dict(None).get("retry_after_ms"), 100)
        assert err.headers is not None
        self.assertEqual(err.headers.get("Retry-After"), "1")

    def test_limit_exceeded_rounding(self) -> None:
        err = LimitExceededError(limiter_name="test", retry_after_ms=3001)
        self.assertEqual(err.error_dict(None).get("retry_after_ms"), 3001)
        assert err.headers is not None
        self.assertEqual(err.headers.get("Retry-After"), "4")
