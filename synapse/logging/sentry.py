#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Sentry integration.

Sentry keeps its own per-request state (scopes) in contextvars. Twisted runs
coroutines in copied contextvars contexts and fires Deferred callbacks in whichever
context happens to be current, so a scope forked per request does not reliably follow
the request. Synapse's own request identity is the thread-local logcontext, which
Synapse's logcontext discipline keeps tied to the request across those same callback
restores, so everything here reads the logcontext at send time instead of using Sentry
scopes.
"""

from typing import TYPE_CHECKING, Any

import sentry_sdk
from sentry_sdk.serializer import serialize

from synapse.logging.context import (
    ContextRequest,
    LoggingContext,
    current_context,
    set_sentry_breadcrumb_ring_size,
)
from synapse.metrics.background_process_metrics import BackgroundProcessLoggingContext
from synapse.types import UserID
from synapse.util import SYNAPSE_VERSION
from synapse.util.hash import sha256_and_url_safe_base64

if TYPE_CHECKING:
    from collections import deque

    from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint

    from synapse.server import HomeServer


# `sentry_sdk`'s logging integration copies every log record attribute it doesn't
# recognise into `event["extra"]`. `LoggingContextFilter` sets these, which would put
# Matrix IDs and IP addresses into Sentry in the clear; the request fields we do want
# are set explicitly by `before_send`.
_LOGCONTEXT_RECORD_ATTRIBUTES = frozenset(
    {
        "request",
        "server_name",
        "ip_address",
        "site_tag",
        "requester",
        "authenticated_entity",
        "method",
        "url",
        "protocol",
        "user_agent",
    }
)

# `sys.argv` comes from `sentry_sdk`'s argv integration and `asctime` from formatting
# the record; neither says anything the rest of the event doesn't.
_UNINTERESTING_EXTRA_KEYS = frozenset({"sys.argv", "asctime"})

_STRIPPED_EXTRA_KEYS = _LOGCONTEXT_RECORD_ATTRIBUTES | _UNINTERESTING_EXTRA_KEYS

# Breadcrumb categories are logger names; these are the access log's, one per listener
# protocol.
_ACCESS_LOG_CATEGORY_PREFIXES = ("synapse.access.http.", "synapse.access.https.")


def _pseudonymous_user_id(identifier: str) -> str:
    """Derive an opaque, stable id from a Matrix ID.

    This is a pseudonym, not anonymisation: it lets events from one user be
    correlated with each other without putting Matrix IDs into Sentry, but anyone
    with a candidate Matrix ID can confirm whether it matches.

    The digest is truncated to keep the id readable in the Sentry UI; the remaining
    96 bits are far more than enough to keep one homeserver's users apart.
    """
    return sha256_and_url_safe_base64(identifier)[:16]


def _strip_query_string(url: str) -> str:
    """Drop the query string from a URI.

    Query parameters carry room, event and user identifiers which Sentry has no use
    for.
    """
    return url.partition("?")[0]


def _attach_request(event: "Event", request: ContextRequest) -> None:
    """Describe the HTTP request being served on an outgoing event.

    The client's IP address is not attached: Sentry is not where Synapse's
    operators should be reading IP addresses from.
    """
    if request.servlet_name is not None:
        event["transaction"] = request.servlet_name

    tags = event.setdefault("tags", {})
    # Only low-cardinality values belong in tags; the request ID goes in the context
    # below instead.
    if request.servlet_name is not None:
        tags["servlet"] = request.servlet_name
    tags["site_tag"] = request.site_tag
    tags["method"] = request.method

    synapse_request: dict[str, object] = {
        "request_id": request.request_id,
        "servlet": request.servlet_name,
        "protocol": request.protocol,
    }

    # `requester` is the user the request is being made on behalf of: for a puppeting
    # request that is the puppeted user, and the admin who authenticated is not sent.
    if request.requester is not None:
        if UserID.is_valid(request.requester):
            event["user"] = {"id": _pseudonymous_user_id(request.requester)}
        else:
            # A federation request's requester is the origin server name, which is
            # public, so hashing it would only cost the ability to group errors by
            # origin. A context is not a tag, so its cardinality is free.
            synapse_request["requester"] = request.requester

    event.setdefault("contexts", {})["synapse_request"] = synapse_request

    event["request"] = {
        "method": request.method,
        "url": _strip_query_string(request.url),
        "headers": {"User-Agent": request.user_agent},
    }


def _snapshot_breadcrumbs(breadcrumbs: "deque[Breadcrumb]") -> list["Breadcrumb"]:
    """Copy a logcontext's breadcrumb ring.

    Appends happen from database threads, which run in a child logcontext sharing the
    ring, while this runs on the reactor thread. `list()` on a deque is a single
    C-level loop, so the GIL is not released mid-iteration on the standard build, but
    that is an implementation detail; if it ever does raise, losing the breadcrumbs is
    better than losing the event.
    """
    try:
        return list(breadcrumbs)
    except RuntimeError:
        return []


def before_send(event: "Event", hint: "Hint") -> "Event | None":
    """Rewrite an outgoing Sentry event to carry the current logcontext's request.

    Registered as `sentry_sdk.init(before_send=...)`, so it runs after every event
    processor, on the thread and stack frame that captured the event. The SDK only
    calls it for error events, so a transaction event (were `traces_sample_rate` ever
    set) would go out untouched, still carrying the SDK's own cross-request state.
    """
    extra = event.get("extra")
    if extra is not None:
        for key in _STRIPPED_EXTRA_KEYS:
            extra.pop(key, None)

    record = hint.get("log_record")
    if record is not None and "exception" not in event:
        # Sentry groups these events on the formatted message, and its normalisation
        # does not cover Matrix room or event IDs, so a `%s`-templated log line
        # otherwise fragments into one issue per room. Group on the template instead.
        event["fingerprint"] = [record.name, str(record.msg)]

    context = current_context()
    if not isinstance(context, LoggingContext):
        # The sentinel context has no request to describe.
        return event

    if isinstance(context, BackgroundProcessLoggingContext):
        event.setdefault("tags", {})["background_process"] = context.desc

    if context.request is not None:
        _attach_request(event, context.request)

    if context.sentry_breadcrumbs is not None:
        # Replace whatever the SDK collected in its shared ring with this request's
        # own breadcrumbs.
        event["breadcrumbs"] = {
            "values": _snapshot_breadcrumbs(context.sentry_breadcrumbs)
        }

    return event


def before_breadcrumb(
    crumb: "Breadcrumb", hint: "BreadcrumbHint"
) -> "Breadcrumb | None":
    """Record a breadcrumb against the current logcontext rather than the SDK.

    Registered as `sentry_sdk.init(before_breadcrumb=...)`. The SDK keeps one
    breadcrumb ring per isolation scope, which every request on a worker shares, so
    returning `None` here keeps the crumb out of it; `before_send` reads the
    logcontext's ring instead.
    """
    category = crumb.get("category")
    if category is not None and category.startswith(_ACCESS_LOG_CATEGORY_PREFIXES):
        # The access log names other users' request paths and Matrix IDs, and is
        # already in the normal logs keyed by request ID.
        return None

    breadcrumbs = current_context().sentry_breadcrumbs
    if breadcrumbs is None:
        # Outside any logcontext, leave the SDK to its default behaviour; events
        # captured there have no request to keep the crumbs apart from.
        return crumb

    # The SDK serialises the event and runs `EventScrubber` over it before it calls
    # `before_send`, so crumbs attached there miss both passes: strip the
    # `LoggingContextFilter` record attributes the SDK's breadcrumb handler copies
    # into `data`, as `before_send` does for `extra`, and serialise the crumb so that
    # the `datetime` the SDK leaves in `timestamp` cannot fail to JSON-encode in the
    # transport and take the whole event down with it. `serialize` is
    # `sentry_sdk`-internal API, and sees the crumb as a top-level object, so the SDK's
    # databag trimming does not reach `data`; strings are still capped at
    # `max_value_length`.
    data = crumb.get("data")
    if data is not None:
        for key in _STRIPPED_EXTRA_KEYS:
            data.pop(key, None)

    options = sentry_sdk.get_client().options
    breadcrumbs.append(
        serialize(dict(crumb), max_value_length=options.get("max_value_length"))
    )
    return None


def sentry_sdk_options(
    *, dsn: str | None, environment: str | None, max_breadcrumbs: int
) -> dict[str, Any]:
    """The options Synapse initialises `sentry_sdk` with.

    Kept out of `setup_sentry` so that tests can drive the SDK end to end with their
    own transport and the options Synapse runs with.
    """
    return {
        "dsn": dsn,
        "release": SYNAPSE_VERSION,
        "environment": environment,
        # Everything the events say about the request is assembled by `before_send`,
        # in a pseudonymised form.
        "send_default_pii": False,
        "before_send": before_send,
        "before_breadcrumb": before_breadcrumb,
        "max_breadcrumbs": max_breadcrumbs,
    }


def setup_sentry(hs: "HomeServer") -> None:
    """Enable the Sentry integration."""

    max_breadcrumbs = hs.config.metrics.sentry_max_breadcrumbs

    sentry_sdk.init(
        **sentry_sdk_options(
            dsn=hs.config.metrics.sentry_dsn,
            environment=hs.config.metrics.sentry_environment,
            max_breadcrumbs=max_breadcrumbs,
        )
    )

    set_sentry_breadcrumb_ring_size(max_breadcrumbs)

    # We set some default tags that give some context to this instance
    global_scope = sentry_sdk.Scope.get_global_scope()
    global_scope.set_tag("matrix_server_name", hs.config.server.server_name)

    app = (
        hs.config.worker.worker_app
        if hs.config.worker.worker_app
        else "synapse.app.homeserver"
    )
    name = hs.get_instance_name()
    global_scope.set_tag("worker_app", app)
    global_scope.set_tag("worker_name", name)
