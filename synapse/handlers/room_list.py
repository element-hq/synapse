#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

import attr
import msgpack
from unpaddedbase64 import decode_base64, encode_base64

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    GuestAccess,
    HistoryVisibility,
    JoinRules,
    PublicRoomsFilterFields,
)
from synapse.api.errors import (
    Codes,
    HttpResponseException,
    RequestSendFailed,
    SynapseError,
)
from synapse.storage.databases.main.room import LargestRoomStats
from synapse.types import JsonDict, JsonMapping, ThirdPartyInstanceID
from synapse.util.caches.descriptors import _CacheContext, cached
from synapse.util.caches.response_cache import ResponseCache

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

REMOTE_ROOM_LIST_POLL_INTERVAL = 60 * 1000

# This is used to indicate we should only return rooms published to the main list.
EMPTY_THIRD_PARTY_ID = ThirdPartyInstanceID(None, None)

# Maximum number of local public rooms returned over the CS or SS API
MAX_PUBLIC_ROOMS_IN_RESPONSE = 100


class RoomListHandler:
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self.hs = hs
        self.enable_room_list_search = hs.config.roomdirectory.enable_room_list_search
        self.response_cache: ResponseCache[
            Tuple[Optional[int], Optional[str], Optional[ThirdPartyInstanceID]]
        ] = ResponseCache(hs.get_clock(), "room_list")
        self.remote_response_cache: ResponseCache[
            Tuple[str, Optional[int], Optional[str], bool, Optional[str]]
        ] = ResponseCache(hs.get_clock(), "remote_room_list", timeout_ms=30 * 1000)

    async def get_local_public_room_list(
        self,
        limit: Optional[int] = None,
        since_token: Optional[str] = None,
        search_filter: Optional[dict] = None,
        network_tuple: Optional[ThirdPartyInstanceID] = EMPTY_THIRD_PARTY_ID,
        from_federation_origin: Optional[str] = None,
    ) -> JsonDict:
        """Generate a local public room list.

        There are multiple different lists: the main one plus one per third
        party network. A client can ask for a specific list or to return all.

        Args:
            limit
            since_token
            search_filter
            network_tuple: Which public list to use.
                This can be (None, None) to indicate the main list, or a particular
                appservice and network id to use an appservice specific one.
                Setting to None returns all public rooms across all lists.
            from_federation_origin: the server name of the requester, or None
                if the request is not from federation.
        """
        if not self.enable_room_list_search:
            return {"chunk": [], "total_room_count_estimate": 0}

        logger.info(
            "Getting public room list: limit=%r, since=%r, search=%r, network=%r",
            limit,
            since_token,
            bool(search_filter),
            network_tuple,
        )

        capped_limit: int = (
            MAX_PUBLIC_ROOMS_IN_RESPONSE
            if limit is None or limit > MAX_PUBLIC_ROOMS_IN_RESPONSE
            else limit
        )

        if search_filter or from_federation_origin is not None:
            # We explicitly don't bother caching searches or requests for
            # appservice specific lists.
            # We also don't bother caching requests from federated homeservers.
            logger.debug("Bypassing cache as search or federation request.")

            return await self._get_public_room_list(
                capped_limit,
                since_token,
                search_filter,
                network_tuple=network_tuple,
                from_federation_origin=from_federation_origin,
            )

        key = (capped_limit, since_token, network_tuple)
        return await self.response_cache.wrap(
            key,
            self._get_public_room_list,
            capped_limit,
            since_token,
            network_tuple=network_tuple,
            from_federation_origin=from_federation_origin,
        )

    async def _get_public_room_list(
        self,
        limit: int,
        since_token: Optional[str] = None,
        search_filter: Optional[dict] = None,
        network_tuple: Optional[ThirdPartyInstanceID] = EMPTY_THIRD_PARTY_ID,
        from_federation_origin: Optional[str] = None,
    ) -> JsonDict:
        """Generate a public room list.
        Args:
            limit: Maximum amount of rooms to return.
            since_token:
            search_filter: Dictionary to filter rooms by.
            network_tuple: Which public list to use.
                This can be (None, None) to indicate the main list, or a particular
                appservice and network id to use an appservice specific one.
                Setting to None returns all public rooms across all lists.
            from_federation_origin: the server name of the requester, or None
                if the request is not from federation.
        """

        # Pagination tokens work by storing the room ID sent in the last batch,
        # plus the direction (forwards or backwards). Next batch tokens always
        # go forwards, prev batch tokens always go backwards.

        if since_token:
            batch_token = RoomListNextBatch.from_token(since_token)

            bounds: Optional[Tuple[int, str]] = (
                batch_token.last_joined_members,
                batch_token.last_room_id,
            )
            forwards = batch_token.direction_is_forward
            has_batch_token = True
        else:
            bounds = None

            forwards = True
            has_batch_token = False

        if from_federation_origin is None:
            # Client-Server API:
            # we request one more than wanted to see if there are more pages to come
            probing_limit = limit + 1
        else:
            # Federation API:
            # we request a handful more in case any get filtered out by ACLs
            # as a best easy effort attempt to return the full number of entries
            # specified by `limit`.
            probing_limit = limit + 10

        results = await self.store.get_largest_public_rooms(
            network_tuple,
            search_filter,
            probing_limit,
            bounds=bounds,
            forwards=forwards,
            ignore_non_federatable=from_federation_origin is not None,
        )

        def build_room_entry(room: LargestRoomStats) -> JsonDict:
            entry = {
                "room_id": room.room_id,
                "name": room.name,
                "topic": room.topic,
                "canonical_alias": room.canonical_alias,
                "num_joined_members": room.joined_members,
                "avatar_url": room.avatar,
                "world_readable": room.history_visibility
                == HistoryVisibility.WORLD_READABLE,
                "guest_can_join": room.guest_access == "can_join",
                "join_rule": room.join_rules,
                "room_type": room.room_type,
            }

            # Filter out Nones – rather omit the field altogether
            return {k: v for k, v in entry.items() if v is not None}

        # Build a list of up to `limit` entries.
        room_entries: List[JsonDict] = []
        rooms_iterator = results if forwards else reversed(results)

        # Track the first and last 'considered' rooms so that we can provide correct
        # next_batch/prev_batch tokens.
        # This is needed because the loop might finish early when
        # `len(room_entries) >= limit` and we might be left with rooms we didn't
        # 'consider' (iterate over) and we should save those rooms for the next
        # batch.
        first_considered_room: Optional[LargestRoomStats] = None
        last_considered_room: Optional[LargestRoomStats] = None
        cut_off_due_to_limit: bool = False

        for room_result in rooms_iterator:
            if len(room_entries) >= limit:
                cut_off_due_to_limit = True
                break

            if first_considered_room is None:
                first_considered_room = room_result
            last_considered_room = room_result

            if from_federation_origin is not None:
                # If this is a federated request, apply server ACLs if the room has any set
                acl_evaluator = (
                    await self._storage_controllers.state.get_server_acl_for_room(
                        room_result.room_id
                    )
                )

                if acl_evaluator is not None:
                    if not acl_evaluator.server_matches_acl_event(
                        from_federation_origin
                    ):
                        # the requesting server is ACL blocked by the room,
                        # don't show in directory
                        continue

            room_entries.append(build_room_entry(room_result))

        if not forwards:
            # If we are paginating backwards, we still return the chunk in
            # biggest-first order, so reverse again.
            room_entries.reverse()
            # Swap the order of first/last considered rooms.
            first_considered_room, last_considered_room = (
                last_considered_room,
                first_considered_room,
            )

        response: JsonDict = {
            "chunk": room_entries,
        }
        num_results = len(results)

        more_to_come_from_database = num_results == probing_limit

        if forwards and has_batch_token:
            # If there was a token given then we assume that there
            # must be previous results, even if there were no results in this batch.
            if first_considered_room is not None:
                response["prev_batch"] = RoomListNextBatch(
                    last_joined_members=first_considered_room.joined_members,
                    last_room_id=first_considered_room.room_id,
                    direction_is_forward=False,
                ).to_token()
            else:
                # If we didn't find any results this time,
                # we don't have an actual room ID to put in the token.
                # But since `first_considered_room` is None, we know that we have
                # reached the end of the results.
                # So we can use a token of (0, empty room ID) to paginate from the end
                # next time.
                response["prev_batch"] = RoomListNextBatch(
                    last_joined_members=0,
                    last_room_id="",
                    direction_is_forward=False,
                ).to_token()

        if num_results > 0:
            assert first_considered_room is not None
            assert last_considered_room is not None
            if forwards:
                if more_to_come_from_database or cut_off_due_to_limit:
                    response["next_batch"] = RoomListNextBatch(
                        last_joined_members=last_considered_room.joined_members,
                        last_room_id=last_considered_room.room_id,
                        direction_is_forward=True,
                    ).to_token()
            else:  # backwards
                if has_batch_token:
                    response["next_batch"] = RoomListNextBatch(
                        last_joined_members=last_considered_room.joined_members,
                        last_room_id=last_considered_room.room_id,
                        direction_is_forward=True,
                    ).to_token()

                if more_to_come_from_database or cut_off_due_to_limit:
                    response["prev_batch"] = RoomListNextBatch(
                        last_joined_members=first_considered_room.joined_members,
                        last_room_id=first_considered_room.room_id,
                        direction_is_forward=False,
                    ).to_token()

        # We can't efficiently count the total number of rooms that are not
        # blocked by ACLs, but this is just an estimate so that should be
        # good enough.
        response["total_room_count_estimate"] = await self.store.count_public_rooms(
            network_tuple,
            ignore_non_federatable=from_federation_origin is not None,
            search_filter=search_filter,
        )

        return response

    @cached(num_args=1, cache_context=True)
    async def generate_room_entry(
        self,
        room_id: str,
        num_joined_users: int,
        cache_context: _CacheContext,
        with_alias: bool = True,
        allow_private: bool = False,
    ) -> Optional[JsonMapping]:
        """Returns the entry for a room

        Args:
            room_id: The room's ID.
            num_joined_users: Number of users in the room.
            cache_context: Information for cached responses.
            with_alias: Whether to return the room's aliases in the result.
            allow_private: Whether invite-only rooms should be shown.

        Returns:
            Returns a room entry as a dictionary, or None if this
            room was determined not to be shown publicly.
        """
        result = {"room_id": room_id, "num_joined_members": num_joined_users}

        if with_alias:
            aliases = await self.store.get_aliases_for_room(
                room_id, on_invalidate=cache_context.invalidate
            )
            if aliases:
                result["aliases"] = aliases

        current_state_ids = await self._storage_controllers.state.get_current_state_ids(
            room_id, on_invalidate=cache_context.invalidate
        )

        if not current_state_ids:
            # We're not in the room, so may as well bail out here.
            return result

        event_map = await self.store.get_events(
            [
                event_id
                for key, event_id in current_state_ids.items()
                if key[0]
                in (
                    EventTypes.Create,
                    EventTypes.JoinRules,
                    EventTypes.Name,
                    EventTypes.Topic,
                    EventTypes.CanonicalAlias,
                    EventTypes.RoomHistoryVisibility,
                    EventTypes.GuestAccess,
                    "m.room.avatar",
                )
            ]
        )

        current_state = {(ev.type, ev.state_key): ev for ev in event_map.values()}

        # Double check that this is actually a public room.

        join_rules_event = current_state.get((EventTypes.JoinRules, ""))
        if join_rules_event:
            join_rule = join_rules_event.content.get("join_rule", None)
            if not allow_private and join_rule and join_rule != JoinRules.PUBLIC:
                return None

        # Return whether this room is open to federation users or not
        create_event = current_state[EventTypes.Create, ""]
        result["m.federate"] = create_event.content.get(
            EventContentFields.FEDERATE, True
        )

        name_event = current_state.get((EventTypes.Name, ""))
        if name_event:
            name = name_event.content.get("name", None)
            if name:
                result["name"] = name

        topic_event = current_state.get((EventTypes.Topic, ""))
        if topic_event:
            topic = topic_event.content.get("topic", None)
            if topic:
                result["topic"] = topic

        canonical_event = current_state.get((EventTypes.CanonicalAlias, ""))
        if canonical_event:
            canonical_alias = canonical_event.content.get("alias", None)
            if canonical_alias:
                result["canonical_alias"] = canonical_alias

        visibility_event = current_state.get((EventTypes.RoomHistoryVisibility, ""))
        visibility = None
        if visibility_event:
            visibility = visibility_event.content.get("history_visibility", None)
        result["world_readable"] = visibility == HistoryVisibility.WORLD_READABLE

        guest_event = current_state.get((EventTypes.GuestAccess, ""))
        guest = None
        if guest_event:
            guest = guest_event.content.get(EventContentFields.GUEST_ACCESS)
        result["guest_can_join"] = guest == GuestAccess.CAN_JOIN

        avatar_event = current_state.get(("m.room.avatar", ""))
        if avatar_event:
            avatar_url = avatar_event.content.get("url", None)
            if avatar_url:
                result["avatar_url"] = avatar_url

        return result

    async def get_remote_public_room_list(
        self,
        server_name: str,
        limit: Optional[int] = None,
        since_token: Optional[str] = None,
        search_filter: Optional[dict] = None,
        include_all_networks: bool = False,
        third_party_instance_id: Optional[str] = None,
    ) -> JsonDict:
        """Get the public room list from remote server

        Raises:
            SynapseError
        """

        if not self.enable_room_list_search:
            return {"chunk": [], "total_room_count_estimate": 0}

        if search_filter:
            # Searching across federation is defined in MSC2197.
            # However, the remote homeserver may or may not actually support it.
            # So we first try an MSC2197 remote-filtered search, then fall back
            # to a locally-filtered search if we must.

            try:
                res = await self._get_remote_list_cached(
                    server_name,
                    limit=limit,
                    since_token=since_token,
                    include_all_networks=include_all_networks,
                    third_party_instance_id=third_party_instance_id,
                    search_filter=search_filter,
                )
                return res
            except HttpResponseException as hre:
                syn_err = hre.to_synapse_error()
                if hre.code in (404, 405) or syn_err.errcode in (
                    Codes.UNRECOGNIZED,
                    Codes.NOT_FOUND,
                ):
                    logger.debug("Falling back to locally-filtered /publicRooms")
                else:
                    # Not an error that should trigger a fallback.
                    raise SynapseError(502, "Failed to fetch room list")
            except RequestSendFailed:
                # Not an error that should trigger a fallback.
                raise SynapseError(502, "Failed to fetch room list")

            # if we reach this point, then we fall back to the situation where
            # we currently don't support searching across federation, so we have
            # to do it manually without pagination
            limit = None
            since_token = None

        try:
            res = await self._get_remote_list_cached(
                server_name,
                limit=limit,
                since_token=since_token,
                include_all_networks=include_all_networks,
                third_party_instance_id=third_party_instance_id,
            )
        except (RequestSendFailed, HttpResponseException):
            raise SynapseError(502, "Failed to fetch room list")

        if search_filter:
            res = {
                "chunk": [
                    entry
                    for entry in list(res.get("chunk", []))
                    if _matches_room_entry(entry, search_filter)
                ]
            }

        return res

    async def _get_remote_list_cached(
        self,
        server_name: str,
        limit: Optional[int] = None,
        since_token: Optional[str] = None,
        search_filter: Optional[dict] = None,
        include_all_networks: bool = False,
        third_party_instance_id: Optional[str] = None,
    ) -> JsonDict:
        """Wrapper around FederationClient.get_public_rooms that caches the
        result.
        """

        repl_layer = self.hs.get_federation_client()
        if search_filter:
            # We can't cache when asking for search
            return await repl_layer.get_public_rooms(
                server_name,
                limit=limit,
                since_token=since_token,
                search_filter=search_filter,
                include_all_networks=include_all_networks,
                third_party_instance_id=third_party_instance_id,
            )

        key = (
            server_name,
            limit,
            since_token,
            include_all_networks,
            third_party_instance_id,
        )
        return await self.remote_response_cache.wrap(
            key,
            repl_layer.get_public_rooms,
            server_name,
            limit=limit,
            since_token=since_token,
            search_filter=search_filter,
            include_all_networks=include_all_networks,
            third_party_instance_id=third_party_instance_id,
        )


@attr.s(slots=True, frozen=True, auto_attribs=True)
class RoomListNextBatch:
    last_joined_members: int  # The count to get rooms after/before
    last_room_id: str  # The room_id to get rooms after/before
    direction_is_forward: bool  # True if this is a next_batch, false if prev_batch

    KEY_DICT = {
        "last_joined_members": "m",
        "last_room_id": "r",
        "direction_is_forward": "d",
    }

    REVERSE_KEY_DICT = {v: k for k, v in KEY_DICT.items()}

    @classmethod
    def from_token(cls, token: str) -> "RoomListNextBatch":
        decoded = msgpack.loads(decode_base64(token), raw=False)
        return RoomListNextBatch(
            **{cls.REVERSE_KEY_DICT[key]: val for key, val in decoded.items()}
        )

    def to_token(self) -> str:
        return encode_base64(
            msgpack.dumps(
                {self.KEY_DICT[key]: val for key, val in attr.asdict(self).items()}
            )
        )

    def copy_and_replace(self, **kwds: Any) -> "RoomListNextBatch":
        return attr.evolve(self, **kwds)


def _matches_room_entry(room_entry: JsonDict, search_filter: dict) -> bool:
    """Determines whether the given search filter matches a room entry returned over
    federation.

    Only used if the remote server does not support MSC2197 remote-filtered search, and
    hence does not support MSC3827 filtering of `/publicRooms` by room type either.

    In this case, we cannot apply the `room_type` filter since no `room_type` field is
    returned.
    """
    if search_filter and search_filter.get(
        PublicRoomsFilterFields.GENERIC_SEARCH_TERM, None
    ):
        generic_search_term = search_filter[
            PublicRoomsFilterFields.GENERIC_SEARCH_TERM
        ].upper()
        if generic_search_term in room_entry.get("name", "").upper():
            return True
        elif generic_search_term in room_entry.get("topic", "").upper():
            return True
        elif generic_search_term in room_entry.get("canonical_alias", "").upper():
            return True
    else:
        return True

    return False
