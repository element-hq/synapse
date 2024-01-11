#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Dirk Klimpel
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

from synapse.util.threepids import canonicalise_email

from tests.unittest import HomeserverTestCase


class CanonicaliseEmailTests(HomeserverTestCase):
    def test_no_at(self) -> None:
        with self.assertRaises(ValueError):
            canonicalise_email("address-without-at.bar")

    def test_two_at(self) -> None:
        with self.assertRaises(ValueError):
            canonicalise_email("foo@foo@test.bar")

    def test_bad_format(self) -> None:
        with self.assertRaises(ValueError):
            canonicalise_email("user@bad.example.net@good.example.com")

    def test_valid_format(self) -> None:
        self.assertEqual(canonicalise_email("foo@test.bar"), "foo@test.bar")

    def test_domain_to_lower(self) -> None:
        self.assertEqual(canonicalise_email("foo@TEST.BAR"), "foo@test.bar")

    def test_domain_with_umlaut(self) -> None:
        self.assertEqual(canonicalise_email("foo@Öumlaut.com"), "foo@öumlaut.com")

    def test_address_casefold(self) -> None:
        self.assertEqual(
            canonicalise_email("Strauß@Example.com"), "strauss@example.com"
        )

    def test_address_trim(self) -> None:
        self.assertEqual(canonicalise_email(" foo@test.bar "), "foo@test.bar")
