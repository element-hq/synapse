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
"""MSC4430 (Member Keys): the identity module.

In room versions with `msc4430_member_keys`, room members are identified by a
per-user, per-room Ed25519 "member key" (`synapse.types.MemberKey`) rather than by
their user ID: it is the `sender` of their events and the `state_key` of their
`m.room.member` events, and it signs their events. The user behind a member key is
not part of the identifier: for keys we hold it is a lookup of our own records, and
for everyone else's it is the *linkage information* (`content.localpart`,
`content.domain`) their `m.room.member` events carry, which we trust only once the
claimed domain's server signing key has co-signed it.

This module owns that mapping in both directions. Nothing outside it should decide
what a member key means: code elsewhere either asks here, or (where it is not yet
member-key aware) says so via `record_unsupported_path` so that the gap is counted
rather than silently degraded.
"""

import logging
from typing import TYPE_CHECKING

from prometheus_client import Counter

from synapse.api.errors import MemberKeyHasNoDomainError
from synapse.metrics import SERVER_NAME_LABEL
from synapse.types import is_member_key

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

unsupported_path_counter = Counter(
    "synapse_msc4430_unsupported_path_total",
    "Member keys that reached a code path which is not member-key aware yet, by path",
    labelnames=[SERVER_NAME_LABEL, "path"],
)

# The paths already warned about in this process, so that a busy room does not
# turn one unadapted branch into a flood of log lines (the counter keeps counting).
_warned_paths: set[str] = set()


def record_unsupported_path(server_name: str, path: str) -> None:
    """Record that a member key reached a code path which is not member-key aware.

    Use this where such code deliberately skips the member key instead of mapping
    it, so that the gap shows up on the `synapse_msc4430_unsupported_path_total`
    counter (and once in the log) rather than as a silent `continue`. Code which
    *cannot* make sense of a member key should raise `MemberKeyHasNoDomainError`
    instead.

    Args:
        server_name: our server name, for the metrics label.
        path: a short, stable, greppable name for the branch, e.g. "push.mentions".
    """
    unsupported_path_counter.labels(**{SERVER_NAME_LABEL: server_name}, path=path).inc()
    if path not in _warned_paths:
        _warned_paths.add(path)
        logger.warning(
            "MSC4430: a member key reached %r, which is not member-key aware yet; "
            "further occurrences are only counted",
            path,
        )


class MemberKeyHandler:
    """Resolves room member identities (user IDs or member keys) to users and back.

    Today this only answers the "is this identity ours" question; it grows the
    key/link lookups as the storage and room handling for member keys land.
    """

    def __init__(self, hs: "HomeServer"):
        self.server_name = hs.hostname
        self._is_mine_id = hs.is_mine_id

    async def is_local_identity(self, room_id: str, identity: str) -> bool:
        """Whether the given room member identity belongs to this server.

        For a user ID this is `is_mine_id`. For a member key it is whether we hold
        the signing key for it in this room, which is not yet stored anywhere, so
        for now such a call is a loud failure rather than a wrong `False`.
        """
        if not is_member_key(identity):
            return self._is_mine_id(identity)

        raise MemberKeyHasNoDomainError(
            identity,
            "cannot be looked up yet (no member key store, room %s)" % (room_id,),
        )
