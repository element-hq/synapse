#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Mapping,
    Sequence,
)

import attr

from synapse.api.constants import Direction, EventTypes, Membership
from synapse.api.errors import SynapseError
from synapse.events import EventBase
from synapse.types import (
    JsonMapping,
    Requester,
    RoomStreamToken,
    ScheduledTask,
    StateMap,
    TaskStatus,
    UserID,
    UserInfo,
    create_requester,
)
from synapse.visibility import filter_events_for_client

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

REDACT_ALL_EVENTS_ACTION_NAME = "redact_all_events"


class AdminHandler:
    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        self._device_handler = hs.get_device_handler()
        self._storage_controllers = hs.get_storage_controllers()
        self._state_storage_controller = self._storage_controllers.state
        self._msc3866_enabled = hs.config.experimental.msc3866.enabled
        self.event_creation_handler = hs.get_event_creation_handler()
        self._task_scheduler = hs.get_task_scheduler()

        self._task_scheduler.register_action(
            self._redact_all_events, REDACT_ALL_EVENTS_ACTION_NAME
        )

        self.hs = hs

    async def get_redact_task(self, redact_id: str) -> ScheduledTask | None:
        """Get the current status of an active redaction process

        Args:
            redact_id: redact_id returned by start_redact_events.
        """
        return await self._task_scheduler.get_task(redact_id)

    async def get_whois(self, user: UserID) -> JsonMapping:
        connections = []

        sessions = await self._store.get_user_ip_and_agents(user)
        for session in sessions:
            connections.append(
                {
                    "ip": session["ip"],
                    "last_seen": session["last_seen"],
                    "user_agent": session["user_agent"],
                }
            )

        ret = {
            "user_id": user.to_string(),
            "devices": {"": {"sessions": [{"connections": connections}]}},
        }

        return ret

    async def get_user(self, user: UserID) -> JsonMapping | None:
        """Function to get user details"""
        user_info: UserInfo | None = await self._store.get_user_by_id(user.to_string())
        if user_info is None:
            return None

        user_info_dict = {
            "name": user.to_string(),
            "admin": user_info.is_admin,
            "deactivated": user_info.is_deactivated,
            "locked": user_info.locked,
            "shadow_banned": user_info.is_shadow_banned,
            "creation_ts": user_info.creation_ts,
            "appservice_id": user_info.appservice_id,
            "consent_server_notice_sent": user_info.consent_server_notice_sent,
            "consent_version": user_info.consent_version,
            "consent_ts": user_info.consent_ts,
            "user_type": user_info.user_type,
            "is_guest": user_info.is_guest,
            "suspended": user_info.suspended,
        }

        if self._msc3866_enabled:
            # Only include the approved flag if support for MSC3866 is enabled.
            user_info_dict["approved"] = user_info.approved

        # Add additional user metadata
        profile = await self._store.get_profileinfo(user)
        threepids = await self._store.user_get_threepids(user.to_string())
        external_ids = [
            ({"auth_provider": auth_provider, "external_id": external_id})
            for auth_provider, external_id in await self._store.get_external_ids_by_user(
                user.to_string()
            )
        ]
        user_info_dict["displayname"] = profile.display_name
        user_info_dict["avatar_url"] = profile.avatar_url
        user_info_dict["threepids"] = [attr.asdict(t) for t in threepids]
        user_info_dict["external_ids"] = external_ids
        user_info_dict["erased"] = await self._store.is_user_erased(user.to_string())

        last_seen_ts = await self._store.get_last_seen_for_user_id(user.to_string())
        user_info_dict["last_seen_ts"] = last_seen_ts

        return user_info_dict

    async def export_user_data(self, user_id: str, writer: "ExfiltrationWriter") -> Any:
        """Write all data we have on the user to the given writer.

        Args:
            user_id: The user ID to fetch data of.
            writer: The writer to write to.

        Returns:
            Resolves when all data for a user has been written.
            The returned value is that returned by `writer.finished()`.
        """
        # Get all rooms the user is in or has been in
        rooms = await self._store.get_rooms_for_local_user_where_membership_is(
            user_id,
            membership_list=Membership.LIST,
        )

        # We only try and fetch events for rooms the user has been in. If
        # they've been e.g. invited to a room without joining then we handle
        # those separately.
        rooms_user_has_been_in = await self._store.get_rooms_user_has_been_in(user_id)

        for index, room in enumerate(rooms):
            room_id = room.room_id

            logger.info(
                "[%s] Handling room %s, %d/%d", user_id, room_id, index + 1, len(rooms)
            )

            forgotten = await self._store.did_forget(user_id, room_id)
            if forgotten:
                logger.info("[%s] User forgot room %d, ignoring", user_id, room_id)
                continue

            if room_id not in rooms_user_has_been_in:
                # If we haven't been in the rooms then the filtering code below
                # won't return anything, so we need to handle these cases
                # explicitly.

                if room.membership == Membership.INVITE:
                    event_id = room.event_id
                    invite = await self._store.get_event(event_id, allow_none=True)
                    if invite:
                        invited_state = invite.unsigned["invite_room_state"]
                        writer.write_invite(room_id, invite, invited_state)

                if room.membership == Membership.KNOCK:
                    event_id = room.event_id
                    knock = await self._store.get_event(event_id, allow_none=True)
                    if knock:
                        knock_state = knock.unsigned["knock_room_state"]
                        writer.write_knock(room_id, knock, knock_state)

                continue

            # We only want to bother fetching events up to the last time they
            # were joined. We estimate that point by looking at the
            # stream_ordering of the last membership if it wasn't a join.
            if room.membership == Membership.JOIN:
                stream_ordering = self._store.get_room_max_stream_ordering()
            else:
                stream_ordering = room.event_pos.stream

            from_key = RoomStreamToken(topological=0, stream=0)
            to_key = RoomStreamToken(stream=stream_ordering)

            # Events that we've processed in this room
            written_events: set[str] = set()

            # We need to track gaps in the events stream so that we can then
            # write out the state at those events. We do this by keeping track
            # of events whose prev events we haven't seen.

            # Map from event ID to prev events that haven't been processed,
            # dict[str, set[str]].
            event_to_unseen_prevs = {}

            # The reverse mapping to above, i.e. map from unseen event to events
            # that have the unseen event in their prev_events, i.e. the unseen
            # events "children".
            unseen_to_child_events: dict[str, set[str]] = {}

            # We fetch events in the room the user could see by fetching *all*
            # events that we have and then filtering, this isn't the most
            # efficient method perhaps but it does guarantee we get everything.
            while True:
                (
                    events,
                    _,
                    _,
                ) = await self._store.paginate_room_events_by_topological_ordering(
                    room_id=room_id,
                    from_key=from_key,
                    to_key=to_key,
                    limit=100,
                    direction=Direction.FORWARDS,
                )
                if not events:
                    break

                last_event = events[-1]
                assert last_event.internal_metadata.stream_ordering
                from_key = RoomStreamToken(
                    stream=last_event.internal_metadata.stream_ordering,
                    topological=last_event.depth,
                )

                events = await filter_events_for_client(
                    self._storage_controllers,
                    user_id,
                    events,
                )

                writer.write_events(room_id, events)

                # Update the extremity tracking dicts
                for event in events:
                    # Check if we have any prev events that haven't been
                    # processed yet, and add those to the appropriate dicts.
                    unseen_events = set(event.prev_event_ids()) - written_events
                    if unseen_events:
                        event_to_unseen_prevs[event.event_id] = unseen_events
                        for unseen in unseen_events:
                            unseen_to_child_events.setdefault(unseen, set()).add(
                                event.event_id
                            )

                    # Now check if this event is an unseen prev event, if so
                    # then we remove this event from the appropriate dicts.
                    for child_id in unseen_to_child_events.pop(event.event_id, []):
                        event_to_unseen_prevs[child_id].discard(event.event_id)

                    written_events.add(event.event_id)

                logger.info(
                    "Written %d events in room %s", len(written_events), room_id
                )

            # Extremities are the events who have at least one unseen prev event.
            extremities = (
                event_id
                for event_id, unseen_prevs in event_to_unseen_prevs.items()
                if unseen_prevs
            )
            for event_id in extremities:
                if not event_to_unseen_prevs[event_id]:
                    continue
                state = await self._state_storage_controller.get_state_for_event(
                    event_id
                )
                writer.write_state(room_id, event_id, state)

        # Get the user profile
        profile = await self.get_user(UserID.from_string(user_id))
        if profile is not None:
            writer.write_profile(profile)
            logger.info("[%s] Written profile", user_id)

        # Get all devices the user has
        devices = await self._device_handler.get_devices_by_user(user_id)
        writer.write_devices(devices)
        logger.info("[%s] Written %s devices", user_id, len(devices))

        # Get all connections the user has
        connections = await self.get_whois(UserID.from_string(user_id))
        writer.write_connections(
            connections["devices"][""]["sessions"][0]["connections"]
        )
        logger.info("[%s] Written %s connections", user_id, len(connections))

        # Get all account data the user has global and in rooms
        global_data = await self._store.get_global_account_data_for_user(user_id)
        by_room_data = await self._store.get_room_account_data_for_user(user_id)
        writer.write_account_data("global", global_data)
        for room_id in by_room_data:
            writer.write_account_data(room_id, by_room_data[room_id])
        logger.info(
            "[%s] Written account data for %s rooms", user_id, len(by_room_data)
        )

        # Get all media ids the user has
        limit = 100
        start = 0
        while True:
            media_ids, total = await self._store.get_local_media_by_user_paginate(
                start, limit, user_id
            )
            for media in media_ids:
                writer.write_media_id(media.media_id, attr.asdict(media))

            logger.info(
                "[%s] Written %d media_ids of %s",
                user_id,
                (start + len(media_ids)),
                total,
            )
            if (start + limit) >= total:
                break
            start += limit

        return writer.finished()

    async def start_redact_events(
        self,
        user_id: str,
        rooms: list,
        requester: JsonMapping,
        use_admin: bool,
        reason: str | None,
        limit: int | None,
    ) -> str:
        """
        Start a task redacting the events of the given user in the given rooms

        Args:
            user_id: the user ID of the user whose events should be redacted
            rooms: the rooms in which to redact the user's events
            requester: the user requesting the events
            use_admin: whether to use the admin account to issue the redactions
            reason: reason for requesting the redaction, ie spam, etc
            limit: limit on the number of events in each room to redact

        Returns:
            a unique ID which can be used to query the status of the task
        """
        active_tasks = await self._task_scheduler.get_tasks(
            actions=[REDACT_ALL_EVENTS_ACTION_NAME],
            resource_id=user_id,
            statuses=[TaskStatus.ACTIVE],
        )

        if len(active_tasks) > 0:
            raise SynapseError(
                400, "Redact already in progress for user %s" % (user_id,)
            )

        if not limit:
            limit = 1000

        redact_id = await self._task_scheduler.schedule_task(
            REDACT_ALL_EVENTS_ACTION_NAME,
            resource_id=user_id,
            params={
                "rooms": rooms,
                "requester": requester,
                "user_id": user_id,
                "use_admin": use_admin,
                "reason": reason,
                "limit": limit,
            },
        )

        logger.info(
            "starting redact events with redact_id %s",
            redact_id,
        )

        return redact_id

    async def _redact_all_events(
        self, task: ScheduledTask
    ) -> tuple[TaskStatus, Mapping[str, Any] | None, str | None]:
        """
        Task to redact all of a users events in the given rooms, tracking which, if any, events
        whose redaction failed
        """

        assert task.params is not None
        rooms = task.params.get("rooms")
        assert rooms is not None

        r = task.params.get("requester")
        assert r is not None
        admin = Requester.deserialize(self._store, r)

        user_id = task.params.get("user_id")
        assert user_id is not None

        use_admin = task.params.get("use_admin", False)

        # default to puppeting the user unless they are not local or it's been requested to
        # use the admin user to issue the redactions
        requester_id = (
            admin.user.to_string()
            if use_admin or not self.hs.is_mine_id(user_id)
            else user_id
        )
        requester = create_requester(
            requester_id,
            authenticated_entity=admin.user.to_string(),
        )

        reason = task.params.get("reason")
        limit = task.params.get("limit")
        assert limit is not None

        result: Mapping[str, Any] = (
            task.result if task.result else {"failed_redactions": {}}
        )
        for room in rooms:
            room_version = await self._store.get_room_version(room)
            event_ids = await self._store.get_events_sent_by_user_in_room(
                user_id,
                room,
                limit,
                ["m.room.member", "m.room.message", "m.room.encrypted"],
            )
            if not event_ids:
                # nothing to redact in this room
                continue

            events = await self._store.get_events_as_list(event_ids)
            for event in events:
                # we care about join events but not other membership events
                if event.type == "m.room.member":
                    content = event.content
                    if content:
                        if content.get("membership") == Membership.JOIN:
                            pass
                        else:
                            continue
                relations = await self._store.get_relations_for_event(
                    room, event.event_id, event, event_type=EventTypes.Redaction
                )

                # if we've already successfully redacted this event then skip processing it
                if relations[0]:
                    continue

                event_dict = {
                    "type": EventTypes.Redaction,
                    "content": {"reason": reason} if reason else {},
                    "room_id": room,
                    "sender": requester.user.to_string(),
                }
                if room_version.updated_redaction_rules:
                    event_dict["content"]["redacts"] = event.event_id
                else:
                    event_dict["redacts"] = event.event_id

                try:
                    # set the prev event to the offending message to allow for redactions
                    # to be processed in the case where the user has been kicked/banned before
                    # redactions are requested
                    (
                        redaction,
                        _,
                    ) = await self.event_creation_handler.create_and_send_nonmember_event(
                        requester,
                        event_dict,
                        prev_event_ids=[event.event_id],
                        ratelimit=False,
                    )
                except Exception as ex:
                    logger.info(
                        "Redaction of event %s failed due to: %s", event.event_id, ex
                    )
                    result["failed_redactions"][event.event_id] = str(ex)
                    await self._task_scheduler.update_task(task.id, result=result)

        return TaskStatus.COMPLETE, result, None


class ExfiltrationWriter(metaclass=abc.ABCMeta):
    """Interface used to specify how to write exported data."""

    @abc.abstractmethod
    def write_events(self, room_id: str, events: list[EventBase]) -> None:
        """Write a batch of events for a room."""
        raise NotImplementedError()

    @abc.abstractmethod
    def write_state(
        self, room_id: str, event_id: str, state: StateMap[EventBase]
    ) -> None:
        """Write the state at the given event in the room.

        This only gets called for backward extremities rather than for each
        event.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def write_invite(
        self, room_id: str, event: EventBase, state: StateMap[EventBase]
    ) -> None:
        """Write an invite for the room, with associated invite state.

        Args:
            room_id: The room ID the invite is for.
            event: The invite event.
            state: A subset of the state at the invite, with a subset of the
                event keys (type, state_key content and sender).
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def write_knock(
        self, room_id: str, event: EventBase, state: StateMap[EventBase]
    ) -> None:
        """Write a knock for the room, with associated knock state.

        Args:
            room_id: The room ID the knock is for.
            event: The knock event.
            state: A subset of the state at the knock, with a subset of the
                event keys (type, state_key content and sender).
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def write_profile(self, profile: JsonMapping) -> None:
        """Write the profile of a user.

        Args:
            profile: The user profile.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def write_devices(self, devices: Sequence[JsonMapping]) -> None:
        """Write the devices of a user.

        Args:
            devices: The list of devices.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def write_connections(self, connections: Sequence[JsonMapping]) -> None:
        """Write the connections of a user.

        Args:
            connections: The list of connections / sessions.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def write_account_data(
        self, file_name: str, account_data: Mapping[str, JsonMapping]
    ) -> None:
        """Write the account data of a user.

        Args:
            file_name: file name to write data
            account_data: mapping of global or room account_data
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def write_media_id(self, media_id: str, media_metadata: JsonMapping) -> None:
        """Write the media's metadata of a user.
        Exports only the metadata, as this can be fetched from the database via
        read only. In order to access the files, a connection to the correct
        media repository would be required.

        Args:
            media_id: ID of the media.
            media_metadata: Metadata of one media file.
        """

    @abc.abstractmethod
    def finished(self) -> Any:
        """Called when all data has successfully been exported and written.

        This functions return value is passed to the caller of
        `export_user_data`.
        """
        raise NotImplementedError()
