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

from synapse.config import ConfigError
from synapse.config._util import validate_config

from tests.unittest import TestCase


class ValidateConfigTestCase(TestCase):
    """Test cases for synapse.config._util.validate_config"""

    def test_bad_object_in_array(self) -> None:
        """malformed objects within an array should be validated correctly"""

        # consider a structure:
        #
        # array_of_objs:
        #   - r: 1
        #     foo: 2
        #
        #   - r: 2
        #     bar: 3
        #
        # ... where each entry must contain an "r": check that the path
        # to the required item is correclty reported.

        schema = {
            "type": "object",
            "properties": {
                "array_of_objs": {
                    "type": "array",
                    "items": {"type": "object", "required": ["r"]},
                },
            },
        }

        with self.assertRaises(ConfigError) as c:
            validate_config(schema, {"array_of_objs": [{}]}, ("base",))

        self.assertEqual(c.exception.path, ["base", "array_of_objs", "<item 0>"])
