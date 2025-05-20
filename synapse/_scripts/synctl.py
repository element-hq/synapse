#!/usr/bin/env python
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

import argparse
import collections
import errno
import glob
import os
import os.path
import signal
import subprocess
import sys
import time
from typing import Iterable, NoReturn, Optional, TextIO

import yaml

from synapse.config import find_config_files

MAIN_PROCESS = "synapse.app.homeserver"

GREEN = "\x1b[1;32m"
YELLOW = "\x1b[1;33m"
RED = "\x1b[1;31m"
NORMAL = "\x1b[m"

SYNCTL_CACHE_FACTOR_WARNING = """\
Setting 'synctl_cache_factor' in the config is deprecated. Instead, please do
one of the following:
 - Either set the environment variable 'SYNAPSE_CACHE_FACTOR'
 - or set 'caches.global_factor' in the homeserver config.
--------------------------------------------------------------------------------"""


def pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as err:
        if err.errno == errno.EPERM:
            pass  # process exists
        else:
            return False

    # When running in a container, orphan processes may not get reaped and their
    # PIDs may remain valid. Try to work around the issue.
    try:
        with open(f"/proc/{pid}/status") as status_file:
            if "zombie" in status_file.read():
                return False
    except Exception:
        # This isn't Linux or `/proc/` is unavailable.
        # Assume that the process is still running.
        pass

    return True


def write(message: str, colour: str = NORMAL, stream: TextIO = sys.stdout) -> None:
    # Lets check if we're writing to a TTY before colouring
    should_colour = False
    try:
        should_colour = stream.isatty()
    except AttributeError:
        # Just in case `isatty` isn't defined on everything. The python
        # docs are incredibly vague.
        pass

    if not should_colour:
        stream.write(message + "\n")
    else:
        stream.write(colour + message + NORMAL + "\n")


def abort(message: str, colour: str = RED, stream: TextIO = sys.stderr) -> NoReturn:
    write(message, colour, stream)
    sys.exit(1)


def start(pidfile: str, app: str, config_files: Iterable[str], daemonize: bool) -> bool:
    """Attempts to start a synapse main or worker process.
    Args:
        pidfile: the pidfile we expect the process to create
        app: the python module to run
        config_files: config files to pass to synapse
        daemonize: if True, will include a --daemonize argument to synapse

    Returns:
        True if the process started successfully or was already running
        False if there was an error starting the process
    """

    if os.path.exists(pidfile) and pid_running(int(open(pidfile).read())):
        print(app + " already running")
        return True

    args = [sys.executable, "-m", app]
    for c in config_files:
        args += ["-c", c]
    if daemonize:
        args.append("--daemonize")

    try:
        subprocess.check_call(args)
        write("started %s(%s)" % (app, ",".join(config_files)), colour=GREEN)
        return True
    except subprocess.CalledProcessError as e:
        err = "%s(%s) failed to start (exit code: %d). Check the Synapse logfile" % (
            app,
            ",".join(config_files),
            e.returncode,
        )
        if daemonize:
            err += ", or run synctl with --no-daemonize"
        err += "."
        write(err, colour=RED, stream=sys.stderr)
        return False


def stop(pidfile: str, app: str) -> Optional[int]:
    """Attempts to kill a synapse worker from the pidfile.
    Args:
        pidfile: path to file containing worker's pid
        app: name of the worker's appservice

    Returns:
        process id, or None if the process was not running
    """

    if os.path.exists(pidfile):
        pid = int(open(pidfile).read())
        try:
            os.kill(pid, signal.SIGTERM)
            write("stopped %s" % (app,), colour=GREEN)
            return pid
        except OSError as err:
            if err.errno == errno.ESRCH:
                write("%s not running" % (app,), colour=YELLOW)
            elif err.errno == errno.EPERM:
                abort("Cannot stop %s: Operation not permitted" % (app,))
            else:
                abort("Cannot stop %s: Unknown error" % (app,))
    else:
        write(
            "No running worker of %s found (from %s)\nThe process might be managed by another controller (e.g. systemd)"
            % (app, pidfile),
            colour=YELLOW,
        )
    return None


Worker = collections.namedtuple(
    "Worker", ["app", "configfile", "pidfile", "cache_factor", "cache_factors"]
)


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "action",
        choices=["start", "stop", "restart"],
        help="whether to start, stop or restart the synapse",
    )
    parser.add_argument(
        "configfile",
        nargs="?",
        default="homeserver.yaml",
        help="the homeserver config file. Defaults to homeserver.yaml. May also be"
        " a directory with *.yaml files",
    )
    parser.add_argument(
        "-w", "--worker", metavar="WORKERCONFIG", help="start or stop a single worker"
    )
    parser.add_argument(
        "-a",
        "--all-processes",
        metavar="WORKERCONFIGDIR",
        help="start or stop all the workers in the given directory"
        " and the main synapse process",
    )
    parser.add_argument(
        "--no-daemonize",
        action="store_false",
        dest="daemonize",
        help="Run synapse in the foreground for debugging. "
        "Will work only if the daemonize option is not set in the config.",
    )

    options = parser.parse_args()

    if options.worker and options.all_processes:
        write('Cannot use "--worker" with "--all-processes"', stream=sys.stderr)
        sys.exit(1)
    if not options.daemonize and options.all_processes:
        write('Cannot use "--no-daemonize" with "--all-processes"', stream=sys.stderr)
        sys.exit(1)

    configfile = options.configfile

    if not os.path.exists(configfile):
        write(
            f"Config file {configfile} does not exist.\n"
            f"To generate a config file, run:\n"
            f"    {sys.executable} -m {MAIN_PROCESS}"
            f" -c {configfile} --generate-config"
            f" --server-name=<server name> --report-stats=<yes/no>\n",
            stream=sys.stderr,
        )
        sys.exit(1)

    config_files = find_config_files([configfile])
    config = {}
    for config_file in config_files:
        with open(config_file) as file_stream:
            yaml_config = yaml.safe_load(file_stream)
        if yaml_config is not None:
            config.update(yaml_config)

    pidfile = config["pid_file"]
    cache_factor = config.get("synctl_cache_factor")
    start_stop_synapse = True

    if cache_factor:
        write(SYNCTL_CACHE_FACTOR_WARNING)
        os.environ["SYNAPSE_CACHE_FACTOR"] = str(cache_factor)

    cache_factors = config.get("synctl_cache_factors", {})
    for cache_name, factor in cache_factors.items():
        os.environ["SYNAPSE_CACHE_FACTOR_" + cache_name.upper()] = str(factor)

    worker_configfiles = []
    if options.worker:
        start_stop_synapse = False
        worker_configfile = options.worker
        if not os.path.exists(worker_configfile):
            write(
                "No worker config found at %r" % (worker_configfile,), stream=sys.stderr
            )
            sys.exit(1)
        worker_configfiles.append(worker_configfile)

    if options.all_processes:
        # To start the main synapse with -a you need to add a worker file
        # with worker_app == "synapse.app.homeserver"
        start_stop_synapse = False
        worker_configdir = options.all_processes
        if not os.path.isdir(worker_configdir):
            write(
                "No worker config directory found at %r" % (worker_configdir,),
                stream=sys.stderr,
            )
            sys.exit(1)
        worker_configfiles.extend(
            sorted(glob.glob(os.path.join(worker_configdir, "*.yaml")))
        )

    workers = []
    for worker_configfile in worker_configfiles:
        with open(worker_configfile) as stream:
            worker_config = yaml.safe_load(stream)
        worker_app = worker_config["worker_app"]
        if worker_app == "synapse.app.homeserver":
            # We need to special case all of this to pick up options that may
            # be set in the main config file or in this worker config file.
            worker_pidfile = worker_config.get("pid_file") or pidfile
            worker_cache_factor = (
                worker_config.get("synctl_cache_factor") or cache_factor
            )
            worker_cache_factors = (
                worker_config.get("synctl_cache_factors") or cache_factors
            )
            # The master process doesn't support using worker_* config.
            for key in worker_config:
                if key == "worker_app":  # But we allow worker_app
                    continue
                assert not key.startswith(
                    "worker_"
                ), "Main process cannot use worker_* config"
        else:
            worker_pidfile = worker_config["worker_pid_file"]
            worker_cache_factor = worker_config.get("synctl_cache_factor")
            worker_cache_factors = worker_config.get("synctl_cache_factors", {})
        workers.append(
            Worker(
                worker_app,
                worker_configfile,
                worker_pidfile,
                worker_cache_factor,
                worker_cache_factors,
            )
        )

    action = options.action

    if action == "stop" or action == "restart":
        running_pids = []
        for worker in workers:
            pid = stop(worker.pidfile, worker.app)
            if pid is not None:
                running_pids.append(pid)

        if start_stop_synapse:
            pid = stop(pidfile, MAIN_PROCESS)
            if pid is not None:
                running_pids.append(pid)

        if len(running_pids) > 0:
            write("Waiting for processes to exit...")
            for running_pid in running_pids:
                while pid_running(running_pid):
                    time.sleep(0.2)
            write("All processes exited")

    if action == "start" or action == "restart":
        error = False
        if start_stop_synapse:
            if not start(pidfile, MAIN_PROCESS, (configfile,), options.daemonize):
                error = True

        for worker in workers:
            env = os.environ.copy()

            if worker.cache_factor:
                os.environ["SYNAPSE_CACHE_FACTOR"] = str(worker.cache_factor)

            for cache_name, factor in worker.cache_factors.items():
                os.environ["SYNAPSE_CACHE_FACTOR_" + cache_name.upper()] = str(factor)

            if not start(
                worker.pidfile,
                worker.app,
                (configfile, worker.configfile),
                options.daemonize,
            ):
                error = True

            # Reset env back to the original
            os.environ.clear()
            os.environ.update(env)

        if error:
            exit(1)


if __name__ == "__main__":
    main()
