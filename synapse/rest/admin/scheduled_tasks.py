#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
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
#
from typing import TYPE_CHECKING

from synapse.http.servlet import (
    RestServlet,
    parse_integer,
    parse_string,
    parse_strings_from_args,
)
from synapse.http.site import SynapseRequest
from synapse.rest.admin import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict, TaskStatus

if TYPE_CHECKING:
    from synapse.server import HomeServer


class ScheduledTasksRestServlet(RestServlet):
    """Get a list of scheduled tasks and their statuses
    optionally filtered by action name, resource id, status, and max timestamp
    """

    PATTERNS = admin_patterns("/scheduled_tasks$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        # twisted.web.server.Request.args is incorrectly defined as Any | None
        args: dict[bytes, list[bytes]] = request.args  # type: ignore

        # extract query params
        actions = parse_strings_from_args(args, "action_name")
        resource_id = parse_string(request, "resource_id")
        status_strings = parse_strings_from_args(
            args,
            "status",
            allowed_values=[status.value for status in TaskStatus],
        )
        # This parameter was historically called `job_status`, while the Admin API docs
        # defined it as `status`. We now support both, as `status` is generally
        # a nicer name. A v2 of this endpoint should keep only `status`.
        if status_strings is None:
            status_strings = parse_strings_from_args(
                args,
                "job_status",
                allowed_values=[status.value for status in TaskStatus],
            )
        max_timestamp = parse_integer(request, "max_timestamp")

        statuses = (
            [TaskStatus(status) for status in status_strings]
            if status_strings
            else None
        )

        tasks = await self._store.get_scheduled_tasks(
            actions=actions,
            resource_id=resource_id,
            statuses=statuses,
            max_timestamp=max_timestamp,
        )

        json_tasks = []
        for task in tasks:
            result_task = {
                "id": task.id,
                "action": task.action,
                "status": task.status,
                "timestamp_ms": task.timestamp,
                "resource_id": task.resource_id,
                "result": task.result,
                "error": task.error,
            }
            json_tasks.append(result_task)

        return 200, {"scheduled_tasks": json_tasks}
