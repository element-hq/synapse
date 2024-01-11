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

import abc
import collections.abc
import os
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    overload,
)

import attr
from typing_extensions import Literal
from unpaddedbase64 import encode_base64

from synapse.api.constants import RelationTypes
from synapse.api.room_versions import EventFormatVersions, RoomVersion, RoomVersions
from synapse.synapse_rust.events import EventInternalMetadata
from synapse.types import JsonDict, StrCollection
from synapse.util.caches import intern_dict
from synapse.util.frozenutils import freeze
from synapse.util.stringutils import strtobool

if TYPE_CHECKING:
    from synapse.events.builder import EventBuilder

# Whether we should use frozen_dict in FrozenEvent. Using frozen_dicts prevents
# bugs where we accidentally share e.g. signature dicts. However, converting a
# dict to frozen_dicts is expensive.
#
# NOTE: This is overridden by the configuration by the Synapse worker apps, but
# for the sake of tests, it is set here while it cannot be configured on the
# homeserver object itself.

USE_FROZEN_DICTS = strtobool(os.environ.get("SYNAPSE_USE_FROZEN_DICTS", "0"))


T = TypeVar("T")


# DictProperty (and DefaultDictProperty) require the classes they're used with to
# have a _dict property to pull properties from.
#
# TODO _DictPropertyInstance should not include EventBuilder but due to
# https://github.com/python/mypy/issues/5570 it thinks the DictProperty and
# DefaultDictProperty get applied to EventBuilder when it is in a Union with
# EventBase. This is the least invasive hack to get mypy to comply.
#
# Note that DictProperty/DefaultDictProperty cannot actually be used with
# EventBuilder as it lacks a _dict property.
_DictPropertyInstance = Union["EventBase", "EventBuilder"]


class DictProperty(Generic[T]):
    """An object property which delegates to the `_dict` within its parent object."""

    __slots__ = ["key"]

    def __init__(self, key: str):
        self.key = key

    @overload
    def __get__(
        self,
        instance: Literal[None],
        owner: Optional[Type[_DictPropertyInstance]] = None,
    ) -> "DictProperty":
        ...

    @overload
    def __get__(
        self,
        instance: _DictPropertyInstance,
        owner: Optional[Type[_DictPropertyInstance]] = None,
    ) -> T:
        ...

    def __get__(
        self,
        instance: Optional[_DictPropertyInstance],
        owner: Optional[Type[_DictPropertyInstance]] = None,
    ) -> Union[T, "DictProperty"]:
        # if the property is accessed as a class property rather than an instance
        # property, return the property itself rather than the value
        if instance is None:
            return self
        try:
            assert isinstance(instance, EventBase)
            return instance._dict[self.key]
        except KeyError as e1:
            # We want this to look like a regular attribute error (mostly so that
            # hasattr() works correctly), so we convert the KeyError into an
            # AttributeError.
            #
            # To exclude the KeyError from the traceback, we explicitly
            # 'raise from e1.__context__' (which is better than 'raise from None',
            # because that would omit any *earlier* exceptions).
            #
            raise AttributeError(
                "'%s' has no '%s' property" % (type(instance), self.key)
            ) from e1.__context__

    def __set__(self, instance: _DictPropertyInstance, v: T) -> None:
        assert isinstance(instance, EventBase)
        instance._dict[self.key] = v

    def __delete__(self, instance: _DictPropertyInstance) -> None:
        assert isinstance(instance, EventBase)
        try:
            del instance._dict[self.key]
        except KeyError as e1:
            raise AttributeError(
                "'%s' has no '%s' property" % (type(instance), self.key)
            ) from e1.__context__


class DefaultDictProperty(DictProperty, Generic[T]):
    """An extension of DictProperty which provides a default if the property is
    not present in the parent's _dict.

    Note that this means that hasattr() on the property always returns True.
    """

    __slots__ = ["default"]

    def __init__(self, key: str, default: T):
        super().__init__(key)
        self.default = default

    @overload
    def __get__(
        self,
        instance: Literal[None],
        owner: Optional[Type[_DictPropertyInstance]] = None,
    ) -> "DefaultDictProperty":
        ...

    @overload
    def __get__(
        self,
        instance: _DictPropertyInstance,
        owner: Optional[Type[_DictPropertyInstance]] = None,
    ) -> T:
        ...

    def __get__(
        self,
        instance: Optional[_DictPropertyInstance],
        owner: Optional[Type[_DictPropertyInstance]] = None,
    ) -> Union[T, "DefaultDictProperty"]:
        if instance is None:
            return self
        assert isinstance(instance, EventBase)
        return instance._dict.get(self.key, self.default)


class EventBase(metaclass=abc.ABCMeta):
    @property
    @abc.abstractmethod
    def format_version(self) -> int:
        """The EventFormatVersion implemented by this event"""
        ...

    def __init__(
        self,
        event_dict: JsonDict,
        room_version: RoomVersion,
        signatures: Dict[str, Dict[str, str]],
        unsigned: JsonDict,
        internal_metadata_dict: JsonDict,
        rejected_reason: Optional[str],
    ):
        assert room_version.event_format == self.format_version

        self.room_version = room_version
        self.signatures = signatures
        self.unsigned = unsigned
        self.rejected_reason = rejected_reason

        self._dict = event_dict

        self.internal_metadata = EventInternalMetadata(internal_metadata_dict)

    depth: DictProperty[int] = DictProperty("depth")
    content: DictProperty[JsonDict] = DictProperty("content")
    hashes: DictProperty[Dict[str, str]] = DictProperty("hashes")
    origin: DictProperty[str] = DictProperty("origin")
    origin_server_ts: DictProperty[int] = DictProperty("origin_server_ts")
    room_id: DictProperty[str] = DictProperty("room_id")
    sender: DictProperty[str] = DictProperty("sender")
    # TODO state_key should be Optional[str]. This is generally asserted in Synapse
    # by calling is_state() first (which ensures it is not None), but it is hard (not possible?)
    # to properly annotate that calling is_state() asserts that state_key exists
    # and is non-None. It would be better to replace such direct references with
    # get_state_key() (and a check for None).
    state_key: DictProperty[str] = DictProperty("state_key")
    type: DictProperty[str] = DictProperty("type")
    user_id: DictProperty[str] = DictProperty("sender")

    @property
    def event_id(self) -> str:
        raise NotImplementedError()

    @property
    def membership(self) -> str:
        return self.content["membership"]

    @property
    def redacts(self) -> Optional[str]:
        """MSC2176 moved the redacts field into the content."""
        if self.room_version.updated_redaction_rules:
            return self.content.get("redacts")
        return self.get("redacts")

    def is_state(self) -> bool:
        return self.get_state_key() is not None

    def get_state_key(self) -> Optional[str]:
        """Get the state key of this event, or None if it's not a state event"""
        return self._dict.get("state_key")

    def get_dict(self) -> JsonDict:
        d = dict(self._dict)
        d.update({"signatures": self.signatures, "unsigned": dict(self.unsigned)})

        return d

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        return self._dict.get(key, default)

    def get_internal_metadata_dict(self) -> JsonDict:
        return self.internal_metadata.get_dict()

    def get_pdu_json(self, time_now: Optional[int] = None) -> JsonDict:
        pdu_json = self.get_dict()

        if time_now is not None and "age_ts" in pdu_json["unsigned"]:
            age = time_now - pdu_json["unsigned"]["age_ts"]
            pdu_json.setdefault("unsigned", {})["age"] = int(age)
            del pdu_json["unsigned"]["age_ts"]

        # This may be a frozen event
        pdu_json["unsigned"].pop("redacted_because", None)

        return pdu_json

    def get_templated_pdu_json(self) -> JsonDict:
        """
        Return a JSON object suitable for a templated event, as used in the
        make_{join,leave,knock} workflow.
        """
        # By using _dict directly we don't pull in signatures/unsigned.
        template_json = dict(self._dict)
        # The hashes (similar to the signature) need to be recalculated by the
        # joining/leaving/knocking server after (potentially) modifying the
        # event.
        template_json.pop("hashes")

        return template_json

    def __getitem__(self, field: str) -> Optional[Any]:
        return self._dict[field]

    def __contains__(self, field: str) -> bool:
        return field in self._dict

    def items(self) -> List[Tuple[str, Optional[Any]]]:
        return list(self._dict.items())

    def keys(self) -> Iterable[str]:
        return self._dict.keys()

    def prev_event_ids(self) -> List[str]:
        """Returns the list of prev event IDs. The order matches the order
        specified in the event, though there is no meaning to it.

        Returns:
            The list of event IDs of this event's prev_events
        """
        return [e for e, _ in self._dict["prev_events"]]

    def auth_event_ids(self) -> StrCollection:
        """Returns the list of auth event IDs. The order matches the order
        specified in the event, though there is no meaning to it.

        Returns:
            The list of event IDs of this event's auth_events
        """
        return [e for e, _ in self._dict["auth_events"]]

    def freeze(self) -> None:
        """'Freeze' the event dict, so it cannot be modified by accident"""

        # this will be a no-op if the event dict is already frozen.
        self._dict = freeze(self._dict)

    def __str__(self) -> str:
        return self.__repr__()

    def __repr__(self) -> str:
        rejection = f"REJECTED={self.rejected_reason}, " if self.rejected_reason else ""

        return (
            f"<{self.__class__.__name__} "
            f"{rejection}"
            f"event_id={self.event_id}, "
            f"type={self.get('type')}, "
            f"state_key={self.get('state_key')}, "
            f"outlier={self.internal_metadata.is_outlier()}"
            ">"
        )


class FrozenEvent(EventBase):
    format_version = EventFormatVersions.ROOM_V1_V2  # All events of this type are V1

    def __init__(
        self,
        event_dict: JsonDict,
        room_version: RoomVersion,
        internal_metadata_dict: Optional[JsonDict] = None,
        rejected_reason: Optional[str] = None,
    ):
        internal_metadata_dict = internal_metadata_dict or {}

        event_dict = dict(event_dict)

        # Signatures is a dict of dicts, and this is faster than doing a
        # copy.deepcopy
        signatures = {
            name: dict(sigs.items())
            for name, sigs in event_dict.pop("signatures", {}).items()
        }

        unsigned = dict(event_dict.pop("unsigned", {}))

        # We intern these strings because they turn up a lot (especially when
        # caching).
        event_dict = intern_dict(event_dict)

        if USE_FROZEN_DICTS:
            frozen_dict = freeze(event_dict)
        else:
            frozen_dict = event_dict

        self._event_id = event_dict["event_id"]

        super().__init__(
            frozen_dict,
            room_version=room_version,
            signatures=signatures,
            unsigned=unsigned,
            internal_metadata_dict=internal_metadata_dict,
            rejected_reason=rejected_reason,
        )

    @property
    def event_id(self) -> str:
        return self._event_id


class FrozenEventV2(EventBase):
    format_version = EventFormatVersions.ROOM_V3  # All events of this type are V2

    def __init__(
        self,
        event_dict: JsonDict,
        room_version: RoomVersion,
        internal_metadata_dict: Optional[JsonDict] = None,
        rejected_reason: Optional[str] = None,
    ):
        internal_metadata_dict = internal_metadata_dict or {}

        event_dict = dict(event_dict)

        # Signatures is a dict of dicts, and this is faster than doing a
        # copy.deepcopy
        signatures = {
            name: dict(sigs.items())
            for name, sigs in event_dict.pop("signatures", {}).items()
        }

        assert "event_id" not in event_dict

        unsigned = dict(event_dict.pop("unsigned", {}))

        # We intern these strings because they turn up a lot (especially when
        # caching).
        event_dict = intern_dict(event_dict)

        if USE_FROZEN_DICTS:
            frozen_dict = freeze(event_dict)
        else:
            frozen_dict = event_dict

        self._event_id: Optional[str] = None

        super().__init__(
            frozen_dict,
            room_version=room_version,
            signatures=signatures,
            unsigned=unsigned,
            internal_metadata_dict=internal_metadata_dict,
            rejected_reason=rejected_reason,
        )

    @property
    def event_id(self) -> str:
        # We have to import this here as otherwise we get an import loop which
        # is hard to break.
        from synapse.crypto.event_signing import compute_event_reference_hash

        if self._event_id:
            return self._event_id
        self._event_id = "$" + encode_base64(compute_event_reference_hash(self)[1])
        return self._event_id

    def prev_event_ids(self) -> List[str]:
        """Returns the list of prev event IDs. The order matches the order
        specified in the event, though there is no meaning to it.

        Returns:
            The list of event IDs of this event's prev_events
        """
        return self._dict["prev_events"]

    def auth_event_ids(self) -> StrCollection:
        """Returns the list of auth event IDs. The order matches the order
        specified in the event, though there is no meaning to it.

        Returns:
            The list of event IDs of this event's auth_events
        """
        return self._dict["auth_events"]


class FrozenEventV3(FrozenEventV2):
    """FrozenEventV3, which differs from FrozenEventV2 only in the event_id format"""

    format_version = EventFormatVersions.ROOM_V4_PLUS  # All events of this type are V3

    @property
    def event_id(self) -> str:
        # We have to import this here as otherwise we get an import loop which
        # is hard to break.
        from synapse.crypto.event_signing import compute_event_reference_hash

        if self._event_id:
            return self._event_id
        self._event_id = "$" + encode_base64(
            compute_event_reference_hash(self)[1], urlsafe=True
        )
        return self._event_id


def _event_type_from_format_version(
    format_version: int,
) -> Type[Union[FrozenEvent, FrozenEventV2, FrozenEventV3]]:
    """Returns the python type to use to construct an Event object for the
    given event format version.

    Args:
        format_version: The event format version

    Returns:
        A type that can be initialized as per the initializer of `FrozenEvent`
    """

    if format_version == EventFormatVersions.ROOM_V1_V2:
        return FrozenEvent
    elif format_version == EventFormatVersions.ROOM_V3:
        return FrozenEventV2
    elif format_version == EventFormatVersions.ROOM_V4_PLUS:
        return FrozenEventV3
    else:
        raise Exception("No event format %r" % (format_version,))


def make_event_from_dict(
    event_dict: JsonDict,
    room_version: RoomVersion = RoomVersions.V1,
    internal_metadata_dict: Optional[JsonDict] = None,
    rejected_reason: Optional[str] = None,
) -> EventBase:
    """Construct an EventBase from the given event dict"""
    event_type = _event_type_from_format_version(room_version.event_format)
    return event_type(
        event_dict, room_version, internal_metadata_dict or {}, rejected_reason
    )


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _EventRelation:
    # The target event of the relation.
    parent_id: str
    # The relation type.
    rel_type: str
    # The aggregation key. Will be None if the rel_type is not m.annotation or is
    # not a string.
    aggregation_key: Optional[str]


def relation_from_event(event: EventBase) -> Optional[_EventRelation]:
    """
    Attempt to parse relation information an event.

    Returns:
        The event relation information, if it is valid. None, otherwise.
    """
    relation = event.content.get("m.relates_to")
    if not relation or not isinstance(relation, collections.abc.Mapping):
        # No relation information.
        return None

    # Relations must have a type and parent event ID.
    rel_type = relation.get("rel_type")
    if not isinstance(rel_type, str):
        return None

    parent_id = relation.get("event_id")
    if not isinstance(parent_id, str):
        return None

    # Annotations have a key field.
    aggregation_key = None
    if rel_type == RelationTypes.ANNOTATION:
        aggregation_key = relation.get("key")
        if not isinstance(aggregation_key, str):
            aggregation_key = None

    return _EventRelation(parent_id, rel_type, aggregation_key)
