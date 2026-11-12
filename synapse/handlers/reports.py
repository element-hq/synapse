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
#
import logging
import re
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import AuthError, Codes, NotFoundError, SynapseError
from synapse.api.ratelimiting import Ratelimiter
from synapse.types import (
    EventID,
    Requester,
    RoomAlias,
    RoomID,
    UserID,
)
from synapse.util.stringutils import (
    parse_and_validate_mxc_uri,
    parse_and_validate_server_name,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

_HARM_PREFIX = "org.matrix.msc4456"

# The "General/Other" harm categories defined by MSC4456. Complaints using one of
# these harms must include a description.
_GENERAL_HARMS = frozenset(
    f"{_HARM_PREFIX}.{suffix}"
    for suffix in (
        "spam",
        "adult",
        "harassment",
        "violence",
        "child_safety",
        "danger",
        "tos",
        "other",
    )
)

# All harm identifiers defined by MSC4456: https://github.com/matrix-org/matrix-spec-proposals/pull/4456
KNOWN_HARMS = _GENERAL_HARMS | frozenset(
    f"{_HARM_PREFIX}.{suffix}"
    for suffix in (
        "spam.fraud",
        "spam.impersonation",
        "spam.election_interference",
        "spam.flooding",
        "adult.sexual_abuse",
        "adult.ncii",
        "adult.deepfake",
        "adult.animal_sexual_abuse",
        "adult.sexual_violence",
        "harassment.trolling",
        "harassment.targeted",
        "harassment.hate",
        "harassment.doxxing",
        "violence.animal_welfare",
        "violence.threats",
        "violence.graphic",
        "violence.glorification",
        "violence.extremist",
        "violence.human_trafficking",
        "violence.domestic",
        "child_safety.csam",
        "child_safety.grooming",
        "child_safety.privacy_violation",
        "child_safety.harassment",
        "danger.self_harm",
        "danger.eating_disorder",
        "danger.challenges",
        "danger.substance_abuse",
        "tos.hacking",
        "tos.prohibited",
        "tos.ban_evasion",
    )
)

MAX_DESCRIPTION_BYTES = 1024

# Event IDs in room versions 3 and above
_HASH_EVENT_ID_PATTERN = re.compile(r"^\$([a-zA-Z0-9_-]{43}|[a-zA-Z0-9+/]{43})$")


def is_valid_report_identifier(regarding: str) -> bool:
    """Checks whether `regarding` is an identifier a complaint can be filed against,
    per MSC4457.

    Accepted forms are user IDs, event IDs, room IDs, room aliases, server names
    prefixed with "server:", MXC URIs, and the string "m.system".

    The MSC also allows common namespaced identifiers, but this implementation
    does not support them.
    """
    if regarding.startswith("@"):
        return UserID.is_valid_strict(regarding)
    if regarding.startswith("$"):
        if len(regarding.encode("utf-8")) > 255:
            return False
        return bool(_HASH_EVENT_ID_PATTERN.match(regarding)) or EventID.is_valid(
            regarding
        )
    if regarding.startswith("!"):
        if len(regarding.encode("utf-8")) > 255:
            return False
        return RoomID.is_valid(regarding)
    if regarding.startswith("#"):
        if len(regarding.encode("utf-8")) > 255:
            return False
        return RoomAlias.is_valid(regarding)
    if regarding.startswith("server:"):
        server_name = regarding[len("server:") :]
        if len(server_name) > 230:
            return False
        try:
            parse_and_validate_server_name(server_name)
        except ValueError:
            return False
        return True
    if regarding.startswith("mxc://"):
        try:
            parse_and_validate_mxc_uri(regarding)
        except ValueError:
            return False
        return True
    if regarding == "m.system":
        return True
    return False


class ReportsHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()
        self._event_handler = hs.get_event_handler()
        self._directory_handler = hs.get_directory_handler()

        # Ratelimiter for management of existing delayed events,
        # keyed by the requesting user ID.
        self._reports_ratelimiter = Ratelimiter(
            store=self._store,
            clock=self._clock,
            cfg=hs.config.ratelimiting.rc_reports,
        )

        # Ratelimiter for reports rejected because the thing being reported
        # doesn't exist or isn't visible to the reporter, keyed by the requesting
        # user ID.
        self._failed_reports_ratelimiter = Ratelimiter(
            store=self._store,
            clock=self._clock,
            cfg=hs.config.ratelimiting.rc_reports_failed,
        )

    async def report_user(
        self, requester: Requester, target_user_id: str, reason: str
    ) -> None:
        """Files a report against a user from a user.

        Rate and size limits are applied to the report. If the user being reported
        does not belong to this server, the report is ignored. This check is done
        after the limits to reduce DoS potential.

        If the user being reported belongs to this server, but doesn't exist, we
        similarly ignore the report. The spec allows us to return an error if we
        want to, but we choose to hide that user's existence instead.

        If the report is otherwise valid (for a user which exists on our server),
        we append it to the database for later processing.

        Args:
            requester - The user filing the report.
            target_user_id - The user being reported.
            reason - The user-supplied reason the user is being reported.

        Raises:
            SynapseError for BAD_REQUEST/BAD_JSON if the reason is too long.
        """

        await self._check_limits(requester)

        if len(reason) > 1000:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Reason must be less than 1000 characters",
                Codes.BAD_JSON,
            )

        if not self._hs.is_mine_id(target_user_id):
            return  # hide that they're not ours/that we can't do anything about them

        user = await self._store.get_user_by_id(target_user_id)
        if user is None:
            return  # hide that they don't exist

        await self._store.add_user_report(
            target_user_id=target_user_id,
            user_id=requester.user.to_string(),
            reason=reason,
            received_ts=self._clock.time_msec(),
        )

    async def report_room(
        self, requester: Requester, room_id: str, reason: str
    ) -> None:
        """Files a report against a room from a user.

        A rate limit is applied to the report.

        If the report is otherwise valid (for a room which exists on our
        server), we append it to the database for later processing.

        Args:
            requester - The user filing the report.
            room_id - The room being reported.
            reason - The user-supplied reason the room is being reported.

        Raises:
            NotFoundError if the room does not exist.
        """

        await self._check_limits(requester)

        room = await self._store.get_room(room_id)
        if room is None:
            raise NotFoundError("Room does not exist")

        await self._store.add_room_report(
            room_id=room_id,
            user_id=requester.user.to_string(),
            reason=reason,
            received_ts=self._clock.time_msec(),
        )

    async def report_safety_complaint(
        self,
        requester: Requester,
        regarding: str,
        harm: str,
        description: str | None,
    ) -> int:
        """Files a complaint via the generic reporting API (MSC4457).

        Args:
            requester: The user filing the complaint.
            regarding: The identifier the complaint is about.
            harm: The MSC4456 harm identifier being reported.
            description: Text the user supplied to support the complaint.

        Returns:
            The ID of the stored complaint.

        Raises:
            LimitExceededError if the user is filing reports too quickly, or has
                recently filed too many reports about things which do not exist.
            SynapseError for BAD_JSON if the description is missing when required,
                too long, or empty; or for INVALID_PARAM if `regarding` is not a
                recognised identifier or `harm` is unknown.
            NotFoundError if `regarding` refers to something which does not exist
                or which the requester cannot see. See `validate_report`.
        """
        await self._check_limits(requester)

        # Reject the request if the user has had too many recent reports
        # rejected as not found. This doesn't count the request: only 404s from
        # `validate_report` below do.
        await self._failed_reports_ratelimiter.ratelimit(requester, update=False)

        if not is_valid_report_identifier(regarding):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "'regarding' is not a recognised identifier",
                Codes.INVALID_PARAM,
            )

        if harm not in KNOWN_HARMS:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Unknown harm identifier",
                Codes.INVALID_PARAM,
            )

        if description is None:
            if harm in _GENERAL_HARMS:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "'description' is required for this harm",
                    Codes.BAD_JSON,
                )
        else:
            if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    f"'description' must not exceed {MAX_DESCRIPTION_BYTES} bytes",
                    Codes.BAD_JSON,
                )
            if not description.strip():
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "'description' must not be empty",
                    Codes.BAD_JSON,
                )

        # Room aliases are resolved to their room ID.
        try:
            regarding = await self.validate_report(requester, regarding)
        except SynapseError as e:
            if e.code == HTTPStatus.NOT_FOUND:
                # Record the failure (`can_do_action` doesn't raise).
                await self._failed_reports_ratelimiter.can_do_action(requester)
            raise

        return await self._store.add_safety_report_complaint(
            regarding=regarding,
            harm=harm,
            description=description,
            user_id=requester.user.to_string(),
            received_ts=self._clock.time_msec(),
        )

    async def validate_report(self, requester: Requester, regarding: str) -> str:
        """Checks that `requester` is able to file a report against `regarding`.

        Args:
            requester: The user filing the report.
            regarding: The identifier the report is about.

        Returns:
            The identifier to file the report against: the resolved room ID if
            `regarding` is a room alias, otherwise `regarding` unchanged.

        Raises:
            NotFoundError if `regarding` is not found or is an event which is not visible
                to the reporter.
            SynapseError for NOT_FOUND if `regarding` is a room alias which does
                not exist, or 502 if a remote alias could not be looked up over
                federation.
        """
        if regarding.startswith("$"):
            try:
                event = await self._event_handler.get_event(
                    requester.user, None, regarding, show_redacted=False
                )
            except AuthError:
                # The event exists, but the requester is not allowed to see it.
                event = None

            if event is None:
                raise NotFoundError(
                    "Unable to report event: "
                    "it does not exist or you aren't able to see it."
                )

        elif regarding.startswith("!"):
            room = await self._store.get_room(regarding)
            if room is None:
                raise NotFoundError("Room does not exist")

        elif regarding.startswith("#"):
            room_alias = RoomAlias.from_string(regarding)

            # Raises a 404 if the alias doesn't exist. Remote aliases are looked
            # up over federation.
            result = await self._directory_handler.get_association(room_alias)
            room_id = result["room_id"]

            if self._hs.is_mine(room_alias):
                room = await self._store.get_room(room_id)
                if room is None:
                    raise NotFoundError("Room does not exist")

            regarding = room_id

        elif regarding.startswith("mxc://"):
            server_name, media_id = regarding[len("mxc://") :].split("/", 1)

            if self._hs.is_mine_server_name(server_name):
                media = await self._store.get_local_media(media_id)
                if media is None:
                    raise NotFoundError("Media does not exist")

        elif regarding.startswith("server:"):
            server_name = regarding[len("server:") :]
            if not await self._store.is_destination_known(server_name):
                raise NotFoundError("Server is not known to this homeserver")

        elif regarding.startswith("@"):
            if self._hs.is_mine_id(regarding):
                # Includes deactivated users
                user = await self._store.get_user_by_id(regarding)
                if user is None:
                    raise NotFoundError("User does not exist")

        return regarding

    async def _check_limits(self, requester: Requester) -> None:
        await self._reports_ratelimiter.ratelimit(
            requester,
            requester.user.to_string(),
        )
