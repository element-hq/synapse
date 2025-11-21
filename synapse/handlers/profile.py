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
import logging
import random
from typing import TYPE_CHECKING

from synapse.api.constants import ProfileFields
from synapse.api.errors import (
    AuthError,
    Codes,
    HttpResponseException,
    RequestSendFailed,
    StoreError,
    SynapseError,
)
from synapse.storage.databases.main.media_repository import LocalMedia, RemoteMedia
from synapse.types import JsonDict, JsonValue, Requester, UserID, create_requester
from synapse.util.caches.descriptors import cached
from synapse.util.stringutils import parse_and_validate_mxc_uri

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

MAX_DISPLAYNAME_LEN = 256
MAX_AVATAR_URL_LEN = 1000
# Field name length is specced at 255 bytes.
MAX_CUSTOM_FIELD_LEN = 255


class ProfileHandler:
    """Handles fetching and updating user profile information.

    ProfileHandler can be instantiated directly on workers and will
    delegate to master when necessary.
    """

    def __init__(self, hs: "HomeServer"):
        self.server_name = hs.hostname  # nb must be called this for @cached
        self.clock = hs.get_clock()  # nb must be called this for @cached
        self.store = hs.get_datastores().main
        self.hs = hs

        self.federation = hs.get_federation_client()
        hs.get_federation_registry().register_query_handler(
            "profile", self.on_profile_query
        )

        self.user_directory_handler = hs.get_user_directory_handler()
        self.request_ratelimiter = hs.get_request_ratelimiter()

        self.max_avatar_size: int | None = hs.config.server.max_avatar_size
        self.allowed_avatar_mimetypes: list[str] | None = (
            hs.config.server.allowed_avatar_mimetypes
        )

        self._is_mine_server_name = hs.is_mine_server_name

        self._third_party_rules = hs.get_module_api_callbacks().third_party_event_rules

    async def get_profile(self, user_id: str, ignore_backoff: bool = True) -> JsonDict:
        """
        Get a user's profile as a JSON dictionary.

        Args:
            user_id: The user to fetch the profile of.
            ignore_backoff: True to ignore backoff when fetching over federation.

        Returns:
            A JSON dictionary. For local queries this will include the displayname and avatar_url
            fields, if set. For remote queries it may contain arbitrary information.
        """
        target_user = UserID.from_string(user_id)

        if self.hs.is_mine(target_user):
            profileinfo = await self.store.get_profileinfo(target_user)
            extra_fields = await self.store.get_profile_fields(target_user)

            if (
                profileinfo.display_name is None
                and profileinfo.avatar_url is None
                and not extra_fields
            ):
                raise SynapseError(404, "Profile was not found", Codes.NOT_FOUND)

            # Do not include display name or avatar if unset.
            ret = {}
            if profileinfo.display_name is not None:
                ret[ProfileFields.DISPLAYNAME] = profileinfo.display_name
            if profileinfo.avatar_url is not None:
                ret[ProfileFields.AVATAR_URL] = profileinfo.avatar_url
            if extra_fields:
                ret.update(extra_fields)

            return ret
        else:
            try:
                result = await self.federation.make_query(
                    destination=target_user.domain,
                    query_type="profile",
                    args={"user_id": user_id},
                    ignore_backoff=ignore_backoff,
                )
                return result
            except RequestSendFailed as e:
                raise SynapseError(502, "Failed to fetch profile") from e
            except HttpResponseException as e:
                if e.code < 500 and e.code not in (403, 404):
                    # Other codes are not allowed in c2s API
                    logger.info(
                        "Server replied with wrong response: %s %s", e.code, e.msg
                    )

                    raise SynapseError(502, "Failed to fetch profile")
                raise e.to_synapse_error()

    async def get_displayname(self, target_user: UserID) -> str | None:
        """
        Fetch a user's display name from their profile.

        Args:
            target_user: The user to fetch the display name of.

        Returns:
            The user's display name or None if unset.
        """
        if self.hs.is_mine(target_user):
            try:
                displayname = await self.store.get_profile_displayname(target_user)
            except StoreError as e:
                if e.code == 404:
                    raise SynapseError(404, "Profile was not found", Codes.NOT_FOUND)
                raise

            return displayname
        else:
            try:
                result = await self.federation.make_query(
                    destination=target_user.domain,
                    query_type="profile",
                    args={"user_id": target_user.to_string(), "field": "displayname"},
                    ignore_backoff=True,
                )
            except RequestSendFailed as e:
                raise SynapseError(502, "Failed to fetch profile") from e
            except HttpResponseException as e:
                raise e.to_synapse_error()

            return result.get("displayname")

    async def set_displayname(
        self,
        target_user: UserID,
        requester: Requester,
        new_displayname: str,
        by_admin: bool = False,
        deactivation: bool = False,
        propagate: bool = True,
    ) -> None:
        """Set the displayname of a user

        Args:
            target_user: the user whose displayname is to be changed.
            requester: The user attempting to make this change.
            new_displayname: The displayname to give this user.
            by_admin: Whether this change was made by an administrator.
            deactivation: Whether this change was made while deactivating the user.
            propagate: Whether this change also applies to the user's membership events.
        """
        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "User is not hosted on this homeserver")

        if not by_admin and target_user != requester.user:
            raise AuthError(400, "Cannot set another user's displayname")

        if not by_admin and not self.hs.config.registration.enable_set_displayname:
            profile = await self.store.get_profileinfo(target_user)
            if profile.display_name:
                raise SynapseError(
                    400,
                    "Changing display name is disabled on this server",
                    Codes.FORBIDDEN,
                )

        if not isinstance(new_displayname, str):
            raise SynapseError(
                400, "'displayname' must be a string", errcode=Codes.INVALID_PARAM
            )

        if len(new_displayname) > MAX_DISPLAYNAME_LEN:
            raise SynapseError(
                400, "Displayname is too long (max %i)" % (MAX_DISPLAYNAME_LEN,)
            )

        displayname_to_set: str | None = new_displayname.strip()
        if new_displayname == "":
            displayname_to_set = None

        # If the admin changes the display name of a user, the requesting user cannot send
        # the join event to update the display name in the rooms.
        # This must be done by the target user themselves.
        if by_admin:
            requester = create_requester(
                target_user,
                authenticated_entity=requester.authenticated_entity,
            )

        await self.store.set_profile_displayname(target_user, displayname_to_set)

        profile = await self.store.get_profileinfo(target_user)
        await self.user_directory_handler.handle_local_profile_change(
            target_user.to_string(), profile
        )

        await self._third_party_rules.on_profile_update(
            target_user.to_string(), profile, by_admin, deactivation
        )

        if propagate:
            await self._update_join_states(requester, target_user)

    async def get_avatar_url(self, target_user: UserID) -> str | None:
        """
        Fetch a user's avatar URL from their profile.

        Args:
            target_user: The user to fetch the avatar URL of.

        Returns:
            The user's avatar URL or None if unset.
        """
        if self.hs.is_mine(target_user):
            try:
                avatar_url = await self.store.get_profile_avatar_url(target_user)
            except StoreError as e:
                if e.code == 404:
                    raise SynapseError(404, "Profile was not found", Codes.NOT_FOUND)
                raise
            return avatar_url
        else:
            try:
                result = await self.federation.make_query(
                    destination=target_user.domain,
                    query_type="profile",
                    args={"user_id": target_user.to_string(), "field": "avatar_url"},
                    ignore_backoff=True,
                )
            except RequestSendFailed as e:
                raise SynapseError(502, "Failed to fetch profile") from e
            except HttpResponseException as e:
                raise e.to_synapse_error()

            return result.get("avatar_url")

    async def set_avatar_url(
        self,
        target_user: UserID,
        requester: Requester,
        new_avatar_url: str,
        by_admin: bool = False,
        deactivation: bool = False,
        propagate: bool = True,
    ) -> None:
        """Set a new avatar URL for a user.

        Args:
            target_user: the user whose avatar URL is to be changed.
            requester: The user attempting to make this change.
            new_avatar_url: The avatar URL to give this user.
            by_admin: Whether this change was made by an administrator.
            deactivation: Whether this change was made while deactivating the user.
            propagate: Whether this change also applies to the user's membership events.
        """
        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "User is not hosted on this homeserver")

        if not by_admin and target_user != requester.user:
            raise AuthError(400, "Cannot set another user's avatar_url")

        if not by_admin and not self.hs.config.registration.enable_set_avatar_url:
            profile = await self.store.get_profileinfo(target_user)
            if profile.avatar_url:
                raise SynapseError(
                    400, "Changing avatar is disabled on this server", Codes.FORBIDDEN
                )

        if not isinstance(new_avatar_url, str):
            raise SynapseError(
                400, "'avatar_url' must be a string", errcode=Codes.INVALID_PARAM
            )

        if len(new_avatar_url) > MAX_AVATAR_URL_LEN:
            raise SynapseError(
                400, "Avatar URL is too long (max %i)" % (MAX_AVATAR_URL_LEN,)
            )

        if not await self.check_avatar_size_and_mime_type(new_avatar_url):
            raise SynapseError(403, "This avatar is not allowed", Codes.FORBIDDEN)

        avatar_url_to_set: str | None = new_avatar_url
        if new_avatar_url == "":
            avatar_url_to_set = None

        # Same like set_displayname
        if by_admin:
            requester = create_requester(
                target_user, authenticated_entity=requester.authenticated_entity
            )

        await self.store.set_profile_avatar_url(target_user, avatar_url_to_set)

        profile = await self.store.get_profileinfo(target_user)
        await self.user_directory_handler.handle_local_profile_change(
            target_user.to_string(), profile
        )

        await self._third_party_rules.on_profile_update(
            target_user.to_string(), profile, by_admin, deactivation
        )

        if propagate:
            await self._update_join_states(requester, target_user)

    @cached()
    async def check_avatar_size_and_mime_type(self, mxc: str) -> bool:
        """Check that the size and content type of the avatar at the given MXC URI are
        within the configured limits.

        If the given `mxc` is empty, no checks are performed. (Users are always able to
        unset their avatar.)

        Args:
            mxc: The MXC URI at which the avatar can be found.

        Returns:
             A boolean indicating whether the file can be allowed to be set as an avatar.
        """
        if mxc == "":
            return True

        if not self.max_avatar_size and not self.allowed_avatar_mimetypes:
            return True

        host, port, media_id = parse_and_validate_mxc_uri(mxc)
        if port is not None:
            server_name = host + ":" + str(port)
        else:
            server_name = host

        if self._is_mine_server_name(server_name):
            media_info: (
                LocalMedia | RemoteMedia | None
            ) = await self.store.get_local_media(media_id)
        else:
            media_info = await self.store.get_cached_remote_media(server_name, media_id)

        if media_info is None:
            # Both configuration options need to access the file's metadata, and
            # retrieving remote avatars just for this becomes a bit of a faff, especially
            # if e.g. the file is too big. It's also generally safe to assume most files
            # used as avatar are uploaded locally, or if the upload didn't happen as part
            # of a PUT request on /avatar_url that the file was at least previewed by the
            # user locally (and therefore downloaded to the remote media cache).
            logger.warning("Forbidding avatar change to %s: avatar not on server", mxc)
            return False

        if self.max_avatar_size:
            if media_info.media_length is None:
                logger.warning(
                    "Forbidding avatar change to %s: unknown media size",
                    mxc,
                )
                return False
            # Ensure avatar does not exceed max allowed avatar size
            if media_info.media_length > self.max_avatar_size:
                logger.warning(
                    "Forbidding avatar change to %s: %d bytes is above the allowed size "
                    "limit",
                    mxc,
                    media_info.media_length,
                )
                return False

        if self.allowed_avatar_mimetypes:
            # Ensure the avatar's file type is allowed
            if (
                self.allowed_avatar_mimetypes
                and media_info.media_type not in self.allowed_avatar_mimetypes
            ):
                logger.warning(
                    "Forbidding avatar change to %s: mimetype %s not allowed",
                    mxc,
                    media_info.media_type,
                )
                return False

        return True

    async def get_profile_field(
        self, target_user: UserID, field_name: str
    ) -> JsonValue:
        """
        Fetch a user's profile from the database for local users and over federation
        for remote users.

        Args:
            target_user: The user ID to fetch the profile for.
            field_name: The field to fetch the profile for.

        Returns:
            The value for the profile field or None if the field does not exist.
        """
        if self.hs.is_mine(target_user):
            try:
                field_value = await self.store.get_profile_field(
                    target_user, field_name
                )
            except StoreError as e:
                if e.code == 404:
                    raise SynapseError(404, "Profile was not found", Codes.NOT_FOUND)
                raise

            return field_value
        else:
            try:
                result = await self.federation.make_query(
                    destination=target_user.domain,
                    query_type="profile",
                    args={"user_id": target_user.to_string(), "field": field_name},
                    ignore_backoff=True,
                )
            except RequestSendFailed as e:
                raise SynapseError(502, "Failed to fetch profile") from e
            except HttpResponseException as e:
                raise e.to_synapse_error()

            return result.get(field_name)

    async def set_profile_field(
        self,
        target_user: UserID,
        requester: Requester,
        field_name: str,
        new_value: JsonValue,
        by_admin: bool = False,
        deactivation: bool = False,
    ) -> None:
        """Set a new profile field for a user.

        Args:
            target_user: the user whose profile is to be changed.
            requester: The user attempting to make this change.
            field_name: The name of the profile field to update.
            new_value: The new field value for this user.
            by_admin: Whether this change was made by an administrator.
            deactivation: Whether this change was made while deactivating the user.
        """
        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "User is not hosted on this homeserver")

        if not by_admin and target_user != requester.user:
            raise AuthError(403, "Cannot set another user's profile")

        await self.store.set_profile_field(target_user, field_name, new_value)

        # Custom fields do not propagate into the user directory *or* rooms.
        profile = await self.store.get_profileinfo(target_user)
        await self._third_party_rules.on_profile_update(
            target_user.to_string(), profile, by_admin, deactivation
        )

    async def delete_profile_field(
        self,
        target_user: UserID,
        requester: Requester,
        field_name: str,
        by_admin: bool = False,
        deactivation: bool = False,
    ) -> None:
        """Delete a field from a user's profile.

        Args:
            target_user: the user whose profile is to be changed.
            requester: The user attempting to make this change.
            field_name: The name of the profile field to remove.
            by_admin: Whether this change was made by an administrator.
            deactivation: Whether this change was made while deactivating the user.
        """
        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "User is not hosted on this homeserver")

        if not by_admin and target_user != requester.user:
            raise AuthError(400, "Cannot set another user's profile")

        await self.store.delete_profile_field(target_user, field_name)

        # Custom fields do not propagate into the user directory *or* rooms.
        profile = await self.store.get_profileinfo(target_user)
        await self._third_party_rules.on_profile_update(
            target_user.to_string(), profile, by_admin, deactivation
        )

    async def on_profile_query(self, args: JsonDict) -> JsonDict:
        """Handles federation profile query requests."""

        if not self.hs.config.federation.allow_profile_lookup_over_federation:
            raise SynapseError(
                403,
                "Profile lookup over federation is disabled on this homeserver",
                Codes.FORBIDDEN,
            )

        user = UserID.from_string(args["user_id"])
        if not self.hs.is_mine(user):
            raise SynapseError(400, "User is not hosted on this homeserver")

        just_field = args.get("field", None)

        response: JsonDict = {}
        try:
            if just_field is None or just_field == ProfileFields.DISPLAYNAME:
                displayname = await self.store.get_profile_displayname(user)
                # do not set the displayname field if it is None,
                # since then we send a null in the JSON response
                if displayname is not None:
                    response["displayname"] = displayname
            if just_field is None or just_field == ProfileFields.AVATAR_URL:
                avatar_url = await self.store.get_profile_avatar_url(user)
                # do not set the avatar_url field if it is None,
                # since then we send a null in the JSON response
                if avatar_url is not None:
                    response["avatar_url"] = avatar_url

            if just_field is None:
                response.update(await self.store.get_profile_fields(user))
            elif just_field not in (
                ProfileFields.DISPLAYNAME,
                ProfileFields.AVATAR_URL,
            ):
                response[just_field] = await self.store.get_profile_field(
                    user, just_field
                )
        except StoreError as e:
            if e.code == 404:
                raise SynapseError(404, "Profile was not found", Codes.NOT_FOUND)
            raise

        return response

    async def _update_join_states(
        self, requester: Requester, target_user: UserID
    ) -> None:
        """
        Update the membership events of each room the user is joined to with the
        new profile information.

        Note that this stomps over any custom display name or avatar URL in member events.
        """
        if not self.hs.is_mine(target_user):
            return

        await self.request_ratelimiter.ratelimit(requester)

        # Do not actually update the room state for shadow-banned users.
        if requester.shadow_banned:
            # We randomly sleep a bit just to annoy the requester.
            await self.clock.sleep(random.randint(1, 10))
            return

        room_ids = await self.store.get_rooms_for_user(target_user.to_string())

        for room_id in room_ids:
            handler = self.hs.get_room_member_handler()
            try:
                # Assume the target_user isn't a guest,
                # because we don't let guests set profile or avatar data.
                await handler.update_membership(
                    requester,
                    target_user,
                    room_id,
                    "join",  # We treat a profile update like a join.
                    ratelimit=False,  # Try to hide that these events aren't atomic.
                )
            except Exception as e:
                logger.warning(
                    "Failed to update join event for room %s - %s", room_id, str(e)
                )

    async def check_profile_query_allowed(
        self, target_user: UserID, requester: UserID | None = None
    ) -> None:
        """Checks whether a profile query is allowed. If the
        'require_auth_for_profile_requests' config flag is set to True and a
        'requester' is provided, the query is only allowed if the two users
        share a room.

        Args:
            target_user: The owner of the queried profile.
            requester: The user querying for the profile.

        Raises:
            SynapseError(403): The two users share no room, or ne user couldn't
                be found to be in any room the server is in, and therefore the query
                is denied.
        """

        # Implementation of MSC1301: don't allow looking up profiles if the
        # requester isn't in the same room as the target. We expect requester to
        # be None when this function is called outside of a profile query, e.g.
        # when building a membership event. In this case, we must allow the
        # lookup.
        if (
            not self.hs.config.server.limit_profile_requests_to_users_who_share_rooms
            or not requester
        ):
            return

        # Always allow the user to query their own profile.
        if target_user.to_string() == requester.to_string():
            return

        try:
            requester_rooms = await self.store.get_rooms_for_user(requester.to_string())
            target_user_rooms = await self.store.get_rooms_for_user(
                target_user.to_string()
            )

            # Check if the room lists have no elements in common.
            if requester_rooms.isdisjoint(target_user_rooms):
                raise SynapseError(403, "Profile isn't available", Codes.FORBIDDEN)
        except StoreError as e:
            if e.code == 404:
                # This likely means that one of the users doesn't exist,
                # so we act as if we couldn't find the profile.
                raise SynapseError(403, "Profile isn't available", Codes.FORBIDDEN)
            raise
