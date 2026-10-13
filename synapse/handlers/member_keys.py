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

For events our own users create, `translate_event_dict` turns the user IDs a
client speaks in into member keys and adds the linkage information we can vouch
for, and `get_signers_for_event_dict` decides which keys sign the result. Those
two are the only places a member key is chosen for an event we build, and the
signing helpers in `synapse.crypto.event_signing` refuse anything they could get
wrong, so a client cannot make this server sign as a key it does not hold or
vouch for linkage information that is not ours.
"""

import logging
from typing import TYPE_CHECKING

from prometheus_client import Counter
from signedjson.key import generate_signing_key
from signedjson.types import SigningKey

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    MemberKeys,
    Membership,
)
from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.api.room_versions import RoomVersion
from synapse.crypto.event_signing import EventSigner
from synapse.events import EventBase
from synapse.events.py_protocol import supports_msc4430_member_keys
from synapse.metrics import SERVER_NAME_LABEL
from synapse.storage.member_keys import LocalMemberKey
from synapse.types import JsonDict, MemberKey, UserID, get_domain_from_id, is_member_key
from synapse.util.caches.expiringcache import ExpiringCache

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

# How long a member key generated for a create event is kept while the room ID is
# not yet known. A room creation that fails after the create event is built never
# claims its key; this bounds what it leaks.
PENDING_MEMBER_KEY_LIFETIME_MS = 10 * 60 * 1000


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
    """Resolves room member identities (user IDs or member keys) to users and back,
    and chooses the member keys and signatures for the events our users create."""

    def __init__(self, hs: "HomeServer"):
        self.server_name = hs.hostname
        self._store = hs.get_datastores().main
        self._is_mine_id = hs.is_mine_id
        self._server_signing_key = hs.signing_key
        self._msc4430_enabled = hs.config.experimental.msc4430_enabled

        # Member keys generated for a create event, whose room does not exist yet
        # (the room ID is derived from the create event, which the key signs).
        # Keyed by member key; the value is the user and the signing key. Moved to
        # storage by `persist_pending_member_keys` once the create event is built.
        self._pending_member_keys: ExpiringCache[str, tuple[str, SigningKey]] = (
            ExpiringCache(
                cache_name="msc4430_pending_member_keys",
                server_name=hs.hostname,
                hs=hs,
                clock=hs.get_clock(),
                expiry_ms=PENDING_MEMBER_KEY_LIFETIME_MS,
            )
        )

    # ------------------------------------------------------------------
    # Identities
    # ------------------------------------------------------------------

    async def local_user_id_for_identity(
        self, room_id: str | None, identity: str
    ) -> str | None:
        """The local user behind a room member identity, or `None` if it is not
        one of ours.

        For a user ID this is `is_mine_id`. For a member key it is whether we hold
        the signing key for it in this room: a key of ours from another room is
        not ours here, and nor is a key still pending for a create event. With
        no room (the create event itself, whose room ID does not exist yet) the
        pending keys are the only ones that can be ours.
        """
        if not is_member_key(identity):
            return identity if self._is_mine_id(identity) else None

        if room_id is None:
            pending = self._pending_member_keys.get(identity)
            return pending[0] if pending is not None else None
        local_key = await self._store.get_local_member_key_by_key(room_id, identity)
        return local_key.user_id if local_key is not None else None

    async def is_local_identity(self, room_id: str, identity: str) -> bool:
        """Whether the given room member identity belongs to this server."""
        return await self.local_user_id_for_identity(room_id, identity) is not None

    async def user_id_for_identity(self, room_id: str, identity: str) -> str | None:
        """The user ID behind a room member identity: a user ID itself, the user
        behind one of our member keys, or the *verified* link of another
        server's key. `None` for a key we hold no verified link for: such a key
        maps to no user, whatever it claims."""
        if not is_member_key(identity):
            return identity
        local_user_id = await self.local_user_id_for_identity(room_id, identity)
        if local_user_id is not None:
            return local_user_id
        link = await self._store.get_member_link(room_id, identity)
        if link is None:
            return None
        return link.verified_user_id

    async def domain_for_identity(self, room_id: str, identity: str) -> str | None:
        """The server a room member identity belongs to, for routing only: the
        domain of a user ID, our own name for a key we hold, or the *claimed*
        domain of another server's key (verified or not; an unverified claim is
        enough to route to and no more). `None` if we have no idea."""
        if not is_member_key(identity):
            return get_domain_from_id(identity)
        if await self.local_user_id_for_identity(room_id, identity) is not None:
            return self.server_name
        link = await self._store.get_member_link(room_id, identity)
        if link is None:
            return None
        return link.domain

    async def room_identity_for_user(
        self, room_id: str, user_id: str, room_version: RoomVersion | None = None
    ) -> str:
        """What a user is called in the given room: their member key if the room
        uses member keys and we hold one for them, else their user ID.

        This is the identity to look a local user up under in the room's state.
        A user of ours who has no key in the room yet (they are about to join for
        the first time) is called by their user ID, under which the state holds
        nothing, which is the right answer.
        """
        if not self._msc4430_enabled or not self._is_mine_id(user_id):
            return user_id
        if room_version is None:
            try:
                room_version = await self._store.get_room_version(room_id)
            except NotFoundError:
                return user_id
        if not room_version.msc4430_member_keys:
            return user_id
        local_key = await self._store.get_local_member_key(room_id, user_id)
        if local_key is None:
            return user_id
        return local_key.member_key

    # ------------------------------------------------------------------
    # The keys we hold
    # ------------------------------------------------------------------

    async def get_or_create_local_member_key(
        self, room_id: str, user_id: str
    ) -> LocalMemberKey:
        """The member key for one of our users in a room, generating and storing a
        fresh one if they have none. A user's key in a room never changes, so a
        user who leaves and re-joins keeps theirs."""
        assert self._is_mine_id(user_id), f"{user_id} is not a local user"
        existing = await self._store.get_local_member_key(room_id, user_id)
        if existing is not None:
            return existing
        # `create_local_member_key` is insert-if-absent, so a concurrent creation
        # for the same user yields the stored key for both.
        return await self._store.create_local_member_key(
            room_id, user_id, generate_signing_key(MemberKeys.KEY_VERSION)
        )

    def generate_pending_member_key(self, user_id: str) -> str:
        """Generate a member key for one of our users in a room that does not
        exist yet: the room ID is derived from the create event, and the create
        event names and is signed by the creator's key, so the key has to exist
        before the room ID does. The key is held in memory (for a bounded time)
        until `persist_pending_member_keys` is called with the create event."""
        assert self._is_mine_id(user_id), f"{user_id} is not a local user"
        signing_key = generate_signing_key(MemberKeys.KEY_VERSION)
        member_key = MemberKey.from_signing_key(signing_key).to_string()
        self._pending_member_keys[member_key] = (user_id, signing_key)
        return member_key

    async def persist_pending_member_keys(self, create_event: EventBase) -> None:
        """Store, under the create event's room ID, the pending keys the event
        names: its sender and its additional creators. Keys the event does not
        name stay pending until they expire."""
        if not supports_msc4430_member_keys(create_event):
            return
        candidates = [create_event.sender]
        additional_creators = create_event.content.get(
            EventContentFields.ADDITIONAL_CREATORS
        )
        if isinstance(additional_creators, list):
            candidates.extend(c for c in additional_creators if isinstance(c, str))

        for member_key in candidates:
            pending = self._pending_member_keys.pop(member_key, None)
            if pending is None:
                continue
            user_id, signing_key = pending
            stored = await self._store.create_local_member_key(
                create_event.room_id, user_id, signing_key
            )
            if stored.member_key != member_key:
                # Cannot happen for a room that did not exist before this create
                # event; if it ever does, the event names a key the user does
                # not have, which is loud rather than wrong.
                raise SynapseError(
                    500,
                    "%s already has member key %s in %s, but the create event names %s"
                    % (user_id, stored.member_key, create_event.room_id, member_key),
                )

    async def _signing_key_for(
        self, room_id: str | None, member_key: str
    ) -> SigningKey | None:
        """The private half of a member key we hold: stored for the room, or,
        for a create event (no room yet), pending for one. `None` otherwise: a
        pending key is not ours in any existing room."""
        if room_id is None:
            pending = self._pending_member_keys.get(member_key)
            return pending[1] if pending is not None else None
        local_key = await self._store.get_local_member_key_by_key(room_id, member_key)
        return local_key.signing_key if local_key is not None else None

    def discard_pending_member_keys(self, event_dict: JsonDict) -> None:
        """Forget the pending keys a create event dict names, because building
        the event failed; nothing will ever claim them."""
        candidates = [event_dict.get("sender")]
        content = event_dict.get("content")
        if isinstance(content, dict):
            additional_creators = content.get(EventContentFields.ADDITIONAL_CREATORS)
            if isinstance(additional_creators, list):
                candidates.extend(additional_creators)
        for member_key in candidates:
            if isinstance(member_key, str):
                self._pending_member_keys.pop(member_key, None)

    # ------------------------------------------------------------------
    # Event creation
    # ------------------------------------------------------------------

    async def translate_event_dict(
        self, room_version: RoomVersion, event_dict: JsonDict
    ) -> None:
        """Rewrite, in place, an event dict one of our users asked us to create so
        that it speaks in member keys: the `sender`, the `state_key` of an
        `m.room.member` event and the `additional_creators` of a create event
        become the users' keys in the room (created if they have none; pending
        keys for a create event), and an `m.room.member` event about one of our
        users gets our linkage information. Rooms without member keys are left
        untouched.

        Linkage information is asserted by servers, never by clients: whatever
        the client put in `localpart` / `domain` is dropped, and the fields are
        filled in only for a target we can vouch for. An erased user gets no
        `localpart`, so their name is not published again.

        Members of other servers cannot be named yet (their keys are learned over
        federation, which a later change adds): a remote user ID as a target,
        additional creator or sender is refused with 400 rather than quietly
        sent as a user ID the room cannot hold. So is a member key the client
        chose itself as a target or additional creator: a key nobody vouches
        for would be a seat in the room for whoever holds its private half.

        Raises:
            SynapseError: 400 for a target or additional creator that is not one
                of our users, or a create event sent into an existing room; 403
                for a remote sender.
        """
        if not room_version.msc4430_member_keys:
            return

        event_type = event_dict.get("type")
        room_id = event_dict.get("room_id")
        if event_type == EventTypes.Create and room_id is not None:
            # Refused later anyway (a create event cannot be sent into a room);
            # refusing here means no key is created or left pending for it.
            raise SynapseError(
                400, "Cannot resend m.room.create event", Codes.INVALID_PARAM
            )
        is_create = event_type == EventTypes.Create
        # The same user named more than once by one create event (as sender and
        # additional creator) gets one key, not one per mention.
        pending_for_user: dict[str, str] = {}

        def pending_key(user_id: str) -> str:
            key = pending_for_user.get(user_id)
            if key is None:
                key = pending_for_user[user_id] = self.generate_pending_member_key(
                    user_id
                )
            return key

        sender = event_dict.get("sender")
        if not isinstance(sender, str):
            raise SynapseError(400, "Missing sender", Codes.BAD_JSON)
        if not is_member_key(sender):
            if not self._is_mine_id(sender):
                raise SynapseError(
                    403, "Cannot send events as a user of another server"
                )
            if is_create:
                event_dict["sender"] = pending_key(sender)
            else:
                if not isinstance(room_id, str):
                    raise SynapseError(400, "Missing room_id", Codes.BAD_JSON)
                local_key = await self.get_or_create_local_member_key(room_id, sender)
                event_dict["sender"] = local_key.member_key

        content = event_dict.get("content")
        if not isinstance(content, dict):
            return

        if event_type == EventTypes.Member and isinstance(room_id, str):
            content.pop(EventContentFields.MSC4430_LOCALPART, None)
            content.pop(EventContentFields.MSC4430_DOMAIN, None)

            state_key = event_dict.get("state_key")
            target_user_id: str | None = None
            if isinstance(state_key, str) and not is_member_key(state_key):
                if not UserID.is_valid(state_key):
                    raise SynapseError(
                        400, "Invalid membership target", Codes.INVALID_PARAM
                    )
                if not self._is_mine_id(state_key):
                    raise SynapseError(
                        400,
                        "Users of other servers cannot be members of a room with "
                        "member keys yet",
                        Codes.INVALID_PARAM,
                    )
                target_user_id = state_key
                local_key = await self.get_or_create_local_member_key(
                    room_id, state_key
                )
                event_dict["state_key"] = local_key.member_key
            elif isinstance(state_key, str):
                target_user_id = await self.local_user_id_for_identity(
                    room_id, state_key
                )
                if target_user_id is None:
                    raise SynapseError(
                        400,
                        "Only members of this server can be named in a room with "
                        "member keys yet",
                        Codes.INVALID_PARAM,
                    )

            if target_user_id is not None:
                target = UserID.from_string(target_user_id)
                content[EventContentFields.MSC4430_DOMAIN] = target.domain
                if not await self._store.is_user_erased(target_user_id):
                    content[EventContentFields.MSC4430_LOCALPART] = target.localpart

        elif event_type == EventTypes.Create:
            additional_creators = content.get(EventContentFields.ADDITIONAL_CREATORS)
            if isinstance(additional_creators, list):
                translated: list[object] = []
                for creator in additional_creators:
                    if isinstance(creator, str) and is_member_key(creator):
                        raise SynapseError(
                            400,
                            "Additional creators of a room with member keys are "
                            "named by user ID",
                            Codes.INVALID_PARAM,
                        )
                    if isinstance(creator, str) and UserID.is_valid(creator):
                        if not self._is_mine_id(creator):
                            raise SynapseError(
                                400,
                                "Users of other servers cannot be additional "
                                "creators of a room with member keys yet",
                                Codes.INVALID_PARAM,
                            )
                        creator = pending_key(creator)
                    # Anything else is left for the auth rules to reject.
                    translated.append(creator)
                content[EventContentFields.ADDITIONAL_CREATORS] = translated

    async def get_signers_for_event_dict(
        self, room_version: RoomVersion, event_dict: JsonDict
    ) -> list[EventSigner]:
        """Who signs an event we are building in a member key room:

        * the sender's member key, under the entity `sender`, if we hold it;
        * our server signing key on an `m.room.member` event whose target is a
          key we hold and whose linkage information is exactly what we vouch for
          (our domain, and the user's localpart unless they are erased): that is
          what lets other servers verify the member's link;
        * the authorising member's key, under its own name, on a restricted join
          that names a key we hold.

        An event whose sender is not a key we hold (a template built for another
        server's member) gets no signers at all: it is hashed and left for that
        member's key to sign. The signing helper checks every signer against the
        event, so a wrong choice here cannot produce a signature the MSC does not
        allow.
        """
        assert room_version.msc4430_member_keys

        sender = event_dict.get("sender")
        if not isinstance(sender, str):
            raise SynapseError(400, "Missing sender", Codes.BAD_JSON)
        room_id = event_dict.get("room_id")
        if room_id is not None and not isinstance(room_id, str):
            raise SynapseError(400, "Invalid room_id", Codes.BAD_JSON)

        sender_signing_key = await self._signing_key_for(room_id, sender)
        if sender_signing_key is None:
            return []
        signers: list[EventSigner] = [(MemberKeys.SIGNATURE_ENTITY, sender_signing_key)]

        if event_dict.get("type") != EventTypes.Member:
            return signers
        content = event_dict.get("content")
        if not isinstance(content, dict):
            return signers

        state_key = event_dict.get("state_key")
        if isinstance(state_key, str):
            target_user_id = await self.local_user_id_for_identity(room_id, state_key)
            if target_user_id is not None and await self._is_our_linkage(
                target_user_id, content
            ):
                signers.append((self.server_name, self._server_signing_key))

        authorising_member = content.get(EventContentFields.AUTHORISING_USER)
        if (
            isinstance(authorising_member, str)
            and content.get(EventContentFields.MEMBERSHIP) == Membership.JOIN
        ):
            authorising_key = await self._signing_key_for(room_id, authorising_member)
            if authorising_key is not None:
                signers.append((authorising_member, authorising_key))

        return signers

    async def _is_our_linkage(self, user_id: str, content: JsonDict) -> bool:
        """Whether a membership event's linkage information is exactly what this
        server vouches for about `user_id`: our domain, and their localpart
        unless they are erased, in which case none. Anything else (another
        localpart, a missing one for a user who is not erased) is not ours to
        co-sign, wherever the dict came from."""
        user = UserID.from_string(user_id)
        if content.get(EventContentFields.MSC4430_DOMAIN) != user.domain:
            return False
        if await self._store.is_user_erased(user_id):
            return EventContentFields.MSC4430_LOCALPART not in content
        return content.get(EventContentFields.MSC4430_LOCALPART) == user.localpart
