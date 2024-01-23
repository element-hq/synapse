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

"""This module contains logic for storing HTTP PUT transactions. This is used
to ensure idempotency when performing PUTs using the REST API."""
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, Hashable, Tuple

from typing_extensions import ParamSpec

from twisted.internet.defer import Deferred
from twisted.python.failure import Failure
from twisted.web.iweb import IRequest

from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.types import JsonDict, Requester
from synapse.util.async_helpers import ObservableDeferred

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

CLEANUP_PERIOD_MS = 1000 * 60 * 30  # 30 mins


P = ParamSpec("P")


class HttpTransactionCache:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.clock = self.hs.get_clock()
        # $txn_key: (ObservableDeferred<(res_code, res_json_body)>, timestamp)
        self.transactions: Dict[
            Hashable, Tuple[ObservableDeferred[Tuple[int, JsonDict]], int]
        ] = {}
        # Try to clean entries every 30 mins. This means entries will exist
        # for at *LEAST* 30 mins, and at *MOST* 60 mins.
        self.cleaner = self.clock.looping_call(self._cleanup, CLEANUP_PERIOD_MS)

    def _get_transaction_key(self, request: IRequest, requester: Requester) -> Hashable:
        """A helper function which returns a transaction key that can be used
        with TransactionCache for idempotent requests.

        Idempotency is based on the returned key being the same for separate
        requests to the same endpoint. The key is formed from the HTTP request
        path and attributes from the requester: the access_token_id for regular users,
        the user ID for guest users, and the appservice ID for appservice users.
        With MSC3970, for regular users, the key is based on the user ID and device ID.

        Args:
            request: The incoming request.
            requester: The requester doing the request.
        Returns:
            A transaction key
        """
        assert request.path is not None
        path: str = request.path.decode("utf8")

        if requester.is_guest:
            assert requester.user is not None, "Guest requester must have a user ID set"
            return (path, "guest", requester.user)

        elif requester.app_service is not None:
            return (path, "appservice", requester.app_service.id)

        # Use the user ID and device ID as the transaction key.
        elif requester.device_id:
            assert requester.user, "Requester must have a user"
            assert requester.device_id, "Requester must have a device_id"
            return (path, "user", requester.user, requester.device_id)

        # Some requsters don't have device IDs, these are mostly handled above
        # (appservice and guest users), but does not cover access tokens minted
        # by the admin API. Use the access token ID instead.
        else:
            assert (
                requester.access_token_id is not None
            ), "Requester must have an access_token_id"
            return (path, "user_admin", requester.access_token_id)

    def fetch_or_execute_request(
        self,
        request: IRequest,
        requester: Requester,
        fn: Callable[P, Awaitable[Tuple[int, JsonDict]]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> "Deferred[Tuple[int, JsonDict]]":
        """Fetches the response for this transaction, or executes the given function
        to produce a response for this transaction.

        Args:
            request:
            requester:
            fn: A function which returns a tuple of (response_code, response_dict).
            *args: Arguments to pass to fn.
            **kwargs: Keyword arguments to pass to fn.
        Returns:
            Deferred which resolves to a tuple of (response_code, response_dict).
        """
        txn_key = self._get_transaction_key(request, requester)
        if txn_key in self.transactions:
            observable = self.transactions[txn_key][0]
        else:
            # execute the function instead.
            deferred = run_in_background(fn, *args, **kwargs)

            observable = ObservableDeferred(deferred)
            self.transactions[txn_key] = (observable, self.clock.time_msec())

            # if the request fails with an exception, remove it
            # from the transaction map. This is done to ensure that we don't
            # cache transient errors like rate-limiting errors, etc.
            def remove_from_map(err: Failure) -> None:
                self.transactions.pop(txn_key, None)
                # we deliberately do not propagate the error any further, as we
                # expect the observers to have reported it.

            deferred.addErrback(remove_from_map)

        return make_deferred_yieldable(observable.observe())

    def _cleanup(self) -> None:
        now = self.clock.time_msec()
        for key in list(self.transactions):
            ts = self.transactions[key][1]
            if now > (ts + CLEANUP_PERIOD_MS):  # after cleanup period
                del self.transactions[key]
