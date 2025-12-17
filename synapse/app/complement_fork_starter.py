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
import argparse
import importlib
import itertools
import multiprocessing
import os
import signal
import sys
from types import FrameType
from typing import Any, Callable

from twisted.internet.main import installReactor

# a list of the original signal handlers, before we installed our custom ones.
# We restore these in our child processes.
_original_signal_handlers: dict[int, Any] = {}


class ProxiedReactor:
    """
    Twisted tracks the 'installed' reactor as a global variable.
    (Actually, it does some module trickery, but the effect is similar.)

    The default EpollReactor is buggy if it's created before a process is
    forked, then used in the child.
    See https://twistedmatrix.com/trac/ticket/4759#comment:17.

    However, importing certain Twisted modules will automatically create and
    install a reactor if one hasn't already been installed.
    It's not normally possible to re-install a reactor.

    Given the goal of launching workers with fork() to only import the code once,
    this presents a conflict.
    Our work around is to 'install' this ProxiedReactor which prevents Twisted
    from creating and installing one, but which lets us replace the actual reactor
    in use later on.
    """

    def __init__(self) -> None:
        self.___reactor_target: Any = None

    def _install_real_reactor(self, new_reactor: Any) -> None:
        """
        Install a real reactor for this ProxiedReactor to forward lookups onto.

        This method is specific to our ProxiedReactor and should not clash with
        any names used on an actual Twisted reactor.
        """
        self.___reactor_target = new_reactor

    def __getattr__(self, attr_name: str) -> Any:
        return getattr(self.___reactor_target, attr_name)


def _worker_entrypoint(
    func: Callable[[], None], proxy_reactor: ProxiedReactor, args: list[str]
) -> None:
    """
    Entrypoint for a forked worker process.

    We just need to set up the command-line arguments, create our real reactor
    and then kick off the worker's main() function.
    """

    from synapse.util.stringutils import strtobool

    sys.argv = args

    # reset the custom signal handlers that we installed, so that the children start
    # from a clean slate.
    for sig, handler in _original_signal_handlers.items():
        signal.signal(sig, handler)

    # Install the asyncio reactor if the
    # SYNAPSE_COMPLEMENT_FORKING_LAUNCHER_ASYNC_IO_REACTOR is set to 1. The
    # SYNAPSE_ASYNC_IO_REACTOR variable would be used, but then causes
    # synapse/__init__.py to also try to install an asyncio reactor.
    if strtobool(
        os.environ.get("SYNAPSE_COMPLEMENT_FORKING_LAUNCHER_ASYNC_IO_REACTOR", "0")
    ):
        import asyncio

        from twisted.internet.asyncioreactor import AsyncioSelectorReactor

        reactor = AsyncioSelectorReactor(asyncio.get_event_loop())
        proxy_reactor._install_real_reactor(reactor)
    else:
        from twisted.internet.epollreactor import EPollReactor

        proxy_reactor._install_real_reactor(EPollReactor())

    func()


def main() -> None:
    """
    Entrypoint for the forking launcher.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("db_config", help="Path to database config file")
    parser.add_argument(
        "args",
        nargs="...",
        help="Argument groups separated by `--`. "
        "The first argument of each group is a Synapse app name. "
        "Subsequent arguments are passed through.",
    )
    ns = parser.parse_args()

    # Split up the subsequent arguments into each workers' arguments;
    # `--` is our delimiter of choice.
    args_by_worker: list[list[str]] = [
        list(args)
        for cond, args in itertools.groupby(ns.args, lambda ele: ele != "--")
        if cond and args
    ]

    # Prevent Twisted from installing a shared reactor that all the workers will
    # inherit when we fork(), by installing our own beforehand.
    proxy_reactor = ProxiedReactor()
    installReactor(proxy_reactor)

    # Import the entrypoints for all the workers.
    worker_functions = []
    for worker_args in args_by_worker:
        worker_module = importlib.import_module(worker_args[0])
        worker_functions.append(worker_module.main)

    # We need to prepare the database first as otherwise all the workers will
    # try to create a schema version table and some will crash out.
    from synapse._scripts import update_synapse_database

    update_proc = multiprocessing.Process(
        target=_worker_entrypoint,
        args=(
            update_synapse_database.main,
            proxy_reactor,
            [
                "update_synapse_database",
                "--database-config",
                ns.db_config,
                "--run-background-updates",
            ],
        ),
    )
    print("===== PREPARING DATABASE =====", file=sys.stderr)
    update_proc.start()
    update_proc.join()
    print("===== PREPARED DATABASE =====", file=sys.stderr)

    processes: list[multiprocessing.Process] = []

    # Install signal handlers to propagate signals to all our children, so that they
    # shut down cleanly. This also inhibits our own exit, but that's good: we want to
    # wait until the children have exited.
    def handle_signal(signum: int, frame: FrameType | None) -> None:
        print(
            f"complement_fork_starter: Caught signal {signum}. Stopping children.",
            file=sys.stderr,
        )
        for p in processes:
            if p.pid:
                os.kill(p.pid, signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        _original_signal_handlers[sig] = signal.signal(sig, handle_signal)

    # At this point, we've imported all the main entrypoints for all the workers.
    # Now we basically just fork() out to create the workers we need.
    # Because we're using fork(), all the workers get a clone of this launcher's
    # memory space and don't need to repeat the work of loading the code!
    # Instead of using fork() directly, we use the multiprocessing library,
    # which uses fork() on Unix platforms.
    for func, worker_args in zip(worker_functions, args_by_worker):
        process = multiprocessing.Process(
            target=_worker_entrypoint, args=(func, proxy_reactor, worker_args)
        )
        process.start()
        processes.append(process)

    # Be a good parent and wait for our children to die before exiting.
    for process in processes:
        process.join()


if __name__ == "__main__":
    main()
