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

from synapse.types.rest.client import ClientSecretStr, EmailRequestTokenBody


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


class ClientSecretStrTestCase(stdlib_unittest.TestCase):
    class Model(BaseModel):
        client_secret: ClientSecretStr

    def test_accepts_valid_client_secrets(self) -> None:
        """Secrets consisting entirely of `[0-9a-zA-Z.=_-]` are accepted."""
        for client_secret in (
            "this.is-a_valid=secret",
            "foobar",
            "a",
            "0123456789",
            "a" * 255,
        ):
            with self.subTest(client_secret=client_secret):
                model = self.Model.model_validate({"client_secret": client_secret})
                self.assertEqual(model.client_secret, client_secret)

    def test_rejects_client_secrets_with_invalid_characters(self) -> None:
        for client_secret in (
            "foo bar",
            "secret!",
            "café",
            # Little bobby tables
            "Robert'; DROP TABLE students;--",
        ):
            with self.subTest(client_secret=client_secret):
                with self.assertRaises(ValidationError):
                    self.Model.model_validate({"client_secret": client_secret})

    def test_rejects_empty_client_secret(self) -> None:
        with self.assertRaises(ValidationError):
            self.Model.model_validate({"client_secret": ""})

    def test_rejects_overlong_client_secret(self) -> None:
        with self.assertRaises(ValidationError):
            self.Model.model_validate({"client_secret": "a" * 256})


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
