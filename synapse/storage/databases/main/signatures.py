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

from typing import Collection, Dict, List, Mapping, Tuple

from unpaddedbase64 import encode_base64

from synapse.crypto.event_signing import compute_event_reference_hash
from synapse.storage.databases.main.events_worker import (
    EventRedactBehaviour,
    EventsWorkerStore,
)
from synapse.util.caches.descriptors import cached, cachedList


class SignatureWorkerStore(EventsWorkerStore):
    @cached()
    def get_event_reference_hash(self, event_id: str) -> Mapping[str, bytes]:
        # This is a dummy function to allow get_event_reference_hashes
        # to use its cache
        raise NotImplementedError()

    @cachedList(
        cached_method_name="get_event_reference_hash", list_name="event_ids", num_args=1
    )
    async def get_event_reference_hashes(
        self, event_ids: Collection[str]
    ) -> Mapping[str, Mapping[str, bytes]]:
        """Get all hashes for given events.

        Args:
            event_ids: The event IDs to get hashes for.

        Returns:
             A mapping of event ID to a mapping of algorithm to hash.
             Returns an empty dict for a given event id if that event is unknown.
        """
        events = await self.get_events(
            event_ids,
            redact_behaviour=EventRedactBehaviour.as_is,
            allow_rejected=True,
        )

        hashes: Dict[str, Dict[str, bytes]] = {}
        for event_id in event_ids:
            event = events.get(event_id)
            if event is None:
                hashes[event_id] = {}
            else:
                ref_alg, ref_hash_bytes = compute_event_reference_hash(event)
                hashes[event_id] = {ref_alg: ref_hash_bytes}

        return hashes

    async def add_event_hashes(
        self, event_ids: Collection[str]
    ) -> List[Tuple[str, Dict[str, str]]]:
        """

        Args:
            event_ids: The event IDs

        Returns:
            A list of tuples of event ID and a mapping of algorithm to base-64 encoded hash.
        """
        hashes = await self.get_event_reference_hashes(event_ids)
        encoded_hashes = {
            e_id: {k: encode_base64(v) for k, v in h.items() if k == "sha256"}
            for e_id, h in hashes.items()
        }

        return list(encoded_hashes.items())


class SignatureStore(SignatureWorkerStore):
    """Persistence for event signatures and hashes"""
