#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Collection,
    Mapping,
    MutableMapping,
)

import attr
from canonicaljson import encode_canonical_json

from synapse.api.constants import (
    CANONICALJSON_MAX_INT,
    CANONICALJSON_MIN_INT,
    MAX_PDU_SIZE,
    EventTypes,
)
from synapse.api.errors import Codes, SynapseError
from synapse.logging.opentracing import SynapseTags, set_tag, trace
from synapse.synapse_rust.events import (
    EventFormat,
    SerializeEventConfig,
    Unsigned,
    format_event_for_client_v1,
    format_event_for_client_v2,
    format_event_for_client_v2_without_room_id,
    format_event_raw,
    redact_event,
    serialize_events,
)
from synapse.synapse_rust.types import Requester
from synapse.types import JsonDict

from . import EventBase, StrippedStateEvent

# These are imported only to re-export them (callers import them from this
# module); listing them in __all__ stops the unused-import lint flagging them
# and re-exports them for `import *`.
#
# The `format_event_*` functions are a backwards compatibility hack: they have
# never been part of the module API and modules shouldn't be pulling them in,
# but some in the wild import them from here anyway. They may be removed in
# the future; nothing in Synapse itself should use them.
__all__ = [
    "EventFormat",
    "SerializeEventConfig",
    "format_event_for_client_v1",
    "format_event_for_client_v2",
    "format_event_for_client_v2_without_room_id",
    "format_event_raw",
]

if TYPE_CHECKING:
    from synapse.handlers.relations import BundledAggregations
    from synapse.server import HomeServer


# Module API callback that allows adding fields to the unsigned section of
# events that are sent to clients.
ADD_EXTRA_FIELDS_TO_UNSIGNED_CLIENT_EVENT_CALLBACK = Callable[
    [EventBase], Awaitable[JsonDict]
]


def prune_event(event: EventBase) -> EventBase:
    """Returns a pruned version of the given event, which removes all keys we
    don't know about or think could potentially be dodgy.

    This is used when we "redact" an event. We want to remove all fields that
    the user has specified, but we do want to keep necessary information like
    type, state_key etc.
    """
    return redact_event(event)


def clone_event(event: EventBase) -> EventBase:
    """Take a copy of the event.

    Most fields of the event are immutable, however fields such as `unsigned`,
    `signatures` and `internal_metadata` are mutable. Cloning the event allows
    us to edit such fields without affecting the original event.
    """

    return event.deep_copy()


@attr.s(slots=True, frozen=True, auto_attribs=True)
class FilteredEvent:
    """An event annotated with per-user data for client serialization.

    Produced by filter_and_transform_events_for_client. Carries the user's
    membership at the time of the event so serialization can inject it into
    unsigned.membership (MSC4115) without cloning the underlying event.
    """

    event: "EventBase"
    """The event to be serialized."""

    membership: str | None
    """The user whose requesting the event's membership at the time of the
    event was sent.

    This is None if we didn't compute the membership. In Synapse this happens a)
    when returning state events to state endpoints, or b) when the event is
    returned to an admin.

    According to the spec we don't have to include the membership for any events
    if we don't want to, especially if its expensive to compute. In practice
    clients really only care about events in the room timeline so that in
    encrypted room they can determine if they should be able to decrypt the
    event or not.
    """

    @classmethod
    def state(cls, event: "EventBase") -> "FilteredEvent":
        """Wrap a state event with no per-user membership annotation.

        The event must be a state event (i.e. have a state_key).
        """
        assert event.is_state(), (
            f"FilteredEvent.state() called with non-state event {event.event_id}"
        )
        return cls(event=event, membership=None)

    @classmethod
    def admin_override(cls, event: "EventBase") -> "FilteredEvent":
        """Wrap an event that bypasses visibility filtering due to admin privileges."""
        return cls(event=event, membership=None)


class EventClientSerializer:
    """Serializes events that are to be sent to clients.

    This is used for bundling extra information with any events to be sent to
    clients.
    """

    def __init__(self, hs: "HomeServer") -> None:
        self._store = hs.get_datastores().main
        self._auth = hs.get_auth()
        self._config = hs.config
        self._clock = hs.get_clock()
        self._add_extra_fields_to_unsigned_client_event_callbacks: list[
            ADD_EXTRA_FIELDS_TO_UNSIGNED_CLIENT_EVENT_CALLBACK
        ] = []

    async def create_config(
        self,
        *,
        as_client_event: bool = True,
        event_format: EventFormat = EventFormat.ClientV1,
        requester: Requester | None = None,
        event_field_allowlist: list[str] | None = None,
        include_stripped_room_state: bool = False,
        include_admin_metadata: bool | None = None,
    ) -> SerializeEventConfig:
        """
        Create a new SerializeEventConfig for the given parameters.

        Helper method that sets the `include_admin_metadata` field based on
        whether the requester is a server admin if it is not explicitly
        provided. Also sets the `msc4354_enabled` field based on the homeserver
        config.

        Args:
            as_client_event: Whether to serialize the events as client events.
            event_format: The format to serialize events in. requester: The user
            requesting the events, if any. Used to determine
                whether to include admin-only metadata in the serialized events.
            event_field_allowlist: A list of event fields to include in the
                serialized events.
            include_stripped_room_state: Whether to include stripped room state
                in the serialized events.
            include_admin_metadata: Whether to include admin-only metadata in
                the serialized events. If None, this will be determined based on
                whether the requester is a server admin.
        Returns:
            A SerializeEventConfig instance.
        """

        # If include_admin_metadata is None, determine whether to include
        # admin-only metadata based on the requester.
        if include_admin_metadata is None:
            # Check if the requester is a server admin.
            if requester is not None and await self._auth.is_server_admin(requester):
                include_admin_metadata = True
            else:
                include_admin_metadata = False

        return SerializeEventConfig(
            as_client_event=as_client_event,
            event_format=event_format,
            requester=requester,
            event_field_allowlist=event_field_allowlist,
            include_stripped_room_state=include_stripped_room_state,
            include_admin_metadata=include_admin_metadata,
            msc4354_enabled=self._config.experimental.msc4354_enabled,
        )

    async def serialize_event(
        self,
        event: JsonDict | FilteredEvent,
        time_now: int,
        *,
        config: SerializeEventConfig | None = None,
        bundle_aggregations: dict[str, "BundledAggregations"] | None = None,
        redaction_map: Mapping[str, "EventBase"] | None = None,
    ) -> JsonDict:
        """Serializes a single event.

        Args:
            event: The event being serialized.
            time_now: The current time in milliseconds
            config: Event serialization config
            bundle_aggregations: A map from event_id to the aggregations to be bundled
               into the event.
            redaction_map: Optional pre-fetched map from redaction event_id to event,
               used to avoid per-event DB lookups when serializing many events.

        Returns:
            The serialized event
        """
        # FIXME: Ideally we would only call `serialize_event` with
        # `FilteredEvent`s. Currently though some of the old `/events` code paths
        # pass through presence events and the like.
        if not isinstance(event, FilteredEvent):
            return event

        if config is None:
            # Generate default config if none was provided.
            config = await self.create_config()

        # Perform all the async DB/IO work up front, then run the synchronous
        # serialization core.
        redaction_map, unsigned_additions = await self._prepare_serialization(
            [event], bundle_aggregations, redaction_map
        )

        return serialize_events(
            [(event.event, event.membership)],
            time_now,
            config,
            bundle_aggregations=bundle_aggregations,
            redaction_map=redaction_map,
            unsigned_additions=unsigned_additions,
        )[0]

    async def _prepare_serialization(
        self,
        events: Collection[FilteredEvent],
        bundle_aggregations: dict[str, "BundledAggregations"] | None,
        redaction_map: Mapping[str, "EventBase"] | None = None,
    ) -> tuple[dict[str, "EventBase"], dict[str, JsonDict]]:
        """Perform all the async DB/IO work needed to serialize `events`.

        Does two things:
        1. Fetches any redaction events needed to serialize `events` (and any
           bundled events) and returns a map from redaction event_id to event.
        2. Runs the module callbacks for each event to build up the additional
           `unsigned` fields they contribute.

        Args:
            events: The events that will be serialized.
            bundle_aggregations: A map from event_id to the aggregations to be
                bundled into the event. Used to discover the sub-events (edits
                and thread latest events) that will also be serialized.
            redaction_map: An optional caller-supplied map from redaction
                event_id to the redaction event. Any redactions already present
                here are not re-fetched, and these entries take precedence over
                anything we fetch ourselves.

        Returns:
            A tuple of:
              - a map from redaction event_id to the redaction event,
              - a map from event_id to the extra `unsigned` fields contributed
                by the registered module callbacks.
        """

        # First we collect all events that get included in the serialization of
        # `events`, including the events themselves and any bundled events (edits
        # and thread latest events, which are themselves serialized).
        collected = {e.event.event_id: e.event for e in events}
        if bundle_aggregations is not None:
            for aggregation in bundle_aggregations.values():
                if aggregation.replace:
                    collected[aggregation.replace.event_id] = aggregation.replace
                if aggregation.thread:
                    latest_event = aggregation.thread.latest_event
                    collected[latest_event.event_id] = latest_event

        # Next, check the redaction status of all events, and fetch the
        # redactions if needed.
        redaction_map = redaction_map or {}

        redaction_ids_to_fetch = {
            redacted_by
            for collected_event in collected.values()
            if (redacted_by := collected_event.internal_metadata.redacted_by)
            is not None
            and redacted_by not in redaction_map
        }

        if redaction_ids_to_fetch:
            fetched_redaction_map = await self._store.get_events(redaction_ids_to_fetch)
        else:
            fetched_redaction_map = {}

        # Ensure the returned redaction map includes any caller-supplied
        # redactions
        fetched_redaction_map.update(redaction_map)

        # Run the module callbacks for each event (once per event_id, since
        # `collected` is already de-duplicated) to build up the additional
        # `unsigned` fields they contribute.
        unsigned_additions: dict[str, JsonDict] = {}
        if self._add_extra_fields_to_unsigned_client_event_callbacks:
            for collected_event in collected.values():
                new_unsigned: JsonDict = {}
                for (
                    callback
                ) in self._add_extra_fields_to_unsigned_client_event_callbacks:
                    new_unsigned.update(await callback(collected_event))

                if new_unsigned:
                    unsigned_additions[collected_event.event_id] = new_unsigned

        return fetched_redaction_map, unsigned_additions

    @trace
    async def serialize_events(
        self,
        events: Collection[JsonDict | FilteredEvent],
        time_now: int,
        *,
        config: SerializeEventConfig | None = None,
        bundle_aggregations: dict[str, "BundledAggregations"] | None = None,
    ) -> list[JsonDict]:
        """Serializes multiple events.

        Args:
            event
            time_now: The current time in milliseconds
            config: Event serialization config
            bundle_aggregations: Whether to include the bundled aggregations for this
                event. Only applies to non-state events. (State events never include
                bundled aggregations.)

        Returns:
            The list of serialized events
        """
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "events.length",
            str(len(events)),
        )

        if config is None:
            # Generate default config if none was provided.
            config = await self.create_config()

        filtered_events = [e for e in events if isinstance(e, FilteredEvent)]

        # Perform all the async DB/IO work up front, then run the synchronous
        # serialization core for the whole batch in one go.
        redaction_map, unsigned_additions = await self._prepare_serialization(
            filtered_events, bundle_aggregations
        )

        serialized = iter(
            serialize_events(
                [(e.event, e.membership) for e in filtered_events],
                time_now,
                config,
                bundle_aggregations=bundle_aggregations,
                redaction_map=redaction_map,
                unsigned_additions=unsigned_additions,
            )
        )

        # Stitch the serialized events back in, passing through anything that
        # wasn't a FilteredEvent (e.g. presence events) unchanged.
        #
        # FIXME: Ideally we would only call `serialize_events` with
        # `FilteredEvent`s. Currently though some of the old `/events` code paths
        # pass through presence events and the like.
        return [
            event if not isinstance(event, FilteredEvent) else next(serialized)
            for event in events
        ]

    def register_add_extra_fields_to_unsigned_client_event_callback(
        self, callback: ADD_EXTRA_FIELDS_TO_UNSIGNED_CLIENT_EVENT_CALLBACK
    ) -> None:
        """Register a callback that returns additions to the unsigned section of
        serialized events.
        """
        self._add_extra_fields_to_unsigned_client_event_callbacks.append(callback)


_PowerLevel = str | int
PowerLevelsContent = Mapping[str, _PowerLevel | Mapping[str, _PowerLevel]]


def copy_and_fixup_power_levels_contents(
    old_power_levels: PowerLevelsContent,
) -> dict[str, int | dict[str, int]]:
    """Copy the content of a power_levels event, unfreezing immutabledicts along the way.

    We accept as input power level values which are strings, provided they represent an
    integer, e.g. `"`100"` instead of 100. Such strings are converted to integers
    in the returned dictionary (hence "fixup" in the function name).

    Note that future room versions will outlaw such stringy power levels (see
    https://github.com/matrix-org/matrix-spec/issues/853).

    Raises:
        TypeError if the input does not look like a valid power levels event content
    """
    if not isinstance(old_power_levels, collections.abc.Mapping):
        raise TypeError("Not a valid power-levels content: %r" % (old_power_levels,))

    power_levels: dict[str, int | dict[str, int]] = {}

    for k, v in old_power_levels.items():
        if isinstance(v, collections.abc.Mapping):
            h: dict[str, int] = {}
            power_levels[k] = h
            for k1, v1 in v.items():
                _copy_power_level_value_as_integer(v1, h, k1)

        else:
            _copy_power_level_value_as_integer(v, power_levels, k)

    return power_levels


def _copy_power_level_value_as_integer(
    old_value: object,
    power_levels: MutableMapping[str, Any],
    key: str,
) -> None:
    """Set `power_levels[key]` to the integer represented by `old_value`.

    :raises TypeError: if `old_value` is neither an integer nor a base-10 string
        representation of an integer.
    """
    if type(old_value) is int:  # noqa: E721
        power_levels[key] = old_value
        return

    if isinstance(old_value, str):
        try:
            parsed_value = int(old_value, base=10)
        except ValueError:
            # Fall through to the final TypeError.
            pass
        else:
            power_levels[key] = parsed_value
            return

    raise TypeError(f"Invalid power_levels value for {key}: {old_value}")


def validate_canonicaljson(value: Any) -> None:
    """
    Ensure that the JSON object is valid according to the rules of canonical JSON.

    See the appendix section 3.1: Canonical JSON.

    This rejects JSON that has:
    * An integer outside the range of [-2 ^ 53 + 1, 2 ^ 53 - 1]
    * Floats
    * NaN, Infinity, -Infinity
    """
    if type(value) is int:  # noqa: E721
        if value < CANONICALJSON_MIN_INT or CANONICALJSON_MAX_INT < value:
            raise SynapseError(400, "JSON integer out of range", Codes.BAD_JSON)

    elif isinstance(value, float):
        # Note that Infinity, -Infinity, and NaN are also considered floats.
        raise SynapseError(400, "Bad JSON value: float", Codes.BAD_JSON)

    elif isinstance(value, collections.abc.Mapping):
        for v in value.values():
            validate_canonicaljson(v)

    elif isinstance(value, (list, tuple)):
        for i in value:
            validate_canonicaljson(i)

    elif not isinstance(value, (bool, str)) and value is not None:
        # Other potential JSON values (bool, None, str) are safe.
        raise SynapseError(400, "Unknown JSON value", Codes.BAD_JSON)


def maybe_upsert_event_field(
    event: EventBase, container: Unsigned, key: str, value: object
) -> bool:
    """Upsert an event field, but only if this doesn't make the event too large.

    Returns true iff the upsert took place.
    """
    if key in container:
        old_value: object = container[key]
        container[key] = value
        # NB: here and below, we assume that passing a non-None `time_now` argument to
        # get_pdu_json doesn't increase the size of the encoded result.
        upsert_okay = len(encode_canonical_json(event.get_pdu_json())) <= MAX_PDU_SIZE
        if not upsert_okay:
            container[key] = old_value
    else:
        container[key] = value
        upsert_okay = len(encode_canonical_json(event.get_pdu_json())) <= MAX_PDU_SIZE
        if not upsert_okay:
            del container[key]

    return upsert_okay


def strip_event(event: EventBase) -> JsonDict:
    """
    Used for "stripped state" events which provide a simplified view of the state of a
    room intended to help a potential joiner identify the room (relevant when the user
    is invited or knocked).

    Stripped state events can only have the `sender`, `type`, `state_key` and `content`
    properties present.
    """
    # MSC4311: Ensure the create event is available on invites and knocks.
    # TODO: Implement the rest of MSC4311
    if (
        event.room_version.msc4291_room_ids_as_hashes
        and event.type == EventTypes.Create
        and event.get_state_key() == ""
    ):
        return event.get_pdu_json()

    return {
        "type": event.type,
        "state_key": event.state_key,
        "content": dict(event.content),
        "sender": event.sender,
    }


def parse_stripped_state_event(raw_stripped_event: Any) -> StrippedStateEvent | None:
    """
    Given a raw value from an event's `unsigned` field, attempt to parse it into a
    `StrippedStateEvent`.
    """
    if isinstance(raw_stripped_event, dict):
        # All of these fields are required
        type = raw_stripped_event.get("type")
        state_key = raw_stripped_event.get("state_key")
        sender = raw_stripped_event.get("sender")
        content = raw_stripped_event.get("content")
        if (
            isinstance(type, str)
            and isinstance(state_key, str)
            and isinstance(sender, str)
            and isinstance(content, dict)
        ):
            return StrippedStateEvent(
                type=type,
                state_key=state_key,
                sender=sender,
                content=content,
            )

    return None
