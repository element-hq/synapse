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
from typing import TYPE_CHECKING, Awaitable, Callable

from twisted.python.failure import Failure

from synapse.logging.context import (
    ContextResourceUsage,
    LoggingContext,
    PreserveLoggingContext,
    nested_logging_context,
)
from synapse.metrics import SERVER_NAME_LABEL, LaterGauge
from synapse.metrics.background_process_metrics import (
    wrap_as_background_process,
)
from synapse.types import JsonMapping, ScheduledTask, TaskStatus
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


running_tasks_gauge = LaterGauge(
    name="synapse_scheduler_running_tasks",
    desc="The number of concurrent running tasks handled by the TaskScheduler",
    labelnames=[SERVER_NAME_LABEL],
)


class TaskScheduler:
    """
    This is a simple task scheduler designed for resumable tasks. Normally,
    you'd use `run_in_background` to start a background task or `clock.call_later`
    if you want to run it later.

    The issue is that these tasks stop completely and won't resume if Synapse is
    shut down for any reason.

    Here's how it works:

    - Register an Action: First, you need to register a function to a named
      action using `register_action`. This function will be called to resume tasks
      after a Synapse shutdown. Make sure to register it when Synapse initializes,
      not right before scheduling the task.

    - Schedule a Task: You can launch a task linked to the named action
      using `schedule_task`. You can pass a `params` dictionary, which will be
      passed to the registered function when it's executed. Tasks can be scheduled
      to run either immediately or later by specifying a `timestamp`.

    - Update Task: The function handling the task can call `update_task` at
      any point to update the task's `result`. This lets you resume the task from
      a specific point or pass results back to the code that scheduled it. When
      the function completes, you can also return a `result` or an `error`.

    Things to keep in mind:

    - The reconciliation loop runs every minute, so this is not a high-precision
      scheduler.

    - Only 10 tasks can run at the same time. If the pool is full, tasks may be
      delayed. Make sure your scheduled tasks can actually finish.

    - Currently, there's no way to stop a task if it gets stuck.

    - Tasks will run on the worker defined by the `run_background_tasks_on`
      setting in your configuration. If no worker is specified, they'll run on
      the main one by default.
    """

    # Precision of the scheduler, evaluation of tasks to run will only happen
    # every `SCHEDULE_INTERVAL_MS` ms
    SCHEDULE_INTERVAL_MS = 1 * 60 * 1000  # 1mn
    # How often to clean up old tasks.
    CLEANUP_INTERVAL_MS = 30 * 60 * 1000
    # Time before a complete or failed task is deleted from the DB
    KEEP_TASKS_FOR_MS = 7 * 24 * 60 * 60 * 1000  # 1 week
    # Maximum number of tasks that can run at the same time
    MAX_CONCURRENT_RUNNING_TASKS = 5
    # Time from the last task update after which we will log a warning
    LAST_UPDATE_BEFORE_WARNING_MS = 24 * 60 * 60 * 1000  # 24hrs
    # Report a running task's status and usage every so often.
    OCCASIONAL_REPORT_INTERVAL_MS = 5 * 60 * 1000  # 5 minutes

    def __init__(self, hs: "HomeServer"):
        self.hs = hs  # nb must be called this for @wrap_as_background_process
        self.server_name = hs.hostname
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()
        self._running_tasks: set[str] = set()
        # A map between action names and their registered function
        self._actions: dict[
            str,
            Callable[
                [ScheduledTask],
                Awaitable[tuple[TaskStatus, JsonMapping | None, str | None]],
            ],
        ] = {}
        self._run_background_tasks = hs.config.worker.run_background_tasks

        # Flag to make sure we only try and launch new tasks once at a time.
        self._launching_new_tasks = False

        if self._run_background_tasks:
            self._clock.looping_call(
                self._launch_scheduled_tasks,
                TaskScheduler.SCHEDULE_INTERVAL_MS,
            )
            self._clock.looping_call(
                self._clean_scheduled_tasks,
                TaskScheduler.SCHEDULE_INTERVAL_MS,
            )

        running_tasks_gauge.register_hook(
            homeserver_instance_id=hs.get_instance_id(),
            hook=lambda: {(self.server_name,): len(self._running_tasks)},
        )

    def register_action(
        self,
        function: Callable[
            [ScheduledTask],
            Awaitable[tuple[TaskStatus, JsonMapping | None, str | None]],
        ],
        action_name: str,
    ) -> None:
        """Register a function to be executed when an action is scheduled with
        the specified action name.

        Actions need to be registered as early as possible so that a resumed action
        can find its matching function. It's usually better to NOT do that right before
        calling `schedule_task` but rather in an `__init__` method.

        Args:
            function: The function to be executed for this action. The parameter
                passed to the function when launched is the `ScheduledTask` being run.
                The function should return a tuple of new `status`, `result`
                and `error` as specified in `ScheduledTask`.
            action_name: The name of the action to be associated with the function
        """
        self._actions[action_name] = function

    async def schedule_task(
        self,
        action: str,
        *,
        resource_id: str | None = None,
        timestamp: int | None = None,
        params: JsonMapping | None = None,
    ) -> str:
        """Schedule a new potentially resumable task. A function matching the specified
        `action` should've been registered with `register_action` before the task is run.

        Args:
            action: the name of a previously registered action
            resource_id: a task can be associated with a resource id to facilitate
                getting all tasks associated with a specific resource
            timestamp: if `None`, the task will be launched as soon as possible, otherwise it
                will be launch as soon as possible after the `timestamp` value.
                Note that this scheduler is not meant to be precise, and the scheduling
                could be delayed if too many tasks are already running
            params: a set of parameters that can be easily accessed from inside the
                executed function

        Returns:
            The id of the scheduled task
        """
        status = TaskStatus.SCHEDULED
        start_now = False
        if timestamp is None or timestamp < self._clock.time_msec():
            timestamp = self._clock.time_msec()
            start_now = True

        task = ScheduledTask(
            random_string(16),
            action,
            status,
            timestamp,
            resource_id,
            params,
            result=None,
            error=None,
        )
        await self._store.insert_scheduled_task(task)

        # If the task is ready to run immediately, run the scheduling algorithm now
        # rather than waiting
        if start_now:
            if self._run_background_tasks:
                self._launch_scheduled_tasks()
            else:
                self.hs.get_replication_command_handler().send_new_active_task(task.id)

        return task.id

    async def update_task(
        self,
        id: str,
        *,
        timestamp: int | None = None,
        status: TaskStatus | None = None,
        result: JsonMapping | None = None,
        error: str | None = None,
    ) -> bool:
        """Update some task-associated values. This is exposed publicly so it can
        be used inside task functions, mainly to update the result or resume
        a task at a specific step after a restart of synapse.

        It can also be used to stage a task, by setting the `status` to `SCHEDULED` with
        a new timestamp.

        The `status` can only be set to `ACTIVE` or `SCHEDULED`. `COMPLETE` and `FAILED`
        are terminal statuses and can only be set by returning them from the function.

        Args:
            id: the id of the task to update
            timestamp: useful to schedule a new stage of the task at a later date
            status: the new `TaskStatus` of the task
            result: the new result of the task
            error: the new error of the task

        Returns:
            True if the update was successful, False otherwise.

        Raises:
            Exception: If a status other than `ACTIVE` or `SCHEDULED` was passed.
        """
        if status == TaskStatus.COMPLETE or status == TaskStatus.FAILED:
            raise Exception(
                "update_task can't be called with a FAILED or COMPLETE status"
            )

        if timestamp is None:
            timestamp = self._clock.time_msec()
        return await self._store.update_scheduled_task(
            id,
            timestamp,
            status=status,
            result=result,
            error=error,
        )

    async def get_task(self, id: str) -> ScheduledTask | None:
        """Get a specific task description by id.

        Args:
            id: the id of the task to retrieve

        Returns:
            The task information or `None` if it doesn't exist or it has
            already been removed because it's too old.
        """
        return await self._store.get_scheduled_task(id)

    async def get_tasks(
        self,
        *,
        actions: list[str] | None = None,
        resource_id: str | None = None,
        statuses: list[TaskStatus] | None = None,
        max_timestamp: int | None = None,
        limit: int | None = None,
    ) -> list[ScheduledTask]:
        """Get a list of tasks. Returns all the tasks if no args are provided.

        If an arg is `None`, all tasks matching the other args will be selected.
        If an arg is an empty list, the corresponding value of the task needs
        to be `None` to be selected.

        Args:
            actions: Limit the returned tasks to those specific action names
            resource_id: Limit the returned tasks to the specific resource id, if specified
            statuses: Limit the returned tasks to the specific statuses
            max_timestamp: Limit the returned tasks to the ones that have
                a timestamp inferior to the specified one
            limit: Only return `limit` number of rows if set.

        Returns:
            A list of `ScheduledTask`, ordered by increasing timestamps.
        """
        return await self._store.get_scheduled_tasks(
            actions=actions,
            resource_id=resource_id,
            statuses=statuses,
            max_timestamp=max_timestamp,
            limit=limit,
        )

    async def delete_task(self, id: str) -> None:
        """Delete a task. Running tasks can't be deleted.

        Can only be called from the worker handling the task scheduling.

        Args:
            id: id of the task to delete
        """
        task = await self.get_task(id)
        if task is None:
            raise Exception(f"Task {id} does not exist")
        if task.status == TaskStatus.ACTIVE:
            raise Exception(f"Task {id} is currently ACTIVE and can't be deleted")
        await self._store.delete_scheduled_task(id)

    def on_new_task(self, task_id: str) -> None:
        """Handle a notification that a new ready-to-run task has been added to the queue"""
        # Just run the scheduler
        self._launch_scheduled_tasks()

    def _launch_scheduled_tasks(self) -> None:
        """Retrieve and launch scheduled tasks that should be running at this time."""
        # Don't bother trying to launch new tasks if we're already at capacity.
        if len(self._running_tasks) >= TaskScheduler.MAX_CONCURRENT_RUNNING_TASKS:
            return

        if self._launching_new_tasks:
            return

        self._launching_new_tasks = True

        async def inner() -> None:
            try:
                for task in await self.get_tasks(
                    statuses=[TaskStatus.ACTIVE],
                    limit=self.MAX_CONCURRENT_RUNNING_TASKS,
                ):
                    # _launch_task will ignore tasks that we're already running, and
                    # will also do nothing if we're already at the maximum capacity.
                    await self._launch_task(task)
                for task in await self.get_tasks(
                    statuses=[TaskStatus.SCHEDULED],
                    max_timestamp=self._clock.time_msec(),
                    limit=self.MAX_CONCURRENT_RUNNING_TASKS,
                ):
                    await self._launch_task(task)

            finally:
                self._launching_new_tasks = False

        self.hs.run_as_background_process("launch_scheduled_tasks", inner)

    @wrap_as_background_process("clean_scheduled_tasks")
    async def _clean_scheduled_tasks(self) -> None:
        """Clean old complete or failed jobs to avoid clutter the DB."""
        now = self._clock.time_msec()
        for task in await self._store.get_scheduled_tasks(
            statuses=[TaskStatus.FAILED, TaskStatus.COMPLETE],
            max_timestamp=now - TaskScheduler.KEEP_TASKS_FOR_MS,
        ):
            # FAILED and COMPLETE tasks should never be running
            assert task.id not in self._running_tasks
            await self._store.delete_scheduled_task(task.id)

    @staticmethod
    def _log_task_usage(
        state: str, task: ScheduledTask, usage: ContextResourceUsage, active_time: float
    ) -> None:
        """
        Log a line describing the state and usage of a task.
        The log line is inspired by / a copy of the request log line format,
        but with irrelevant fields removed.

        active_time: Time that the task has been running for, in seconds.
        """

        logger.info(
            "Task %s: %.3fsec (%.3fsec, %.3fsec) (%.3fsec/%.3fsec/%d)"
            " [%d dbevts] %r, %r",
            state,
            active_time,
            usage.ru_utime,
            usage.ru_stime,
            usage.db_sched_duration_sec,
            usage.db_txn_duration_sec,
            int(usage.db_txn_count),
            usage.evt_db_fetch_count,
            task.resource_id,
            task.params,
        )

    async def _launch_task(self, task: ScheduledTask) -> None:
        """Launch a scheduled task now.

        Args:
            task: the task to launch
        """
        assert self._run_background_tasks

        if task.action not in self._actions:
            raise Exception(
                f"No function associated with action {task.action} of the scheduled task {task.id}"
            )
        function = self._actions[task.action]

        def _occasional_report(
            task_log_context: LoggingContext, start_time: float
        ) -> None:
            """
            Helper to log a 'Task continuing' line every so often.
            """

            current_time = self._clock.time()
            with PreserveLoggingContext(task_log_context):
                usage = task_log_context.get_resource_usage()
                TaskScheduler._log_task_usage(
                    "continuing", task, usage, current_time - start_time
                )

        async def wrapper() -> None:
            with nested_logging_context(task.id) as log_context:
                start_time = self._clock.time()
                occasional_status_call = self._clock.looping_call(
                    _occasional_report,
                    TaskScheduler.OCCASIONAL_REPORT_INTERVAL_MS,
                    log_context,
                    start_time,
                )
                try:
                    (status, result, error) = await function(task)
                except Exception:
                    f = Failure()
                    logger.error(
                        "scheduled task %s failed",
                        task.id,
                        exc_info=(f.type, f.value, f.getTracebackObject()),
                    )
                    status = TaskStatus.FAILED
                    result = None
                    error = f.getErrorMessage()

                await self._store.update_scheduled_task(
                    task.id,
                    self._clock.time_msec(),
                    status=status,
                    result=result,
                    error=error,
                )
                self._running_tasks.remove(task.id)

                current_time = self._clock.time()
                usage = log_context.get_resource_usage()
                TaskScheduler._log_task_usage(
                    status.value, task, usage, current_time - start_time
                )
                occasional_status_call.stop()

            # Try launch a new task since we've finished with this one.
            self._clock.call_later(
                0.1,
                self._launch_scheduled_tasks,
            )

        if len(self._running_tasks) >= TaskScheduler.MAX_CONCURRENT_RUNNING_TASKS:
            return

        if (
            self._clock.time_msec()
            > task.timestamp + TaskScheduler.LAST_UPDATE_BEFORE_WARNING_MS
        ):
            logger.warning(
                "Task %s (action %s) has seen no update for more than 24h and may be stuck",
                task.id,
                task.action,
            )

        if task.id in self._running_tasks:
            return

        self._running_tasks.add(task.id)
        await self.update_task(task.id, status=TaskStatus.ACTIVE)
        self.hs.run_as_background_process(f"task-{task.action}", wrapper)
