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

from html.parser import HTMLParser
from typing import Iterable, NoReturn


class TestHtmlParser(HTMLParser):
    """A generic HTML page parser which extracts useful things from the HTML"""

    def __init__(self) -> None:
        super().__init__()

        # a list of links found in the doc
        self.links: list[str] = []

        # the values of any hidden <input>s: map from name to value
        self.hiddens: dict[str, str | None] = {}

        # the values of any radio buttons: map from name to list of values
        self.radios: dict[str, list[str | None]] = {}

    def handle_starttag(
        self, tag: str, attrs: Iterable[tuple[str, str | None]]
    ) -> None:
        attr_dict = dict(attrs)
        if tag == "a":
            href = attr_dict["href"]
            if href:
                self.links.append(href)
        elif tag == "input":
            input_name = attr_dict.get("name")
            if attr_dict["type"] == "radio":
                assert input_name
                self.radios.setdefault(input_name, []).append(attr_dict["value"])
            elif attr_dict["type"] == "hidden":
                assert input_name
                self.hiddens[input_name] = attr_dict["value"]

    def error(self, message: str) -> NoReturn:
        raise AssertionError(message)
