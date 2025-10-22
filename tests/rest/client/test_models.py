#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
import unittest as stdlib_unittest
from typing import Literal

from pydantic import BaseModel, ValidationError

from synapse.types.rest.client import EmailRequestTokenBody


class ThreepidMediumEnumTestCase(stdlib_unittest.TestCase):
    class Model(BaseModel):
        medium: Literal["email", "msisdn"]

    def test_accepts_valid_medium_string(self) -> None:
        """Sanity check that Pydantic behaves sensibly with an enum-of-str

        This is arguably more of a test of a class that inherits from str and Enum
        simultaneously.
        """
        model = self.Model.model_validate({"medium": "email"})
        self.assertEqual(model.medium, "email")

    def test_rejects_invalid_medium_value(self) -> None:
        with self.assertRaises(ValidationError):
            self.Model.model_validate({"medium": "interpretive_dance"})

    def test_rejects_invalid_medium_type(self) -> None:
        with self.assertRaises(ValidationError):
            self.Model.model_validate({"medium": 123})


class EmailRequestTokenBodyTestCase(stdlib_unittest.TestCase):
    base_request = {
        "client_secret": "hunter2",
        "email": "alice@wonderland.com",
        "send_attempt": 1,
    }

    def test_token_required_if_id_server_provided(self) -> None:
        with self.assertRaises(ValidationError):
            EmailRequestTokenBody.model_validate(
                {
                    **self.base_request,
                    "id_server": "identity.wonderland.com",
                }
            )
        with self.assertRaises(ValidationError):
            EmailRequestTokenBody.model_validate(
                {
                    **self.base_request,
                    "id_server": "identity.wonderland.com",
                    "id_access_token": None,
                }
            )

    def test_token_typechecked_when_id_server_provided(self) -> None:
        with self.assertRaises(ValidationError):
            EmailRequestTokenBody.model_validate(
                {
                    **self.base_request,
                    "id_server": "identity.wonderland.com",
                    "id_access_token": 1337,
                }
            )
