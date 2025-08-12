import logging
from typing import TYPE_CHECKING, Optional

from synapse.api.errors import AuthError, NotFoundError
from synapse.storage.databases.main.thread_subscriptions import ThreadSubscription
from synapse.types import UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ThreadSubscriptionsHandler:
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.event_handler = hs.get_event_handler()
        self.auth = hs.get_auth()

    async def get_thread_subscription_settings(
        self,
        user_id: UserID,
        room_id: str,
        thread_root_event_id: str,
    ) -> Optional[ThreadSubscription]:
        """Get thread subscription settings for a specific thread and user.
        Checks that the thread root is both a real event and also that it is visible
        to the user.

        Args:
            user_id: The ID of the user
            thread_root_event_id: The event ID of the thread root

        Returns:
            A `ThreadSubscription` containing the active subscription settings or None if not set
        """
        # First check that the user can access the thread root event
        # and that it exists
        try:
            event = await self.event_handler.get_event(
                user_id, room_id, thread_root_event_id
            )
            if event is None:
                raise NotFoundError("No such thread root")
        except AuthError:
            raise NotFoundError("No such thread root")

        return await self.store.get_subscription_for_thread(
            user_id.to_string(), event.room_id, thread_root_event_id
        )

    async def subscribe_user_to_thread(
        self,
        user_id: UserID,
        room_id: str,
        thread_root_event_id: str,
        *,
        automatic: bool,
    ) -> Optional[int]:
        """Sets or updates a user's subscription settings for a specific thread root.

        Args:
            requester_user_id: The ID of the user whose settings are being updated.
            thread_root_event_id: The event ID of the thread root.
            automatic: whether the user was subscribed by an automatic decision by
                their client.

        Returns:
            The stream ID for this update, if the update isn't no-opped.

        Raises:
            NotFoundError if the user cannot access the thread root event, or it isn't
            known to this homeserver.
        """
        # First check that the user can access the thread root event
        # and that it exists
        try:
            event = await self.event_handler.get_event(
                user_id, room_id, thread_root_event_id
            )
            if event is None:
                raise NotFoundError("No such thread root")
        except AuthError:
            logger.info("rejecting thread subscriptions change (thread not accessible)")
            raise NotFoundError("No such thread root")

        return await self.store.subscribe_user_to_thread(
            user_id.to_string(),
            event.room_id,
            thread_root_event_id,
            automatic=automatic,
        )

    async def unsubscribe_user_from_thread(
        self, user_id: UserID, room_id: str, thread_root_event_id: str
    ) -> Optional[int]:
        """Clears a user's subscription settings for a specific thread root.

        Args:
            requester_user_id: The ID of the user whose settings are being updated.
            thread_root_event_id: The event ID of the thread root.

        Returns:
            The stream ID for this update, if the update isn't no-opped.

        Raises:
            NotFoundError if the user cannot access the thread root event, or it isn't
            known to this homeserver.
        """
        # First check that the user can access the thread root event
        # and that it exists
        try:
            event = await self.event_handler.get_event(
                user_id, room_id, thread_root_event_id
            )
            if event is None:
                raise NotFoundError("No such thread root")
        except AuthError:
            logger.info("rejecting thread subscriptions change (thread not accessible)")
            raise NotFoundError("No such thread root")

        return await self.store.unsubscribe_user_from_thread(
            user_id.to_string(),
            event.room_id,
            thread_root_event_id,
        )
