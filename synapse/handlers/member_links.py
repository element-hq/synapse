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
"""MSC4430: learning what a member key stands for.

A member key names no server. What it stands for is *linkage information* —
`content.localpart` and `content.domain` on the member's `m.room.member` event —
and that information is worth something only when two signatures are present:

* one by the member key itself, which is what makes the claim theirs (on an
  event the member sent, the `sender` signature already is that; on an event
  somebody else sent about them, a signature under the entity `state_key`).
  Without it, anybody able to kick or ban a member could claim they belong to a
  domain of the kicker's choosing.
* one by the current server signing key of the domain claimed, which is what
  makes the claim *verified*: the domain says it stands behind the member. This
  is the half that needs the network, and so the half that can be absent for
  reasons that are nobody's fault.

A link is therefore the linkage information asserted by the *current*
`m.room.member` event for that key, and nothing else: a member who has left has
no link, because the event which removed them was sent by somebody else and
asserts nothing. That makes the mapping a function of the room's current state,
so every server that has the state agrees, no history is needed, and events
arriving out of order cannot change it.

Verification is cached in `msc4430_member_links` rather than redone per read.
The claimed domain is used for *routing* whether verified or not — an unverified
claim is somewhere to send to, and grants nothing — while trust (which servers
may see what) follows only verified links.
"""

import logging
from typing import TYPE_CHECKING, Collection

import attr
from signedjson.sign import SignatureVerifyException, verify_signed_json

from synapse.api.constants import EventContentFields, EventTypes, MemberKeys
from synapse.api.errors import SynapseError
from synapse.crypto.keyring import VerifyJsonRequest
from synapse.events import EventBase
from synapse.events.py_protocol import supports_msc4430_member_keys
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.storage.member_keys import MemberLink
from synapse.types import MemberKey, StateMap, is_member_key
from synapse.types.state import StateFilter
from synapse.util.caches.expiringcache import ExpiringCache
from synapse.util.duration import Duration

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# How long a verification result is remembered against the event that produced
# it. Long enough to span an event's arrival (where the result decides soft
# failure) and its becoming current state (where the link is written).
ASSERTION_CACHE_LIFETIME_MS = 60 * 60 * 1000


# The unverified links to re-check, and how often. Verification fails for
# reasons that have nothing to do with the claim — the claimed domain was
# unreachable when its member arrived — and without a retry such a member stays
# soft-failed until they next send a membership event. This is deliberately
# small: it is a best-effort repair, not the periodic re-verification that key
# rotation needs.
RETRY_UNVERIFIED_LINKS_INTERVAL = Duration(minutes=1)
RETRY_UNVERIFIED_LINKS_BATCH = 50
RETRY_UNVERIFIED_LINKS_ATTEMPTS = 5


@attr.s(slots=True, frozen=True, auto_attribs=True)
class LinkAssertion:
    """The linkage information an `m.room.member` event asserts about the member
    key in its `state_key`, having been signed by that key."""

    domain: str
    localpart: str | None
    verified: bool
    """Whether `domain`'s current server signing key has vouched for the claim."""
    key_id: str | None
    """The signing key that vouched for it, if any."""


class MemberLinkHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self._store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self._keyring = hs.get_keyring()
        self._clock = hs.get_clock()
        self._server_name = hs.hostname
        self._enabled = hs.config.experimental.msc4430_enabled

        # Verification results, keyed by event ID: computed when an event
        # arrives (to decide soft failure) and read again when it becomes
        # current state (to write the link), without a second key fetch.
        self._assertions: ExpiringCache[str, LinkAssertion | None] = ExpiringCache(
            cache_name="msc4430_link_assertions",
            server_name=hs.hostname,
            hs=hs,
            clock=self._clock,
            max_len=10000,
            expiry_ms=ASSERTION_CACHE_LIFETIME_MS,
        )
        # How many times we have re-checked each unverified link since startup,
        # so that a domain which simply does not vouch for its members is not
        # asked forever. Deliberately not persisted.
        self._retry_attempts: dict[tuple[str, str], int] = {}

        if self._enabled and hs.config.worker.run_background_tasks:
            self._clock.looping_call(
                self._retry_unverified_links, RETRY_UNVERIFIED_LINKS_INTERVAL
            )

    # ------------------------------------------------------------------
    # Reading an event's claim
    # ------------------------------------------------------------------

    def _asserted_linkage(self, event: EventBase) -> tuple[str, str | None] | None:
        """The linkage information this event asserts about its `state_key`, if
        the member key named there has signed the event. `None` if the event
        carries no linkage information, or none that the member stands behind:
        anybody can write a localpart and a domain into an event about somebody
        else, and only the member's signature makes it theirs.

        No network: the member key is in the `state_key`.
        """
        if event.type != EventTypes.Member or not supports_msc4430_member_keys(event):
            return None
        state_key = event.get_state_key()
        if state_key is None or not is_member_key(state_key):
            return None

        domain = event.content.get(EventContentFields.MSC4430_DOMAIN)
        if not isinstance(domain, str) or not domain:
            return None
        localpart = event.content.get(EventContentFields.MSC4430_LOCALPART)
        if localpart is not None and not isinstance(localpart, str):
            return None

        try:
            member_key = MemberKey.from_string(state_key)
        except SynapseError:
            return None

        # The member's own signature: under `sender` on their own event, which
        # every event in the room carries and which was checked when the event
        # was accepted, or under `state_key` on somebody else's.
        entity = (
            MemberKeys.SIGNATURE_ENTITY
            if event.sender == state_key
            else MemberKeys.STATE_KEY_SIGNATURE_ENTITY
        )
        if not _check_member_key_signature(event, entity, member_key):
            logger.info(
                "MSC4430: ignoring linkage information on %s: no signature under %r "
                "by the member key it is about",
                event.event_id,
                entity,
            )
            return None

        return domain, localpart

    async def assertion_for_event(self, event: EventBase) -> LinkAssertion | None:
        """What this event says its `state_key` stands for, and whether the
        domain it claims vouched for it. `None` if the event asserts nothing.

        May hit the network to fetch the claimed domain's signing keys. The
        result is remembered against the event ID, so that deciding whether to
        soft fail the event and later writing its link agree.
        """
        if event.event_id in self._assertions:
            return self._assertions[event.event_id]

        asserted = self._asserted_linkage(event)
        if asserted is None:
            self._assertions[event.event_id] = None
            return None

        domain, localpart = asserted
        verified, key_id = await self._check_domain_signature(event, domain)
        assertion = LinkAssertion(
            domain=domain, localpart=localpart, verified=verified, key_id=key_id
        )
        self._assertions[event.event_id] = assertion
        return assertion

    async def _check_domain_signature(
        self, event: EventBase, domain: str
    ) -> tuple[bool, str | None]:
        """Whether the claimed domain's *current* signing key vouches for this
        event, and which key ID it was.

        Only the current key counts (MSC4430): a key that has been rotated away
        must not be able to vouch for a member, which is what makes rotation a
        remedy for a leaked member key. A domain we cannot reach is not treated
        as a refusal — the link stays unverified and is re-checked later.
        """
        if domain not in event.signatures:
            return False, None
        key_ids = sorted(event.signatures[domain])
        try:
            await self._keyring.verify_event_for_server(
                domain, event, self._clock.time_msec()
            )
        except Exception as e:
            logger.info(
                "MSC4430: %s does not (yet) vouch for %s: %s", domain, event.event_id, e
            )
            return False, None
        # `verify_event_for_server` succeeds if any of the event's signatures by
        # that domain verifies against a current key; where there is more than
        # one, this names the first. The key ID is a hint for finding the links a
        # rotated key vouched for, and re-checking one always re-checks the event.
        return True, key_ids[0]

    # ------------------------------------------------------------------
    # Soft failure
    # ------------------------------------------------------------------

    async def is_sender_verified(self, event: EventBase) -> bool:
        """Whether the member key that sent this event has a verified link, so
        that its messages may be shown to clients. Keys we hold are ours to
        vouch for and always count.

        A membership event the member sent about themselves is judged on the
        claim it carries rather than on what we knew before it: it is the event
        that establishes the link, so judging it by the previous one would leave
        a member who has moved servers permanently soft failed.
        """
        if not supports_msc4430_member_keys(event) or not is_member_key(event.sender):
            return True

        if await self._store.get_local_member_key_by_key(event.room_id, event.sender):
            return True

        if event.get_state_key() == event.sender:
            assertion = await self.assertion_for_event(event)
            return assertion is not None and assertion.verified

        link = await self._store.get_member_link(event.room_id, event.sender)
        return link is not None and link.verified

    # ------------------------------------------------------------------
    # Keeping the links in line with current state
    # ------------------------------------------------------------------

    async def update_links_for_events(self, events: Collection[EventBase]) -> None:
        """Bring the links of any member keys these just-persisted events are
        about into line with the room's current state."""
        if not self._enabled:
            return
        by_room: dict[str, set[str]] = {}
        for event in events:
            if event.type != EventTypes.Member or not supports_msc4430_member_keys(
                event
            ):
                continue
            state_key = event.get_state_key()
            if state_key is not None and is_member_key(state_key):
                by_room.setdefault(event.room_id, set()).add(state_key)
        for room_id, member_keys in by_room.items():
            await self.update_links(room_id, member_keys)

    async def learn_links_for_room(self, room_id: str) -> None:
        """Learn the links of every member of a room, from its current state.
        Used when we have just joined and have the whole state at once."""
        if not self._enabled:
            return
        members = await self._store.get_users_in_room(room_id)
        member_keys = [m for m in members if is_member_key(m)]
        if member_keys:
            await self.update_links(room_id, member_keys)

    async def update_links(self, room_id: str, member_keys: Collection[str]) -> None:
        """Set each key's link from its current `m.room.member` event, or forget
        it if that event asserts nothing — which is the case for every member who
        has left, since the event that removed them was somebody else's."""
        if not self._enabled or not member_keys:
            return

        state_ids = await self._current_member_event_ids(room_id, member_keys)
        events = await self._store.get_events(list(state_ids.values()))

        for member_key in member_keys:
            event_id = state_ids.get((EventTypes.Member, member_key))
            event = events.get(event_id) if event_id is not None else None
            assertion = (
                await self.assertion_for_event(event) if event is not None else None
            )
            if assertion is None:
                if await self._store.delete_member_link(room_id, member_key):
                    logger.info(
                        "MSC4430: forgot the link for %s in %s", member_key, room_id
                    )
                    await self._notify_link_changed(room_id, member_key)
                self._retry_attempts.pop((room_id, member_key), None)
                continue

            assert event is not None
            link = MemberLink(
                room_id=room_id,
                member_key=member_key,
                domain=assertion.domain,
                localpart=assertion.localpart,
                verified=assertion.verified,
                event_id=event.event_id,
                key_id=assertion.key_id,
            )
            previous = await self._store.upsert_member_link(link)
            if previous != link:
                logger.info(
                    "MSC4430: %s in %s is %s (%s)",
                    member_key,
                    room_id,
                    _claimed_user_id(link),
                    "verified" if link.verified else "unverified",
                )
                await self._notify_link_changed(room_id, member_key)
            self._retry_attempts.pop((room_id, member_key), None)

    async def _current_member_event_ids(
        self, room_id: str, member_keys: Collection[str]
    ) -> StateMap[str]:
        return await self._storage_controllers.state.get_current_state_ids(
            room_id,
            StateFilter.from_types(
                (EventTypes.Member, member_key) for member_key in member_keys
            ),
        )

    async def _notify_link_changed(self, room_id: str, member_key: str) -> None:
        """Tell clients that the user behind a member key changed. The change has
        no event of its own when it comes from a verification rather than from a
        membership event, so it goes on the member info stream."""
        await self.hs.get_member_info_handler().on_links_changed(room_id, [member_key])

    # ------------------------------------------------------------------
    # Re-checking what we could not verify
    # ------------------------------------------------------------------

    @wrap_as_background_process("msc4430_retry_unverified_links")
    async def _retry_unverified_links(self) -> None:
        rows = await self._store.get_unverified_member_links(
            RETRY_UNVERIFIED_LINKS_BATCH
        )
        for room_id, member_key, event_id in rows:
            attempts = self._retry_attempts.get((room_id, member_key), 0)
            if event_id is None or attempts >= RETRY_UNVERIFIED_LINKS_ATTEMPTS:
                continue
            self._retry_attempts[(room_id, member_key)] = attempts + 1
            # Drop the remembered failure so the keys are fetched again.
            self._assertions.pop(event_id, None)
            try:
                await self.update_links(room_id, [member_key])
            except Exception:
                logger.exception(
                    "MSC4430: failed to re-check the link for %s in %s",
                    member_key,
                    room_id,
                )


def _claimed_user_id(link: MemberLink) -> str:
    return (
        f"@{link.localpart}:{link.domain}"
        if link.localpart is not None
        else f"(unnamed)@{link.domain}"
    )


def _check_member_key_signature(
    event: EventBase, entity: str, member_key: MemberKey
) -> bool:
    """Whether the event carries a valid signature by `member_key` under the
    given entity, over the redacted event (as all signatures are)."""
    try:
        verify_request = VerifyJsonRequest.from_event(entity, event, 0)
        verify_signed_json(
            verify_request.get_json_object(), entity, member_key.verify_key()
        )
    except (SynapseError, SignatureVerifyException):
        return False
    return True
