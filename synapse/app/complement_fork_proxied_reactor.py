#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# [This file includes modifications made by New Vector Limited]
#
#

from typing import Any


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

        # Import here to avoid circular imports.
        from synapse.metrics._reactor_metrics import install_reactor_metrics

        # Install reactor metrics now we've got a real reactor.
        install_reactor_metrics(new_reactor)

    def __getattr__(self, attr_name: str) -> Any:
        return getattr(self.___reactor_target, attr_name)
