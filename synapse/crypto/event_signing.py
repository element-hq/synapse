#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

import collections.abc
import hashlib
import logging
from typing import Any, Callable, Iterable

from canonicaljson import encode_canonical_json
from signedjson.sign import sign_json
from signedjson.types import SigningKey, VerifyKey
from unpaddedbase64 import decode_base64, encode_base64

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    MemberKeys,
    Membership,
)
from synapse.api.errors import Codes, MemberKeySigningRefusedError, SynapseError
from synapse.api.room_versions import RoomVersion
from synapse.events import EventBase
from synapse.events.utils import prune_event
from synapse.logging.opentracing import trace
from synapse.synapse_rust.events import redact_event_dict
from synapse.types import JsonDict, MemberKey, UserID, is_member_key

logger = logging.getLogger(__name__)

Hasher = Callable[[bytes], "hashlib._Hash"]


@trace
def check_event_content_hash(
    event: EventBase, hash_algorithm: Hasher = hashlib.sha256
) -> bool:
    """Check whether the hash for this PDU matches the contents"""
    name, expected_hash = compute_content_hash(event.get_pdu_json(), hash_algorithm)
    logger.debug(
        "Verifying content hash on %s (expecting: %s)",
        event.event_id,
        encode_base64(expected_hash),
    )

    # some malformed events lack a 'hashes'. Protect against it being missing
    # or a weird type by basically treating it the same as an unhashed event.
    hashes = event.get("hashes")
    # nb it might be a immutabledict or a dict
    if not isinstance(hashes, collections.abc.Mapping):
        raise SynapseError(
            400, "Malformed 'hashes': %s" % (type(hashes),), Codes.UNAUTHORIZED
        )

    if name not in hashes:
        raise SynapseError(
            400,
            "Algorithm %s not in hashes %s" % (name, list(hashes)),
            Codes.UNAUTHORIZED,
        )
    message_hash_base64 = hashes[name]
    try:
        message_hash_bytes = decode_base64(message_hash_base64)
    except Exception:
        raise SynapseError(
            400, "Invalid base64: %s" % (message_hash_base64,), Codes.UNAUTHORIZED
        )
    return message_hash_bytes == expected_hash


def compute_content_hash(
    event_dict: dict[str, Any], hash_algorithm: Hasher
) -> tuple[str, bytes]:
    """Compute the content hash of an event, which is the hash of the
    unredacted event.

    Args:
        event_dict: The unredacted event as a dict
        hash_algorithm: A hasher from `hashlib`, e.g. hashlib.sha256, to use
            to hash the event

    Returns:
        A tuple of the name of hash and the hash as raw bytes.
    """
    event_dict = dict(event_dict)
    event_dict.pop("age_ts", None)
    event_dict.pop("unsigned", None)
    event_dict.pop("signatures", None)
    event_dict.pop("hashes", None)
    event_dict.pop("outlier", None)
    event_dict.pop("destinations", None)

    # N.B. no need to pop the room_id from create events in MSC4291 rooms
    # as they shouldn't have one.

    event_json_bytes = encode_canonical_json(event_dict)

    hashed = hash_algorithm(event_json_bytes)
    return hashed.name, hashed.digest()


def compute_event_reference_hash(
    event: EventBase, hash_algorithm: Hasher = hashlib.sha256
) -> tuple[str, bytes]:
    """Computes the event reference hash. This is the hash of the redacted
    event.

    Args:
        event
        hash_algorithm: A hasher from `hashlib`, e.g. hashlib.sha256, to use
            to hash the event

    Returns:
        A tuple of the name of hash and the hash as raw bytes.
    """
    tmp_event = prune_event(event)
    event_dict = tmp_event.get_pdu_json()
    event_dict.pop("signatures", None)
    event_dict.pop("age_ts", None)
    event_dict.pop("unsigned", None)
    event_json_bytes = encode_canonical_json(event_dict)
    hashed = hash_algorithm(event_json_bytes)
    return hashed.name, hashed.digest()


def compute_event_signature(
    room_version: RoomVersion,
    event_dict: JsonDict,
    signature_name: str,
    signing_key: SigningKey,
) -> dict[str, dict[str, str]]:
    """Compute the signature of the event for the given name and key.

    Args:
        room_version: the version of the room that this event is in.
            (the room version determines the redaction algorithm and hence the
            json to be signed)

        event_dict: The event as a dict

        signature_name: The name of the entity signing the event
            (typically the server's hostname).

        signing_key: The key to sign with

    Returns:
        a dictionary in the same format of an event's signatures field.
    """
    redact_json = redact_event_dict(room_version, event_dict)
    redact_json.pop("age_ts", None)
    redact_json.pop("unsigned", None)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Signing event: %s", encode_canonical_json(redact_json))
    redact_json = sign_json(redact_json, signature_name, signing_key)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Signed event: %s", encode_canonical_json(redact_json))
    return redact_json["signatures"]


# An entity name and the key it signs with, see `add_hashes_and_signatures_from_signers`.
EventSigner = tuple[str, SigningKey]


def add_hashes_and_signatures_from_signers(
    room_version: RoomVersion,
    event_dict: JsonDict,
    signers: Iterable[EventSigner],
    *,
    server_name: str,
) -> None:
    """Add the content hash and sign the event with each of the given signers.

    The MSC4430 counterpart of `add_hashes_and_signatures`: in a member key room an
    event is signed by the sender's member key rather than by the server, and
    `m.room.member` events may carry further signatures. Which signers an event gets
    is decided by the caller; this function checks that each one is a signature the
    MSC allows, so that a bug in that decision cannot make us sign as a key we do
    not hold or vouch for linkage information that is not ours:

    * `MemberKeys.SIGNATURE_ENTITY` (`sender`): the key must be the one named by the
      event's `sender`.
    * A member key string (the authorising member of a restricted join, MSC-Q §2):
      the event must be a join naming that key in `join_authorised_via_users_server`,
      and the key must be that member key.
    * `MemberKeys.LINKAGE_SIGNATURE_ENTITY` (`state_key`), co-signing the linkage
      information of the member named by `state_key`: the event must be an
      `m.room.member` event whose `content.domain` is us, and the key must be our
      server signing key.

    Anything else is refused. To add one signature to an event that is already
    hashed (one received from another server), use
    `compute_member_key_room_signature` instead.

    Args:
        room_version: the version of the room this event is in; must have
            `msc4430_member_keys`.
        event_dict: The event to add hashes to and sign.
        signers: (entity name, signing key) pairs. May be empty: a template built
            for a remote member (`/make_join` and friends) is hashed but left for
            the remote member key to sign.
        server_name: this server's name, the only server entity we sign as.

    Raises:
        MemberKeySigningRefusedError: if a signer is not one the event may carry.
    """
    _assert_member_key_room(room_version)

    # Check every signer before touching the event.
    signers = list(signers)
    for signature_name, signing_key in signers:
        _check_member_key_room_signer(
            event_dict, signature_name, signing_key, server_name
        )

    name, digest = compute_content_hash(event_dict, hash_algorithm=hashlib.sha256)

    event_dict.setdefault("hashes", {})[name] = encode_base64(digest)

    for signer in signers:
        # Each signer adds to the signatures made so far rather than replacing
        # them, see `compute_member_key_room_signature`.
        event_dict["signatures"] = compute_member_key_room_signature(
            room_version, event_dict, signer, server_name=server_name
        )


def compute_member_key_room_signature(
    room_version: RoomVersion,
    event_dict: JsonDict,
    signer: EventSigner,
    *,
    server_name: str,
) -> dict[str, dict[str, str]]:
    """Compute one more signature for an already hashed event in a member key room.

    Applies the same checks as `add_hashes_and_signatures_from_signers` but does not
    recompute `hashes`, so it is also the way to add our signature to an event
    received from another server: the authorising member's signature on a
    `/send_join`, or our co-signature on an inbound `/invite` for our user.

    Returns:
        The event's signatures, including the new one, in the format of an event's
        `signatures` field (`sign_json` carries the existing signatures through).
    """
    _assert_member_key_room(room_version)
    signature_name, signing_key = signer
    _check_member_key_room_signer(event_dict, signature_name, signing_key, server_name)
    return compute_event_signature(
        room_version,
        event_dict,
        signature_name=signature_name,
        signing_key=signing_key,
    )


def _assert_member_key_room(room_version: RoomVersion) -> None:
    if not room_version.msc4430_member_keys:
        raise SynapseError(
            500,
            "Member key signing helpers are for MSC4430 rooms only, got %s"
            % (room_version.identifier,),
        )


def _check_member_key_room_signer(
    event_dict: JsonDict,
    signature_name: str,
    signing_key: SigningKey,
    server_name: str,
) -> None:
    """Check that an (entity, key) pair is a signature the given event may carry in
    a member key room. See `add_hashes_and_signatures_from_signers`."""

    def refuse(why: str) -> MemberKeySigningRefusedError:
        return MemberKeySigningRefusedError(signature_name, why)

    content = event_dict.get("content")
    # (A `Mapping`: the content of an event copied from another room, as a
    # room upgrade does, may be a frozen dict.)
    if not isinstance(content, collections.abc.Mapping):
        raise refuse("the event has no content")

    if signature_name == MemberKeys.SIGNATURE_ENTITY:
        if signing_key.version != MemberKeys.KEY_VERSION:
            raise refuse("member keys sign with key ID %s" % (MemberKeys.KEY_ID,))
        if (
            event_dict.get("sender")
            != MemberKey.from_signing_key(signing_key).to_string()
        ):
            raise refuse("the signing key is not the event's sender")
        return

    if is_member_key(signature_name):
        if signing_key.version != MemberKeys.KEY_VERSION:
            raise refuse("member keys sign with key ID %s" % (MemberKeys.KEY_ID,))
        if MemberKey.from_signing_key(signing_key).to_string() != signature_name:
            raise refuse("the signing key is not that member key")
        if (
            event_dict.get("type") != EventTypes.Member
            or content.get(EventContentFields.MEMBERSHIP) != Membership.JOIN
            or content.get(EventContentFields.AUTHORISING_USER) != signature_name
        ):
            raise refuse(
                "only the authorising member key of a restricted join signs under "
                "its own name"
            )
        return

    # The co-signature on linkage information. The entity is the constant
    # `state_key`, naming the member the claim is about; the claim itself is
    # `content.domain`, which must be us, since this server signs for nobody else.
    if signature_name != MemberKeys.LINKAGE_SIGNATURE_ENTITY:
        raise refuse(
            "not a signature this event may carry (expected %s)"
            % (MemberKeys.LINKAGE_SIGNATURE_ENTITY,)
        )
    if event_dict.get("type") != EventTypes.Member:
        raise refuse("servers only co-sign m.room.member events")
    if content.get(EventContentFields.MSC4430_DOMAIN) != server_name:
        raise refuse("the event's content.domain is not this server")


def add_hashes_and_signatures(
    room_version: RoomVersion,
    event_dict: JsonDict,
    signature_name: str,
    signing_key: SigningKey,
) -> None:
    """Add content hash and sign the event

    Args:
        room_version: the version of the room this event is in

        event_dict: The event to add hashes to and sign
        signature_name: The name of the entity signing the event
            (typically the server's hostname).
        signing_key: The key to sign with
    """

    name, digest = compute_content_hash(event_dict, hash_algorithm=hashlib.sha256)

    event_dict.setdefault("hashes", {})[name] = encode_base64(digest)

    event_dict["signatures"] = compute_event_signature(
        room_version, event_dict, signature_name=signature_name, signing_key=signing_key
    )


def resign_event(
    ev: EventBase,
    server_name: str,
    signing_key: SigningKey,
    time_now: int | None = None,
) -> JsonDict:
    """Re-sign the provided event with the given signing key. Any existing signatures on the event
    for this server_name are removed.

    If there has been no signature for this event by this server_name, the event is still re-signed.
    If there have been signatures on this event by this server_name, the event is not re-checked for
    validity. As such, only events that have valid signatures should be passed into this function
    e.g. from the event_json table in the database.
    """
    event_dict = ev.get_pdu_json(time_now=time_now)
    event_dict["signatures"].pop(
        server_name, None
    )  # remove existing signatures for this server_name
    event_dict["signatures"].update(
        compute_event_signature(
            ev.room_version,
            event_dict,
            server_name,
            signing_key,
        )
    )
    return event_dict


def event_needs_resigning(
    ev: EventBase, server_name: str, verify_key: VerifyKey
) -> bool:
    """Check if this event needs re-signing.

    This returns True if all of the following are True:
     - the event `sender` domain matches the `server_name` provided.
     - the event has not been already signed with this `verify_key`.
    """
    sender = UserID.from_string(ev.sender)
    if sender.domain != server_name:
        return False
    want_key_id = verify_key.alg + ":" + verify_key.version
    signed_with_current_key_id = ev.signatures.get_signature(server_name, want_key_id)
    if signed_with_current_key_id:
        return False

    return True
