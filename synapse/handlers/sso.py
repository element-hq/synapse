#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
import hashlib
import io
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NoReturn,
    Optional,
    Set,
)
from urllib.parse import urlencode

import attr
from typing_extensions import Protocol

from twisted.web.iweb import IRequest
from twisted.web.server import Request

from synapse.api.constants import LoginType
from synapse.api.errors import Codes, NotFoundError, RedirectException, SynapseError
from synapse.config.sso import SsoAttributeRequirement
from synapse.handlers.device import DeviceHandler
from synapse.handlers.register import init_counters_for_auth_provider
from synapse.handlers.ui_auth import UIAuthSessionDataConstants
from synapse.http import get_request_user_agent
from synapse.http.server import respond_with_html, respond_with_redirect
from synapse.http.site import SynapseRequest
from synapse.types import (
    JsonDict,
    StrCollection,
    UserID,
    contains_invalid_mxid_characters,
    create_requester,
)
from synapse.util.async_helpers import Linearizer
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class MappingException(Exception):
    """Used to catch errors when mapping an SSO response to user attributes.

    Note that the msg that is raised is shown to end-users.
    """


class SsoIdentityProvider(Protocol):
    """Abstract base class to be implemented by SSO Identity Providers

    An Identity Provider, or IdP, is an external HTTP service which authenticates a user
    to say whether they should be allowed to log in, or perform a given action.

    Synapse supports various implementations of IdPs, including OpenID Connect, SAML,
    and CAS.

    The main entry point is `handle_redirect_request`, which should return a URI to
    redirect the user's browser to the IdP's authentication page.

    Each IdP should be registered with the SsoHandler via
    `hs.get_sso_handler().register_identity_provider()`, so that requests to
    `/_matrix/client/r0/login/sso/redirect` can be correctly dispatched.
    """

    @property
    @abc.abstractmethod
    def idp_id(self) -> str:
        """A unique identifier for this SSO provider

        Eg, "saml", "cas", "github"
        """

    @property
    @abc.abstractmethod
    def idp_name(self) -> str:
        """User-facing name for this provider"""

    @property
    def idp_icon(self) -> Optional[str]:
        """Optional MXC URI for user-facing icon"""
        return None

    @property
    def idp_brand(self) -> Optional[str]:
        """Optional branding identifier"""
        return None

    @abc.abstractmethod
    async def handle_redirect_request(
        self,
        request: SynapseRequest,
        client_redirect_url: Optional[bytes],
        ui_auth_session_id: Optional[str] = None,
    ) -> str:
        """Handle an incoming request to /login/sso/redirect

        Args:
            request: the incoming HTTP request
            client_redirect_url: the URL that we should redirect the
                client to after login (or None for UI Auth).
            ui_auth_session_id: The session ID of the ongoing UI Auth (or
                None if this is a login).

        Returns:
            URL to redirect to
        """
        raise NotImplementedError()


@attr.s(auto_attribs=True)
class UserAttributes:
    # NB: This struct is documented in docs/sso_mapping_providers.md so that users can
    # populate it with data from their own mapping providers.

    # the localpart of the mxid that the mapper has assigned to the user.
    # if `None`, the mapper has not picked a userid, and the user should be prompted to
    # enter one.
    localpart: Optional[str]
    confirm_localpart: bool = False
    display_name: Optional[str] = None
    picture: Optional[str] = None
    # mypy thinks these are incompatible for some reason.
    emails: StrCollection = attr.Factory(list)


@attr.s(slots=True, auto_attribs=True)
class UsernameMappingSession:
    """Data we track about SSO sessions"""

    # A unique identifier for this SSO provider, e.g.  "oidc" or "saml".
    auth_provider_id: str

    # An optional session ID from the IdP.
    auth_provider_session_id: Optional[str]

    # user ID on the IdP server
    remote_user_id: str

    # attributes returned by the ID mapper
    display_name: Optional[str]
    emails: StrCollection
    avatar_url: Optional[str]

    # An optional dictionary of extra attributes to be provided to the client in the
    # login response.
    extra_login_attributes: Optional[JsonDict]

    # where to redirect the client back to
    client_redirect_url: str

    # expiry time for the session, in milliseconds
    expiry_time_ms: int

    # choices made by the user
    chosen_localpart: Optional[str] = None
    use_display_name: bool = True
    use_avatar: bool = True
    emails_to_use: StrCollection = ()
    terms_accepted_version: Optional[str] = None


# the HTTP cookie used to track the mapping session id
USERNAME_MAPPING_SESSION_COOKIE_NAME = b"username_mapping_session"


class SsoHandler:
    # The number of attempts to ask the mapping provider for when generating an MXID.
    _MAP_USERNAME_RETRIES = 1000

    # the time a UsernameMappingSession remains valid for
    _MAPPING_SESSION_VALIDITY_PERIOD_MS = 15 * 60 * 1000

    def __init__(self, hs: "HomeServer"):
        self._clock = hs.get_clock()
        self._store = hs.get_datastores().main
        self._server_name = hs.hostname
        self._is_mine_server_name = hs.is_mine_server_name
        self._registration_handler = hs.get_registration_handler()
        self._auth_handler = hs.get_auth_handler()
        self._device_handler = hs.get_device_handler()
        self._error_template = hs.config.sso.sso_error_template
        self._bad_user_template = hs.config.sso.sso_auth_bad_user_template
        self._profile_handler = hs.get_profile_handler()
        self._media_repo = (
            hs.get_media_repository() if hs.config.media.can_load_media_repo else None
        )
        self._http_client = hs.get_proxied_blocklisted_http_client()

        # The following template is shown after a successful user interactive
        # authentication session. It tells the user they can close the window.
        self._sso_auth_success_template = hs.config.sso.sso_auth_success_template

        self._sso_update_profile_information = (
            hs.config.sso.sso_update_profile_information
        )

        # a lock on the mappings
        self._mapping_lock = Linearizer(name="sso_user_mapping", clock=hs.get_clock())

        # a map from session id to session data
        self._username_mapping_sessions: Dict[str, UsernameMappingSession] = {}

        # map from idp_id to SsoIdentityProvider
        self._identity_providers: Dict[str, SsoIdentityProvider] = {}

        self._consent_at_registration = hs.config.consent.user_consent_at_registration

    def register_identity_provider(self, p: SsoIdentityProvider) -> None:
        p_id = p.idp_id
        assert p_id not in self._identity_providers
        self._identity_providers[p_id] = p
        init_counters_for_auth_provider(p_id)

    def get_identity_providers(self) -> Mapping[str, SsoIdentityProvider]:
        """Get the configured identity providers"""
        return self._identity_providers

    async def get_identity_providers_for_user(
        self, user_id: str
    ) -> Mapping[str, SsoIdentityProvider]:
        """Get the SsoIdentityProviders which a user has used

        Given a user id, get the identity providers that that user has used to log in
        with in the past (and thus could use to re-identify themselves for UI Auth).

        Args:
            user_id: MXID of user to look up

        Raises:
            a map of idp_id to SsoIdentityProvider
        """
        external_ids = await self._store.get_external_ids_by_user(user_id)

        valid_idps = {}
        for idp_id, _ in external_ids:
            idp = self._identity_providers.get(idp_id)
            if not idp:
                logger.warning(
                    "User %r has an SSO mapping for IdP %r, but this is no longer "
                    "configured.",
                    user_id,
                    idp_id,
                )
            else:
                valid_idps[idp_id] = idp

        return valid_idps

    def render_error(
        self,
        request: Request,
        error: str,
        error_description: Optional[str] = None,
        code: int = 400,
    ) -> None:
        """Renders the error template and responds with it.

        This is used to show errors to the user. The template of this page can
        be found under `synapse/res/templates/sso_error.html`.

        Args:
            request: The incoming request from the browser.
                We'll respond with an HTML page describing the error.
            error: A technical identifier for this error.
            error_description: A human-readable description of the error.
            code: The integer error code (an HTTP response code)
        """
        html = self._error_template.render(
            error=error, error_description=error_description
        )
        respond_with_html(request, code, html)

    async def handle_redirect_request(
        self,
        request: SynapseRequest,
        client_redirect_url: bytes,
        idp_id: Optional[str],
    ) -> str:
        """Handle a request to /login/sso/redirect

        Args:
            request: incoming HTTP request
            client_redirect_url: the URL that we should redirect the
                client to after login.
            idp_id: optional identity provider chosen by the client

        Returns:
             the URI to redirect to
        """
        if not self._identity_providers:
            raise SynapseError(
                400, "Homeserver not configured for SSO.", errcode=Codes.UNRECOGNIZED
            )

        # if the client chose an IdP, use that
        idp: Optional[SsoIdentityProvider] = None
        if idp_id:
            idp = self._identity_providers.get(idp_id)
            if not idp:
                raise NotFoundError("Unknown identity provider")

        # if we only have one auth provider, redirect to it directly
        elif len(self._identity_providers) == 1:
            idp = next(iter(self._identity_providers.values()))

        if idp:
            return await idp.handle_redirect_request(request, client_redirect_url)

        # otherwise, redirect to the IDP picker
        return "/_synapse/client/pick_idp?" + urlencode(
            (("redirectUrl", client_redirect_url),)
        )

    async def get_sso_user_by_remote_user_id(
        self, auth_provider_id: str, remote_user_id: str
    ) -> Optional[str]:
        """
        Maps the user ID of a remote IdP to a mxid for a previously seen user.

        If the user has not been seen yet, this will return None.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".
            remote_user_id: The user ID according to the remote IdP. This might
                be an e-mail address, a GUID, or some other form. It must be
                unique and immutable.

        Returns:
            The mxid of a previously seen user.
        """
        logger.debug(
            "Looking for existing mapping for user %s:%s",
            auth_provider_id,
            remote_user_id,
        )

        # Check if we already have a mapping for this user.
        previously_registered_user_id = await self._store.get_user_by_external_id(
            auth_provider_id,
            remote_user_id,
        )

        # A match was found, return the user ID.
        if previously_registered_user_id is not None:
            logger.info(
                "Found existing mapping for IdP '%s' and remote_user_id '%s': %s",
                auth_provider_id,
                remote_user_id,
                previously_registered_user_id,
            )
            return previously_registered_user_id

        # No match.
        return None

    async def complete_sso_login_request(
        self,
        auth_provider_id: str,
        remote_user_id: str,
        request: SynapseRequest,
        client_redirect_url: str,
        sso_to_matrix_id_mapper: Callable[[int], Awaitable[UserAttributes]],
        grandfather_existing_users: Callable[[], Awaitable[Optional[str]]],
        extra_login_attributes: Optional[JsonDict] = None,
        auth_provider_session_id: Optional[str] = None,
        registration_enabled: bool = True,
    ) -> None:
        """
        Given an SSO ID, retrieve the user ID for it and possibly register the user.

        This first checks if the SSO ID has previously been linked to a matrix ID,
        if it has that matrix ID is returned regardless of the current mapping
        logic.

        If a callable is provided for grandfathering users, it is called and can
        potentially return a matrix ID to use. If it does, the SSO ID is linked to
        this matrix ID for subsequent calls.

        The mapping function is called (potentially multiple times) to generate
        a localpart for the user.

        If an unused localpart is generated, the user is registered from the
        given user-agent and IP address and the SSO ID is linked to this matrix
        ID for subsequent calls.

        Finally, we generate a redirect to the supplied redirect uri, with a login token

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".

            remote_user_id: The unique identifier from the SSO provider.

            request: The request to respond to

            client_redirect_url: The redirect URL passed in by the client.

            sso_to_matrix_id_mapper: A callable to generate the user attributes.
                The only parameter is an integer which represents the amount of
                times the returned mxid localpart mapping has failed.

                It is expected that the mapper can raise two exceptions, which
                will get passed through to the caller:

                    MappingException if there was a problem mapping the response
                        to the user.
                    RedirectException to redirect to an additional page (e.g.
                        to prompt the user for more information).

            grandfather_existing_users: A callable which can return an previously
                existing matrix ID. The SSO ID is then linked to the returned
                matrix ID.

            extra_login_attributes: An optional dictionary of extra
                attributes to be provided to the client in the login response.

            auth_provider_session_id: An optional session ID from the IdP.

            registration_enabled: An optional boolean to enable/disable automatic
            registrations of new users. If false and the user does not exist then the
            flow is aborted. Defaults to true.

        Raises:
            MappingException if there was a problem mapping the response to a user.
            RedirectException: if the mapping provider needs to redirect the user
                to an additional page. (e.g. to prompt for more information)

        """
        new_user = False

        # grab a lock while we try to find a mapping for this user. This seems...
        # optimistic, especially for implementations that end up redirecting to
        # interstitial pages.
        async with self._mapping_lock.queue(auth_provider_id):
            # first of all, check if we already have a mapping for this user
            user_id = await self.get_sso_user_by_remote_user_id(
                auth_provider_id,
                remote_user_id,
            )

            # Check for grandfathering of users.
            if not user_id:
                user_id = await grandfather_existing_users()
                if user_id:
                    # Future logins should also match this user ID.
                    await self._store.record_user_external_id(
                        auth_provider_id, remote_user_id, user_id
                    )

            if not user_id and not registration_enabled:
                logger.info(
                    "User does not exist and registration are disabled for IdP '%s' and remote_user_id '%s'",
                    auth_provider_id,
                    remote_user_id,
                )
                raise MappingException(
                    "User does not exist and registrations are disabled"
                )
            elif not user_id:  # Otherwise, generate a new user.
                attributes = await self._call_attribute_mapper(sso_to_matrix_id_mapper)

                next_step_url = self._get_url_for_next_new_user_step(
                    attributes=attributes
                )
                if next_step_url:
                    await self._redirect_to_next_new_user_step(
                        auth_provider_id,
                        remote_user_id,
                        attributes,
                        client_redirect_url,
                        next_step_url,
                        extra_login_attributes,
                        auth_provider_session_id,
                    )

                user_id = await self._register_mapped_user(
                    attributes,
                    auth_provider_id,
                    remote_user_id,
                    get_request_user_agent(request),
                    request.getClientAddress().host,
                )
                new_user = True
            elif self._sso_update_profile_information:
                attributes = await self._call_attribute_mapper(sso_to_matrix_id_mapper)
                if attributes.display_name:
                    user_id_obj = UserID.from_string(user_id)
                    profile_display_name = await self._profile_handler.get_displayname(
                        user_id_obj
                    )
                    if profile_display_name != attributes.display_name:
                        requester = create_requester(
                            user_id,
                            authenticated_entity=user_id,
                        )
                        await self._profile_handler.set_displayname(
                            user_id_obj, requester, attributes.display_name, True
                        )
                if attributes.picture:
                    await self.set_avatar(user_id, attributes.picture)

        await self._auth_handler.complete_sso_login(
            user_id,
            auth_provider_id,
            request,
            client_redirect_url,
            extra_login_attributes,
            new_user=new_user,
            auth_provider_session_id=auth_provider_session_id,
        )

    async def _call_attribute_mapper(
        self,
        sso_to_matrix_id_mapper: Callable[[int], Awaitable[UserAttributes]],
    ) -> UserAttributes:
        """Call the attribute mapper function in a loop, until we get a unique userid"""
        for i in range(self._MAP_USERNAME_RETRIES):
            try:
                attributes = await sso_to_matrix_id_mapper(i)
            except (RedirectException, MappingException):
                # Mapping providers are allowed to issue a redirect (e.g. to ask
                # the user for more information) and can issue a mapping exception
                # if a name cannot be generated.
                raise
            except Exception as e:
                # Any other exception is unexpected.
                raise MappingException(
                    "Could not extract user attributes from SSO response."
                ) from e

            logger.debug(
                "Retrieved user attributes from user mapping provider: %r (attempt %d)",
                attributes,
                i,
            )

            if not attributes.localpart:
                # the mapper has not picked a localpart
                return attributes

            # Check if this mxid already exists
            user_id = UserID(attributes.localpart, self._server_name).to_string()
            if not await self._store.get_users_by_id_case_insensitive(user_id):
                # This mxid is free
                break
        else:
            # Unable to generate a username in 1000 iterations
            # Break and return error to the user
            raise MappingException(
                "Unable to generate a Matrix ID from the SSO response"
            )
        return attributes

    def _get_url_for_next_new_user_step(
        self,
        attributes: Optional[UserAttributes] = None,
        session: Optional[UsernameMappingSession] = None,
    ) -> bytes:
        """Returns the URL to redirect to for the next step of new user registration

        Given attributes from the user mapping provider or a UsernameMappingSession,
        returns the URL to redirect to for the next step of the registration flow.

        Args:
            attributes: the user attributes returned by the user mapping provider,
                from before a UsernameMappingSession has begun.

            session: an active UsernameMappingSession, possibly with some of its
                attributes chosen by the user.

        Returns:
            The URL to redirect to, or an empty value if no redirect is necessary
        """
        # Must provide either attributes or session, not both
        assert (attributes is not None) != (session is not None)

        if (
            attributes
            and (attributes.localpart is None or attributes.confirm_localpart is True)
        ) or (session and session.chosen_localpart is None):
            return b"/_synapse/client/pick_username/account_details"
        elif self._consent_at_registration and not (
            session and session.terms_accepted_version
        ):
            return b"/_synapse/client/new_user_consent"
        else:
            return b"/_synapse/client/sso_register" if session else b""

    async def _redirect_to_next_new_user_step(
        self,
        auth_provider_id: str,
        remote_user_id: str,
        attributes: UserAttributes,
        client_redirect_url: str,
        next_step_url: bytes,
        extra_login_attributes: Optional[JsonDict],
        auth_provider_session_id: Optional[str],
    ) -> NoReturn:
        """Creates a UsernameMappingSession and redirects the browser

        Called if the user mapping provider doesn't return complete information for a new user.
        Raises a RedirectException which redirects the browser to a specified URL.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".

            remote_user_id: The unique identifier from the SSO provider.

            attributes: the user attributes returned by the user mapping provider.

            client_redirect_url: The redirect URL passed in by the client, which we
                will eventually redirect back to.

            next_step_url: The URL to redirect to for the next step of the new user flow.

            extra_login_attributes: An optional dictionary of extra
                attributes to be provided to the client in the login response.

            auth_provider_session_id: An optional session ID from the IdP.

        Raises:
            RedirectException
        """
        # TODO: If needed, allow using/looking up an existing session here.
        session_id = random_string(16)
        now = self._clock.time_msec()
        session = UsernameMappingSession(
            auth_provider_id=auth_provider_id,
            auth_provider_session_id=auth_provider_session_id,
            remote_user_id=remote_user_id,
            display_name=attributes.display_name,
            emails=attributes.emails,
            avatar_url=attributes.picture,
            # Default to using all mapped emails. Will be overwritten in handle_submit_username_request.
            emails_to_use=attributes.emails,
            client_redirect_url=client_redirect_url,
            expiry_time_ms=now + self._MAPPING_SESSION_VALIDITY_PERIOD_MS,
            extra_login_attributes=extra_login_attributes,
            # Treat the localpart returned by the user mapping provider as though
            # it was chosen by the user. If it's None, it must be chosen eventually.
            chosen_localpart=attributes.localpart,
            # TODO: Consider letting the user mapping provider specify defaults for
            #       other user-chosen attributes.
        )

        self._username_mapping_sessions[session_id] = session
        logger.info("Recorded registration session id %s", session_id)

        # Set the cookie and redirect to the next step
        e = RedirectException(next_step_url)
        e.cookies.append(
            b"%s=%s; path=/"
            % (USERNAME_MAPPING_SESSION_COOKIE_NAME, session_id.encode("ascii"))
        )
        raise e

    async def _register_mapped_user(
        self,
        attributes: UserAttributes,
        auth_provider_id: str,
        remote_user_id: str,
        user_agent: str,
        ip_address: str,
    ) -> str:
        """Register a new SSO user.

        This is called once we have successfully mapped the remote user id onto a local
        user id, one way or another.

        Args:
             attributes: user attributes returned by the user mapping provider,
                including a non-empty localpart.

            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".

            remote_user_id: The unique identifier from the SSO provider.

            user_agent: The user-agent in the HTTP request (used for potential
                shadow-banning.)

            ip_address: The IP address of the requester (used for potential
                shadow-banning.)

        Raises:
            a MappingException if the localpart is invalid.

            a SynapseError with code 400 and errcode Codes.USER_IN_USE if the localpart
            is already taken.
        """

        # Since the localpart is provided via a potentially untrusted module,
        # ensure the MXID is valid before registering.
        if not attributes.localpart or contains_invalid_mxid_characters(
            attributes.localpart
        ):
            raise MappingException("localpart is invalid: %s" % (attributes.localpart,))

        logger.debug("Mapped SSO user to local part %s", attributes.localpart)
        registered_user_id = await self._registration_handler.register_user(
            localpart=attributes.localpart,
            default_display_name=attributes.display_name,
            bind_emails=attributes.emails,
            user_agent_ips=[(user_agent, ip_address)],
            auth_provider_id=auth_provider_id,
        )

        await self._store.record_user_external_id(
            auth_provider_id, remote_user_id, registered_user_id
        )

        # Set avatar, if available
        if attributes.picture:
            await self.set_avatar(registered_user_id, attributes.picture)

        return registered_user_id

    async def set_avatar(self, user_id: str, picture_https_url: str) -> bool:
        """Set avatar of the user.

        This downloads the image file from the URL provided, stores that in
        the media repository and then sets the avatar on the user's profile.

        It can detect if the same image is being saved again and bails early by storing
        the hash of the file in the `upload_name` of the avatar image.

        Currently, it only supports server configurations which run the media repository
        within the same process.

        It silently fails and logs a warning by raising an exception and catching it
        internally if:
         * it is unable to fetch the image itself (non 200 status code) or
         * the image supplied is bigger than max allowed size or
         * the image type is not one of the allowed image types.

        Args:
            user_id: matrix user ID in the form @localpart:domain as a string.

            picture_https_url: HTTPS url for the picture image file.

        Returns: `True` if the user's avatar has been successfully set to the image at
            `picture_https_url`.
        """
        if self._media_repo is None:
            logger.info(
                "failed to set user avatar because out-of-process media repositories "
                "are not supported yet "
            )
            return False

        try:
            uid = UserID.from_string(user_id)

            def is_allowed_mime_type(content_type: str) -> bool:
                if (
                    self._profile_handler.allowed_avatar_mimetypes
                    and content_type
                    not in self._profile_handler.allowed_avatar_mimetypes
                ):
                    return False
                return True

            # download picture, enforcing size limit & mime type check
            picture = io.BytesIO()

            content_length, headers, uri, code = await self._http_client.get_file(
                url=picture_https_url,
                output_stream=picture,
                max_size=self._profile_handler.max_avatar_size,
                is_allowed_content_type=is_allowed_mime_type,
            )

            if code != 200:
                raise Exception(
                    f"GET request to download sso avatar image returned {code}"
                )

            # upload name includes hash of the image file's content so that we can
            # easily check if it requires an update or not, the next time user logs in
            upload_name = "sso_avatar_" + hashlib.sha256(picture.read()).hexdigest()

            # bail if user already has the same avatar
            profile = await self._profile_handler.get_profile(user_id)
            if profile["avatar_url"] is not None:
                server_name = profile["avatar_url"].split("/")[-2]
                media_id = profile["avatar_url"].split("/")[-1]
                if self._is_mine_server_name(server_name):
                    media = await self._media_repo.store.get_local_media(media_id)  # type: ignore[has-type]
                    if media is not None and upload_name == media.upload_name:
                        logger.info("skipping saving the user avatar")
                        return True

            # store it in media repository
            avatar_mxc_url = await self._media_repo.create_content(
                media_type=headers[b"Content-Type"][0].decode("utf-8"),
                upload_name=upload_name,
                content=picture,
                content_length=content_length,
                auth_user=uid,
            )

            # save it as user avatar
            await self._profile_handler.set_avatar_url(
                uid,
                create_requester(uid),
                str(avatar_mxc_url),
            )

            logger.info("successfully saved the user avatar")
            return True
        except Exception:
            logger.warning("failed to save the user avatar")
            return False

    async def complete_sso_ui_auth_request(
        self,
        auth_provider_id: str,
        remote_user_id: str,
        ui_auth_session_id: str,
        request: Request,
    ) -> None:
        """
        Given an SSO ID, retrieve the user ID for it and complete UIA.

        Note that this requires that the user is mapped in the "user_external_ids"
        table. This will be the case if they have ever logged in via SAML or OIDC in
        recentish synapse versions, but may not be for older users.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".
            remote_user_id: The unique identifier from the SSO provider.
            ui_auth_session_id: The ID of the user-interactive auth session.
            request: The request to complete.
        """

        user_id = await self.get_sso_user_by_remote_user_id(
            auth_provider_id,
            remote_user_id,
        )

        user_id_to_verify: str = await self._auth_handler.get_session_data(
            ui_auth_session_id, UIAuthSessionDataConstants.REQUEST_USER_ID
        )

        if not user_id:
            logger.warning(
                "Remote user %s/%s has not previously logged in here: UIA will fail",
                auth_provider_id,
                remote_user_id,
            )
        elif user_id != user_id_to_verify:
            logger.warning(
                "Remote user %s/%s mapped onto incorrect user %s: UIA will fail",
                auth_provider_id,
                remote_user_id,
                user_id,
            )
        else:
            # success!
            # Mark the stage of the authentication as successful.
            await self._store.mark_ui_auth_stage_complete(
                ui_auth_session_id, LoginType.SSO, user_id
            )

            # Render the HTML confirmation page and return.
            html = self._sso_auth_success_template
            respond_with_html(request, 200, html)
            return

        # the user_id didn't match: mark the stage of the authentication as unsuccessful
        await self._store.mark_ui_auth_stage_complete(
            ui_auth_session_id, LoginType.SSO, ""
        )

        # render an error page.
        html = self._bad_user_template.render(
            server_name=self._server_name,
            user_id_to_verify=user_id_to_verify,
        )
        respond_with_html(request, 200, html)

    def get_mapping_session(self, session_id: str) -> UsernameMappingSession:
        """Look up the given username mapping session

        If it is not found, raises a SynapseError with an http code of 400

        Args:
            session_id: session to look up
        Returns:
            active mapping session
        Raises:
            SynapseError if the session is not found/has expired
        """
        self._expire_old_sessions()
        session = self._username_mapping_sessions.get(session_id)
        if session:
            return session
        logger.info("Couldn't find session id %s", session_id)
        raise SynapseError(400, "unknown session")

    async def check_username_availability(
        self,
        localpart: str,
        session_id: str,
    ) -> bool:
        """Handle an "is username available" callback check

        Args:
            localpart: desired localpart
            session_id: the session id for the username picker
        Returns:
            True if the username is available
        Raises:
            SynapseError if the localpart is invalid or the session is unknown
        """

        # make sure that there is a valid mapping session, to stop people dictionary-
        # scanning for accounts
        self.get_mapping_session(session_id)

        logger.info(
            "[session %s] Checking for availability of username %s",
            session_id,
            localpart,
        )

        if contains_invalid_mxid_characters(localpart):
            raise SynapseError(400, "localpart is invalid: %s" % (localpart,))
        user_id = UserID(localpart, self._server_name).to_string()
        user_infos = await self._store.get_users_by_id_case_insensitive(user_id)

        logger.info("[session %s] users: %s", session_id, user_infos)
        return not user_infos

    async def handle_submit_username_request(
        self,
        request: SynapseRequest,
        session_id: str,
        localpart: str,
        use_display_name: bool,
        use_avatar: bool,
        emails_to_use: Iterable[str],
    ) -> None:
        """Handle a request to the username-picker 'submit' endpoint

        Will serve an HTTP response to the request.

        Args:
            request: HTTP request
            localpart: localpart requested by the user
            session_id: ID of the username mapping session, extracted from a cookie
            use_display_name: whether the user wants to use the suggested display name
            emails_to_use: emails that the user would like to use
        """
        try:
            session = self.get_mapping_session(session_id)
        except SynapseError as e:
            self.render_error(request, "bad_session", e.msg, code=e.code)
            return

        # update the session with the user's choices
        session.chosen_localpart = localpart
        session.use_display_name = use_display_name
        session.use_avatar = use_avatar

        emails_from_idp = set(session.emails)
        filtered_emails: Set[str] = set()

        # we iterate through the list rather than just building a set conjunction, so
        # that we can log attempts to use unknown addresses
        for email in emails_to_use:
            if email in emails_from_idp:
                filtered_emails.add(email)
            else:
                logger.warning(
                    "[session %s] ignoring user request to use unknown email address %r",
                    session_id,
                    email,
                )
        session.emails_to_use = filtered_emails

        respond_with_redirect(
            request, self._get_url_for_next_new_user_step(session=session)
        )

    async def handle_terms_accepted(
        self, request: SynapseRequest, session_id: str, terms_version: str
    ) -> None:
        """Handle a request to the new-user 'consent' endpoint

        Will serve an HTTP response to the request.

        Args:
            request: HTTP request
            session_id: ID of the username mapping session, extracted from a cookie
            terms_version: the version of the terms which the user viewed and consented
                to
        """
        logger.info(
            "[session %s] User consented to terms version %s",
            session_id,
            terms_version,
        )
        try:
            session = self.get_mapping_session(session_id)
        except SynapseError as e:
            self.render_error(request, "bad_session", e.msg, code=e.code)
            return

        session.terms_accepted_version = terms_version

        respond_with_redirect(
            request, self._get_url_for_next_new_user_step(session=session)
        )

    async def register_sso_user(self, request: Request, session_id: str) -> None:
        """Called once we have all the info we need to register a new user.

        Does so and serves an HTTP response

        Args:
            request: HTTP request
            session_id: ID of the username mapping session, extracted from a cookie
        """
        try:
            session = self.get_mapping_session(session_id)
        except SynapseError as e:
            self.render_error(request, "bad_session", e.msg, code=e.code)
            return

        logger.info(
            "[session %s] Registering localpart %s",
            session_id,
            session.chosen_localpart,
        )

        attributes = UserAttributes(
            localpart=session.chosen_localpart,
            emails=session.emails_to_use,
        )

        if session.use_display_name:
            attributes.display_name = session.display_name

        if session.use_avatar:
            attributes.picture = session.avatar_url

        # the following will raise a 400 error if the username has been taken in the
        # meantime.
        user_id = await self._register_mapped_user(
            attributes,
            session.auth_provider_id,
            session.remote_user_id,
            get_request_user_agent(request),
            request.getClientAddress().host,
        )

        logger.info(
            "[session %s] Registered userid %s with attributes %s",
            session_id,
            user_id,
            attributes,
        )

        # delete the mapping session and the cookie
        del self._username_mapping_sessions[session_id]

        # delete the cookie
        request.addCookie(
            USERNAME_MAPPING_SESSION_COOKIE_NAME,
            b"",
            expires=b"Thu, 01 Jan 1970 00:00:00 GMT",
            path=b"/",
        )

        auth_result = {}
        if session.terms_accepted_version:
            # TODO: make this less awful.
            auth_result[LoginType.TERMS] = True

        await self._registration_handler.post_registration_actions(
            user_id, auth_result, access_token=None
        )

        await self._auth_handler.complete_sso_login(
            user_id,
            session.auth_provider_id,
            request,
            session.client_redirect_url,
            session.extra_login_attributes,
            new_user=True,
            auth_provider_session_id=session.auth_provider_session_id,
        )

    def _expire_old_sessions(self) -> None:
        to_expire = []
        now = int(self._clock.time_msec())

        for session_id, session in self._username_mapping_sessions.items():
            if session.expiry_time_ms <= now:
                to_expire.append(session_id)

        for session_id in to_expire:
            logger.info("Expiring mapping session %s", session_id)
            del self._username_mapping_sessions[session_id]

    def check_required_attributes(
        self,
        request: SynapseRequest,
        attributes: Mapping[str, List[Any]],
        attribute_requirements: Iterable[SsoAttributeRequirement],
    ) -> bool:
        """
        Confirm that the required attributes were present in the SSO response.

        If all requirements are met, this will return True.

        If any requirement is not met, then the request will be finalized by
        showing an error page to the user and False will be returned.

        Args:
            request: The request to (potentially) respond to.
            attributes: The attributes from the SSO IdP.
            attribute_requirements: The requirements that attributes must meet.

        Returns:
            True if all requirements are met, False if any attribute fails to
            meet the requirement.

        """
        # Ensure that the attributes of the logged in user meet the required
        # attributes.
        for requirement in attribute_requirements:
            if not _check_attribute_requirement(attributes, requirement):
                self.render_error(
                    request, "unauthorised", "You are not authorised to log in here."
                )
                return False

        return True

    async def revoke_sessions_for_provider_session_id(
        self,
        auth_provider_id: str,
        auth_provider_session_id: str,
        expected_user_id: Optional[str] = None,
    ) -> None:
        """Revoke any devices and in-flight logins tied to a provider session.

        Can only be called from the main process.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".
            auth_provider_session_id: The session ID from the provider to logout
            expected_user_id: The user we're expecting to logout. If set, it will ignore
                sessions belonging to other users and log an error.
        """

        # It is expected that this is the main process.
        assert isinstance(
            self._device_handler, DeviceHandler
        ), "revoking SSO sessions can only be called on the main process"

        # Invalidate any running user-mapping sessions
        to_delete = []
        for session_id, session in self._username_mapping_sessions.items():
            if (
                session.auth_provider_id == auth_provider_id
                and session.auth_provider_session_id == auth_provider_session_id
            ):
                to_delete.append(session_id)

        for session_id in to_delete:
            logger.info("Revoking mapping session %s", session_id)
            del self._username_mapping_sessions[session_id]

        # Invalidate any in-flight login tokens
        await self._store.invalidate_login_tokens_by_session_id(
            auth_provider_id=auth_provider_id,
            auth_provider_session_id=auth_provider_session_id,
        )

        # Fetch any device(s) in the store associated with the session ID.
        devices = await self._store.get_devices_by_auth_provider_session_id(
            auth_provider_id=auth_provider_id,
            auth_provider_session_id=auth_provider_session_id,
        )

        # We have no guarantee that all the devices of that session are for the same
        # `user_id`. Hence, we have to iterate over the list of devices and log them out
        # one by one.
        for user_id, device_id in devices:
            # If the user_id associated with that device/session is not the one we got
            # out of the `sub` claim, skip that device and show log an error.
            if expected_user_id is not None and user_id != expected_user_id:
                logger.error(
                    "Received a logout notification from SSO provider "
                    f"{auth_provider_id!r} for the user {expected_user_id!r}, but with "
                    f"a session ID ({auth_provider_session_id!r}) which belongs to "
                    f"{user_id!r}. This may happen when the SSO provider user mapper "
                    "uses something else than the standard attribute as mapping ID. "
                    "For OIDC providers, set `backchannel_logout_ignore_sub` to `true` "
                    "in the provider config if that is the case."
                )
                continue

            logger.info(
                "Logging out %r (device %r) via SSO (%r) logout notification (session %r).",
                user_id,
                device_id,
                auth_provider_id,
                auth_provider_session_id,
            )
            await self._device_handler.delete_devices(user_id, [device_id])


def get_username_mapping_session_cookie_from_request(request: IRequest) -> str:
    """Extract the session ID from the cookie

    Raises a SynapseError if the cookie isn't found
    """
    session_id = request.getCookie(USERNAME_MAPPING_SESSION_COOKIE_NAME)
    if not session_id:
        raise SynapseError(code=400, msg="missing session_id")
    return session_id.decode("ascii", errors="replace")


def _check_attribute_requirement(
    attributes: Mapping[str, List[Any]], req: SsoAttributeRequirement
) -> bool:
    """Check if SSO attributes meet the proper requirements.

    Args:
        attributes: A mapping of attributes to an iterable of one or more values.
        requirement: The configured requirement to check.

    Returns:
        True if the required attribute was found and had a proper value.
    """
    if req.attribute not in attributes:
        logger.info("SSO attribute missing: %s", req.attribute)
        return False

    # If the requirement is None, the attribute existing is enough.
    if req.value is None:
        return True

    values = attributes[req.attribute]
    if req.value in values:
        return True

    logger.info(
        "SSO attribute %s did not match required value '%s' (was '%s')",
        req.attribute,
        req.value,
        values,
    )
    return False
