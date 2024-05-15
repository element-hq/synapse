#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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
import itertools
import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union
from typing_extensions import Annotated

from synapse._pydantic_compat import HAS_PYDANTIC_V2

if TYPE_CHECKING or HAS_PYDANTIC_V2:
    from pydantic.v1 import (
        StrictBool,
        StrictInt,
        StrictStr,
        constr,
        validator,
    )
else:
    from pydantic import (
        StrictBool,
        StrictInt,
        StrictStr,
        constr,
        validator,
    )

from synapse.api.constants import AccountDataTypes, EduTypes, Membership, PresenceState
from synapse.api.errors import Codes, StoreError, SynapseError
from synapse.api.filtering import FilterCollection
from synapse.api.presence import UserPresenceState
from synapse.events.utils import (
    SerializeEventConfig,
    format_event_for_client_v2_without_room_id,
    format_event_raw,
)
from synapse.handlers.presence import format_user_presence_state
from synapse.handlers.sync import (
    ArchivedSyncResult,
    InvitedSyncResult,
    JoinedSyncResult,
    KnockedSyncResult,
    SyncConfig,
    SyncResult,
    SyncVersion,
)
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_and_validate_json_object_from_request,
    parse_boolean,
    parse_integer,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.logging.opentracing import trace_with_opname
from synapse.rest.models import RequestBodyModel
from synapse.types import JsonDict, Requester, StreamToken
from synapse.util import json_decoder

from ._base import client_patterns, set_timeline_upper_limit

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class SyncRestServlet(RestServlet):
    """

    GET parameters::
        timeout(int): How long to wait for new events in milliseconds.
        since(batch_token): Batch token when asking for incremental deltas.
        set_presence(str): What state the device presence should be set to.
            default is "online".
        filter(filter_id): A filter to apply to the events returned.

    Response JSON::
        {
          "next_batch": // batch token for the next /sync
          "presence": // presence data for the user.
          "rooms": {
            "join": { // Joined rooms being updated.
              "${room_id}": { // Id of the room being updated
                "event_map": // Map of EventID -> event JSON.
                "timeline": { // The recent events in the room if gap is "true"
                  "limited": // Was the per-room event limit exceeded?
                             // otherwise the next events in the room.
                  "events": [] // list of EventIDs in the "event_map".
                  "prev_batch": // back token for getting previous events.
                }
                "state": {"events": []} // list of EventIDs updating the
                                        // current state to be what it should
                                        // be at the end of the batch.
                "ephemeral": {"events": []} // list of event objects
              }
            },
            "invite": {}, // Invited rooms being updated.
            "leave": {} // Archived rooms being updated.
          }
        }
    """

    PATTERNS = client_patterns("/sync$")
    ALLOWED_PRESENCE = {"online", "offline", "unavailable"}
    CATEGORY = "Sync requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.sync_handler = hs.get_sync_handler()
        self.clock = hs.get_clock()
        self.filtering = hs.get_filtering()
        self.presence_handler = hs.get_presence_handler()
        self._server_notices_sender = hs.get_server_notices_sender()
        self._event_serializer = hs.get_event_client_serializer()
        self._msc2654_enabled = hs.config.experimental.msc2654_enabled
        self._msc3773_enabled = hs.config.experimental.msc3773_enabled

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        # This will always be set by the time Twisted calls us.
        assert request.args is not None

        if b"from" in request.args:
            # /events used to use 'from', but /sync uses 'since'.
            # Lets be helpful and whine if we see a 'from'.
            raise SynapseError(
                400, "'from' is not a valid query parameter. Did you mean 'since'?"
            )

        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        user = requester.user
        device_id = requester.device_id

        timeout = parse_integer(request, "timeout", default=0)
        since = parse_string(request, "since")
        set_presence = parse_string(
            request,
            "set_presence",
            default="online",
            allowed_values=self.ALLOWED_PRESENCE,
        )
        filter_id = parse_string(request, "filter")
        full_state = parse_boolean(request, "full_state", default=False)

        logger.debug(
            "/sync: user=%r, timeout=%r, since=%r, "
            "set_presence=%r, filter_id=%r, device_id=%r",
            user,
            timeout,
            since,
            set_presence,
            filter_id,
            device_id,
        )

        # Stream position of the last ignored users account data event for this user,
        # if we're initial syncing.
        # We include this in the request key to invalidate an initial sync
        # in the response cache once the set of ignored users has changed.
        # (We filter out ignored users from timeline events, so our sync response
        # is invalid once the set of ignored users changes.)
        last_ignore_accdata_streampos: Optional[int] = None
        if not since:
            # No `since`, so this is an initial sync.
            last_ignore_accdata_streampos = await self.store.get_latest_stream_id_for_global_account_data_by_type_for_user(
                user.to_string(), AccountDataTypes.IGNORED_USER_LIST
            )

        request_key = (
            user,
            timeout,
            since,
            filter_id,
            full_state,
            device_id,
            last_ignore_accdata_streampos,
        )

        if filter_id is None:
            filter_collection = self.filtering.DEFAULT_FILTER_COLLECTION
        elif filter_id.startswith("{"):
            try:
                filter_object = json_decoder.decode(filter_id)
            except Exception:
                raise SynapseError(400, "Invalid filter JSON", errcode=Codes.NOT_JSON)
            self.filtering.check_valid_filter(filter_object)
            set_timeline_upper_limit(
                filter_object, self.hs.config.server.filter_timeline_limit
            )
            filter_collection = FilterCollection(self.hs, filter_object)
        else:
            try:
                filter_collection = await self.filtering.get_user_filter(
                    user, filter_id
                )
            except StoreError as err:
                if err.code != 404:
                    raise
                # fix up the description and errcode to be more useful
                raise SynapseError(400, "No such filter", errcode=Codes.INVALID_PARAM)

        sync_config = SyncConfig(
            user=user,
            filter_collection=filter_collection,
            is_guest=requester.is_guest,
            device_id=device_id,
        )

        since_token = None
        if since is not None:
            since_token = await StreamToken.from_string(self.store, since)

        # send any outstanding server notices to the user.
        await self._server_notices_sender.on_user_syncing(user.to_string())

        affect_presence = set_presence != PresenceState.OFFLINE

        context = await self.presence_handler.user_syncing(
            user.to_string(),
            requester.device_id,
            affect_presence=affect_presence,
            presence_state=set_presence,
        )
        with context:
            sync_result = await self.sync_handler.wait_for_sync_for_user(
                requester,
                sync_config,
                SyncVersion.SYNC_V2,
                request_key,
                since_token=since_token,
                timeout=timeout,
                full_state=full_state,
            )

        # the client may have disconnected by now; don't bother to serialize the
        # response if so.
        if request._disconnected:
            logger.info("Client has disconnected; not serializing response.")
            return 200, {}

        time_now = self.clock.time_msec()
        # We know that the the requester has an access token since appservices
        # cannot use sync.
        response_content = await self.encode_response(
            time_now, sync_result, requester, filter_collection
        )

        logger.debug("Event formatting complete")
        return 200, response_content

    @trace_with_opname("sync.encode_response")
    async def encode_response(
        self,
        time_now: int,
        sync_result: SyncResult,
        requester: Requester,
        filter: FilterCollection,
    ) -> JsonDict:
        logger.debug("Formatting events in sync response")
        if filter.event_format == "client":
            event_formatter = format_event_for_client_v2_without_room_id
        elif filter.event_format == "federation":
            event_formatter = format_event_raw
        else:
            raise Exception("Unknown event format %s" % (filter.event_format,))

        serialize_options = SerializeEventConfig(
            event_format=event_formatter,
            requester=requester,
            only_event_fields=filter.event_fields,
        )
        stripped_serialize_options = SerializeEventConfig(
            event_format=event_formatter,
            requester=requester,
            include_stripped_room_state=True,
        )

        joined = await self.encode_joined(
            sync_result.joined, time_now, serialize_options
        )

        invited = await self.encode_invited(
            sync_result.invited, time_now, stripped_serialize_options
        )

        knocked = await self.encode_knocked(
            sync_result.knocked, time_now, stripped_serialize_options
        )

        archived = await self.encode_archived(
            sync_result.archived, time_now, serialize_options
        )

        logger.debug("building sync response dict")

        response: JsonDict = defaultdict(dict)
        response["next_batch"] = await sync_result.next_batch.to_string(self.store)

        if sync_result.account_data:
            response["account_data"] = {"events": sync_result.account_data}
        if sync_result.presence:
            response["presence"] = SyncRestServlet.encode_presence(
                sync_result.presence, time_now
            )

        if sync_result.to_device:
            response["to_device"] = {"events": sync_result.to_device}

        if sync_result.device_lists.changed:
            response["device_lists"]["changed"] = list(sync_result.device_lists.changed)
        if sync_result.device_lists.left:
            response["device_lists"]["left"] = list(sync_result.device_lists.left)

        # We always include this because https://github.com/vector-im/element-android/issues/3725
        # The spec isn't terribly clear on when this can be omitted and how a client would tell
        # the difference between "no keys present" and "nothing changed" in terms of whole field
        # absent / individual key type entry absent
        # Corresponding synapse issue: https://github.com/matrix-org/synapse/issues/10456
        response["device_one_time_keys_count"] = sync_result.device_one_time_keys_count

        # https://github.com/matrix-org/matrix-doc/blob/54255851f642f84a4f1aaf7bc063eebe3d76752b/proposals/2732-olm-fallback-keys.md
        # states that this field should always be included, as long as the server supports the feature.
        response["org.matrix.msc2732.device_unused_fallback_key_types"] = (
            sync_result.device_unused_fallback_key_types
        )
        response["device_unused_fallback_key_types"] = (
            sync_result.device_unused_fallback_key_types
        )

        if joined:
            response["rooms"][Membership.JOIN] = joined
        if invited:
            response["rooms"][Membership.INVITE] = invited
        if knocked:
            response["rooms"][Membership.KNOCK] = knocked
        if archived:
            response["rooms"][Membership.LEAVE] = archived

        return response

    @staticmethod
    def encode_presence(events: List[UserPresenceState], time_now: int) -> JsonDict:
        return {
            "events": [
                {
                    "type": EduTypes.PRESENCE,
                    "sender": event.user_id,
                    "content": format_user_presence_state(
                        event, time_now, include_user_id=False
                    ),
                }
                for event in events
            ]
        }

    @trace_with_opname("sync.encode_joined")
    async def encode_joined(
        self,
        rooms: List[JoinedSyncResult],
        time_now: int,
        serialize_options: SerializeEventConfig,
    ) -> JsonDict:
        """
        Encode the joined rooms in a sync result

        Args:
            rooms: list of sync results for rooms this user is joined to
            time_now: current time - used as a baseline for age calculations
            serialize_options: Event serializer options
        Returns:
            The joined rooms list, in our response format
        """
        joined = {}
        for room in rooms:
            joined[room.room_id] = await self.encode_room(
                room, time_now, joined=True, serialize_options=serialize_options
            )

        return joined

    @trace_with_opname("sync.encode_invited")
    async def encode_invited(
        self,
        rooms: List[InvitedSyncResult],
        time_now: int,
        serialize_options: SerializeEventConfig,
    ) -> JsonDict:
        """
        Encode the invited rooms in a sync result

        Args:
            rooms: list of sync results for rooms this user is invited to
            time_now: current time - used as a baseline for age calculations
            serialize_options: Event serializer options

        Returns:
            The invited rooms list, in our response format
        """
        invited = {}
        for room in rooms:
            invite = await self._event_serializer.serialize_event(
                room.invite, time_now, config=serialize_options
            )
            unsigned = dict(invite.get("unsigned", {}))
            invite["unsigned"] = unsigned
            invited_state = list(unsigned.pop("invite_room_state", []))
            invited_state.append(invite)
            invited[room.room_id] = {"invite_state": {"events": invited_state}}

        return invited

    @trace_with_opname("sync.encode_knocked")
    async def encode_knocked(
        self,
        rooms: List[KnockedSyncResult],
        time_now: int,
        serialize_options: SerializeEventConfig,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Encode the rooms we've knocked on in a sync result.

        Args:
            rooms: list of sync results for rooms this user is knocking on
            time_now: current time - used as a baseline for age calculations
            serialize_options: Event serializer options

        Returns:
            The list of rooms the user has knocked on, in our response format.
        """
        knocked = {}
        for room in rooms:
            knock = await self._event_serializer.serialize_event(
                room.knock, time_now, config=serialize_options
            )

            # Extract the `unsigned` key from the knock event.
            # This is where we (cheekily) store the knock state events
            unsigned = knock.setdefault("unsigned", {})

            # Duplicate the dictionary in order to avoid modifying the original
            unsigned = dict(unsigned)

            # Extract the stripped room state from the unsigned dict
            # This is for clients to get a little bit of information about
            # the room they've knocked on, without revealing any sensitive information
            knocked_state = list(unsigned.pop("knock_room_state", []))

            # Append the actual knock membership event itself as well. This provides
            # the client with:
            #
            # * A knock state event that they can use for easier internal tracking
            # * The rough timestamp of when the knock occurred contained within the event
            knocked_state.append(knock)

            # Build the `knock_state` dictionary, which will contain the state of the
            # room that the client has knocked on
            knocked[room.room_id] = {"knock_state": {"events": knocked_state}}

        return knocked

    @trace_with_opname("sync.encode_archived")
    async def encode_archived(
        self,
        rooms: List[ArchivedSyncResult],
        time_now: int,
        serialize_options: SerializeEventConfig,
    ) -> JsonDict:
        """
        Encode the archived rooms in a sync result

        Args:
            rooms: list of sync results for rooms this user is joined to
            time_now: current time - used as a baseline for age calculations
            serialize_options: Event serializer options
        Returns:
            The archived rooms list, in our response format
        """
        joined = {}
        for room in rooms:
            joined[room.room_id] = await self.encode_room(
                room, time_now, joined=False, serialize_options=serialize_options
            )

        return joined

    async def encode_room(
        self,
        room: Union[JoinedSyncResult, ArchivedSyncResult],
        time_now: int,
        joined: bool,
        serialize_options: SerializeEventConfig,
    ) -> JsonDict:
        """
        Args:
            room: sync result for a single room
            time_now: current time - used as a baseline for age calculations
            token_id: ID of the user's auth token - used for namespacing
                of transaction IDs
            joined: True if the user is joined to this room - will mean
                we handle ephemeral events
            only_fields: Optional. The list of event fields to include.
            event_formatter: function to convert from federation format
                to client format
        Returns:
            The room, encoded in our response format
        """
        state_dict = room.state
        timeline_events = room.timeline.events

        state_events = state_dict.values()

        for event in itertools.chain(state_events, timeline_events):
            # We've had bug reports that events were coming down under the
            # wrong room.
            if event.room_id != room.room_id:
                logger.warning(
                    "Event %r is under room %r instead of %r",
                    event.event_id,
                    room.room_id,
                    event.room_id,
                )

        serialized_state = await self._event_serializer.serialize_events(
            state_events, time_now, config=serialize_options
        )
        serialized_timeline = await self._event_serializer.serialize_events(
            timeline_events,
            time_now,
            config=serialize_options,
            bundle_aggregations=room.timeline.bundled_aggregations,
        )

        account_data = room.account_data

        result: JsonDict = {
            "timeline": {
                "events": serialized_timeline,
                "prev_batch": await room.timeline.prev_batch.to_string(self.store),
                "limited": room.timeline.limited,
            },
            "state": {"events": serialized_state},
            "account_data": {"events": account_data},
        }

        if joined:
            assert isinstance(room, JoinedSyncResult)
            ephemeral_events = room.ephemeral
            result["ephemeral"] = {"events": ephemeral_events}
            result["unread_notifications"] = room.unread_notifications
            if room.unread_thread_notifications:
                result["unread_thread_notifications"] = room.unread_thread_notifications
                if self._msc3773_enabled:
                    result["org.matrix.msc3773.unread_thread_notifications"] = (
                        room.unread_thread_notifications
                    )
            result["summary"] = room.summary
            if self._msc2654_enabled:
                result["org.matrix.msc2654.unread_count"] = room.unread_count

        return result


class SlidingSyncE2eeRestServlet(RestServlet):
    """
    API endpoint for MSC3575 Sliding Sync `/sync/e2ee`. This is being introduced as part
    of Sliding Sync but doesn't have any sliding window component. It's just a way to
    get E2EE events without having to sit through a big initial sync (`/sync` v2). And
    we can avoid encryption events being backed up by the main sync response.

    Having To-Device messages split out to this sync endpoint also helps when clients
    need to have 2 or more sync streams open at a time, e.g a push notification process
    and a main process. This can cause the two processes to race to fetch the To-Device
    events, resulting in the need for complex synchronisation rules to ensure the token
    is correctly and atomically exchanged between processes.

    GET parameters::
        timeout(int): How long to wait for new events in milliseconds.
        since(batch_token): Batch token when asking for incremental deltas.

    Response JSON::
        {
            "next_batch": // batch token for the next /sync
            "to_device": {
                // list of to-device events
                "events": [
                    {
                        "content: { "algorithm": "m.olm.v1.curve25519-aes-sha2", "ciphertext": { ... }, "org.matrix.msgid": "abcd", "session_id": "abcd" },
                        "type": "m.room.encrypted",
                        "sender": "@alice:example.com",
                    }
                    // ...
                ]
            },
            "device_lists": {
                "changed": ["@alice:example.com"],
                "left": ["@bob:example.com"]
            },
            "device_one_time_keys_count": {
                "signed_curve25519": 50
            },
            "device_unused_fallback_key_types": [
                "signed_curve25519"
            ]
        }
    """

    PATTERNS = (re.compile("^/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee$"),)

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.filtering = hs.get_filtering()
        self.sync_handler = hs.get_sync_handler()

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        user = requester.user
        device_id = requester.device_id

        timeout = parse_integer(request, "timeout", default=0)
        since = parse_string(request, "since")

        sync_config = SyncConfig(
            user=user,
            # Filtering doesn't apply to this endpoint so just use a default to fill in
            # the SyncConfig
            filter_collection=self.filtering.DEFAULT_FILTER_COLLECTION,
            is_guest=requester.is_guest,
            device_id=device_id,
        )

        since_token = None
        if since is not None:
            since_token = await StreamToken.from_string(self.store, since)

        # Request cache key
        request_key = (
            SyncVersion.E2EE_SYNC,
            user,
            timeout,
            since,
        )

        # Gather data for the response
        sync_result = await self.sync_handler.wait_for_sync_for_user(
            requester,
            sync_config,
            SyncVersion.E2EE_SYNC,
            request_key,
            since_token=since_token,
            timeout=timeout,
            full_state=False,
        )

        # The client may have disconnected by now; don't bother to serialize the
        # response if so.
        if request._disconnected:
            logger.info("Client has disconnected; not serializing response.")
            return 200, {}

        response: JsonDict = defaultdict(dict)
        response["next_batch"] = await sync_result.next_batch.to_string(self.store)

        if sync_result.to_device:
            response["to_device"] = {"events": sync_result.to_device}

        if sync_result.device_lists.changed:
            response["device_lists"]["changed"] = list(sync_result.device_lists.changed)
        if sync_result.device_lists.left:
            response["device_lists"]["left"] = list(sync_result.device_lists.left)

        # We always include this because https://github.com/vector-im/element-android/issues/3725
        # The spec isn't terribly clear on when this can be omitted and how a client would tell
        # the difference between "no keys present" and "nothing changed" in terms of whole field
        # absent / individual key type entry absent
        # Corresponding synapse issue: https://github.com/matrix-org/synapse/issues/10456
        response["device_one_time_keys_count"] = sync_result.device_one_time_keys_count

        # https://github.com/matrix-org/matrix-doc/blob/54255851f642f84a4f1aaf7bc063eebe3d76752b/proposals/2732-olm-fallback-keys.md
        # states that this field should always be included, as long as the server supports the feature.
        response["device_unused_fallback_key_types"] = (
            sync_result.device_unused_fallback_key_types
        )

        return 200, response


class SlidingSyncBody(RequestBodyModel):
    """
    Attributes:
        lists: Sliding window API. A map of list key to list information
            (:class:`SlidingSyncList`). Max lists: 100. The list keys should be
            arbitrary strings which the client is using to refer to the list. Keep this
            small as it needs to be sent a lot. Max length: 64 bytes.
        room_subscriptions: Room subscription API. A map of room ID to room subscription
            information. Used to subscribe to a specific room. Sometimes clients know
            exactly which room they want to get information about e.g by following a
            permalink or by refreshing a webapp currently viewing a specific room. The
            sliding window API alone is insufficient for this use case because there's
            no way to say "please track this room explicitly".
        extensions: TODO
    """

    class CommonRoomParameters(RequestBodyModel):
        """
        Common parameters shared between the sliding window and room subscription APIs.

        Attributes:
            required_state: Required state for each room returned. An array of event
                type and state key tuples. Elements in this array are ORd together to
                produce the final set of state events to return. One unique exception is
                when you request all state events via `["*", "*"]`. When used, all state
                events are returned by default, and additional entries FILTER OUT the
                returned set of state events. These additional entries cannot use `*`
                themselves. For example, `["*", "*"], ["m.room.member",
                "@alice:example.com"]` will *exclude* every `m.room.member` event
                *except* for `@alice:example.com`, and include every other state event.
                In addition, `["*", "*"], ["m.space.child", "*"]` is an error, the
                `m.space.child` filter is not required as it would have been returned
                anyway.
            timeline_limit: The maximum number of timeline events to return per response.
            include_old_rooms: Determines if `predecessor` rooms are included in the
                `rooms` response. The user MUST be joined to old rooms for them to show up
                in the response.
        """

        class IncludeOldRooms(RequestBodyModel):
            timeline_limit: StrictInt
            required_state: List[Tuple[StrictStr, StrictStr]]

        required_state: List[Tuple[StrictStr, StrictStr]]
        timeline_limit: StrictInt
        include_old_rooms: Optional[IncludeOldRooms]

    class SlidingSyncList(CommonRoomParameters):
        """
        Attributes:
            ranges: Sliding window ranges. If this field is missing, no sliding window
                is used and all rooms are returned in this list. Integers are
                *inclusive*.
            sort: How the list should be sorted on the server. The first value is
                applied first, then tiebreaks are performed with each subsequent sort
                listed.

                    FIXME: Furthermore, it's not currently defined how servers should behave
                    if they encounter a filter or sort operation they do not recognise. If
                    the server rejects the request with an HTTP 400 then that will break
                    backwards compatibility with new clients vs old servers. However, the
                    client would be otherwise unaware that only some of the sort/filter
                    operations have taken effect. We may need to include a "warnings"
                    section to indicate which sort/filter operations are unrecognised,
                    allowing for some form of graceful degradation of service.
                    -- https://github.com/matrix-org/matrix-spec-proposals/blob/kegan/sync-v3/proposals/3575-sync.md#filter-and-sort-extensions

            slow_get_all_rooms: Just get all rooms (for clients that don't want to deal with
                sliding windows). When true, the `ranges` and `sort` fields are ignored.
            required_state: Required state for each room returned. An array of event
                type and state key tuples. Elements in this array are ORd together to
                produce the final set of state events to return.

                One unique exception is when you request all state events via `["*",
                "*"]`. When used, all state events are returned by default, and
                additional entries FILTER OUT the returned set of state events. These
                additional entries cannot use `*` themselves. For example, `["*", "*"],
                ["m.room.member", "@alice:example.com"]` will *exclude* every
                `m.room.member` event *except* for `@alice:example.com`, and include
                every other state event. In addition, `["*", "*"], ["m.space.child",
                "*"]` is an error, the `m.space.child` filter is not required as it
                would have been returned anyway.

                Room members can be lazily-loaded by using the special `$LAZY` state key
                (`["m.room.member", "$LAZY"]`). Typically, when you view a room, you
                want to retrieve all state events except for m.room.member events which
                you want to lazily load. To get this behaviour, clients can send the
                following::

                    {
                        "required_state": [
                            // activate lazy loading
                            ["m.room.member", "$LAZY"],
                            // request all state events _except_ for m.room.member
                            events which are lazily loaded
                            ["*", "*"]
                        ]
                    }

            timeline_limit: The maximum number of timeline events to return per response.
            include_old_rooms: Determines if `predecessor` rooms are included in the
                `rooms` response. The user MUST be joined to old rooms for them to show up
                in the response.
            include_heroes: Return a stripped variant of membership events (containing
                `user_id` and optionally `avatar_url` and `displayname`) for the users used
                to calculate the room name.
            filters: Filters to apply to the list before sorting.
            bump_event_types: Allowlist of event types which should be considered recent activity
                when sorting `by_recency`. By omitting event types from this field,
                clients can ensure that uninteresting events (e.g. a profile rename) do
                not cause a room to jump to the top of its list(s). Empty or omitted
                `bump_event_types` have no effect—all events in a room will be
                considered recent activity.
        """

        class Filters(RequestBodyModel):
            is_dm: Optional[StrictBool]
            spaces: Optional[List[StrictStr]]
            is_encrypted: Optional[StrictBool]
            is_invite: Optional[StrictBool]
            room_types: Optional[List[Union[StrictStr, None]]]
            not_room_types: Optional[List[StrictStr]]
            room_name_like: Optional[StrictStr]
            tags: Optional[List[StrictStr]]
            not_tags: Optional[List[StrictStr]]

        ranges: Optional[List[Tuple[StrictInt, StrictInt]]]
        sort: Optional[List[StrictStr]]
        slow_get_all_rooms: Optional[StrictBool] = False
        include_heroes: Optional[StrictBool] = False
        filters: Optional[Filters]
        bump_event_types: Optional[List[StrictStr]]

    class RoomSubscription(CommonRoomParameters):
        pass

    class Extension(RequestBodyModel):
        enabled: Optional[StrictBool] = False
        lists: Optional[List[StrictStr]]
        rooms: Optional[List[StrictStr]]

    lists: Optional[Dict[constr(max_length=64, strict=True), SlidingSyncList]]
    room_subscriptions: Optional[Dict[StrictStr, RoomSubscription]]
    extensions: Optional[Dict[StrictStr, Extension]]

    @validator("lists")
    def lists_length_check(cls, v):
        assert len(v) <= 100, f"Max lists: 100 but saw {len(v)}"
        return v


class SlidingSyncRestServlet(RestServlet):
    """
    API endpoint for MSC3575 Sliding Sync `/sync`. TODO

    GET parameters::
        timeout(int): How long to wait for new events in milliseconds.
        since(batch_token): Batch token when asking for incremental deltas.

    Response JSON::
        {
            TODO
        }
    """

    PATTERNS = (re.compile("^/_matrix/client/unstable/org.matrix.msc3575/sync$"),)

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.filtering = hs.get_filtering()
        self.sliding_sync_handler = hs.get_sliding_sync_handler()

    # TODO: Update this to `on_GET` once we figure out how we want to handle params
    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        user = requester.user
        device_id = requester.device_id

        timeout = parse_integer(request, "timeout", default=0)
        # Position in the stream
        since_token = parse_string(request, "pos")

        # TODO: We currently don't know whether we're going to use sticky params or
        # maybe some filters like sync v2  where they are built up once and referenced
        # by filter ID. For now, we will just prototype with always passing everything
        # in.
        body = parse_and_validate_json_object_from_request(request, SlidingSyncBody)

        sliding_sync_results = await wait_for_sync_for_user()

        logger.info("Sliding sync request: %r", body)

        return 200, {"foo": "bar"}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    SyncRestServlet(hs).register(http_server)

    if hs.config.experimental.msc3575_enabled:
        SlidingSyncRestServlet(hs).register(http_server)
        SlidingSyncE2eeRestServlet(hs).register(http_server)
