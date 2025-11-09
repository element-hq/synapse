#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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

from twisted.internet.defer import Deferred
from twisted.internet.testing import MemoryReactor

from synapse.logging.context import make_deferred_yieldable
from synapse.server import HomeServer
from synapse.types import JsonMapping, ScheduledTask, TaskStatus
from synapse.util.clock import Clock
from synapse.util.task_scheduler import TaskScheduler

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.unittest import HomeserverTestCase, override_config


class TestTaskScheduler(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.task_scheduler = hs.get_task_scheduler()
        self.task_scheduler.register_action(self._test_task, "_test_task")
        self.task_scheduler.register_action(self._sleeping_task, "_sleeping_task")
        self.task_scheduler.register_action(self._raising_task, "_raising_task")
        self.task_scheduler.register_action(self._resumable_task, "_resumable_task")

    async def _test_task(
        self, task: ScheduledTask
    ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
        # This test task will copy the parameters to the result
        result = None
        if task.params:
            result = task.params
        return (TaskStatus.COMPLETE, result, None)

    def test_schedule_task(self) -> None:
        """Schedule a task in the future with some parameters to be copied as a result and check it executed correctly.
        Also check that it get removed after `KEEP_TASKS_FOR_MS`."""
        timestamp = self.clock.time_msec() + 30 * 1000
        task_id = self.get_success(
            self.task_scheduler.schedule_task(
                "_test_task",
                timestamp=timestamp,
                params={"val": 1},
            )
        )

        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        self.assertEqual(task.status, TaskStatus.SCHEDULED)
        self.assertIsNone(task.result)

        # The timestamp being 30s after now the task should been executed
        # after the first scheduling loop is run
        self.reactor.advance(TaskScheduler.SCHEDULE_INTERVAL_MS / 1000)

        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        assert task.result is not None
        # The passed parameter should have been copied to the result
        self.assertTrue(task.result.get("val") == 1)

        # Let's wait for the complete task to be deleted and hence unavailable
        self.reactor.advance((TaskScheduler.KEEP_TASKS_FOR_MS / 1000) + 1)

        task = self.get_success(self.task_scheduler.get_task(task_id))
        self.assertIsNone(task)

    async def _sleeping_task(
        self, task: ScheduledTask
    ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
        # Sleep for a second
        await self.hs.get_clock().sleep(1)
        return TaskStatus.COMPLETE, None, None

    def test_schedule_lot_of_tasks(self) -> None:
        """Schedule more than `TaskScheduler.MAX_CONCURRENT_RUNNING_TASKS` tasks and check the behavior."""
        task_ids = []
        for i in range(TaskScheduler.MAX_CONCURRENT_RUNNING_TASKS + 1):
            task_ids.append(
                self.get_success(
                    self.task_scheduler.schedule_task(
                        "_sleeping_task",
                        params={"val": i},
                    )
                )
            )

        def get_tasks_of_status(status: TaskStatus) -> list[ScheduledTask]:
            tasks = (
                self.get_success(self.task_scheduler.get_task(task_id))
                for task_id in task_ids
            )
            return [t for t in tasks if t is not None and t.status == status]

        # At this point, there should be MAX_CONCURRENT_RUNNING_TASKS active tasks and
        # one scheduled task.
        self.assertEqual(
            len(get_tasks_of_status(TaskStatus.ACTIVE)),
            TaskScheduler.MAX_CONCURRENT_RUNNING_TASKS,
        )
        self.assertEqual(
            len(get_tasks_of_status(TaskStatus.SCHEDULED)),
            1,
        )

        # Give the time to the active tasks to finish
        self.reactor.advance(1)

        # Check that MAX_CONCURRENT_RUNNING_TASKS tasks have run and that one
        # is still scheduled.
        self.assertEqual(
            len(get_tasks_of_status(TaskStatus.COMPLETE)),
            TaskScheduler.MAX_CONCURRENT_RUNNING_TASKS,
        )
        scheduled_tasks = get_tasks_of_status(TaskStatus.SCHEDULED)
        self.assertEqual(len(scheduled_tasks), 1)

        # The scheduled task should start 0.1s after the first of the active tasks
        # finishes
        self.reactor.advance(0.1)
        self.assertEqual(len(get_tasks_of_status(TaskStatus.ACTIVE)), 1)

        # ... and should finally complete after another second
        self.reactor.advance(1)
        prev_scheduled_task = self.get_success(
            self.task_scheduler.get_task(scheduled_tasks[0].id)
        )
        assert prev_scheduled_task is not None
        self.assertEqual(
            prev_scheduled_task.status,
            TaskStatus.COMPLETE,
        )

    async def _raising_task(
        self, task: ScheduledTask
    ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
        raise Exception("raising")

    def test_schedule_raising_task(self) -> None:
        """Schedule a task raising an exception and check it runs to failure and report exception content."""
        task_id = self.get_success(self.task_scheduler.schedule_task("_raising_task"))

        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.error, "raising")

    async def _resumable_task(
        self, task: ScheduledTask
    ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
        if task.result and "in_progress" in task.result:
            return TaskStatus.COMPLETE, {"success": True}, None
        else:
            await self.task_scheduler.update_task(task.id, result={"in_progress": True})
            # Create a deferred which we will never complete
            incomplete_d: Deferred = Deferred()
            # Await forever to simulate an aborted task because of a restart
            await make_deferred_yieldable(incomplete_d)
            # This should never been called
            return TaskStatus.ACTIVE, None, None

    def test_schedule_resumable_task(self) -> None:
        """Schedule a resumable task and check that it gets properly resumed and complete after simulating a synapse restart."""
        task_id = self.get_success(self.task_scheduler.schedule_task("_resumable_task"))

        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        self.assertEqual(task.status, TaskStatus.ACTIVE)

        # Simulate a synapse restart by emptying the list of running tasks
        self.task_scheduler._running_tasks = set()
        self.reactor.advance((TaskScheduler.SCHEDULE_INTERVAL_MS / 1000))

        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        assert task.result is not None
        self.assertTrue(task.result.get("success"))


class TestTaskSchedulerWithBackgroundWorker(BaseMultiWorkerStreamTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.task_scheduler = hs.get_task_scheduler()
        self.task_scheduler.register_action(self._test_task, "_test_task")

    async def _test_task(
        self, task: ScheduledTask
    ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
        return (TaskStatus.COMPLETE, None, None)

    @override_config({"run_background_tasks_on": "worker1"})
    def test_schedule_task(self) -> None:
        """Check that a task scheduled to run now is launch right away on the background worker."""
        bg_worker_hs = self.make_worker_hs(
            "synapse.app.generic_worker",
            extra_config={"worker_name": "worker1"},
        )
        bg_worker_hs.get_task_scheduler().register_action(self._test_task, "_test_task")

        task_id = self.get_success(
            self.task_scheduler.schedule_task(
                "_test_task",
            )
        )

        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        self.assertEqual(task.status, TaskStatus.COMPLETE)
