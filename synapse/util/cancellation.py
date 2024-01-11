#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def cancellable(function: F) -> F:
    """Marks a function as cancellable.

    Servlet methods with this decorator will be cancelled if the client disconnects before we
    finish processing the request.

    Although this annotation is particularly useful for servlet methods, it's also
    useful for intermediate functions, where it documents the fact that the function has
    been audited for cancellation safety and needs to preserve that.
    This then simplifies auditing new functions that call those same intermediate
    functions.

    During cancellation, `Deferred.cancel()` will be invoked on the `Deferred` wrapping
    the method. The `cancel()` call will propagate down to the `Deferred` that is
    currently being waited on. That `Deferred` will raise a `CancelledError`, which will
    propagate up, as per normal exception handling.

    Before applying this decorator to a new function, you MUST recursively check
    that all `await`s in the function are on `async` functions or `Deferred`s that
    handle cancellation cleanly, otherwise a variety of bugs may occur, ranging from
    premature logging context closure, to stuck requests, to database corruption.

    See the documentation page on Cancellation for more information.

    Usage:
        class SomeServlet(RestServlet):
            @cancellable
            async def on_GET(self, request: SynapseRequest) -> ...:
                ...
    """

    function.cancellable = True  # type: ignore[attr-defined]
    return function


def is_function_cancellable(function: Callable[..., Any]) -> bool:
    """Checks whether a servlet method has the `@cancellable` flag."""
    return getattr(function, "cancellable", False)
