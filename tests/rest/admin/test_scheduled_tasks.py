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
from typing import Mapping

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.errors import Codes
from synapse.rest.client import login
from synapse.server import HomeServer
from synapse.types import JsonMapping, ScheduledTask, TaskStatus
from synapse.util.clock import Clock

from tests import unittest


class ScheduledTasksAdminApiTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")
        self._task_scheduler = hs.get_task_scheduler()

        # create and schedule a few tasks
        async def _test_task(
            task: ScheduledTask,
        ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
            return TaskStatus.ACTIVE, None, None

        async def _finished_test_task(
            task: ScheduledTask,
        ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
            return TaskStatus.COMPLETE, None, None

        async def _failed_test_task(
            task: ScheduledTask,
        ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
            return TaskStatus.FAILED, None, "Everything failed"

        self._task_scheduler.register_action(_test_task, "test_task")
        self.get_success(
            self._task_scheduler.schedule_task("test_task", resource_id="test")
        )

        self._task_scheduler.register_action(_finished_test_task, "finished_test_task")
        self.get_success(
            self._task_scheduler.schedule_task(
                "finished_test_task", resource_id="finished_task"
            )
        )

        self._task_scheduler.register_action(_failed_test_task, "failed_test_task")
        self.get_success(
            self._task_scheduler.schedule_task(
                "failed_test_task", resource_id="failed_task"
            )
        )

    def check_scheduled_tasks_response(self, scheduled_tasks: Mapping) -> list:
        result = []
        for task in scheduled_tasks:
            if task["resource_id"] == "test":
                self.assertEqual(task["status"], TaskStatus.ACTIVE)
                self.assertEqual(task["action"], "test_task")
                result.append(task)
            if task["resource_id"] == "finished_task":
                self.assertEqual(task["status"], TaskStatus.COMPLETE)
                self.assertEqual(task["action"], "finished_test_task")
                result.append(task)
            if task["resource_id"] == "failed_task":
                self.assertEqual(task["status"], TaskStatus.FAILED)
                self.assertEqual(task["action"], "failed_test_task")
                result.append(task)

        return result

    def test_requester_is_not_admin(self) -> None:
        """
        If the user is not a server admin, an error 403 is returned.
        """

        self.register_user("user", "pass", admin=False)
        other_user_tok = self.login("user", "pass")

        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/scheduled_tasks",
            content={},
            access_token=other_user_tok,
        )

        self.assertEqual(403, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_scheduled_tasks(self) -> None:
        """
        Test that endpoint returns scheduled tasks.
        """

        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/scheduled_tasks",
            content={},
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        scheduled_tasks = channel.json_body["scheduled_tasks"]

        # make sure we got back all the scheduled tasks
        found_tasks = self.check_scheduled_tasks_response(scheduled_tasks)
        self.assertEqual(len(found_tasks), 3)

    def test_filtering_scheduled_tasks(self) -> None:
        """
        Test that filtering the scheduled tasks response via query params works as expected.
        """
        # filter via job_status
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/scheduled_tasks?job_status=active",
            content={},
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        scheduled_tasks = channel.json_body["scheduled_tasks"]
        found_tasks = self.check_scheduled_tasks_response(scheduled_tasks)

        # only the active task should have been returned
        self.assertEqual(len(found_tasks), 1)
        self.assertEqual(found_tasks[0]["status"], "active")

        # filter via action_name
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/scheduled_tasks?action_name=test_task",
            content={},
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        scheduled_tasks = channel.json_body["scheduled_tasks"]

        # only test_task should have been returned
        found_tasks = self.check_scheduled_tasks_response(scheduled_tasks)
        self.assertEqual(len(found_tasks), 1)
        self.assertEqual(found_tasks[0]["action"], "test_task")

        # filter via max_timestamp
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/scheduled_tasks?max_timestamp=0",
            content={},
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        scheduled_tasks = channel.json_body["scheduled_tasks"]
        found_tasks = self.check_scheduled_tasks_response(scheduled_tasks)

        # none should have been returned
        self.assertEqual(len(found_tasks), 0)

        # filter via resource id
        channel = self.make_request(
            "GET",
            "/_synapse/admin/v1/scheduled_tasks?resource_id=failed_task",
            content={},
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        scheduled_tasks = channel.json_body["scheduled_tasks"]
        found_tasks = self.check_scheduled_tasks_response(scheduled_tasks)

        # only the task with the matching resource id should have been returned
        self.assertEqual(len(found_tasks), 1)
        self.assertEqual(found_tasks[0]["resource_id"], "failed_task")
