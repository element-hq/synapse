#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
# Copyright 2014 - 2016 OpenMarket Ltd
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

import logging
import typing
from collections import ChainMap
from typing import (
    Any,
    Iterable,
    Mapping,
    MutableMapping,
    Optional,
    Protocol,
    Union,
    cast,
)

from canonicaljson import encode_canonical_json
from signedjson.key import decode_verify_key_bytes
from signedjson.sign import SignatureVerifyException, verify_signed_json
from unpaddedbase64 import decode_base64

from synapse.api.constants import (
    CREATOR_POWER_LEVEL,
    MAX_PDU_SIZE,
    EventContentFields,
    EventTypes,
    Membership,
)
from synapse.api.errors import (
    AuthError,
    EventSizeError,
)
from synapse.api.room_versions import (
    KNOWN_ROOM_VERSIONS,
    EventFormatVersions,
    RoomVersion,
)
from synapse.events import is_creator
from synapse.events.py_protocol import supports_msc4242_state_dag
from synapse.state import CREATE_KEY
from synapse.storage.databases.main.events_worker import EventRedactBehaviour
from synapse.synapse_rust import event_auth as _rust_event_auth
from synapse.types import (
    JsonMapping,
    MutableStateMap,
    StateKey,
    StateMap,
    StrCollection,
    UserID,
    get_domain_from_id,
)

if typing.TYPE_CHECKING:
    # conditional imports to avoid import cycle
    from synapse.events import EventBase
    from synapse.events.builder import EventBuilder

logger = logging.getLogger(__name__)


class _EventSourceStore(Protocol):
    async def get_events(
        self,
        event_ids: StrCollection,
        redact_behaviour: EventRedactBehaviour,
        get_prev_content: bool = False,
        allow_rejected: bool = False,
    ) -> dict[str, "EventBase"]: ...


def validate_event_for_room_version(event: "EventBase") -> None:
    """Ensure that the event complies with the limits, and has the right signatures

    NB: does not *validate* the signatures - it assumes that any signatures present
    have already been checked.

    NB: it does not check that the event satisfies the auth rules (that is done in
    check_auth_rules_for_event) - these tests are independent of the rest of the state
    in the room.

    NB: This is used to check events that have been received over federation. As such,
    it can only enforce the checks specified in the relevant room version, to avoid
    a split-brain situation where some servers accept such events, and others reject
    them. See also EventValidator, which contains extra checks which are applied only to
    locally-generated events.

    Args:
        event: the event to be checked

    Raises:
        SynapseError if there is a problem with the event
    """
    _check_size_limits(event)

    if not hasattr(event, "room_id"):
        raise AuthError(500, "Event has no room_id: %s" % event)

    # check that the event has the correct signatures
    sender_domain = get_domain_from_id(event.sender)

    is_invite_via_3pid = (
        event.type == EventTypes.Member
        and event.membership == Membership.INVITE
        and "third_party_invite" in event.content
    )

    # Check the sender's domain has signed the event
    if sender_domain not in event.signatures:
        # We allow invites via 3pid to have a sender from a different
        # HS, as the sender must match the sender of the original
        # 3pid invite. This is checked further down with the
        # other dedicated membership checks.
        if not is_invite_via_3pid:
            raise AuthError(403, "Event not signed by sender's server")

    if event.format_version in (EventFormatVersions.ROOM_V1_V2,):
        # Only older room versions have event IDs to check.
        event_id_domain = get_domain_from_id(event.event_id)

        # Check the origin domain has signed the event
        if event_id_domain not in event.signatures:
            raise AuthError(403, "Event not signed by sending server")

    is_invite_via_allow_rule = (
        event.room_version.restricted_join_rule
        and event.type == EventTypes.Member
        and event.membership == Membership.JOIN
        and EventContentFields.AUTHORISING_USER in event.content
    )
    if is_invite_via_allow_rule:
        authoriser_domain = get_domain_from_id(
            event.content[EventContentFields.AUTHORISING_USER]
        )
        if authoriser_domain not in event.signatures:
            raise AuthError(403, "Event not signed by authorising server")


async def check_state_independent_auth_rules(
    store: _EventSourceStore,
    event: "EventBase",
    batched_auth_events: Mapping[str, "EventBase"] | None = None,
) -> None:
    """Check that an event complies with auth rules that are independent of room state

    Runs through the first few auth rules, which are independent of room state. (Which
    means that we only need to them once for each received event)

    Args:
        store: the datastore; used to fetch the auth events for validation
        event: the event being checked.
        batched_auth_events: if the event being authed is part of a batch, any events
            from the same batch that may be necessary to auth the current event

    Raises:
        AuthError if the checks fail
    """
    # Implementation of https://spec.matrix.org/v1.2/rooms/v9/#authorization-rules

    # 1. If type is m.room.create:
    if event.type == EventTypes.Create:
        _check_create(event)

        # 1.5 Otherwise, allow
        return

    # State DAGs 2. Considering the event's prev_state_events:
    if supports_msc4242_state_dag(event):
        prev_state_events_ids = set(event.prev_state_events)
        # Fetch all of the `prev_state_events`
        prev_state_events = {}
        # Try to load the `prev_state_events` from `batched_auth_events` initially as
        # that can save us a database hit.
        if batched_auth_events is not None:
            prev_state_events = {
                event_id: value
                for event_id in prev_state_events_ids
                if (value := batched_auth_events.get(event_id)) is not None
            }
        # Fetch the rest of the `prev_state_events`
        missing_prev_state_events_ids = prev_state_events_ids - set(
            prev_state_events.keys()
        )
        fetched_prev_state_events = await store.get_events(
            missing_prev_state_events_ids,
            redact_behaviour=EventRedactBehaviour.as_is,
            allow_rejected=True,
        )
        prev_state_events.update(fetched_prev_state_events)
        if len(prev_state_events) != len(prev_state_events_ids):
            # we should have all the `prev_state_events` by now, so if we do not, that suggests
            # a Synapse programming error
            known_prev_state_event_ids = set(prev_state_events)
            raise AssertionError(
                f"Event {event.event_id} has unknown prev_state_events "
                + f"({len(prev_state_events)}/{len(prev_state_events_ids)} known)"
                + f"{prev_state_events_ids - known_prev_state_event_ids} missing "
                + f"out of {prev_state_events_ids}"
            )
        for prev_state_event in prev_state_events.values():
            # 2.1 If there are entries which do not belong in the same room, reject.
            if prev_state_event.room_id != event.room_id:
                raise AuthError(
                    403,
                    "During auth for event %s in room %s, found event %s in prev_state_events "
                    "which belongs to a different room %s"
                    % (
                        event.event_id,
                        event.room_id,
                        prev_state_event.event_id,
                        prev_state_event.room_id,
                    ),
                )
            # 2.2 If there are entries which do not have a state_key, reject.
            if not prev_state_event.is_state():
                raise AuthError(
                    403,
                    f"During auth for event {event.event_id} in room {event.room_id}, event has a "
                    + f"prev_state_event which is not state: {prev_state_event.event_id}",
                )
            # 2.3 If there are entries which were themselves rejected under the checks performed on
            # receipt of a PDU, reject.
            if prev_state_event.rejected_reason is not None:
                raise AuthError(
                    403,
                    f"During auth for event {event.event_id} in room {event.room_id}, event has a "
                    + f"prev_state_event which is rejected ({prev_state_event.rejected_reason}): "
                    + f"{prev_state_event.event_id}",
                )

    # 2. Reject if event has auth_events that: ...
    auth_events: ChainMap[str, EventBase] = ChainMap()
    if batched_auth_events:
        # batched_auth_events can become very large. To avoid repeatedly copying it, which
        # would significantly impact performance, we use a ChainMap.
        # batched_auth_events must be cast to MutableMapping because .new_child() requires
        # this type. This casting is safe as the mapping is never mutated.
        auth_events = auth_events.new_child(
            cast(MutableMapping[str, "EventBase"], batched_auth_events)
        )
        needed_auth_event_ids = [
            event_id
            for event_id in event.auth_event_ids()
            if event_id not in batched_auth_events
        ]
        if needed_auth_event_ids:
            auth_events = auth_events.new_child(
                await store.get_events(
                    needed_auth_event_ids,
                    redact_behaviour=EventRedactBehaviour.as_is,
                    allow_rejected=True,
                )
            )
    else:
        auth_events = auth_events.new_child(
            await store.get_events(
                event.auth_event_ids(),
                redact_behaviour=EventRedactBehaviour.as_is,
                allow_rejected=True,
            )
        )

    room_id = event.room_id
    auth_dict: MutableStateMap[str] = {}
    expected_auth_types = auth_types_for_event(event.room_version, event)
    for auth_event_id in event.auth_event_ids():
        auth_event = auth_events.get(auth_event_id)

        # we should have all the auth events by now, so if we do not, that suggests
        # a synapse programming error
        if auth_event is None:
            raise RuntimeError(
                f"Event {event.event_id} has unknown auth event {auth_event_id}"
            )

        # We need to ensure that the auth events are actually for the same room, to
        # stop people from using powers they've been granted in other rooms for
        # example.
        if auth_event.room_id != room_id:
            raise AuthError(
                403,
                "During auth for event %s in room %s, found event %s in the state "
                "which is in room %s"
                % (event.event_id, room_id, auth_event_id, auth_event.room_id),
            )

        k = (auth_event.type, auth_event.state_key)

        # 2.1 ... have duplicate entries for a given type and state_key pair
        if k in auth_dict:
            raise AuthError(
                403,
                f"Event {event.event_id} has duplicate auth_events for {k}: {auth_dict[k]} and {auth_event_id}",
            )

        # 2.2 ... have entries whose type and state_key don’t match those specified by
        #   the auth events selection algorithm described in the server
        #   specification.
        if k not in expected_auth_types:
            raise AuthError(
                403,
                f"Event {event.event_id} has unexpected auth_event for {k}: {auth_event_id}",
            )

        # 2.3 ... If there are entries which were themselves rejected under the checks performed on receipt
        # of a PDU, reject.
        if auth_event.rejected_reason:
            raise AuthError(
                403,
                "During auth for event %s: found rejected event %s in the state"
                % (event.event_id, auth_event.event_id),
            )

        auth_dict[k] = auth_event_id

    # 2.4. If event does not have a m.room.create in its auth_events, reject.
    creation_event = auth_dict.get((EventTypes.Create, ""), None)
    if not creation_event:
        raise AuthError(403, "No create event in auth events")


def check_state_dependent_auth_rules(
    event: "EventBase",
    auth_events: Iterable["EventBase"],
) -> None:
    """Check that an event complies with auth rules that depend on room state

    Runs through the parts of the auth rules that check an event against bits of room
    state. The rules are implemented in Rust, in `synapse_rust.event_auth`.

    Note:

     - it's fine for use in state resolution, when we have already decided whether to
       accept the event or not, and are now trying to decide whether it should make it
       into the room state

     - when we're doing the initial event auth, it is only suitable in combination with
       a bunch of other tests (including, but not limited to, check_state_independent_auth_rules).

    Args:
        event: the event being checked.
        auth_events: the room state to check the events against.

    Raises:
        AuthError if the checks fail
        SynapseError if the event or its auth events are malformed (e.g. invalid
            power levels)
    """
    _rust_event_auth.check_state_dependent_auth_rules(event, auth_events)


def _check_size_limits(event: "EventBase") -> None:
    """
    Checks the size limits in a PDU.

    The entire size limit of the PDU is checked first.
    Then the size of fields is checked, first in codepoints and then in bytes.

    The codepoint size limits are only for Synapse compatibility.

    Raises:
        EventSizeError:
            when a size limit has been violated.

            unpersistable=True if Synapse never would have accepted the event and
                the PDU must NOT be persisted.

            unpersistable=False if a prior version of Synapse would have accepted the
                event and so the PDU must be persisted as rejected to avoid
                breaking the room.
    """

    # Whole PDU check
    if len(encode_canonical_json(event.get_pdu_json())) > MAX_PDU_SIZE:
        raise EventSizeError("event too large", unpersistable=True)

    # Codepoint size check: Synapse always enforced these limits, so apply
    # them strictly.
    if len(event.sender) > 255:
        raise EventSizeError("'sender' too large", unpersistable=True)
    if len(event.room_id) > 255:
        raise EventSizeError("'room_id' too large", unpersistable=True)
    if event.is_state() and len(event.state_key) > 255:
        raise EventSizeError("'state_key' too large", unpersistable=True)
    if len(event.type) > 255:
        raise EventSizeError("'type' too large", unpersistable=True)
    if len(event.event_id) > 255:
        raise EventSizeError("'event_id' too large", unpersistable=True)

    strict_byte_limits = event.room_version.strict_event_byte_limits_room_versions

    # Byte size check: if these fail, then be lenient to avoid breaking rooms.
    if len(event.sender.encode("utf-8")) > 255:
        raise EventSizeError("'sender' too large", unpersistable=strict_byte_limits)
    if len(event.room_id.encode("utf-8")) > 255:
        raise EventSizeError("'room_id' too large", unpersistable=strict_byte_limits)
    if event.is_state() and len(event.state_key.encode("utf-8")) > 255:
        raise EventSizeError("'state_key' too large", unpersistable=strict_byte_limits)
    if len(event.type.encode("utf-8")) > 255:
        raise EventSizeError("'type' too large", unpersistable=strict_byte_limits)
    if len(event.event_id.encode("utf-8")) > 255:
        raise EventSizeError("'event_id' too large", unpersistable=strict_byte_limits)


def _check_create(event: "EventBase") -> None:
    """Implementation of the auth rules for m.room.create events

    Args:
        event: The `m.room.create` event to be checked

    Raises:
        AuthError if the event does not pass the auth rules
    """
    assert event.type == EventTypes.Create

    #  1.1 If it has any previous events, reject.
    if event.prev_event_ids():
        raise AuthError(403, "Create event has prev events")

    # State DAGs 1.2 If it has any prev_state_events, reject.
    if supports_msc4242_state_dag(event):
        if len(event.prev_state_events) > 0:
            raise AuthError(403, "Create event has prev state events")

    if event.room_version.msc4291_room_ids_as_hashes:
        # 1.2 If the create event has a room_id, reject
        if "room_id" in event:
            raise AuthError(403, "Create event has a room_id")
    else:
        # 1.2 If the domain of the room_id does not match the domain of the sender,
        # reject.
        if not event.room_version.msc4291_room_ids_as_hashes:
            sender_domain = get_domain_from_id(event.sender)
            room_id_domain = get_domain_from_id(event.room_id)
            if room_id_domain != sender_domain:
                raise AuthError(
                    403, "Creation event's room_id domain does not match sender's"
                )

    # 1.3 If content.room_version is present and is not a recognised version, reject
    room_version_prop = event.content.get("room_version", "1")
    if room_version_prop not in KNOWN_ROOM_VERSIONS:
        raise AuthError(
            403,
            "room appears to have unsupported version %s" % (room_version_prop,),
        )

    # 1.4 If content has no creator field, reject if the room version requires it.
    if (
        not event.room_version.implicit_room_creator
        and EventContentFields.ROOM_CREATOR not in event.content
    ):
        raise AuthError(403, "Create event lacks a 'creator' property")

    # 1.5 If the additional_creators field is present and is not an array of strings where each
    # string is a valid user ID, reject.
    if (
        event.room_version.msc4289_creator_power_enabled
        and EventContentFields.ADDITIONAL_CREATORS in event.content
    ):
        check_valid_additional_creators(
            event.content[EventContentFields.ADDITIONAL_CREATORS]
        )


def get_send_level(
    etype: str, state_key: str | None, power_levels_event: Optional["EventBase"]
) -> int:
    """Get the power level required to send an event of a given type

    The federation spec [1] refers to this as "Required Power Level".

    https://matrix.org/docs/spec/server_server/unstable.html#definitions

    Args:
        etype: type of event
        state_key: state_key of state event, or None if it is not
            a state event.
        power_levels_event: power levels event
            in force at this point in the room
    Returns:
        power level required to send this event.
    """

    power_levels_content: JsonMapping
    if power_levels_event:
        power_levels_content = power_levels_event.content
    else:
        power_levels_content = {}

    # see if we have a custom level for this event type
    send_level = power_levels_content.get("events", {}).get(etype)

    # otherwise, fall back to the state_default/events_default.
    if send_level is None:
        if state_key is not None:
            send_level = power_levels_content.get("state_default", 50)
        else:
            send_level = power_levels_content.get("events_default", 0)

    return int(send_level)


def check_redaction(
    room_version_obj: RoomVersion,
    event: "EventBase",
    auth_events: StateMap["EventBase"],
) -> bool:
    """Check whether the event sender is allowed to redact the target event.

    Returns:
        True if the sender is allowed to redact the target event if the
        target event was created by them.
        False if the sender is allowed to redact the target event with no
        further checks.

    Raises:
        AuthError if the event sender is definitely not allowed to redact
        the target event.
    """
    user_level = get_user_power_level(event.sender, auth_events)

    redact_level = get_named_level(auth_events, "redact", 50)

    if user_level >= redact_level:
        return False

    if room_version_obj.event_format == EventFormatVersions.ROOM_V1_V2:
        redacter_domain = get_domain_from_id(event.event_id)
        if not isinstance(event.redacts, str):
            return False
        redactee_domain = get_domain_from_id(event.redacts)
        if redacter_domain == redactee_domain:
            return True
    else:
        event.internal_metadata.recheck_redaction = True
        return True

    raise AuthError(403, "You don't have permission to redact events")


def get_power_level_event(auth_events: StateMap["EventBase"]) -> Optional["EventBase"]:
    return auth_events.get((EventTypes.PowerLevels, ""))


def get_user_power_level(user_id: str, auth_events: StateMap["EventBase"]) -> int:
    """Get a user's power level

    Args:
        user_id: user's id to look up in power_levels
        auth_events:
            state in force at this point in the room (or rather, a subset of
            it including at least the create event, and possibly a power levels event).

    Returns:
        the user's power level in this room.
    """
    create_event = auth_events.get(CREATE_KEY)
    assert create_event is not None, (
        "A create event in the auth events chain is required to calculate user power level correctly,"
        " but was not found. This indicates a bug"
    )
    if create_event.room_version.msc4289_creator_power_enabled:
        if is_creator(create_event, user_id):
            return CREATOR_POWER_LEVEL
    power_level_event = get_power_level_event(auth_events)
    if power_level_event:
        level = power_level_event.content.get("users", {}).get(user_id)
        if level is None:
            level = power_level_event.content.get("users_default", 0)

        if level is None:
            return 0
        else:
            return int(level)
    else:
        # if there is no power levels event, the creator gets 100 and everyone
        # else gets 0.
        if create_event.room_version.implicit_room_creator:
            creator = create_event.sender
        else:
            creator = create_event.content[EventContentFields.ROOM_CREATOR]
        if creator == user_id:
            return 100
        return 0


def get_named_level(auth_events: StateMap["EventBase"], name: str, default: int) -> int:
    power_level_event = get_power_level_event(auth_events)

    if not power_level_event:
        return default

    level = power_level_event.content.get(name, None)
    if level is not None:
        return int(level)
    else:
        return default


def _verify_third_party_invite_signatures(
    signed: JsonMapping, invite_content: JsonMapping
) -> bool:
    """Check the signatures on the `signed` block of a third-party invite.

    Called from the Rust auth rules once the structural checks on the invite
    have passed. Checks that `signed` has an ed25519 signature from one of the
    public keys in the `m.room.third_party_invite` event's content.

    Args:
        signed: the `third_party_invite.signed` block of the invite event.
        invite_content: the content of the `m.room.third_party_invite` event.

    Returns:
        True if `signed` was signed with one of the invite's public keys.
    """
    for public_key_object in _get_public_keys_from_content(invite_content):
        public_key = public_key_object["public_key"]
        try:
            for server, signature_block in signed["signatures"].items():
                for key_name in signature_block.keys():
                    if not key_name.startswith("ed25519:"):
                        continue
                    verify_key = decode_verify_key_bytes(
                        key_name, decode_base64(public_key)
                    )
                    # verify_signed_json incorrectly states it wants a dict, it
                    # just needs a mapping.
                    verify_signed_json(signed, server, verify_key)  # type: ignore[arg-type]

                    # We got the public key from the invite, so we know that the
                    # correct server signed the signed bundle.
                    # The caller is responsible for checking that the signing
                    # server has not revoked that public key.
                    return True
        except (KeyError, SignatureVerifyException):
            continue
    return False


def get_public_keys(invite_event: "EventBase") -> list[dict[str, Any]]:
    return _get_public_keys_from_content(invite_event.content)


def _get_public_keys_from_content(content: JsonMapping) -> list[dict[str, Any]]:
    public_keys = []
    if "public_key" in content:
        o = {"public_key": content["public_key"]}
        if "key_validity_url" in content:
            o["key_validity_url"] = content["key_validity_url"]
        public_keys.append(o)
    public_keys.extend(content.get("public_keys", []))
    return public_keys


def auth_types_for_event(
    room_version: RoomVersion, event: Union["EventBase", "EventBuilder"]
) -> set[StateKey]:
    """Given an event, return a list of (state event type, state key) that may be
    needed to auth the event. The returned list may be a superset of what
    would actually be required depending on the full state of the room.

    Used to limit the number of events to fetch from the database to
    actually auth the event.
    """
    return _rust_event_auth.auth_types_for_event(room_version, event)


def check_valid_additional_creators(additional_creators: Any) -> None:
    """Check if the additional_creators provided is valid according to MSC4289.

    The additional_creators can be supplied from an m.room.create event or from an /upgrade request.

    Raises:
        AuthError if the additional_creators is invalid for some reason.
    """
    if type(additional_creators) is not list:
        raise AuthError(400, "additional_creators must be an array")
    for entry in additional_creators:
        if type(entry) is not str:
            raise AuthError(400, "entry in additional_creators is not a string")
        if not UserID.is_valid(entry):
            raise AuthError(400, "entry in additional_creators is not a valid user ID")
        # UserID.is_valid doesn't actually validate everything, so check the rest manually.
        if len(entry) > 255 or len(entry.encode("utf-8")) > 255:
            raise AuthError(
                400,
                "entry in additional_creators too long",
            )
