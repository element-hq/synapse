#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

import collections
import itertools
import logging
from http import HTTPStatus
from typing import (
    TYPE_CHECKING,
    Collection,
    Container,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from prometheus_client import Counter, Histogram

from synapse import event_auth
from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    GuestAccess,
    Membership,
    RejectedReason,
    RoomEncryptionAlgorithms,
)
from synapse.api.errors import (
    AuthError,
    Codes,
    EventSizeError,
    FederationError,
    FederationPullAttemptBackoffError,
    HttpResponseException,
    PartialStateConflictError,
    RequestSendFailed,
    SynapseError,
)
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersion, RoomVersions
from synapse.event_auth import (
    auth_types_for_event,
    check_state_dependent_auth_rules,
    check_state_independent_auth_rules,
    validate_event_for_room_version,
)
from synapse.events import EventBase
from synapse.events.snapshot import EventContext, UnpersistedEventContextBase
from synapse.federation.federation_client import InvalidResponseError, PulledPduInfo
from synapse.logging.context import nested_logging_context
from synapse.logging.opentracing import (
    SynapseTags,
    set_tag,
    start_active_span,
    tag_args,
    trace,
)
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.replication.http.devices import (
    ReplicationMultiUserDevicesResyncRestServlet,
)
from synapse.replication.http.federation import (
    ReplicationFederationSendEventsRestServlet,
)
from synapse.state import StateResolutionStore
from synapse.storage.databases.main.events_worker import EventRedactBehaviour
from synapse.types import (
    PersistedEventPosition,
    RoomStreamToken,
    StateMap,
    StrCollection,
    UserID,
    get_domain_from_id,
)
from synapse.types.state import StateFilter
from synapse.util.async_helpers import Linearizer, concurrently_execute
from synapse.util.iterutils import batch_iter, partition, sorted_topologically
from synapse.util.retryutils import NotRetryingDestination
from synapse.util.stringutils import shortstr

if TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)

soft_failed_event_counter = Counter(
    "synapse_federation_soft_failed_events_total",
    "Events received over federation that we marked as soft_failed",
)

# Added to debug performance and track progress on optimizations
backfill_processing_after_timer = Histogram(
    "synapse_federation_backfill_processing_after_time_seconds",
    "sec",
    [],
    buckets=(
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        7.5,
        10.0,
        15.0,
        20.0,
        25.0,
        30.0,
        40.0,
        50.0,
        60.0,
        80.0,
        100.0,
        120.0,
        150.0,
        180.0,
        "+Inf",
    ),
)


class FederationEventHandler:
    """Handles events that originated from federation.

    Responsible for handing incoming events and passing them on to the rest
    of the homeserver (including auth and state conflict resolutions)
    """

    def __init__(self, hs: "HomeServer"):
        self._clock = hs.get_clock()
        self._store = hs.get_datastores().main
        self._state_store = hs.get_datastores().state
        self._state_deletion_store = hs.get_datastores().state_deletion
        self._storage_controllers = hs.get_storage_controllers()
        self._state_storage_controller = self._storage_controllers.state

        self._state_handler = hs.get_state_handler()
        self._event_creation_handler = hs.get_event_creation_handler()
        self._event_auth_handler = hs.get_event_auth_handler()
        self._message_handler = hs.get_message_handler()
        self._bulk_push_rule_evaluator = hs.get_bulk_push_rule_evaluator()
        self._state_resolution_handler = hs.get_state_resolution_handler()
        # avoid a circular dependency by deferring execution here
        self._get_room_member_handler = hs.get_room_member_handler

        self._federation_client = hs.get_federation_client()
        self._third_party_event_rules = (
            hs.get_module_api_callbacks().third_party_event_rules
        )
        self._notifier = hs.get_notifier()

        self._is_mine_id = hs.is_mine_id
        self._is_mine_server_name = hs.is_mine_server_name
        self._server_name = hs.hostname
        self._instance_name = hs.get_instance_name()

        self._config = hs.config
        self._ephemeral_messages_enabled = hs.config.server.enable_ephemeral_messages

        self._send_events = ReplicationFederationSendEventsRestServlet.make_client(hs)
        if hs.config.worker.worker_app:
            self._multi_user_device_resync = (
                ReplicationMultiUserDevicesResyncRestServlet.make_client(hs)
            )
        else:
            self._device_list_updater = hs.get_device_handler().device_list_updater

        # When joining a room we need to queue any events for that room up.
        # For each room, a list of (pdu, origin) tuples.
        # TODO: replace this with something more elegant, probably based around the
        # federation event staging area.
        self.room_queues: Dict[str, List[Tuple[EventBase, str]]] = {}

        self._room_pdu_linearizer = Linearizer("fed_room_pdu")

    async def on_receive_pdu(self, origin: str, pdu: EventBase) -> None:
        """Process a PDU received via a federation /send/ transaction

        Args:
            origin: server which initiated the /send/ transaction. Will
                be used to fetch missing events or state.
            pdu: received PDU
        """

        # We should never see any outliers here.
        assert not pdu.internal_metadata.outlier

        room_id = pdu.room_id
        event_id = pdu.event_id

        # We reprocess pdus when we have seen them only as outliers
        existing = await self._store.get_event(
            event_id, allow_none=True, allow_rejected=True
        )

        # FIXME: Currently we fetch an event again when we already have it
        # if it has been marked as an outlier.
        if existing:
            if not existing.internal_metadata.is_outlier():
                logger.info(
                    "Ignoring received event %s which we have already seen", event_id
                )
                return
            if pdu.internal_metadata.is_outlier():
                logger.info(
                    "Ignoring received outlier %s which we already have as an outlier",
                    event_id,
                )
                return
            logger.info("De-outliering event %s", event_id)

        # do some initial sanity-checking of the event. In particular, make
        # sure it doesn't have hundreds of prev_events or auth_events, which
        # could cause a huge state resolution or cascade of event fetches.
        try:
            self._sanity_check_event(pdu)
        except SynapseError as err:
            logger.warning("Received event failed sanity checks")
            raise FederationError("ERROR", err.code, err.msg, affected=pdu.event_id)

        # If we are currently in the process of joining this room, then we
        # queue up events for later processing.
        if room_id in self.room_queues:
            logger.info(
                "Queuing PDU from %s for now: join in progress",
                origin,
            )
            self.room_queues[room_id].append((pdu, origin))
            return

        # If we're not in the room just ditch the event entirely. This is
        # probably an old server that has come back and thinks we're still in
        # the room (or we've been rejoined to the room by a state reset).
        #
        # Note that if we were never in the room then we would have already
        # dropped the event, since we wouldn't know the room version.
        is_in_room = await self._event_auth_handler.is_host_in_room(
            room_id, self._server_name
        )
        if not is_in_room:
            logger.info(
                "Ignoring PDU from %s as we're not in the room",
                origin,
            )
            return None

        # Try to fetch any missing prev events to fill in gaps in the graph
        prevs = set(pdu.prev_event_ids())
        seen = await self._store.have_events_in_timeline(prevs)
        missing_prevs = prevs - seen

        if missing_prevs:
            # We only backfill backwards to the min depth.
            min_depth = await self._store.get_min_depth(pdu.room_id)
            logger.debug("min_depth: %d", min_depth)

            if min_depth is not None and pdu.depth > min_depth:
                # If we're missing stuff, ensure we only fetch stuff one
                # at a time.
                logger.info(
                    "Acquiring room lock to fetch %d missing prev_events: %s",
                    len(missing_prevs),
                    shortstr(missing_prevs),
                )
                async with self._room_pdu_linearizer.queue(pdu.room_id):
                    logger.info(
                        "Acquired room lock to fetch %d missing prev_events",
                        len(missing_prevs),
                    )

                    try:
                        await self._get_missing_events_for_pdu(
                            origin, pdu, prevs, min_depth
                        )
                    except Exception as e:
                        raise Exception(
                            "Error fetching missing prev_events for %s: %s"
                            % (event_id, e)
                        ) from e

                # Update the set of things we've seen after trying to
                # fetch the missing stuff
                seen = await self._store.have_events_in_timeline(prevs)
                missing_prevs = prevs - seen

                if not missing_prevs:
                    logger.info("Found all missing prev_events")

            if missing_prevs:
                # since this event was pushed to us, it is possible for it to
                # become the only forward-extremity in the room, and we would then
                # trust its state to be the state for the whole room. This is very
                # bad. Further, if the event was pushed to us, there is no excuse
                # for us not to have all the prev_events. (XXX: apart from
                # min_depth?)
                #
                # We therefore reject any such events.
                logger.warning(
                    "Rejecting: failed to fetch %d prev events: %s",
                    len(missing_prevs),
                    shortstr(missing_prevs),
                )
                raise FederationError(
                    "ERROR",
                    403,
                    (
                        "Your server isn't divulging details about prev_events "
                        "referenced in this event."
                    ),
                    affected=pdu.event_id,
                )

        try:
            context = await self._state_handler.compute_event_context(pdu)
            await self._process_received_pdu(origin, pdu, context)
        except PartialStateConflictError:
            # The room was un-partial stated while we were processing the PDU.
            # Try once more, with full state this time.
            logger.info(
                "Room %s was un-partial stated while processing the PDU, trying again.",
                room_id,
            )
            context = await self._state_handler.compute_event_context(pdu)
            await self._process_received_pdu(origin, pdu, context)

    async def on_send_membership_event(
        self, origin: str, event: EventBase
    ) -> Tuple[EventBase, EventContext]:
        """
        We have received a join/leave/knock event for a room via send_join/leave/knock.

        Verify that event and send it into the room on the remote homeserver's behalf.

        This is quite similar to on_receive_pdu, with the following principal
        differences:
          * only membership events are permitted (and only events with
            sender==state_key -- ie, no kicks or bans)
          * *We* send out the event on behalf of the remote server.
          * We enforce the membership restrictions of restricted rooms.
          * Rejected events result in an exception rather than being stored.

        There are also other differences, however it is not clear if these are by
        design or omission. In particular, we do not attempt to backfill any missing
        prev_events.

        Args:
            origin: The homeserver of the remote (joining/invited/knocking) user.
            event: The member event that has been signed by the remote homeserver.

        Returns:
            The event and context of the event after inserting it into the room graph.

        Raises:
            RuntimeError if any prev_events are missing
            SynapseError if the event is not accepted into the room
            PartialStateConflictError if the room was un-partial stated in between
                computing the state at the event and persisting it. The caller should
                retry exactly once in this case.
        """
        logger.debug(
            "on_send_membership_event: Got event: %s, signatures: %s",
            event.event_id,
            event.signatures,
        )

        if get_domain_from_id(event.sender) != origin:
            logger.info(
                "Got send_membership request for user %r from different origin %s",
                event.sender,
                origin,
            )
            raise SynapseError(403, "User not from origin", Codes.FORBIDDEN)

        if event.sender != event.state_key:
            raise SynapseError(400, "state_key and sender must match", Codes.BAD_JSON)

        assert not event.internal_metadata.outlier

        # Send this event on behalf of the other server.
        #
        # The remote server isn't a full participant in the room at this point, so
        # may not have an up-to-date list of the other homeservers participating in
        # the room, so we send it on their behalf.
        event.internal_metadata.send_on_behalf_of = origin

        context = await self._state_handler.compute_event_context(event)
        await self._check_event_auth(origin, event, context)
        if context.rejected:
            raise SynapseError(
                403, f"{event.membership} event was rejected", Codes.FORBIDDEN
            )

        # for joins, we need to check the restrictions of restricted rooms
        if event.membership == Membership.JOIN:
            await self.check_join_restrictions(context, event)

        # for knock events, we run the third-party event rules. It's not entirely clear
        # why we don't do this for other sorts of membership events.
        if event.membership == Membership.KNOCK:
            event_allowed, _ = await self._third_party_event_rules.check_event_allowed(
                event, context
            )
            if not event_allowed:
                logger.info("Sending of knock %s forbidden by third-party rules", event)
                raise SynapseError(
                    403, "This event is not allowed in this context", Codes.FORBIDDEN
                )

        # all looks good, we can persist the event.

        # First, precalculate the joined hosts so that the federation sender doesn't
        # need to.
        await self._event_creation_handler.cache_joined_hosts_for_events(
            [(event, context)]
        )

        await self._check_for_soft_fail(event, context=context, origin=origin)
        await self._run_push_actions_and_persist_event(event, context)
        return event, context

    async def check_join_restrictions(
        self,
        context: UnpersistedEventContextBase,
        event: EventBase,
    ) -> None:
        """Check that restrictions in restricted join rules are matched

        Called when we receive a join event via send_join.

        Raises an auth error if the restrictions are not matched.
        """
        prev_state_ids = await context.get_prev_state_ids()

        # Check if the user is already in the room or invited to the room.
        user_id = event.state_key
        prev_member_event_id = prev_state_ids.get((EventTypes.Member, user_id), None)
        prev_membership = None
        if prev_member_event_id:
            prev_member_event = await self._store.get_event(prev_member_event_id)
            prev_membership = prev_member_event.membership

        # Check if the member should be allowed access via membership in a space.
        await self._event_auth_handler.check_restricted_join_rules(
            prev_state_ids,
            event.room_version,
            user_id,
            prev_membership,
        )

    @trace
    async def process_remote_join(
        self,
        origin: str,
        room_id: str,
        auth_events: List[EventBase],
        state: List[EventBase],
        event: EventBase,
        room_version: RoomVersion,
        partial_state: bool,
    ) -> int:
        """Persists the events returned by a send_join

        Checks the auth chain is valid (and passes auth checks) for the
        state and event. Then persists all of the events.
        Notifies about the persisted events where appropriate.

        Args:
            origin: Where the events came from
            room_id:
            auth_events
            state
            event
            room_version: The room version we expect this room to have, and
                will raise if it doesn't match the version in the create event.
            partial_state: True if the state omits non-critical membership events

        Returns:
            The stream ID after which all events have been persisted.

        Raises:
            SynapseError if the response is in some way invalid.
            PartialStateConflictError if the homeserver is already in the room and it
                has been un-partial stated.
        """
        create_event = None
        for e in state:
            if (e.type, e.state_key) == (EventTypes.Create, ""):
                create_event = e
                break

        if create_event is None:
            # If the state doesn't have a create event then the room is
            # invalid, and it would fail auth checks anyway.
            raise SynapseError(400, "No create event in state")

        room_version_id = create_event.content.get(
            "room_version", RoomVersions.V1.identifier
        )

        if room_version.identifier != room_version_id:
            raise SynapseError(400, "Room version mismatch")

        # persist the auth chain and state events.
        #
        # any invalid events here will be marked as rejected, and we'll carry on.
        #
        # any events whose auth events are missing (ie, not in the send_join response,
        # and not already in our db) will just be ignored. This is correct behaviour,
        # because the reason that auth_events are missing might be due to us being
        # unable to validate their signatures. The fact that we can't validate their
        # signatures right now doesn't mean that we will *never* be able to, so it
        # is premature to reject them.
        #
        await self._auth_and_persist_outliers(
            room_id, itertools.chain(auth_events, state)
        )

        # and now persist the join event itself.
        logger.info(
            "Peristing join-via-remote %s (partial_state: %s)", event, partial_state
        )
        with nested_logging_context(suffix=event.event_id):
            if partial_state:
                # When handling a second partial state join into a partial state room,
                # the returned state will exclude the membership from the first join. To
                # preserve prior memberships, we try to compute the partial state before
                # the event ourselves if we know about any of the prev events.
                #
                # When we don't know about any of the prev events, it's fine to just use
                # the returned state, since the new join will create a new forward
                # extremity, and leave the forward extremity containing our prior
                # memberships alone.
                prev_event_ids = set(event.prev_event_ids())
                seen_event_ids = await self._store.have_events_in_timeline(
                    prev_event_ids
                )
                missing_event_ids = prev_event_ids - seen_event_ids

                state_maps_to_resolve: List[StateMap[str]] = []

                # Fetch the state after the prev events that we know about.
                state_maps_to_resolve.extend(
                    (
                        await self._state_storage_controller.get_state_groups_ids(
                            room_id, seen_event_ids, await_full_state=False
                        )
                    ).values()
                )

                # When there are prev events we do not have the state for, we state
                # resolve with the state returned by the remote homeserver.
                if missing_event_ids or len(state_maps_to_resolve) == 0:
                    state_maps_to_resolve.append(
                        {(e.type, e.state_key): e.event_id for e in state}
                    )

                state_ids_before_event = (
                    await self._state_resolution_handler.resolve_events_with_store(
                        event.room_id,
                        room_version.identifier,
                        state_maps_to_resolve,
                        event_map=None,
                        state_res_store=StateResolutionStore(
                            self._store, self._state_deletion_store
                        ),
                    )
                )
            else:
                state_ids_before_event = {
                    (e.type, e.state_key): e.event_id for e in state
                }

            context = await self._state_handler.compute_event_context(
                event,
                state_ids_before_event=state_ids_before_event,
                partial_state=partial_state,
            )

            await self._check_event_auth(origin, event, context)
            if context.rejected:
                raise SynapseError(403, "Join event was rejected")

            # the remote server is responsible for sending our join event to the rest
            # of the federation. Indeed, attempting to do so will result in problems
            # when we try to look up the state before the join (to get the server list)
            # and discover that we do not have it.
            event.internal_metadata.proactively_send = False

            stream_id_after_persist = await self.persist_events_and_notify(
                room_id, [(event, context)]
            )

            return stream_id_after_persist

    async def update_state_for_partial_state_event(
        self, destination: str, event: EventBase
    ) -> None:
        """Recalculate the state at an event as part of a de-partial-stating process

        Args:
            destination: server to request full state from
            event: partial-state event to be de-partial-stated

        Raises:
            FederationPullAttemptBackoffError if we are are deliberately not attempting
                to pull the given event over federation because we've already done so
                recently and are backing off.
            FederationError if we fail to request state from the remote server.
        """
        logger.info("Updating state for %s", event.event_id)
        with nested_logging_context(suffix=event.event_id):
            # if we have all the event's prev_events, then we can work out the
            # state based on their states. Otherwise, we request it from the destination
            # server.
            #
            # This is the same operation as we do when we receive a regular event
            # over federation.
            context = await self._compute_event_context_with_maybe_missing_prevs(
                destination, event
            )
            if context.partial_state:
                # this can happen if some or all of the event's prev_events still have
                # partial state. We were careful to only pick events from the db without
                # partial-state prev events, so that implies that a prev event has
                # been persisted (with partial state) since we did the query.
                #
                # So, let's just ignore `event` for now; when we re-run the db query
                # we should instead get its partial-state prev event, which we will
                # de-partial-state, and then come back to event.
                logger.warning(
                    "%s still has prev_events with partial state: can't de-partial-state it yet",
                    event.event_id,
                )
                return

            # since the state at this event has changed, we should now re-evaluate
            # whether it should have been rejected. We must already have all of the
            # auth events (from last time we went round this path), so there is no
            # need to pass the origin.
            await self._check_event_auth(None, event, context)

            await self._store.update_state_for_partial_state_event(event, context)
            self._state_storage_controller.notify_event_un_partial_stated(
                event.event_id
            )
            # Notify that there's a new row in the un_partial_stated_events stream.
            self._notifier.notify_replication()

    @trace
    async def backfill(
        self, dest: str, room_id: str, limit: int, extremities: StrCollection
    ) -> None:
        """Trigger a backfill request to `dest` for the given `room_id`

        This will attempt to get more events from the remote. If the other side
        has no new events to offer, this will return an empty list.

        As the events are received, we check their signatures, and also do some
        sanity-checking on them. If any of the backfilled events are invalid,
        this method throws a SynapseError.

        We might also raise an InvalidResponseError if the response from the remote
        server is just bogus.

        TODO: make this more useful to distinguish failures of the remote
        server from invalid events (there is probably no point in trying to
        re-fetch invalid events from every other HS in the room.)
        """
        if self._is_mine_server_name(dest):
            raise SynapseError(400, "Can't backfill from self.")

        events = await self._federation_client.backfill(
            dest, room_id, limit=limit, extremities=extremities
        )

        if not events:
            return

        with backfill_processing_after_timer.time():
            # if there are any events in the wrong room, the remote server is buggy and
            # should not be trusted.
            for ev in events:
                if ev.room_id != room_id:
                    raise InvalidResponseError(
                        f"Remote server {dest} returned event {ev.event_id} which is in "
                        f"room {ev.room_id}, when we were backfilling in {room_id}"
                    )

            await self._process_pulled_events(
                dest,
                events,
                backfilled=True,
            )

    @trace
    async def _get_missing_events_for_pdu(
        self, origin: str, pdu: EventBase, prevs: Set[str], min_depth: int
    ) -> None:
        """
        Args:
            origin: Origin of the pdu. Will be called to get the missing events
            pdu: received pdu
            prevs: List of event ids which we are missing
            min_depth: Minimum depth of events to return.
        """

        room_id = pdu.room_id
        event_id = pdu.event_id

        seen = await self._store.have_events_in_timeline(prevs)

        if not prevs - seen:
            return

        latest_frozen = await self._store.get_latest_event_ids_in_room(room_id)

        # We add the prev events that we have seen to the latest
        # list to ensure the remote server doesn't give them to us
        latest = seen | latest_frozen

        logger.info(
            "Requesting missing events between %s and %s",
            shortstr(latest),
            event_id,
        )

        # XXX: we set timeout to 10s to help workaround
        # https://github.com/matrix-org/synapse/issues/1733.
        # The reason is to avoid holding the linearizer lock
        # whilst processing inbound /send transactions, causing
        # FDs to stack up and block other inbound transactions
        # which empirically can currently take up to 30 minutes.
        #
        # N.B. this explicitly disables retry attempts.
        #
        # N.B. this also increases our chances of falling back to
        # fetching fresh state for the room if the missing event
        # can't be found, which slightly reduces our security.
        # it may also increase our DAG extremity count for the room,
        # causing additional state resolution?  See https://github.com/matrix-org/synapse/issues/1760.
        # However, fetching state doesn't hold the linearizer lock
        # apparently.
        #
        # see https://github.com/matrix-org/synapse/pull/1744
        #
        # ----
        #
        # Update richvdh 2018/09/18: There are a number of problems with timing this
        # request out aggressively on the client side:
        #
        # - it plays badly with the server-side rate-limiter, which starts tarpitting you
        #   if you send too many requests at once, so you end up with the server carefully
        #   working through the backlog of your requests, which you have already timed
        #   out.
        #
        # - for this request in particular, we now (as of
        #   https://github.com/matrix-org/synapse/pull/3456) reject any PDUs where the
        #   server can't produce a plausible-looking set of prev_events - so we becone
        #   much more likely to reject the event.
        #
        # - contrary to what it says above, we do *not* fall back to fetching fresh state
        #   for the room if get_missing_events times out. Rather, we give up processing
        #   the PDU whose prevs we are missing, which then makes it much more likely that
        #   we'll end up back here for the *next* PDU in the list, which exacerbates the
        #   problem.
        #
        # - the aggressive 10s timeout was introduced to deal with incoming federation
        #   requests taking 8 hours to process. It's not entirely clear why that was going
        #   on; certainly there were other issues causing traffic storms which are now
        #   resolved, and I think in any case we may be more sensible about our locking
        #   now. We're *certainly* more sensible about our logging.
        #
        # All that said: Let's try increasing the timeout to 60s and see what happens.

        try:
            missing_events = await self._federation_client.get_missing_events(
                origin,
                room_id,
                earliest_events_ids=list(latest),
                latest_events=[pdu],
                limit=10,
                min_depth=min_depth,
                timeout=60000,
            )
        except (RequestSendFailed, HttpResponseException, NotRetryingDestination) as e:
            # We failed to get the missing events, but since we need to handle
            # the case of `get_missing_events` not returning the necessary
            # events anyway, it is safe to simply log the error and continue.
            logger.warning("Failed to get prev_events: %s", e)
            return

        logger.info("Got %d prev_events", len(missing_events))
        await self._process_pulled_events(origin, missing_events, backfilled=False)

    @trace
    async def _process_pulled_events(
        self, origin: str, events: Collection[EventBase], backfilled: bool
    ) -> None:
        """Process a batch of events we have pulled from a remote server

        Pulls in any events required to auth the events, persists the received events,
        and notifies clients, if appropriate.

        Assumes the events have already had their signatures and hashes checked.

        Params:
            origin: The server we received these events from
            events: The received events.
            backfilled: True if this is part of a historical batch of events (inhibits
                notification to clients, and validation of device keys.)
        """
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "event_ids",
            str([event.event_id for event in events]),
        )
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "event_ids.length",
            str(len(events)),
        )
        set_tag(SynapseTags.FUNC_ARG_PREFIX + "backfilled", str(backfilled))
        logger.debug(
            "processing pulled backfilled=%s events=%s",
            backfilled,
            [
                "event_id=%s,depth=%d,body=%s,prevs=%s\n"
                % (
                    event.event_id,
                    event.depth,
                    event.content.get("body", event.type),
                    event.prev_event_ids(),
                )
                for event in events
            ],
        )

        # Check if we already any of these have these events.
        # Note: we currently make a lookup in the database directly here rather than
        # checking the event cache, due to:
        # https://github.com/matrix-org/synapse/issues/13476
        existing_events_map = await self._store._get_events_from_db(
            [event.event_id for event in events]
        )

        new_events: List[EventBase] = []
        for event in events:
            event_id = event.event_id

            # If we've already seen this event ID...
            if event_id in existing_events_map:
                existing_event = existing_events_map[event_id]

                # ...and the event itself was not previously stored as an outlier...
                if not existing_event.event.internal_metadata.is_outlier():
                    # ...then there's no need to persist it. We have it already.
                    logger.info(
                        "_process_pulled_event: Ignoring received event %s which we "
                        "have already seen",
                        event.event_id,
                    )
                    continue

                # While we have seen this event before, it was stored as an outlier.
                # We'll now persist it as a non-outlier.
                logger.info("De-outliering event %s", event_id)

            # Continue on with the events that are new to us.
            new_events.append(event)

        set_tag(
            SynapseTags.RESULT_PREFIX + "new_events.length",
            str(len(new_events)),
        )

        @trace
        async def _process_new_pulled_events(new_events: Collection[EventBase]) -> None:
            # We want to sort these by depth so we process them and tell clients about
            # them in order. It's also more efficient to backfill this way (`depth`
            # ascending) because one backfill event is likely to be the `prev_event` of
            # the next event we're going to process.
            sorted_events = sorted(new_events, key=lambda x: x.depth)
            for ev in sorted_events:
                with nested_logging_context(ev.event_id):
                    await self._process_pulled_event(origin, ev, backfilled=backfilled)

        # Check if we've already tried to process these events at some point in the
        # past. We aren't concerned with the expontntial backoff here, just whether it
        # has failed to be processed before.
        event_ids_with_failed_pull_attempts = (
            await self._store.get_event_ids_with_failed_pull_attempts(
                [event.event_id for event in new_events]
            )
        )

        events_with_failed_pull_attempts, fresh_events = partition(
            new_events, lambda e: e.event_id in event_ids_with_failed_pull_attempts
        )
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "events_with_failed_pull_attempts",
            str(event_ids_with_failed_pull_attempts),
        )
        set_tag(
            SynapseTags.RESULT_PREFIX + "events_with_failed_pull_attempts.length",
            str(len(events_with_failed_pull_attempts)),
        )
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "fresh_events",
            str([event.event_id for event in fresh_events]),
        )
        set_tag(
            SynapseTags.RESULT_PREFIX + "fresh_events.length",
            str(len(fresh_events)),
        )

        # Process previously failed backfill events in the background to not waste
        # time on something that is likely to fail again.
        if len(events_with_failed_pull_attempts) > 0:
            run_as_background_process(
                "_process_new_pulled_events_with_failed_pull_attempts",
                _process_new_pulled_events,
                events_with_failed_pull_attempts,
            )

        # We can optimistically try to process and wait for the event to be fully
        # persisted if we've never tried before.
        if len(fresh_events) > 0:
            await _process_new_pulled_events(fresh_events)

    @trace
    @tag_args
    async def _process_pulled_event(
        self, origin: str, event: EventBase, backfilled: bool
    ) -> None:
        """Process a single event that we have pulled from a remote server

        Pulls in any events required to auth the event, persists the received event,
        and notifies clients, if appropriate.

        Assumes the event has already had its signatures and hashes checked.

        This is somewhat equivalent to on_receive_pdu, but applies somewhat different
        logic in the case that we are missing prev_events (in particular, it just
        requests the state at that point, rather than triggering a get_missing_events) -
        so is appropriate when we have pulled the event from a remote server, rather
        than having it pushed to us.

        Params:
            origin: The server we received this event from
            events: The received event
            backfilled: True if this is part of a historical batch of events (inhibits
                notification to clients, and validation of device keys.)
        """
        logger.info("Processing pulled event %s", event)

        # This function should not be used to persist outliers (use something
        # else) because this does a bunch of operations that aren't necessary
        # (extra work; in particular, it makes sure we have all the prev_events
        # and resolves the state across those prev events). If you happen to run
        # into a situation where the event you're trying to process/backfill is
        # marked as an `outlier`, then you should update that spot to return an
        # `EventBase` copy that doesn't have `outlier` flag set.
        #
        # `EventBase` is used to represent both an event we have not yet
        # persisted, and one that we have persisted and now keep in the cache.
        # In an ideal world this method would only be called with the first type
        # of event, but it turns out that's not actually the case and for
        # example, you could get an event from cache that is marked as an
        # `outlier` (fix up that spot though).
        assert not event.internal_metadata.is_outlier(), (
            "Outlier event passed to _process_pulled_event. "
            "To persist an event as a non-outlier, make sure to pass in a copy without `event.internal_metadata.outlier = true`."
        )

        event_id = event.event_id

        try:
            self._sanity_check_event(event)
        except SynapseError as err:
            logger.warning("Event %s failed sanity check: %s", event_id, err)
            await self._store.record_event_failed_pull_attempt(
                event.room_id, event_id, str(err)
            )
            return

        try:
            try:
                context = await self._compute_event_context_with_maybe_missing_prevs(
                    origin, event
                )
                await self._process_received_pdu(
                    origin,
                    event,
                    context,
                    backfilled=backfilled,
                )
            except PartialStateConflictError:
                # The room was un-partial stated while we were processing the event.
                # Try once more, with full state this time.
                context = await self._compute_event_context_with_maybe_missing_prevs(
                    origin, event
                )

                # We ought to have full state now, barring some unlikely race where we left and
                # rejoned the room in the background.
                if context.partial_state:
                    raise AssertionError(
                        f"Event {event.event_id} still has a partial resolved state "
                        f"after room {event.room_id} was un-partial stated"
                    )

                await self._process_received_pdu(
                    origin,
                    event,
                    context,
                    backfilled=backfilled,
                )
        except FederationPullAttemptBackoffError as exc:
            # Log a warning about why we failed to process the event (the error message
            # for `FederationPullAttemptBackoffError` is pretty good)
            logger.warning("_process_pulled_event: %s", exc)
            # We do not record a failed pull attempt when we backoff fetching a missing
            # `prev_event` because not being able to fetch the `prev_events` just means
            # we won't be able to de-outlier the pulled event. But we can still use an
            # `outlier` in the state/auth chain for another event. So we shouldn't stop
            # a downstream event from trying to pull it.
            #
            # This avoids a cascade of backoff for all events in the DAG downstream from
            # one event backoff upstream.
        except FederationError as e:
            await self._store.record_event_failed_pull_attempt(
                event.room_id, event_id, str(e)
            )

            if e.code == 403:
                logger.warning("Pulled event %s failed history check.", event_id)
            else:
                raise

    @trace
    async def _compute_event_context_with_maybe_missing_prevs(
        self, dest: str, event: EventBase
    ) -> EventContext:
        """Build an EventContext structure for a non-outlier event whose prev_events may
        be missing.

        This is used when we have pulled a batch of events from a remote server, and may
        not have all the prev_events.

        To build an EventContext, we need to calculate the state before the event. If we
        already have all the prev_events for `event`, we can simply use the state after
        the prev_events to calculate the state before `event`.

        Otherwise, the missing prevs become new backwards extremities, and we fall back
        to asking the remote server for the state after each missing `prev_event`,
        and resolving across them.

        That's ok provided we then resolve the state against other bits of the DAG
        before using it - in other words, that the received event `event` is not going
        to become the only forwards_extremity in the room (which will ensure that you
        can't just take over a room by sending an event, withholding its prev_events,
        and declaring yourself to be an admin in the subsequent state request).

        In other words: we should only call this method if `event` has been *pulled*
        as part of a batch of missing prev events, or similar.

        Params:
            dest: the remote server to ask for state at the missing prevs. Typically,
                this will be the server we got `event` from.
            event: an event to check for missing prevs.

        Returns:
            The event context.

        Raises:
            FederationPullAttemptBackoffError if we are are deliberately not attempting
                to pull one of the given event's `prev_event`s over federation because
                we've already done so recently and are backing off.
            FederationError if we fail to get the state from the remote server after any
                missing `prev_event`s.
        """
        room_id = event.room_id
        event_id = event.event_id

        prevs = set(event.prev_event_ids())
        seen = await self._store.have_events_in_timeline(prevs)
        missing_prevs = prevs - seen

        # If we've already recently attempted to pull this missing event, don't
        # try it again so soon. Since we have to fetch all of the prev_events, we can
        # bail early here if we find any to ignore.
        prevs_with_pull_backoff = (
            await self._store.get_event_ids_to_not_pull_from_backoff(
                room_id, missing_prevs
            )
        )
        if len(prevs_with_pull_backoff) > 0:
            raise FederationPullAttemptBackoffError(
                event_ids=prevs_with_pull_backoff.keys(),
                message=(
                    f"While computing context for event={event_id}, not attempting to "
                    f"pull missing prev_events={list(prevs_with_pull_backoff.keys())} "
                    "because we already tried to pull recently (backing off)."
                ),
                retry_after_ms=(
                    max(prevs_with_pull_backoff.values()) - self._clock.time_msec()
                ),
            )

        if not missing_prevs:
            return await self._state_handler.compute_event_context(event)

        logger.info(
            "Event %s is missing prev_events %s: calculating state for a "
            "backwards extremity",
            event_id,
            shortstr(missing_prevs),
        )
        # Calculate the state after each of the previous events, and
        # resolve them to find the correct state at the current event.

        try:
            # Determine whether we may be about to retrieve partial state
            # Events may be un-partial stated right after we compute the partial state
            # flag, but that's okay, as long as the flag errs on the conservative side.
            partial_state_flags = await self._store.get_partial_state_events(seen)
            partial_state = any(partial_state_flags.values())

            # state_maps is a list of mappings from (type, state_key) to event_id
            state_maps: List[StateMap[str]] = []

            # Ask the remote server for the states we don't
            # know about
            for p in missing_prevs:
                logger.info("Requesting state after missing prev_event %s", p)

                with nested_logging_context(p):
                    # note that if any of the missing prevs share missing state or
                    # auth events, the requests to fetch those events are deduped
                    # by the get_pdu_cache in federation_client.
                    remote_state_map = (
                        await self._get_state_ids_after_missing_prev_event(
                            dest, room_id, p
                        )
                    )

                    state_maps.append(remote_state_map)

            # Get the state of the events we know about. We do this *after*
            # trying to fetch missing state over federation as that might fail
            # and then we can skip loading the local state.
            ours = await self._state_storage_controller.get_state_groups_ids(
                room_id, seen, await_full_state=False
            )
            state_maps.extend(ours.values())

            # we don't need this any more, let's delete it.
            del ours

            room_version = await self._store.get_room_version_id(room_id)
            state_map = await self._state_resolution_handler.resolve_events_with_store(
                room_id,
                room_version,
                state_maps,
                event_map={event_id: event},
                state_res_store=StateResolutionStore(
                    self._store, self._state_deletion_store
                ),
            )

        except Exception as e:
            logger.warning(
                "Error attempting to resolve state at missing prev_events: %s", e
            )
            raise FederationError(
                "ERROR",
                403,
                "We can't get valid state history.",
                affected=event_id,
            )
        return await self._state_handler.compute_event_context(
            event, state_ids_before_event=state_map, partial_state=partial_state
        )

    @trace
    @tag_args
    async def _get_state_ids_after_missing_prev_event(
        self,
        destination: str,
        room_id: str,
        event_id: str,
    ) -> StateMap[str]:
        """Requests all of the room state at a given event from a remote homeserver.

        Args:
            destination: The remote homeserver to query for the state.
            room_id: The id of the room we're interested in.
            event_id: The id of the event we want the state at.

        Returns:
            The event ids of the state *after* the given event.

        Raises:
            InvalidResponseError: if the remote homeserver's response contains fields
                of the wrong type.
        """

        # It would be better if we could query the difference from our known
        # state to the given `event_id` so the sending server doesn't have to
        # send as much and we don't have to process as many events. For example
        # in a room like #matrix:matrix.org, we get 200k events (77k state_events, 122k
        # auth_events) from this call.
        #
        # Tracked by https://github.com/matrix-org/synapse/issues/13618
        (
            state_event_ids,
            auth_event_ids,
        ) = await self._federation_client.get_room_state_ids(
            destination, room_id, event_id=event_id
        )

        logger.debug(
            "state_ids returned %i state events, %i auth events",
            len(state_event_ids),
            len(auth_event_ids),
        )

        # Start by checking events we already have in the DB
        desired_events = set(state_event_ids)
        desired_events.add(event_id)
        logger.debug("Fetching %i events from cache/store", len(desired_events))
        have_events = await self._store.have_seen_events(room_id, desired_events)

        missing_desired_event_ids = desired_events - have_events
        logger.debug(
            "We are missing %i events (got %i)",
            len(missing_desired_event_ids),
            len(have_events),
        )

        # We probably won't need most of the auth events, so let's just check which
        # we have for now, rather than thrashing the event cache with them all
        # unnecessarily.

        # TODO: we probably won't actually need all of the auth events, since we
        #   already have a bunch of the state events. It would be nice if the
        #   federation api gave us a way of finding out which we actually need.

        missing_auth_event_ids = set(auth_event_ids) - have_events
        missing_auth_event_ids.difference_update(
            await self._store.have_seen_events(room_id, missing_auth_event_ids)
        )
        logger.debug("We are also missing %i auth events", len(missing_auth_event_ids))

        missing_event_ids = missing_desired_event_ids | missing_auth_event_ids

        set_tag(
            SynapseTags.RESULT_PREFIX + "missing_auth_event_ids",
            str(missing_auth_event_ids),
        )
        set_tag(
            SynapseTags.RESULT_PREFIX + "missing_auth_event_ids.length",
            str(len(missing_auth_event_ids)),
        )
        set_tag(
            SynapseTags.RESULT_PREFIX + "missing_desired_event_ids",
            str(missing_desired_event_ids),
        )
        set_tag(
            SynapseTags.RESULT_PREFIX + "missing_desired_event_ids.length",
            str(len(missing_desired_event_ids)),
        )

        # Making an individual request for each of 1000s of events has a lot of
        # overhead. On the other hand, we don't really want to fetch all of the events
        # if we already have most of them.
        #
        # As an arbitrary heuristic, if we are missing more than 10% of the events, then
        # we fetch the whole state.
        #
        # TODO: might it be better to have an API which lets us do an aggregate event
        #   request
        if (len(missing_event_ids) * 10) >= len(auth_event_ids) + len(state_event_ids):
            logger.debug("Requesting complete state from remote")
            await self._get_state_and_persist(destination, room_id, event_id)
        else:
            logger.debug("Fetching %i events from remote", len(missing_event_ids))
            await self._get_events_and_persist(
                destination=destination, room_id=room_id, event_ids=missing_event_ids
            )

        # We now need to fill out the state map, which involves fetching the
        # type and state key for each event ID in the state.
        state_map = {}

        event_metadata = await self._store.get_metadata_for_events(state_event_ids)
        for state_event_id, metadata in event_metadata.items():
            if metadata.room_id != room_id:
                # This is a bogus situation, but since we may only discover it a long time
                # after it happened, we try our best to carry on, by just omitting the
                # bad events from the returned state set.
                #
                # This can happen if a remote server claims that the state or
                # auth_events at an event in room A are actually events in room B
                logger.warning(
                    "Remote server %s claims event %s in room %s is an auth/state "
                    "event in room %s",
                    destination,
                    state_event_id,
                    metadata.room_id,
                    room_id,
                )
                continue

            if metadata.state_key is None:
                logger.warning(
                    "Remote server gave us non-state event in state: %s", state_event_id
                )
                continue

            state_map[(metadata.event_type, metadata.state_key)] = state_event_id

        # if we couldn't get the prev event in question, that's a problem.
        remote_event = await self._store.get_event(
            event_id,
            allow_none=True,
            allow_rejected=True,
            redact_behaviour=EventRedactBehaviour.as_is,
        )
        if not remote_event:
            raise Exception("Unable to get missing prev_event %s" % (event_id,))

        # missing state at that event is a warning, not a blocker
        # XXX: this doesn't sound right? it means that we'll end up with incomplete
        #   state.
        failed_to_fetch = desired_events - event_metadata.keys()
        # `event_id` could be missing from `event_metadata` because it's not necessarily
        # a state event. We've already checked that we've fetched it above.
        failed_to_fetch.discard(event_id)
        if failed_to_fetch:
            logger.warning(
                "Failed to fetch missing state events for %s %s",
                event_id,
                failed_to_fetch,
            )
            set_tag(
                SynapseTags.RESULT_PREFIX + "failed_to_fetch",
                str(failed_to_fetch),
            )
            set_tag(
                SynapseTags.RESULT_PREFIX + "failed_to_fetch.length",
                str(len(failed_to_fetch)),
            )

        if remote_event.is_state() and remote_event.rejected_reason is None:
            state_map[(remote_event.type, remote_event.state_key)] = (
                remote_event.event_id
            )

        return state_map

    @trace
    @tag_args
    async def _get_state_and_persist(
        self, destination: str, room_id: str, event_id: str
    ) -> None:
        """Get the complete room state at a given event, and persist any new events
        as outliers"""
        room_version = await self._store.get_room_version(room_id)
        auth_events, state_events = await self._federation_client.get_room_state(
            destination, room_id, event_id=event_id, room_version=room_version
        )
        logger.info("/state returned %i events", len(auth_events) + len(state_events))

        await self._auth_and_persist_outliers(
            room_id, itertools.chain(auth_events, state_events)
        )

        # we also need the event itself.
        if not await self._store.have_seen_event(room_id, event_id):
            await self._get_events_and_persist(
                destination=destination, room_id=room_id, event_ids=(event_id,)
            )

    @trace
    async def _process_received_pdu(
        self,
        origin: str,
        event: EventBase,
        context: EventContext,
        backfilled: bool = False,
    ) -> None:
        """Called when we have a new non-outlier event.

        This is called when we have a new event to add to the room DAG. This can be
        due to:
           * events received directly via a /send request
           * events retrieved via get_missing_events after a /send request
           * events backfilled after a client request.

        It's not currently used for events received from incoming send_{join,knock,leave}
        requests (which go via on_send_membership_event), nor for joins created by a
        remote join dance (which go via process_remote_join).

        We need to do auth checks and put it through the StateHandler.

        Args:
            origin: server sending the event

            event: event to be persisted

            context: The `EventContext` to persist the event with.

            backfilled: True if this is part of a historical batch of events (inhibits
                notification to clients, and validation of device keys.)

        PartialStateConflictError: if the room was un-partial stated in between
            computing the state at the event and persisting it. The caller should
            recompute `context` and retry exactly once when this happens.
        """
        logger.debug("Processing event: %s", event)
        assert not event.internal_metadata.outlier

        try:
            await self._check_event_auth(origin, event, context)
        except AuthError as e:
            # This happens only if we couldn't find the auth events. We'll already have
            # logged a warning, so now we just convert to a FederationError.
            raise FederationError("ERROR", e.code, e.msg, affected=event.event_id)

        if not backfilled and not context.rejected:
            # For new (non-backfilled and non-outlier) events we check if the event
            # passes auth based on the current state. If it doesn't then we
            # "soft-fail" the event.
            await self._check_for_soft_fail(event, context=context, origin=origin)

        await self._run_push_actions_and_persist_event(event, context, backfilled)

        if backfilled or context.rejected:
            return

        await self._maybe_kick_guest_users(event)

        # For encrypted messages we check that we know about the sending device,
        # if we don't then we mark the device cache for that user as stale.
        if event.type == EventTypes.Encrypted:
            device_id = event.content.get("device_id")
            sender_key = event.content.get("sender_key")

            cached_devices = await self._store.get_cached_devices_for_user(event.sender)

            resync = False  # Whether we should resync device lists.

            device = None
            if device_id is not None:
                device = cached_devices.get(device_id)
                if device is None:
                    logger.info(
                        "Received event from remote device not in our cache: %s %s",
                        event.sender,
                        device_id,
                    )
                    resync = True

            # We also check if the `sender_key` matches what we expect.
            if sender_key is not None:
                # Figure out what sender key we're expecting. If we know the
                # device and recognize the algorithm then we can work out the
                # exact key to expect. Otherwise check it matches any key we
                # have for that device.

                current_keys: Container[str] = []

                if device:
                    keys = device.get("keys", {}).get("keys", {})

                    if (
                        event.content.get("algorithm")
                        == RoomEncryptionAlgorithms.MEGOLM_V1_AES_SHA2
                    ):
                        # For this algorithm we expect a curve25519 key.
                        key_name = "curve25519:%s" % (device_id,)
                        current_keys = [keys.get(key_name)]
                    else:
                        # We don't know understand the algorithm, so we just
                        # check it matches a key for the device.
                        current_keys = keys.values()
                elif device_id:
                    # We don't have any keys for the device ID.
                    pass
                else:
                    # The event didn't include a device ID, so we just look for
                    # keys across all devices.
                    current_keys = [
                        key
                        for device in cached_devices.values()
                        for key in device.get("keys", {}).get("keys", {}).values()
                    ]

                # We now check that the sender key matches (one of) the expected
                # keys.
                if sender_key not in current_keys:
                    logger.info(
                        "Received event from remote device with unexpected sender key: %s %s: %s",
                        event.sender,
                        device_id or "<no device_id>",
                        sender_key,
                    )
                    resync = True

            if resync:
                run_as_background_process(
                    "resync_device_due_to_pdu",
                    self._resync_device,
                    event.sender,
                )

    async def _resync_device(self, sender: str) -> None:
        """We have detected that the device list for the given user may be out
        of sync, so we try and resync them.
        """

        try:
            await self._store.mark_remote_users_device_caches_as_stale((sender,))

            # Immediately attempt a resync in the background
            if self._config.worker.worker_app:
                await self._multi_user_device_resync(user_ids=[sender])
            else:
                await self._device_list_updater.multi_user_device_resync(
                    user_ids=[sender]
                )
        except Exception:
            logger.exception("Failed to resync device for %s", sender)

    async def backfill_event_id(
        self, destinations: StrCollection, room_id: str, event_id: str
    ) -> PulledPduInfo:
        """Backfill a single event and persist it as a non-outlier which means
        we also pull in all of the state and auth events necessary for it.

        Args:
            destination: The homeserver to pull the given event_id from.
            room_id: The room where the event is from.
            event_id: The event ID to backfill.

        Raises:
            FederationError if we are unable to find the event from the destination
        """
        logger.info("backfill_event_id: event_id=%s", event_id)

        room_version = await self._store.get_room_version(room_id)

        pulled_pdu_info = await self._federation_client.get_pdu(
            destinations,
            event_id,
            room_version,
        )

        if not pulled_pdu_info:
            raise FederationError(
                "ERROR",
                404,
                f"Unable to find event_id={event_id} from remote servers to backfill.",
                affected=event_id,
            )

        # Persist the event we just fetched, including pulling all of the state
        # and auth events to de-outlier it. This also sets up the necessary
        # `state_groups` for the event.
        await self._process_pulled_events(
            pulled_pdu_info.pull_origin,
            [pulled_pdu_info.pdu],
            # Prevent notifications going to clients
            backfilled=True,
        )

        return pulled_pdu_info

    @trace
    @tag_args
    async def _get_events_and_persist(
        self, destination: str, room_id: str, event_ids: StrCollection
    ) -> None:
        """Fetch the given events from a server, and persist them as outliers.

        This function *does not* recursively get missing auth events of the
        newly fetched events. Callers must include in the `event_ids` argument
        any missing events from the auth chain.

        Logs a warning if we can't find the given event.
        """

        room_version = await self._store.get_room_version(room_id)

        events: List[EventBase] = []

        async def get_event(event_id: str) -> None:
            with nested_logging_context(event_id):
                try:
                    pulled_pdu_info = await self._federation_client.get_pdu(
                        [destination],
                        event_id,
                        room_version,
                    )
                    if pulled_pdu_info is None:
                        logger.warning(
                            "Server %s didn't return event %s",
                            destination,
                            event_id,
                        )
                        return
                    events.append(pulled_pdu_info.pdu)

                except Exception as e:
                    logger.warning(
                        "Error fetching missing state/auth event %s: %s %s",
                        event_id,
                        type(e),
                        e,
                    )

        await concurrently_execute(get_event, event_ids, 5)
        logger.info("Fetched %i events of %i requested", len(events), len(event_ids))
        await self._auth_and_persist_outliers(room_id, events)

    @trace
    async def _auth_and_persist_outliers(
        self, room_id: str, events: Iterable[EventBase]
    ) -> None:
        """Persist a batch of outlier events fetched from remote servers.

        We first sort the events to make sure that we process each event's auth_events
        before the event itself.

        We then mark the events as outliers, persist them to the database, and, where
        appropriate (eg, an invite), awake the notifier.

        Params:
            room_id: the room that the events are meant to be in (though this has
               not yet been checked)
            events: the events that have been fetched
        """
        event_map = {event.event_id: event for event in events}

        event_ids = event_map.keys()
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "event_ids",
            str(event_ids),
        )
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "event_ids.length",
            str(len(event_ids)),
        )

        # filter out any events we have already seen. This might happen because
        # the events were eagerly pushed to us (eg, during a room join), or because
        # another thread has raced against us since we decided to request the event.
        #
        # This is just an optimisation, so it doesn't need to be watertight - the event
        # persister does another round of deduplication.
        seen_remotes = await self._store.have_seen_events(room_id, event_map.keys())
        for s in seen_remotes:
            event_map.pop(s, None)

        # XXX: it might be possible to kick this process off in parallel with fetching
        # the events.

        # We need to persist an event's auth events before the event.
        auth_graph = {
            ev.event_id: [e_id for e_id in ev.auth_event_ids() if e_id in event_map]
            for ev in event_map.values()
        }
        sorted_auth_event_ids = sorted_topologically(event_map.keys(), auth_graph)
        sorted_auth_events = [event_map[e_id] for e_id in sorted_auth_event_ids]
        logger.info(
            "Persisting %i remaining outliers: %s",
            len(sorted_auth_events),
            shortstr(e.event_id for e in sorted_auth_events),
        )

        # get all the auth events for all the events in this batch. By now, they should
        # have been persisted.
        auth_event_ids = {
            aid for event in sorted_auth_events for aid in event.auth_event_ids()
        }
        auth_map = {
            ev.event_id: ev
            for ev in sorted_auth_events
            if ev.event_id in auth_event_ids
        }

        missing_events = auth_event_ids.difference(auth_map)
        if missing_events:
            persisted_events = await self._store.get_events(
                missing_events,
                allow_rejected=True,
                redact_behaviour=EventRedactBehaviour.as_is,
            )
            auth_map.update(persisted_events)

        events_and_contexts_to_persist: List[Tuple[EventBase, EventContext]] = []

        async def prep(event: EventBase) -> None:
            with nested_logging_context(suffix=event.event_id):
                auth = []
                for auth_event_id in event.auth_event_ids():
                    ae = auth_map.get(auth_event_id)
                    if not ae:
                        # the fact we can't find the auth event doesn't mean it doesn't
                        # exist, which means it is premature to reject `event`. Instead we
                        # just ignore it for now.
                        logger.warning(
                            "Dropping event %s, which relies on auth_event %s, which could not be found",
                            event,
                            auth_event_id,
                        )
                        return
                    auth.append(ae)

                # we're not bothering about room state, so flag the event as an outlier.
                event.internal_metadata.outlier = True

                context = EventContext.for_outlier(self._storage_controllers)
                try:
                    validate_event_for_room_version(event)
                    await check_state_independent_auth_rules(
                        self._store, event, batched_auth_events=auth_map
                    )
                    check_state_dependent_auth_rules(event, auth)
                except AuthError as e:
                    logger.warning("Rejecting %r because %s", event, e)
                    context.rejected = RejectedReason.AUTH_ERROR
                except EventSizeError as e:
                    if e.unpersistable:
                        # This event is completely unpersistable.
                        raise e
                    # Otherwise, we are somewhat lenient and just persist the event
                    # as rejected, for moderate compatibility with older Synapse
                    # versions.
                    logger.warning("While validating received event %r: %s", event, e)
                    context.rejected = RejectedReason.OVERSIZED_EVENT

            events_and_contexts_to_persist.append((event, context))

        for i, event in enumerate(sorted_auth_events):
            await prep(event)

            # The above function is typically not async, and so won't yield to
            # the reactor. For large rooms let's yield to the reactor
            # occasionally to ensure we don't block other work.
            if (i + 1) % 1000 == 0:
                await self._clock.sleep(0)

        # Also persist the new event in batches for similar reasons as above.
        for batch in batch_iter(events_and_contexts_to_persist, 1000):
            await self.persist_events_and_notify(
                room_id,
                batch,
                # Mark these events as backfilled as they're historic events that will
                # eventually be backfilled. For example, missing events we fetch
                # during backfill should be marked as backfilled as well.
                backfilled=True,
            )

    @trace
    async def _check_event_auth(
        self, origin: Optional[str], event: EventBase, context: EventContext
    ) -> None:
        """
        Checks whether an event should be rejected (for failing auth checks).

        Args:
            origin: The host the event originates from. This is used to fetch
               any missing auth events. It can be set to None, but only if we are
               sure that we already have all the auth events.
            event: The event itself.
            context:
                The event context.

        Raises:
            AuthError if we were unable to find copies of the event's auth events.
               (Most other failures just cause us to set `context.rejected`.)
        """
        # This method should only be used for non-outliers
        assert not event.internal_metadata.outlier

        # first of all, check that the event itself is valid.
        try:
            validate_event_for_room_version(event)
        except AuthError as e:
            logger.warning("While validating received event %r: %s", event, e)
            # TODO: use a different rejected reason here?
            context.rejected = RejectedReason.AUTH_ERROR
            return
        except EventSizeError as e:
            if e.unpersistable:
                # This event is completely unpersistable.
                raise e
            # Otherwise, we are somewhat lenient and just persist the event
            # as rejected, for moderate compatibility with older Synapse
            # versions.
            logger.warning("While validating received event %r: %s", event, e)
            context.rejected = RejectedReason.OVERSIZED_EVENT
            return

        # next, check that we have all of the event's auth events.
        #
        # Note that this can raise AuthError, which we want to propagate to the
        # caller rather than swallow with `context.rejected` (since we cannot be
        # certain that there is a permanent problem with the event).
        claimed_auth_events = await self._load_or_fetch_auth_events_for_event(
            origin, event
        )
        set_tag(
            SynapseTags.RESULT_PREFIX + "claimed_auth_events",
            str([ev.event_id for ev in claimed_auth_events]),
        )
        set_tag(
            SynapseTags.RESULT_PREFIX + "claimed_auth_events.length",
            str(len(claimed_auth_events)),
        )

        # ... and check that the event passes auth at those auth events.
        # https://spec.matrix.org/v1.3/server-server-api/#checks-performed-on-receipt-of-a-pdu:
        #   4. Passes authorization rules based on the event’s auth events,
        #      otherwise it is rejected.
        try:
            await check_state_independent_auth_rules(self._store, event)
            check_state_dependent_auth_rules(event, claimed_auth_events)
        except AuthError as e:
            logger.warning(
                "While checking auth of %r against auth_events: %s", event, e
            )
            context.rejected = RejectedReason.AUTH_ERROR
            return

        # now check the auth rules pass against the room state before the event
        # https://spec.matrix.org/v1.3/server-server-api/#checks-performed-on-receipt-of-a-pdu:
        #   5. Passes authorization rules based on the state before the event,
        #      otherwise it is rejected.
        #
        # ... however, if we only have partial state for the room, then there is a good
        # chance that we'll be missing some of the state needed to auth the new event.
        # So, we state-resolve the auth events that we are given against the state that
        # we know about, which ensures things like bans are applied. (Note that we'll
        # already have checked we have all the auth events, in
        # _load_or_fetch_auth_events_for_event above)
        if context.partial_state:
            room_version = await self._store.get_room_version_id(event.room_id)

            local_state_id_map = await context.get_prev_state_ids()
            claimed_auth_events_id_map = {
                (ev.type, ev.state_key): ev.event_id for ev in claimed_auth_events
            }

            state_for_auth_id_map = (
                await self._state_resolution_handler.resolve_events_with_store(
                    event.room_id,
                    room_version,
                    [local_state_id_map, claimed_auth_events_id_map],
                    event_map=None,
                    state_res_store=StateResolutionStore(
                        self._store, self._state_deletion_store
                    ),
                )
            )
        else:
            event_types = event_auth.auth_types_for_event(event.room_version, event)
            state_for_auth_id_map = await context.get_prev_state_ids(
                StateFilter.from_types(event_types)
            )

        calculated_auth_event_ids = self._event_auth_handler.compute_auth_events(
            event, state_for_auth_id_map, for_verification=True
        )

        # if those are the same, we're done here.
        if collections.Counter(event.auth_event_ids()) == collections.Counter(
            calculated_auth_event_ids
        ):
            return

        # otherwise, re-run the auth checks based on what we calculated.
        calculated_auth_events = await self._store.get_events_as_list(
            calculated_auth_event_ids
        )

        # log the differences

        claimed_auth_event_map = {(e.type, e.state_key): e for e in claimed_auth_events}
        calculated_auth_event_map = {
            (e.type, e.state_key): e for e in calculated_auth_events
        }
        logger.info(
            "event's auth_events are different to our calculated auth_events. "
            "Claimed but not calculated: %s. Calculated but not claimed: %s",
            [
                ev
                for k, ev in claimed_auth_event_map.items()
                if k not in calculated_auth_event_map
                or calculated_auth_event_map[k].event_id != ev.event_id
            ],
            [
                ev
                for k, ev in calculated_auth_event_map.items()
                if k not in claimed_auth_event_map
                or claimed_auth_event_map[k].event_id != ev.event_id
            ],
        )

        try:
            check_state_dependent_auth_rules(event, calculated_auth_events)
        except AuthError as e:
            logger.warning(
                "While checking auth of %r against room state before the event: %s",
                event,
                e,
            )
            context.rejected = RejectedReason.AUTH_ERROR

    @trace
    async def _maybe_kick_guest_users(self, event: EventBase) -> None:
        if event.type != EventTypes.GuestAccess:
            return

        guest_access = event.content.get(EventContentFields.GUEST_ACCESS)
        if guest_access == GuestAccess.CAN_JOIN:
            return

        current_state = await self._storage_controllers.state.get_current_state(
            event.room_id
        )
        current_state_list = list(current_state.values())
        await self._get_room_member_handler().kick_guest_users(current_state_list)

    async def _check_for_soft_fail(
        self,
        event: EventBase,
        context: EventContext,
        origin: str,
    ) -> None:
        """Checks if we should soft fail the event; if so, marks the event as
        such.

        Does nothing for events in rooms with partial state, since we may not have an
        accurate membership event for the sender in the current state.

        Args:
            event
            context: The `EventContext` which we are about to persist the event with.
            origin: The host the event originates from.
        """
        if await self._store.is_partial_state_room(event.room_id):
            # We might not know the sender's membership in the current state, so don't
            # soft fail anything. Even if we do have a membership for the sender in the
            # current state, it may have been derived from state resolution between
            # partial and full state and may not be accurate.
            return

        extrem_ids = await self._store.get_latest_event_ids_in_room(event.room_id)
        prev_event_ids = set(event.prev_event_ids())

        if extrem_ids == prev_event_ids:
            # If they're the same then the current state is the same as the
            # state at the event, so no point rechecking auth for soft fail.
            return

        room_version = await self._store.get_room_version_id(event.room_id)
        room_version_obj = KNOWN_ROOM_VERSIONS[room_version]

        # The event types we want to pull from the "current" state.
        auth_types = auth_types_for_event(room_version_obj, event)

        # Calculate the "current state".
        seen_event_ids = await self._store.have_events_in_timeline(prev_event_ids)
        has_missing_prevs = bool(prev_event_ids - seen_event_ids)
        if has_missing_prevs:
            # We don't have all the prev_events of this event, which means we have a
            # gap in the graph, and the new event is going to become a new backwards
            # extremity.
            #
            # In this case we want to be a little careful as we might have been
            # down for a while and have an incorrect view of the current state,
            # however we still want to do checks as gaps are easy to
            # maliciously manufacture.
            #
            # So we use a "current state" that is actually a state
            # resolution across the current forward extremities and the
            # given state at the event. This should correctly handle cases
            # like bans, especially with state res v2.

            state_sets_d = await self._state_storage_controller.get_state_groups_ids(
                event.room_id, extrem_ids
            )
            state_sets: List[StateMap[str]] = list(state_sets_d.values())
            state_ids = await context.get_prev_state_ids()
            state_sets.append(state_ids)
            current_state_ids = (
                await self._state_resolution_handler.resolve_events_with_store(
                    event.room_id,
                    room_version,
                    state_sets,
                    event_map=None,
                    state_res_store=StateResolutionStore(
                        self._store, self._state_deletion_store
                    ),
                )
            )
        else:
            current_state_ids = (
                await self._state_storage_controller.get_current_state_ids(
                    event.room_id, StateFilter.from_types(auth_types)
                )
            )

        logger.debug(
            "Doing soft-fail check for %s: state %s",
            event.event_id,
            current_state_ids,
        )

        # Now check if event pass auth against said current state
        current_state_ids_list = [
            e for k, e in current_state_ids.items() if k in auth_types
        ]
        current_auth_events = await self._store.get_events_as_list(
            current_state_ids_list
        )

        try:
            check_state_dependent_auth_rules(event, current_auth_events)
        except AuthError as e:
            logger.warning(
                "Soft-failing %r (from %s) because %s",
                event,
                e,
                origin,
                extra={
                    "room_id": event.room_id,
                    "mxid": event.sender,
                    "hs": origin,
                },
            )
            soft_failed_event_counter.inc()
            event.internal_metadata.soft_failed = True

    async def _load_or_fetch_auth_events_for_event(
        self, destination: Optional[str], event: EventBase
    ) -> Collection[EventBase]:
        """Fetch this event's auth_events, from database or remote

        Loads any of the auth_events that we already have from the database/cache. If
        there are any that are missing, calls /event_auth to get the complete auth
        chain for the event (and then attempts to load the auth_events again).

        If any of the auth_events cannot be found, raises an AuthError. This can happen
        for a number of reasons; eg: the events don't exist, or we were unable to talk
        to `destination`, or we couldn't validate the signature on the event (which
        in turn has multiple potential causes).

        Args:
            destination: where to send the /event_auth request. Typically the server
               that sent us `event` in the first place.

               If this is None, no attempt is made to load any missing auth events:
               rather, an AssertionError is raised if there are any missing events.

            event: the event whose auth_events we want

        Returns:
            all of the events listed in `event.auth_events_ids`, after deduplication

        Raises:
            AssertionError if some auth events were missing and no `destination` was
            supplied.

            AuthError if we were unable to fetch the auth_events for any reason.
        """
        event_auth_event_ids = set(event.auth_event_ids())
        event_auth_events = await self._store.get_events(
            event_auth_event_ids, allow_rejected=True
        )
        missing_auth_event_ids = event_auth_event_ids.difference(
            event_auth_events.keys()
        )
        if not missing_auth_event_ids:
            return event_auth_events.values()
        if destination is None:
            # this shouldn't happen: destination must be set unless we know we have already
            # persisted the auth events.
            raise AssertionError(
                "_load_or_fetch_auth_events_for_event() called with no destination for "
                "an event with missing auth_events"
            )

        logger.info(
            "Event %s refers to unknown auth events %s: fetching auth chain",
            event,
            missing_auth_event_ids,
        )
        try:
            await self._get_remote_auth_chain_for_event(
                destination, event.room_id, event.event_id
            )
        except Exception as e:
            logger.warning("Failed to get auth chain for %s: %s", event, e)
            # in this case, it's very likely we still won't have all the auth
            # events - but we pick that up below.

        # try to fetch the auth events we missed list time.
        extra_auth_events = await self._store.get_events(
            missing_auth_event_ids, allow_rejected=True
        )
        missing_auth_event_ids.difference_update(extra_auth_events.keys())
        event_auth_events.update(extra_auth_events)
        if not missing_auth_event_ids:
            return event_auth_events.values()

        # we still don't have all the auth events.
        logger.warning(
            "Missing auth events for %s: %s",
            event,
            shortstr(missing_auth_event_ids),
        )
        # the fact we can't find the auth event doesn't mean it doesn't
        # exist, which means it is premature to store `event` as rejected.
        # instead we raise an AuthError, which will make the caller ignore it.
        raise AuthError(code=HTTPStatus.FORBIDDEN, msg="Auth events could not be found")

    @trace
    @tag_args
    async def _get_remote_auth_chain_for_event(
        self, destination: str, room_id: str, event_id: str
    ) -> None:
        """If we are missing some of an event's auth events, attempt to request them

        Args:
            destination: where to fetch the auth tree from
            room_id: the room in which we are lacking auth events
            event_id: the event for which we are lacking auth events
        """
        try:
            remote_events = await self._federation_client.get_event_auth(
                destination, room_id, event_id
            )

        except RequestSendFailed as e1:
            # The other side isn't around or doesn't implement the
            # endpoint, so lets just bail out.
            logger.info("Failed to get event auth from remote: %s", e1)
            return

        logger.info("/event_auth returned %i events", len(remote_events))

        # `event` may be returned, but we should not yet process it.
        remote_auth_events = (e for e in remote_events if e.event_id != event_id)

        await self._auth_and_persist_outliers(room_id, remote_auth_events)

    @trace
    async def _run_push_actions_and_persist_event(
        self, event: EventBase, context: EventContext, backfilled: bool = False
    ) -> None:
        """Run the push actions for a received event, and persist it.

        Args:
            event: The event itself.
            context: The event context.
            backfilled: True if the event was backfilled.

        PartialStateConflictError: if attempting to persist a partial state event in
            a room that has been un-partial stated.
        """
        # this method should not be called on outliers (those code paths call
        # persist_events_and_notify directly.)
        assert not event.internal_metadata.outlier

        if not backfilled and not context.rejected:
            min_depth = await self._store.get_min_depth(event.room_id)
            if min_depth is None or min_depth > event.depth:
                # XXX richvdh 2021/10/07: I don't really understand what this
                # condition is doing. I think it's trying not to send pushes
                # for events that predate our join - but that's not really what
                # min_depth means, and anyway ancient events are a more general
                # problem.
                #
                # for now I'm just going to log about it.
                logger.info(
                    "Skipping push actions for old event with depth %s < %s",
                    event.depth,
                    min_depth,
                )
            else:
                await self._bulk_push_rule_evaluator.action_for_events_by_user(
                    [(event, context)]
                )

        try:
            await self.persist_events_and_notify(
                event.room_id, [(event, context)], backfilled=backfilled
            )
        except Exception:
            await self._store.remove_push_actions_from_staging(event.event_id)
            raise

    async def persist_events_and_notify(
        self,
        room_id: str,
        event_and_contexts: Sequence[Tuple[EventBase, EventContext]],
        backfilled: bool = False,
    ) -> int:
        """Persists events and tells the notifier/pushers about them, if
        necessary.

        Args:
            room_id: The room ID of events being persisted.
            event_and_contexts: Sequence of events with their associated
                context that should be persisted. All events must belong to
                the same room.
            backfilled: Whether these events are a result of
                backfilling or not

        Returns:
            The stream ID after which all events have been persisted.

        Raises:
            PartialStateConflictError: if attempting to persist a partial state event in
                a room that has been un-partial stated.
        """
        if not event_and_contexts:
            return self._store.get_room_max_stream_ordering()

        instance = self._config.worker.events_shard_config.get_instance(room_id)
        if instance != self._instance_name:
            # Limit the number of events sent over replication. We choose 200
            # here as that is what we default to in `max_request_body_size(..)`
            result = {}
            try:
                for batch in batch_iter(event_and_contexts, 200):
                    result = await self._send_events(
                        instance_name=instance,
                        store=self._store,
                        room_id=room_id,
                        event_and_contexts=batch,
                        backfilled=backfilled,
                    )
            except SynapseError as e:
                if e.code == HTTPStatus.CONFLICT:
                    raise PartialStateConflictError()
                raise
            return result["max_stream_id"]
        else:
            assert self._storage_controllers.persistence

            # Note that this returns the events that were persisted, which may not be
            # the same as were passed in if some were deduplicated due to transaction IDs.
            (
                events,
                max_stream_token,
            ) = await self._storage_controllers.persistence.persist_events(
                event_and_contexts, backfilled=backfilled
            )

            # After persistence, we never notify clients (wake up `/sync` streams) about
            # backfilled events but it's important to let all the workers know about any
            # new event (backfilled or not) because TODO
            self._notifier.notify_replication()

            if self._ephemeral_messages_enabled:
                for event in events:
                    # If there's an expiry timestamp on the event, schedule its expiry.
                    self._message_handler.maybe_schedule_expiry(event)

            if not backfilled:  # Never notify for backfilled events
                with start_active_span("notify_persisted_events"):
                    set_tag(
                        SynapseTags.RESULT_PREFIX + "event_ids",
                        str([ev.event_id for ev in events]),
                    )
                    set_tag(
                        SynapseTags.RESULT_PREFIX + "event_ids.length",
                        str(len(events)),
                    )
                    for event in events:
                        await self._notify_persisted_event(event, max_stream_token)

            return max_stream_token.stream

    async def _notify_persisted_event(
        self, event: EventBase, max_stream_token: RoomStreamToken
    ) -> None:
        """Checks to see if notifier/pushers should be notified about the
        event or not.

        Args:
            event:
            max_stream_token: The max_stream_id returned by persist_events
        """

        extra_users = []
        if event.type == EventTypes.Member:
            target_user_id = event.state_key

            # We notify for memberships if its an invite for one of our
            # users
            if event.internal_metadata.is_outlier():
                if event.membership != Membership.INVITE:
                    if not self._is_mine_id(target_user_id):
                        return

            target_user = UserID.from_string(target_user_id)
            extra_users.append(target_user)
        elif event.internal_metadata.is_outlier():
            return

        # the event has been persisted so it should have a stream ordering.
        assert event.internal_metadata.stream_ordering

        event_pos = PersistedEventPosition(
            self._instance_name, event.internal_metadata.stream_ordering
        )
        await self._notifier.on_new_room_events(
            [(event, event_pos)], max_stream_token, extra_users=extra_users
        )

        if event.type == EventTypes.Member and event.membership == Membership.JOIN:
            # TODO retrieve the previous state, and exclude join -> join transitions
            self._notifier.notify_user_joined_room(event.event_id, event.room_id)

        # If this is a server ACL event, clear the cache in the storage controller.
        if event.type == EventTypes.ServerACL:
            self._state_storage_controller.get_server_acl_for_room.invalidate(
                (event.room_id,)
            )

    def _sanity_check_event(self, ev: EventBase) -> None:
        """
        Do some early sanity checks of a received event

        In particular, checks it doesn't have an excessive number of
        prev_events or auth_events, which could cause a huge state resolution
        or cascade of event fetches.

        Args:
            ev: event to be checked

        Raises:
            SynapseError if the event does not pass muster
        """
        if len(ev.prev_event_ids()) > 20:
            logger.warning(
                "Rejecting event %s which has %i prev_events",
                ev.event_id,
                len(ev.prev_event_ids()),
            )
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Too many prev_events")

        if len(ev.auth_event_ids()) > 10:
            logger.warning(
                "Rejecting event %s which has %i auth_events",
                ev.event_id,
                len(ev.auth_event_ids()),
            )
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Too many auth_events")
