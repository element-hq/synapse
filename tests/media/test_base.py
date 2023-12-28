#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from unittest.mock import Mock

from synapse.media._base import add_file_headers, get_filename_from_headers

from tests import unittest


class GetFileNameFromHeadersTests(unittest.TestCase):
    # input -> expected result
    TEST_CASES = {
        b"attachment; filename=abc.txt": "abc.txt",
        b'attachment; filename="azerty"': "azerty",
        b'attachment; filename="aze%20rty"': "aze%20rty",
        b'attachment; filename="aze"rty"': 'aze"rty',
        b'attachment; filename="azer;ty"': "azer;ty",
        b"attachment; filename*=utf-8''foo%C2%A3bar": "foo£bar",
    }

    def tests(self) -> None:
        for hdr, expected in self.TEST_CASES.items():
            res = get_filename_from_headers({b"Content-Disposition": [hdr]})
            self.assertEqual(
                res,
                expected,
                f"expected output for {hdr!r} to be {expected} but was {res}",
            )


class AddFileHeadersTests(unittest.TestCase):
    TEST_CASES = {
        # Safe values use inline.
        "text/plain": b"inline; filename=file.name",
        "text/csv": b"inline; filename=file.name",
        "image/png": b"inline; filename=file.name",
        # Unlisted values are set to attachment.
        "text/html": b"attachment; filename=file.name",
        "any/thing": b"attachment; filename=file.name",
        # Parameters get ignored.
        "text/plain; charset=utf-8": b"inline; filename=file.name",
        "text/markdown; charset=utf-8; variant=CommonMark": b"attachment; filename=file.name",
        # Parsed as lowercase.
        "Text/Plain": b"inline; filename=file.name",
        # Bad values don't choke.
        "": b"attachment; filename=file.name",
        ";": b"attachment; filename=file.name",
    }

    def test_content_disposition(self) -> None:
        for media_type, expected in self.TEST_CASES.items():
            request = Mock()
            add_file_headers(request, media_type, 0, "file.name")
            # There should be a single call to set Content-Disposition.
            for call in request.setHeader.call_args_list:
                args, _ = call
                if args[0] == b"Content-Disposition":
                    break
            else:
                self.fail(f"No Content-Disposition header found for {media_type}")
            self.assertEqual(args[1], expected, media_type)

    def test_no_filename(self) -> None:
        request = Mock()
        add_file_headers(request, "text/plain", 0, None)
        request.setHeader.assert_any_call(b"Content-Disposition", b"inline")

        request.reset_mock()
        add_file_headers(request, "text/html", 0, None)
        request.setHeader.assert_any_call(b"Content-Disposition", b"attachment")
