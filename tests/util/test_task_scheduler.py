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

import logging

from twisted.internet.defer import Deferred
from twisted.internet.testing import MemoryReactor

from synapse.logging.context import make_deferred_yieldable
from synapse.server import HomeServer
from synapse.types import JsonMapping, ScheduledTask, TaskStatus
from synapse.util.clock import Clock
from synapse.util.duration import Duration
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
        self.task_scheduler.register_action(
            self._incrementing_active_task, "_incrementing_active_task"
        )
        self.task_scheduler.register_action(
            self._incrementing_running_task, "_incrementing_running_task"
        )

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
        self.reactor.advance(TaskScheduler.SCHEDULE_INTERVAL.as_secs())

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
        await self.hs.get_clock().sleep(Duration(seconds=1))
        return TaskStatus.COMPLETE, None, None

    def test_schedule_lot_of_tasks(self) -> None:
        """Schedule more than `self.task_scheduler._max_concurrent_tasks` tasks and check the behavior."""
        max_concurrent = self.task_scheduler._max_concurrent_tasks
        task_ids = []
        for i in range(max_concurrent + 1):
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

        # At this point, there should be max_concurrent active tasks and
        # one scheduled task.
        self.assertEqual(
            len(get_tasks_of_status(TaskStatus.ACTIVE)),
            max_concurrent,
        )
        self.assertEqual(
            len(get_tasks_of_status(TaskStatus.SCHEDULED)),
            1,
        )

        # Give the time to the active tasks to finish
        self.reactor.advance(1)

        # Check that max_concurrent tasks have run and that one
        # is still scheduled.
        self.assertEqual(
            len(get_tasks_of_status(TaskStatus.COMPLETE)),
            max_concurrent,
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

    @override_config({"task_scheduler": {"max_concurrent_tasks": 4}})
    def test_schedule_lot_of_tasks_custom_concurrency(self) -> None:
        """Test that configuring a higher concurrency limit allows more concurrent tasks."""
        self.assertEqual(self.task_scheduler._max_concurrent_tasks, 4)
        max_concurrent = self.task_scheduler._max_concurrent_tasks
        task_ids = []
        for i in range(max_concurrent + 1):
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

        self.assertEqual(
            len(get_tasks_of_status(TaskStatus.ACTIVE)),
            4,
        )
        self.assertEqual(
            len(get_tasks_of_status(TaskStatus.SCHEDULED)),
            1,
        )

        # Give the time to the active tasks to finish
        self.reactor.advance(1)

        # Check that 4 tasks have run and that one is still scheduled.
        self.assertEqual(
            len(get_tasks_of_status(TaskStatus.COMPLETE)),
            4,
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
        self.task_scheduler._running_tasks = {}
        self.reactor.advance(TaskScheduler.SCHEDULE_INTERVAL.as_secs())

        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        assert task.result is not None
        self.assertTrue(task.result.get("success"))

    def _test_cancel_task(self, task_id: str) -> None:
        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        assert task.status == TaskStatus.ACTIVE

        assert task.result and "counter" in task.result
        current_counter = int(task.result["counter"])

        self.reactor.advance(1)

        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        assert task.status == TaskStatus.ACTIVE

        # At this point the task should have run at least one more time, let's check the counter
        assert task.result and "counter" in task.result
        new_counter = int(task.result["counter"])
        assert new_counter > current_counter
        current_counter = new_counter

        # Cancelling active task
        self.get_success(self.task_scheduler.cancel_task(task_id))

        self.reactor.advance(1)

        # Task should be marked as cancelled
        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        self.assertEqual(task.status, TaskStatus.CANCELLED)

        # Task should be in the running tasks
        assert task_id not in self.task_scheduler._running_tasks

        # Counter should not increase anymore and stay the same
        assert task.result and "counter" in task.result
        new_counter = int(task.result["counter"])
        assert new_counter == current_counter
        current_counter = new_counter

        # Let's check one more time to be sure that it is not increasing
        self.reactor.advance(100)

        task = self.get_success(self.task_scheduler.get_task(task_id))
        assert task is not None
        assert task.result and "counter" in task.result
        new_counter = int(task.result["counter"])
        assert new_counter == current_counter

    async def _incrementing_running_task(
        self, task: ScheduledTask
    ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
        current_counter = 0

        while True:
            current_counter += 1
            await self.task_scheduler.update_task(
                task.id, result={"counter": current_counter}
            )
            await self.hs.get_clock().sleep(Duration(seconds=1))

        return TaskStatus.COMPLETE, None, None  # type: ignore[unreachable]

    def test_cancel_running_task(self) -> None:
        """Schedule and then cancel a long running task that increments a counter."""

        task_id = self.get_success(
            self.task_scheduler.schedule_task(
                "_incrementing_running_task",
            )
        )

        self._test_cancel_task(task_id)

    async def _incrementing_active_task(
        self, task: ScheduledTask
    ) -> tuple[TaskStatus, JsonMapping | None, str | None]:
        current_counter = 0
        if task.result and "counter" in task.result:
            current_counter = int(task.result["counter"])

        return TaskStatus.ACTIVE, {"counter": current_counter + 1}, None

    def test_cancel_active_task(self) -> None:
        """Schedule and then cancel a long active task that increments a counter,
        but step by step, by keeping the task ACTIVE even if it is not running
        between the steps."""

        task_id = self.get_success(
            self.task_scheduler.schedule_task(
                "_incrementing_active_task",
            )
        )

        self._test_cancel_task(task_id)

    def test_final_update_failure_releases_slot(self) -> None:
        """Check that a failure to persist the final status of a task doesn't
        leak its running slot, so that the scheduler can keep launching tasks.

        Such a failure can happen e.g. with a serialization error under
        REPEATABLE READ if a concurrent writer updates the same row and the
        database layer exhausts its retries.
        """
        # Make the write of the final status of the task fail once.
        real_update_scheduled_task = self.task_scheduler._store.update_scheduled_task
        failed = False

        async def flaky_update_scheduled_task(
            id: str,
            timestamp: int,
            *,
            status: TaskStatus | None = None,
            result: JsonMapping | None = None,
            error: str | None = None,
        ) -> bool:
            nonlocal failed
            if status == TaskStatus.COMPLETE and not failed:
                failed = True
                # Suspend before failing, as a real database error would: the
                # write is issued on the database connection pool and the
                # error comes back on a later reactor iteration.
                await self.hs.get_clock().sleep(Duration(milliseconds=1))
                raise Exception("could not serialize access due to concurrent update")
            return await real_update_scheduled_task(
                id, timestamp, status=status, result=result, error=error
            )

        self.task_scheduler._store.update_scheduled_task = flaky_update_scheduled_task  # type: ignore[method-assign]

        with self.assertNoLogs(
            "synapse.metrics.background_process_metrics", level=logging.ERROR
        ):
            task_id = self.get_success(self.task_scheduler.schedule_task("_test_task"))

            # The task function has completed, and the wrapper is suspended
            # in the write of the final status, which is about to fail.
            self.reactor.advance(0.05)

            # The write of the final status failed. The running slot must
            # nevertheless be released, otherwise the pool of running tasks
            # would progressively leak and eventually prevent any new task
            # from being launched.
            self.assertNotIn(task_id, self.task_scheduler._running_tasks)

            # The task keeps its previous state in the database and gets
            # launched again — the call to launch new tasks scheduled at the
            # end of the wrapper runs 100ms after it — at which point
            # persisting its final status succeeds.
            self.reactor.advance(TaskScheduler.SCHEDULE_INTERVAL.as_secs())

            task = self.get_success(self.task_scheduler.get_task(task_id))
            assert task is not None
            self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_cancel_running_task_does_not_log_error(self) -> None:
        """Check that cancelling a running task doesn't log an error.

        Cancelling a running task makes the task function see
        `defer.CancelledError`, after which both `on_cancel_task` and the
        wrapper try to remove the task from the dict of running tasks; the
        wrapper must not fail with a `KeyError` because `on_cancel_task` got
        there first.
        """
        with self.assertNoLogs(
            "synapse.metrics.background_process_metrics", level=logging.ERROR
        ):
            task_id = self.get_success(
                self.task_scheduler.schedule_task("_incrementing_running_task")
            )

            self.reactor.advance(1)

            self.get_success(self.task_scheduler.cancel_task(task_id))

            self.reactor.advance(1)

            task = self.get_success(self.task_scheduler.get_task(task_id))
            assert task is not None
            self.assertEqual(task.status, TaskStatus.CANCELLED)
            self.assertNotIn(task_id, self.task_scheduler._running_tasks)


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
