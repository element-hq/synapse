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
"""Data types for MSC4430 member keys, shared between the storage layer and its
consumers."""

import attr
from signedjson.key import decode_signing_key_base64, encode_signing_key_base64
from signedjson.types import SigningKey

from synapse.api.constants import MemberKeys


def encode_member_signing_key(signing_key: SigningKey) -> str:
    """Serialise the private half of a member key for storage: the unpadded base64
    of the 32 byte seed."""
    return encode_signing_key_base64(signing_key)


def decode_member_signing_key(encoded: str) -> SigningKey:
    """Inverse of `encode_member_signing_key`. Member keys always have the key ID
    `ed25519:1`."""
    return decode_signing_key_base64(
        MemberKeys.KEY_ALGORITHM, MemberKeys.KEY_VERSION, encoded
    )


@attr.s(slots=True, frozen=True, auto_attribs=True)
class LocalMemberKey:
    """A member key this server holds: the identity of one of our users in one
    room, with the private key that signs their events there."""

    room_id: str
    user_id: str
    member_key: str
    """The `@<base64>` form that appears as `sender` and `state_key` in the room."""
    signing_key: SigningKey


@attr.s(slots=True, frozen=True, auto_attribs=True)
class MemberLink:
    """What we know about a member key we do not hold: the user it claims to be and
    whether that claim has been verified. One row of `msc4430_member_links`."""

    room_id: str
    member_key: str
    domain: str
    """The claimed domain. Routing information, used whether verified or not."""
    localpart: str | None
    """The claimed localpart. `None` once the event that carried it was redacted."""
    verified: bool
    """Whether `domain`'s server signing key `key_id` co-signed `event_id` and was
    `domain`'s current key when last checked."""
    event_id: str | None
    """The `m.room.member` event the link was learned from, if known. Always set
    on a verified link."""
    key_id: str | None
    """The server signing key ID the co-signature on `event_id` named, if any.
    Always set on a verified link, and kept when the link is later downgraded
    because that key stopped being current."""

    def __attrs_post_init__(self) -> None:
        if self.verified and (self.event_id is None or self.key_id is None):
            raise ValueError(
                "A verified member link must name the event and server signing key "
                "that verified it"
            )

    @property
    def claimed_user_id(self) -> str | None:
        """The user ID this member key claims to be, verified or not; `None` once
        the localpart has been redacted."""
        if self.localpart is None:
            return None
        return f"@{self.localpart}:{self.domain}"

    @property
    def verified_user_id(self) -> str | None:
        """The user ID this member key is known to belong to: the claimed user ID if
        the link is verified, else `None`."""
        if not self.verified:
            return None
        return self.claimed_user_id
