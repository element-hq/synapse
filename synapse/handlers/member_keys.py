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
from collections.abc import Mapping
from typing import TYPE_CHECKING, Iterable

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
from synapse.types import (
    JsonDict,
    MemberKey,
    RoomMemberIdentity,
    UserID,
    get_domain_from_id,
    is_member_key,
)
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


def _may_assert_linkage(event_dict: JsonDict, content: JsonDict) -> bool:
    """Whether a membership event we are building may carry its target's linkage
    information.

    Only the member themselves may assert who a member key belongs to, by signing
    the event with that key: a claim made by anyone else is ignored by every other
    server, since otherwise a moderator could kick a member while naming a
    localpart and a domain of their choosing and have it believed. So an event the
    member sent carries their linkage information, and an invite carries it because
    the invitee's own server signs it with their key alongside its own. A kick, a
    ban or an unban carries none, and so names nobody: it would be a claim nobody
    believes, and would publish a localpart in a room that has no use for it.
    """
    return event_dict.get("sender") == event_dict.get("state_key") or (
        content.get(EventContentFields.MEMBERSHIP) == Membership.INVITE
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

        # The key generated for a remote join in progress, so that a retry of the
        # same join offers the same key rather than a fresh one each time.
        self._pending_join_keys: ExpiringCache[tuple[str, str], str] = ExpiringCache(
            cache_name="msc4430_pending_join_keys",
            server_name=hs.hostname,
            hs=hs,
            clock=hs.get_clock(),
            expiry_ms=PENDING_MEMBER_KEY_LIFETIME_MS,
        )

        # Member keys generated for a create event, whose room does not exist yet
        # (the room ID is derived from the create event, which the key signs).
        # Keyed by member key; the value is the user and the signing key. Moved to
        # storage by `persist_pending_member_keys` once the create event is built.
        self._pending_member_keys: ExpiringCache[
            str, tuple[str, SigningKey, str | None]
        ] = ExpiringCache(
            cache_name="msc4430_pending_member_keys",
            server_name=hs.hostname,
            hs=hs,
            clock=hs.get_clock(),
            expiry_ms=PENDING_MEMBER_KEY_LIFETIME_MS,
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

        if room_id is not None:
            local_key = await self._store.get_local_member_key_by_key(room_id, identity)
            if local_key is not None:
                return local_key.user_id
        pending = self._pending_member_keys.get(identity)
        if pending is not None and pending[2] == room_id:
            return pending[0]
        return None

    async def is_local_identity(self, room_id: str, identity: str) -> bool:
        """Whether the given room member identity belongs to this server."""
        return await self.local_user_id_for_identity(room_id, identity) is not None

    async def user_id_for_identity(self, room_id: str, identity: str) -> str | None:
        """The user ID behind a room member identity: a user ID itself, the user
        behind one of our member keys, or the *verified* link of another
        server's key. `None` for a key we hold no verified link for: such a key
        maps to no user, whatever it claims."""
        return (await self.user_ids_for_identities(room_id, [identity]))[identity]

    async def user_ids_for_identities(
        self, room_id: str, identities: Iterable[str]
    ) -> dict[str, str | None]:
        """Batched `user_id_for_identity`: every identity given maps to a user ID
        or to `None`."""
        return await self._store.get_user_ids_for_identities(room_id, identities)

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

    async def parse_membership_target(
        self, room_id: str, target: str
    ) -> RoomMemberIdentity:
        """Parse a membership target a client named (`/invite`, `/kick`, `/ban`,
        `/unban`, `PUT /state/m.room.member/{target}`).

        In a room without member keys the target is a user ID, as ever. In an
        MSC4430 member key room a client may also name a member by their key:
        a key we hold is handled as the user behind it, so that everything the
        membership code does for a user of ours (suspension, invite rules,
        profiles, notifications) applies and the user ID is turned back into
        the key when the event is built; any other key (one nobody vouches
        for, or a member of another server, whose handling is a later change)
        is returned as a `MemberKey`, which only moderation (a kick, a ban, an
        unban) can act on, and which the event then names as it is.

        Raises:
            SynapseError: 400 for a malformed target.
        """
        try:
            room_version = await self._store.get_room_version(room_id)
            member_keys = room_version.msc4430_member_keys
        except NotFoundError:
            member_keys = False
        if not member_keys or not is_member_key(target):
            return UserID.from_string(target)
        member_key = MemberKey.from_string(target)
        user_id = await self.local_user_id_for_identity(room_id, target)
        if user_id is not None:
            return UserID.from_string(user_id)
        return member_key

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
        return self._generate_pending_member_key(user_id, None)

    async def member_key_for_remote_join(self, room_id: str, user_id: str) -> str:
        """The key one of our users will join a remote room with: the one we
        already hold for that room, or a fresh one held in memory until the join
        goes ahead.

        A server joining a room it has never seen does not know whether the room
        identifies its members by key until `/make_join` answers, so it has to
        offer one either way. Keeping it in memory until the room turns out to
        want it means a join to a room without member keys leaves nothing behind.
        """
        assert self._is_mine_id(user_id), f"{user_id} is not a local user"
        existing = await self._store.get_local_member_key(room_id, user_id)
        if existing is not None:
            return existing.member_key
        pending = self._pending_join_keys.get((room_id, user_id))
        if pending is not None:
            return pending
        member_key = self._generate_pending_member_key(user_id, room_id)
        self._pending_join_keys[(room_id, user_id)] = member_key
        return member_key

    async def persist_pending_join_key(self, room_id: str, member_key: str) -> None:
        """Store a key generated for a remote join, now that the room is known to
        want one. Called before the join event is sent, so that a join which
        succeeds is never one we cannot sign for afterwards."""
        pending = self._pending_member_keys.pop(member_key, None)
        if pending is None:
            return
        user_id, signing_key, pending_room_id = pending
        assert pending_room_id == room_id, (
            f"{member_key} is pending for {pending_room_id}, not {room_id}"
        )
        self._pending_join_keys.pop((room_id, user_id), None)
        await self._store.create_local_member_key(room_id, user_id, signing_key)

    def _generate_pending_member_key(self, user_id: str, room_id: str | None) -> str:
        signing_key = generate_signing_key(MemberKeys.KEY_VERSION)
        member_key = MemberKey.from_signing_key(signing_key).to_string()
        self._pending_member_keys[member_key] = (user_id, signing_key, room_id)
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
            user_id, signing_key, _ = pending
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

    async def signing_key_for_identity(
        self, room_id: str, identity: str
    ) -> SigningKey | None:
        """The private half of a member key we hold in a room, for signing on
        behalf of that member: the authorising member of a restricted join.
        `None` for anything that is not a key of ours there."""
        if not is_member_key(identity):
            return None
        return await self._signing_key_for(room_id, identity)

    async def _signing_key_for(
        self, room_id: str | None, member_key: str
    ) -> SigningKey | None:
        """The private half of a member key we hold: stored for the room, or
        pending for it — a create event, whose room does not exist yet, or a
        remote join, whose room we are not in yet. `None` otherwise: a key
        pending for one room is not ours in another."""
        local_key = (
            await self._store.get_local_member_key_by_key(room_id, member_key)
            if room_id is not None
            else None
        )
        if local_key is not None:
            return local_key.signing_key
        pending = self._pending_member_keys.get(member_key)
        if pending is not None and pending[2] == room_id:
            return pending[1]
        return None

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

        Linkage information is never asserted by clients: whatever the client put
        in `localpart` / `domain` is dropped, and the fields are filled in only
        for a target we can vouch for, on an event that may assert them at all
        (see `_may_assert_linkage`). An erased user gets no `localpart`, so their
        name is not published again.

        The `users` of an `m.room.power_levels` event are translated the same
        way (user IDs of ours become keys; keys stay as they are, so a client
        may name any member by key).

        Members of other servers cannot be named yet (their keys are learned over
        federation, which a later change adds): a remote user ID as a target,
        additional creator, power level entry or sender is refused with 400
        rather than quietly sent as a user ID the room cannot hold. A member
        key the client chose itself as a target or additional creator is
        refused too (a key nobody vouches for would be a seat in the room for
        whoever holds its private half), except as the target of a kick, ban or
        unban: a moderator must be able to act on any member, so a membership
        of `leave` or `ban` sent by someone else may name any key, and carries
        no linkage information for it. Third-party invites bind a user ID to
        a membership, which has no meaning for keys, and are refused.

        Raises:
            SynapseError: 400 for a target, additional creator or power level
                entry that is not one of our users, a create event sent into
                an existing room, or a third-party invite; 403 for a remote
                sender.
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
        if isinstance(content, Mapping) and not isinstance(content, dict):
            # (The content of an event copied from another room, as a room
            # upgrade does, may be a frozen dict.)
            content = event_dict["content"] = dict(content)
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
                is_moderation = (
                    content.get(EventContentFields.MEMBERSHIP)
                    in (Membership.LEAVE, Membership.BAN)
                    and event_dict.get("sender") != state_key
                )
                if target_user_id is None and not is_moderation:
                    raise SynapseError(
                        400,
                        "Only members of this server can be named in a room with "
                        "member keys yet",
                        Codes.INVALID_PARAM,
                    )

            if target_user_id is not None and _may_assert_linkage(event_dict, content):
                target = UserID.from_string(target_user_id)
                content[EventContentFields.MSC4430_DOMAIN] = target.domain
                if not await self._store.is_user_erased(target_user_id):
                    content[EventContentFields.MSC4430_LOCALPART] = target.localpart

        elif event_type == EventTypes.ThirdPartyInvite:
            raise SynapseError(
                400,
                "Third-party invites are not supported in rooms with member keys",
                Codes.UNRECOGNIZED,
            )

        elif event_type == EventTypes.PowerLevels and isinstance(room_id, str):
            users = content.get("users")
            if isinstance(users, dict):
                translated_users: dict[str, object] = {}
                for identity, level in users.items():
                    if (
                        isinstance(identity, str)
                        and not is_member_key(identity)
                        and UserID.is_valid(identity)
                    ):
                        if not self._is_mine_id(identity):
                            raise SynapseError(
                                400,
                                "Users of other servers cannot be given power "
                                "levels in a room with member keys yet",
                                Codes.INVALID_PARAM,
                            )
                        local_key = await self.get_or_create_local_member_key(
                            room_id, identity
                        )
                        identity = local_key.member_key
                    # Keys, and anything else, are left for the auth rules.
                    if identity in translated_users:
                        # (The same member by user ID and by key: which level?)
                        raise SynapseError(
                            400,
                            "Power levels name %s more than once" % (identity,),
                            Codes.INVALID_PARAM,
                        )
                    translated_users[identity] = level
                content["users"] = translated_users

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
        * on an `m.room.member` event whose target is a key we hold and whose
          linkage information is exactly what we vouch for (our domain, and the
          user's localpart unless they are erased): that member's own key, under
          the entity `state_key`, if somebody else sent the event, asserting the
          claim; and our server signing key, under our name, vouching for it.
          Together they are what lets other servers verify the member's link;
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
                if state_key != sender:
                    # Somebody else's event about our member (an invite): their
                    # key asserts the claim, since ours is not enough for other
                    # servers to believe it. An event the member sent themselves
                    # needs no second signature.
                    target_signing_key = await self._signing_key_for(room_id, state_key)
                    if target_signing_key is not None:
                        signers.append(
                            (
                                MemberKeys.STATE_KEY_SIGNATURE_ENTITY,
                                target_signing_key,
                            )
                        )
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

    async def linkage_content_for_local_user(self, user_id: str) -> JsonDict:
        """The linkage information this server vouches for about one of its
        users: our domain, and their localpart unless they are erased, in which
        case their name is not published again.

        The counterpart of `_is_our_linkage`, for the places that have to fill
        linkage information in rather than check it: an event we build for one
        of our users in a room we are not in yet (a join over federation), where
        `translate_event_dict` has no part to play.
        """
        user = UserID.from_string(user_id)
        assert self._is_mine_id(user_id), f"{user_id} is not a local user"
        content: JsonDict = {EventContentFields.MSC4430_DOMAIN: user.domain}
        if not await self._store.is_user_erased(user_id):
            content[EventContentFields.MSC4430_LOCALPART] = user.localpart
        return content

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
