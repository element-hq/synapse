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
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Mapping

import attr

from synapse.api.constants import AccountDataTypes, EduTypes, Membership, PresenceState
from synapse.api.errors import Codes, StoreError, SynapseError
from synapse.api.filtering import FilterCollection
from synapse.api.presence import UserPresenceState
from synapse.api.ratelimiting import Ratelimiter
from synapse.events.utils import (
    SerializeEventConfig,
    format_event_for_client_v2_without_room_id,
    format_event_raw,
)
from synapse.handlers.presence import format_user_presence_state
from synapse.handlers.sliding_sync import SlidingSyncConfig, SlidingSyncResult
from synapse.handlers.sync import (
    ArchivedSyncResult,
    InvitedSyncResult,
    JoinedSyncResult,
    KnockedSyncResult,
    SyncConfig,
    SyncResult,
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
from synapse.logging.opentracing import log_kv, set_tag, trace_with_opname
from synapse.rest.admin.experimental_features import ExperimentalFeature
from synapse.types import JsonDict, Requester, SlidingSyncStreamToken, StreamToken
from synapse.types.rest.client import SlidingSyncBody
from synapse.util.caches.lrucache import LruCache
from synapse.util.json import json_decoder

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
        self.server_name = hs.hostname
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

        self._json_filter_cache: LruCache[str, bool] = LruCache(
            max_size=1000,
            clock=self.clock,
            cache_name="sync_valid_filter",
            server_name=self.server_name,
        )

        # Ratelimiter for presence updates, keyed by requester.
        self._presence_per_user_limiter = Ratelimiter(
            store=self.store,
            clock=self.clock,
            cfg=hs.config.ratelimiting.rc_presence_per_user,
        )

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
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

        use_state_after = False
        if await self.store.is_feature_enabled(
            user.to_string(), ExperimentalFeature.MSC4222
        ):
            use_state_after = parse_boolean(
                request, "org.matrix.msc4222.use_state_after", default=False
            )

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
        last_ignore_accdata_streampos: int | None = None
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
            use_state_after,
        )

        if filter_id is None:
            filter_collection = self.filtering.DEFAULT_FILTER_COLLECTION
        elif filter_id.startswith("{"):
            try:
                filter_object = json_decoder.decode(filter_id)
            except Exception:
                raise SynapseError(400, "Invalid filter JSON", errcode=Codes.NOT_JSON)

            # We cache the validation, as this can get quite expensive if people use
            # a literal json blob as a query param.
            if not self._json_filter_cache.get(filter_id):
                self.filtering.check_valid_filter(filter_object)
                self._json_filter_cache[filter_id] = True

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
            use_state_after=use_state_after,
        )

        since_token = None
        if since is not None:
            since_token = await StreamToken.from_string(self.store, since)

        # send any outstanding server notices to the user.
        await self._server_notices_sender.on_user_syncing(user.to_string())

        # ignore the presence update if the ratelimit is exceeded but do not pause the request
        allowed, _ = await self._presence_per_user_limiter.can_do_action(requester)
        if not allowed:
            affect_presence = False
            logger.debug("User set_presence ratelimit exceeded; ignoring it.")
        else:
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
            time_now, sync_config, sync_result, requester, filter_collection
        )

        logger.debug("Event formatting complete")
        return 200, response_content

    @trace_with_opname("sync.encode_response")
    async def encode_response(
        self,
        time_now: int,
        sync_config: SyncConfig,
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
            sync_config, sync_result.joined, time_now, serialize_options
        )

        invited = await self.encode_invited(
            sync_result.invited, time_now, stripped_serialize_options
        )

        knocked = await self.encode_knocked(
            sync_result.knocked, time_now, stripped_serialize_options
        )

        archived = await self.encode_archived(
            sync_config, sync_result.archived, time_now, serialize_options
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
    def encode_presence(events: list[UserPresenceState], time_now: int) -> JsonDict:
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
        sync_config: SyncConfig,
        rooms: list[JoinedSyncResult],
        time_now: int,
        serialize_options: SerializeEventConfig,
    ) -> JsonDict:
        """
        Encode the joined rooms in a sync result

        Args:
            sync_config
            rooms: list of sync results for rooms this user is joined to
            time_now: current time - used as a baseline for age calculations
            serialize_options: Event serializer options
        Returns:
            The joined rooms list, in our response format
        """
        joined = {}
        for room in rooms:
            joined[room.room_id] = await self.encode_room(
                sync_config,
                room,
                time_now,
                joined=True,
                serialize_options=serialize_options,
            )

        return joined

    @trace_with_opname("sync.encode_invited")
    async def encode_invited(
        self,
        rooms: list[InvitedSyncResult],
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

            invited_state = unsigned.pop("invite_room_state", [])
            if not isinstance(invited_state, list):
                invited_state = []

            invited_state = list(invited_state)
            invited_state.append(invite)
            invited[room.room_id] = {"invite_state": {"events": invited_state}}

        return invited

    @trace_with_opname("sync.encode_knocked")
    async def encode_knocked(
        self,
        rooms: list[KnockedSyncResult],
        time_now: int,
        serialize_options: SerializeEventConfig,
    ) -> dict[str, dict[str, Any]]:
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
            knocked_state = unsigned.pop("knock_room_state", [])
            if not isinstance(knocked_state, list):
                knocked_state = []
            knocked_state = list(knocked_state)

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
        sync_config: SyncConfig,
        rooms: list[ArchivedSyncResult],
        time_now: int,
        serialize_options: SerializeEventConfig,
    ) -> JsonDict:
        """
        Encode the archived rooms in a sync result

        Args:
            sync_config
            rooms: list of sync results for rooms this user is joined to
            time_now: current time - used as a baseline for age calculations
            serialize_options: Event serializer options
        Returns:
            The archived rooms list, in our response format
        """
        joined = {}
        for room in rooms:
            joined[room.room_id] = await self.encode_room(
                sync_config,
                room,
                time_now,
                joined=False,
                serialize_options=serialize_options,
            )

        return joined

    async def encode_room(
        self,
        sync_config: SyncConfig,
        room: JoinedSyncResult | ArchivedSyncResult,
        time_now: int,
        joined: bool,
        serialize_options: SerializeEventConfig,
    ) -> JsonDict:
        """
        Args:
            sync_config
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

        # We either include a `state` or `state_after` field depending on
        # whether the client has opted in to the newer `state_after` behavior.
        if sync_config.use_state_after:
            state_key_name = "org.matrix.msc4222.state_after"
        else:
            state_key_name = "state"

        result: JsonDict = {
            "timeline": {
                "events": serialized_timeline,
                "prev_batch": await room.timeline.prev_batch.to_string(self.store),
                "limited": room.timeline.limited,
            },
            state_key_name: {"events": serialized_state},
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


class SlidingSyncRestServlet(RestServlet):
    """
    API endpoint for MSC4186 Simplified Sliding Sync `/sync`, which was historically derived
    from MSC3575 (Sliding Sync; now abandoned). Allows for clients to request a
    subset (sliding window) of rooms, state, and timeline events (just what they need)
    in order to bootstrap quickly and subscribe to only what the client cares about.
    Because the client can specify what it cares about, we can respond quickly and skip
    all of the work we would normally have to do with a sync v2 response.

    Extensions of various features are defined in:
        - to-device messaging (MSC3885)
        - end-to-end encryption (MSC3884)
        - typing notifications (MSC3961)
        - receipts (MSC3960)
        - account data (MSC3959)
        - thread subscriptions (MSC4308)

    Request query parameters:
        timeout: How long to wait for new events in milliseconds.
        pos: Stream position token when asking for incremental deltas.

    Request body::
        {
            // Sliding Window API
            "lists": {
                "foo-list": {
                    "ranges": [ [0, 99] ],
                    "required_state": [
                        ["m.room.join_rules", ""],
                        ["m.room.history_visibility", ""],
                        ["m.space.child", "*"]
                    ],
                    "timeline_limit": 10,
                    "filters": {
                        "is_dm": true
                    },
                }
            },
            // Room Subscriptions API
            "room_subscriptions": {
                "!sub1:bar": {
                    "required_state": [ ["*","*"] ],
                    "timeline_limit": 10,
                }
            },
            // Extensions API
            "extensions": {}
        }

    Response JSON::
        {
            "pos": "s58_224_0_13_10_1_1_16_0_1",
            "lists": {
                "foo-list": {
                    "count": 1337,
                    "ops": [{
                        "op": "SYNC",
                        "range": [0, 99],
                        "room_ids": [
                            "!foo:bar",
                            // ... 99 more room IDs
                        ]
                    }]
                }
            },
            // Aggregated rooms from lists and room subscriptions
            "rooms": {
                // Room from room subscription
                "!sub1:bar": {
                    "name": "Alice and Bob",
                    "avatar": "mxc://...",
                    "initial": true,
                    "required_state": [
                        {"sender":"@alice:example.com","type":"m.room.create", "state_key":"", "content":{"creator":"@alice:example.com"}},
                        {"sender":"@alice:example.com","type":"m.room.join_rules", "state_key":"", "content":{"join_rule":"invite"}},
                        {"sender":"@alice:example.com","type":"m.room.history_visibility", "state_key":"", "content":{"history_visibility":"joined"}},
                        {"sender":"@alice:example.com","type":"m.room.member", "state_key":"@alice:example.com", "content":{"membership":"join"}}
                    ],
                    "timeline": [
                        {"sender":"@alice:example.com","type":"m.room.create", "state_key":"", "content":{"creator":"@alice:example.com"}},
                        {"sender":"@alice:example.com","type":"m.room.join_rules", "state_key":"", "content":{"join_rule":"invite"}},
                        {"sender":"@alice:example.com","type":"m.room.history_visibility", "state_key":"", "content":{"history_visibility":"joined"}},
                        {"sender":"@alice:example.com","type":"m.room.member", "state_key":"@alice:example.com", "content":{"membership":"join"}},
                        {"sender":"@alice:example.com","type":"m.room.message", "content":{"body":"A"}},
                        {"sender":"@alice:example.com","type":"m.room.message", "content":{"body":"B"}},
                    ],
                    "prev_batch": "t111_222_333",
                    "joined_count": 41,
                    "invited_count": 1,
                    "notification_count": 1,
                    "highlight_count": 0,
                    "num_live": 2"
                },
                // rooms from list
                "!foo:bar": {
                    "name": "The calculated room name",
                    "avatar": "mxc://...",
                    "initial": true,
                    "required_state": [
                        {"sender":"@alice:example.com","type":"m.room.join_rules", "state_key":"", "content":{"join_rule":"invite"}},
                        {"sender":"@alice:example.com","type":"m.room.history_visibility", "state_key":"", "content":{"history_visibility":"joined"}},
                        {"sender":"@alice:example.com","type":"m.space.child", "state_key":"!foo:example.com", "content":{"via":["example.com"]}},
                        {"sender":"@alice:example.com","type":"m.space.child", "state_key":"!bar:example.com", "content":{"via":["example.com"]}},
                        {"sender":"@alice:example.com","type":"m.space.child", "state_key":"!baz:example.com", "content":{"via":["example.com"]}}
                    ],
                    "timeline": [
                        {"sender":"@alice:example.com","type":"m.room.join_rules", "state_key":"", "content":{"join_rule":"invite"}},
                        {"sender":"@alice:example.com","type":"m.room.message", "content":{"body":"A"}},
                        {"sender":"@alice:example.com","type":"m.room.message", "content":{"body":"B"}},
                        {"sender":"@alice:example.com","type":"m.room.message", "content":{"body":"C"}},
                        {"sender":"@alice:example.com","type":"m.room.message", "content":{"body":"D"}},
                    ],
                    "prev_batch": "t111_222_333",
                    "joined_count": 4,
                    "invited_count": 0,
                    "notification_count": 54,
                    "highlight_count": 3,
                    "num_live": 1,
                },
                 // ... 99 more items
            },
            "extensions": {}
        }
    """

    PATTERNS = client_patterns(
        "/org.matrix.simplified_msc3575/sync$", releases=[], v1=False, unstable=True
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()
        self.filtering = hs.get_filtering()
        self.sliding_sync_handler = hs.get_sliding_sync_handler()
        self.event_serializer = hs.get_event_client_serializer()

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req_experimental_feature(
            request, allow_guest=True, feature=ExperimentalFeature.MSC3575
        )

        user = requester.user

        timeout = parse_integer(request, "timeout", default=0)
        # Position in the stream
        from_token_string = parse_string(request, "pos")

        from_token = None
        if from_token_string is not None:
            from_token = await SlidingSyncStreamToken.from_string(
                self.store, from_token_string
            )

        # TODO: We currently don't know whether we're going to use sticky params or
        # maybe some filters like sync v2  where they are built up once and referenced
        # by filter ID. For now, we will just prototype with always passing everything
        # in.
        body = parse_and_validate_json_object_from_request(request, SlidingSyncBody)

        # Tag and log useful data to differentiate requests.
        set_tag(
            "sliding_sync.sync_type", "initial" if from_token is None else "incremental"
        )
        set_tag("sliding_sync.conn_id", body.conn_id or "")
        log_kv(
            {
                "sliding_sync.lists": {
                    list_name: {
                        "ranges": list_config.ranges,
                        "timeline_limit": list_config.timeline_limit,
                    }
                    for list_name, list_config in (body.lists or {}).items()
                },
                "sliding_sync.room_subscriptions": list(
                    (body.room_subscriptions or {}).keys()
                ),
                # We also include the number of room subscriptions because logs are
                # limited to 1024 characters and the large room ID list above can be cut
                # off.
                "sliding_sync.num_room_subscriptions": len(
                    (body.room_subscriptions or {}).keys()
                ),
            }
        )

        sync_config = SlidingSyncConfig(
            user=user,
            requester=requester,
            # FIXME: Currently, we're just manually copying the fields from the
            # `SlidingSyncBody` into the config. How can we guarantee into the future
            # that we don't forget any? I would like something more structured like
            # `copy_attributes(from=body, to=config)`
            conn_id=body.conn_id,
            lists=body.lists,
            room_subscriptions=body.room_subscriptions,
            extensions=body.extensions,
        )

        (
            sliding_sync_results,
            did_wait,
        ) = await self.sliding_sync_handler.wait_for_sync_for_user(
            requester,
            sync_config,
            from_token,
            timeout,
        )
        # Knowing whether we waited is useful in traces to filter out long-running
        # requests where we were just waiting.
        set_tag("sliding_sync.did_wait", str(did_wait))

        # The client may have disconnected by now; don't bother to serialize the
        # response if so.
        if request._disconnected:
            logger.info("Client has disconnected; not serializing response.")
            return 200, {}

        response_content = await self.encode_response(requester, sliding_sync_results)

        return 200, response_content

    @trace_with_opname("sliding_sync.encode_response")
    async def encode_response(
        self,
        requester: Requester,
        sliding_sync_result: SlidingSyncResult,
    ) -> JsonDict:
        response: JsonDict = defaultdict(dict)

        response["pos"] = await sliding_sync_result.next_pos.to_string(self.store)
        serialized_lists = self.encode_lists(sliding_sync_result.lists)
        if serialized_lists:
            response["lists"] = serialized_lists
        response["rooms"] = await self.encode_rooms(
            requester, sliding_sync_result.rooms
        )
        response["extensions"] = await self.encode_extensions(
            requester, sliding_sync_result.extensions
        )

        return response

    @trace_with_opname("sliding_sync.encode_lists")
    def encode_lists(
        self, lists: Mapping[str, SlidingSyncResult.SlidingWindowList]
    ) -> JsonDict:
        def encode_operation(
            operation: SlidingSyncResult.SlidingWindowList.Operation,
        ) -> JsonDict:
            return {
                "op": operation.op.value,
                "range": operation.range,
                "room_ids": operation.room_ids,
            }

        serialized_lists = {}
        for list_key, list_result in lists.items():
            serialized_lists[list_key] = {
                "count": list_result.count,
                "ops": [encode_operation(op) for op in list_result.ops],
            }

        return serialized_lists

    @trace_with_opname("sliding_sync.encode_rooms")
    async def encode_rooms(
        self,
        requester: Requester,
        rooms: dict[str, SlidingSyncResult.RoomResult],
    ) -> JsonDict:
        time_now = self.clock.time_msec()

        serialize_options = SerializeEventConfig(
            event_format=format_event_for_client_v2_without_room_id,
            requester=requester,
        )

        serialized_rooms: dict[str, JsonDict] = {}
        for room_id, room_result in rooms.items():
            serialized_rooms[room_id] = {
                "notification_count": room_result.notification_count,
                "highlight_count": room_result.highlight_count,
            }

            if room_result.bump_stamp is not None:
                serialized_rooms[room_id]["bump_stamp"] = room_result.bump_stamp

            if room_result.joined_count is not None:
                serialized_rooms[room_id]["joined_count"] = room_result.joined_count

            if room_result.invited_count is not None:
                serialized_rooms[room_id]["invited_count"] = room_result.invited_count

            if room_result.name:
                serialized_rooms[room_id]["name"] = room_result.name

            if room_result.avatar:
                serialized_rooms[room_id]["avatar"] = room_result.avatar

            if room_result.heroes is not None and len(room_result.heroes) > 0:
                serialized_heroes = []
                for hero in room_result.heroes:
                    serialized_hero = {
                        "user_id": hero.user_id,
                    }
                    if hero.display_name is not None:
                        # Not a typo, just how "displayname" is spelled in the spec
                        serialized_hero["displayname"] = hero.display_name

                    if hero.avatar_url is not None:
                        serialized_hero["avatar_url"] = hero.avatar_url

                    serialized_heroes.append(serialized_hero)
                serialized_rooms[room_id]["heroes"] = serialized_heroes

            # We should only include the `initial` key if it's `True` to save bandwidth.
            # The absence of this flag means `False`.
            if room_result.initial:
                serialized_rooms[room_id]["initial"] = room_result.initial

            if room_result.unstable_expanded_timeline:
                serialized_rooms[room_id]["unstable_expanded_timeline"] = (
                    room_result.unstable_expanded_timeline
                )

            # This will be omitted for invite/knock rooms with `stripped_state`
            if (
                room_result.required_state is not None
                and len(room_result.required_state) > 0
            ):
                serialized_required_state = (
                    await self.event_serializer.serialize_events(
                        room_result.required_state,
                        time_now,
                        config=serialize_options,
                    )
                )
                serialized_rooms[room_id]["required_state"] = serialized_required_state

            # This will be omitted for invite/knock rooms with `stripped_state`
            if (
                room_result.timeline_events is not None
                and len(room_result.timeline_events) > 0
            ):
                serialized_timeline = await self.event_serializer.serialize_events(
                    room_result.timeline_events,
                    time_now,
                    config=serialize_options,
                    bundle_aggregations=room_result.bundled_aggregations,
                )
                serialized_rooms[room_id]["timeline"] = serialized_timeline

            # This will be omitted for invite/knock rooms with `stripped_state`
            if room_result.limited is not None:
                serialized_rooms[room_id]["limited"] = room_result.limited

            # This will be omitted for invite/knock rooms with `stripped_state`
            if room_result.prev_batch is not None:
                serialized_rooms[room_id][
                    "prev_batch"
                ] = await room_result.prev_batch.to_string(self.store)

            # This will be omitted for invite/knock rooms with `stripped_state`
            if room_result.num_live is not None:
                serialized_rooms[room_id]["num_live"] = room_result.num_live

            # Field should be absent on non-DM rooms
            if room_result.is_dm:
                serialized_rooms[room_id]["is_dm"] = room_result.is_dm

            # Stripped state only applies to invite/knock rooms
            if (
                room_result.stripped_state is not None
                and len(room_result.stripped_state) > 0
            ):
                # TODO: `knocked_state` but that isn't specced yet.
                #
                # TODO: Instead of adding `knocked_state`, it would be good to rename
                # this to `stripped_state` so it can be shared between invite and knock
                # rooms, see
                # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1117629919
                serialized_rooms[room_id]["invite_state"] = room_result.stripped_state

        return serialized_rooms

    @trace_with_opname("sliding_sync.encode_extensions")
    async def encode_extensions(
        self, requester: Requester, extensions: SlidingSyncResult.Extensions
    ) -> JsonDict:
        serialized_extensions: JsonDict = {}

        if extensions.to_device is not None:
            serialized_extensions["to_device"] = {
                "next_batch": extensions.to_device.next_batch,
                "events": extensions.to_device.events,
            }

        if extensions.e2ee is not None:
            serialized_extensions["e2ee"] = {
                # We always include this because
                # https://github.com/vector-im/element-android/issues/3725. The spec
                # isn't terribly clear on when this can be omitted and how a client
                # would tell the difference between "no keys present" and "nothing
                # changed" in terms of whole field absent / individual key type entry
                # absent Corresponding synapse issue:
                # https://github.com/matrix-org/synapse/issues/10456
                "device_one_time_keys_count": extensions.e2ee.device_one_time_keys_count,
                # https://github.com/matrix-org/matrix-doc/blob/54255851f642f84a4f1aaf7bc063eebe3d76752b/proposals/2732-olm-fallback-keys.md
                # states that this field should always be included, as long as the
                # server supports the feature.
                "device_unused_fallback_key_types": extensions.e2ee.device_unused_fallback_key_types,
            }

            if extensions.e2ee.device_list_updates is not None:
                serialized_extensions["e2ee"]["device_lists"] = {}

                serialized_extensions["e2ee"]["device_lists"]["changed"] = list(
                    extensions.e2ee.device_list_updates.changed
                )
                serialized_extensions["e2ee"]["device_lists"]["left"] = list(
                    extensions.e2ee.device_list_updates.left
                )

        if extensions.account_data is not None:
            serialized_extensions["account_data"] = {
                # Same as the the top-level `account_data.events` field in Sync v2.
                "global": [
                    {"type": account_data_type, "content": content}
                    for account_data_type, content in extensions.account_data.global_account_data_map.items()
                ],
                # Same as the joined room's account_data field in Sync v2, e.g the path
                # `rooms.join["!foo:bar"].account_data.events`.
                "rooms": {
                    room_id: [
                        {"type": account_data_type, "content": content}
                        for account_data_type, content in event_map.items()
                    ]
                    for room_id, event_map in extensions.account_data.account_data_by_room_map.items()
                },
            }

        if extensions.receipts is not None:
            serialized_extensions["receipts"] = {
                "rooms": extensions.receipts.room_id_to_receipt_map,
            }

        if extensions.typing is not None:
            serialized_extensions["typing"] = {
                "rooms": extensions.typing.room_id_to_typing_map,
            }

        # excludes both None and falsy `thread_subscriptions`
        if extensions.thread_subscriptions:
            serialized_extensions["io.element.msc4308.thread_subscriptions"] = (
                _serialise_thread_subscriptions(extensions.thread_subscriptions)
            )

        return serialized_extensions


def _serialise_thread_subscriptions(
    thread_subscriptions: SlidingSyncResult.Extensions.ThreadSubscriptionsExtension,
) -> JsonDict:
    out: JsonDict = {}

    if thread_subscriptions.subscribed:
        out["subscribed"] = {
            room_id: {
                thread_root_id: attr.asdict(
                    change, filter=lambda _attr, v: v is not None
                )
                for thread_root_id, change in room_threads.items()
            }
            for room_id, room_threads in thread_subscriptions.subscribed.items()
        }

    if thread_subscriptions.unsubscribed:
        out["unsubscribed"] = {
            room_id: {
                thread_root_id: attr.asdict(
                    change, filter=lambda _attr, v: v is not None
                )
                for thread_root_id, change in room_threads.items()
            }
            for room_id, room_threads in thread_subscriptions.unsubscribed.items()
        }

    if thread_subscriptions.prev_batch:
        out["prev_batch"] = thread_subscriptions.prev_batch.to_string()

    return out


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    SyncRestServlet(hs).register(http_server)

    SlidingSyncRestServlet(hs).register(http_server)
