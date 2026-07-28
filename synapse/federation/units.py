#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

"""Defines the JSON structure of the protocol units used by the server to
server protocol.
"""

import logging
from typing import Sequence

import attr

from synapse.api.constants import CANONICALJSON_MAX_INT, CANONICALJSON_MIN_INT
from synapse.events import EventBase
from synapse.types import JsonDict

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class Edu:
    """An Edu represents a piece of data sent from one homeserver to another.

    In comparison to Pdus, Edus are not persisted for a long time on disk, are
    not meaningful beyond a given pair of homeservers, and don't have an
    internal ID or previous references graph.
    """

    edu_type: str
    content: dict
    origin: str
    destination: str

    def get_dict(self) -> JsonDict:
        return {
            "edu_type": self.edu_type,
            "content": self.content,
        }

    def get_internal_dict(self) -> JsonDict:
        return {
            "edu_type": self.edu_type,
            "content": self.content,
            "origin": self.origin,
            "destination": self.destination,
        }

    def get_context(self) -> str:
        return getattr(self, "content", {}).get("org.matrix.opentracing_context", "{}")

    def strip_context(self) -> None:
        getattr(self, "content", {})["org.matrix.opentracing_context"] = "{}"


def _none_to_list(edus: list[JsonDict] | None) -> list[JsonDict]:
    if edus is None:
        return []
    return edus


@attr.s(slots=True, frozen=True, auto_attribs=True)
class Transaction:
    """A transaction is a list of Pdus and Edus to be sent to a remote home
    server with some extra metadata.

    Example transaction::

        {
            "origin": "foo",
            "prev_ids": ["abc", "def"],
            "pdus": [
                ...
            ],
        }

    """

    # Required keys.
    transaction_id: str
    origin: str
    destination: str
    origin_server_ts: int
    pdus: list[JsonDict] = attr.ib(factory=list, converter=_none_to_list)
    edus: list[JsonDict] = attr.ib(factory=list, converter=_none_to_list)

    def get_dict(self) -> JsonDict:
        """A JSON-ready dictionary of valid keys which aren't internal."""
        result = {
            "origin": self.origin,
            "origin_server_ts": self.origin_server_ts,
            "pdus": filter_pdus_for_valid_depth(self.pdus),
        }
        if self.edus:
            result["edus"] = self.edus
        return result


def filter_pdus_for_valid_depth(pdus: Sequence[JsonDict]) -> list[JsonDict]:
    filtered_pdus = []
    for pdu in pdus:
        # Drop PDUs that have a depth that is outside of the range allowed
        # by canonical json.
        if (
            "depth" in pdu
            and CANONICALJSON_MIN_INT <= pdu["depth"] <= CANONICALJSON_MAX_INT
        ):
            filtered_pdus.append(pdu)

    return filtered_pdus


def serialize_and_filter_pdus(
    pdus: Sequence[EventBase], time_now: int | None = None
) -> list[JsonDict]:
    return filter_pdus_for_valid_depth([pdu.get_pdu_json(time_now) for pdu in pdus])
