import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.constants import RelationTypes
from synapse.api.errors import AuthError, Codes, NotFoundError, SynapseError
from synapse.events import relation_from_event
from synapse.storage.databases.main.thread_subscriptions import (
    AutomaticSubscriptionConflicted,
    ThreadSubscription,
)
from synapse.types import EventOrderings, StreamKeyType, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ThreadSubscriptionsHandler:
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.event_handler = hs.get_event_handler()
        self.auth = hs.get_auth()
        self._notifier = hs.get_notifier()

    async def get_thread_subscription_settings(
        self,
        user_id: UserID,
        room_id: str,
        thread_root_event_id: str,
    ) -> ThreadSubscription | None:
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
        automatic_event_id: str | None,
    ) -> int | None:
        """Sets or updates a user's subscription settings for a specific thread root.

        Args:
            requester_user_id: The ID of the user whose settings are being updated.
            thread_root_event_id: The event ID of the thread root.
            automatic_event_id: if the user was subscribed by an automatic decision by
                their client, the event ID that caused this.

        Returns:
            The stream ID for this update, if the update isn't no-opped.

        Raises:
            NotFoundError if the user cannot access the thread root event, or it isn't
            known to this homeserver. Ditto for the automatic cause event if supplied.

            SynapseError(400, M_NOT_IN_THREAD): if client supplied an automatic cause event
            but user cannot access the event.

            SynapseError(409, M_SKIPPED): if client requested an automatic subscription
            but it was skipped because the cause event is logically later than an unsubscription.
        """
        # First check that the user can access the thread root event
        # and that it exists
        try:
            thread_root_event = await self.event_handler.get_event(
                user_id, room_id, thread_root_event_id
            )
            if thread_root_event is None:
                raise NotFoundError("No such thread root")
        except AuthError:
            logger.info("rejecting thread subscriptions change (thread not accessible)")
            raise NotFoundError("No such thread root")

        if automatic_event_id:
            autosub_cause_event = await self.event_handler.get_event(
                user_id, room_id, automatic_event_id
            )
            if autosub_cause_event is None:
                raise NotFoundError("Automatic subscription event not found")
            relation = relation_from_event(autosub_cause_event)
            if (
                relation is None
                or relation.rel_type != RelationTypes.THREAD
                or relation.parent_id != thread_root_event_id
            ):
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "Automatic subscription must use an event in the thread",
                    errcode=Codes.MSC4306_NOT_IN_THREAD,
                )

            automatic_event_orderings = EventOrderings.from_event(autosub_cause_event)
        else:
            automatic_event_orderings = None

        outcome = await self.store.subscribe_user_to_thread(
            user_id.to_string(),
            room_id,
            thread_root_event_id,
            automatic_event_orderings=automatic_event_orderings,
        )

        if isinstance(outcome, AutomaticSubscriptionConflicted):
            raise SynapseError(
                HTTPStatus.CONFLICT,
                "Automatic subscription obsoleted by an unsubscription request.",
                errcode=Codes.MSC4306_CONFLICTING_UNSUBSCRIPTION,
            )

        if outcome is not None:
            # wake up user streams (e.g. sliding sync) on the same worker
            self._notifier.on_new_event(
                StreamKeyType.THREAD_SUBSCRIPTIONS,
                # outcome is a stream_id
                outcome,
                users=[user_id.to_string()],
            )

        return outcome

    async def unsubscribe_user_from_thread(
        self, user_id: UserID, room_id: str, thread_root_event_id: str
    ) -> int | None:
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

        outcome = await self.store.unsubscribe_user_from_thread(
            user_id.to_string(),
            event.room_id,
            thread_root_event_id,
        )

        if outcome is not None:
            # wake up user streams (e.g. sliding sync) on the same worker
            self._notifier.on_new_event(
                StreamKeyType.THREAD_SUBSCRIPTIONS,
                # outcome is a stream_id
                outcome,
                users=[user_id.to_string()],
            )

        return outcome
