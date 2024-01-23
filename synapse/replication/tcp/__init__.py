#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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

"""This module implements the TCP replication protocol used by synapse to
communicate between the master process and its workers (when they're enabled).

Further details can be found in docs/tcp_replication.md


Structure of the module:
 * handler.py  - the classes used to handle sending/receiving commands to
                 replication
 * command.py  - the definitions of all the valid commands
 * protocol.py - the TCP protocol classes
 * resource.py - handles streaming stream updates to replications
 * streams/    - the definitions of all the valid streams


The general interaction of the classes are:

        +---------------------+
        | ReplicationStreamer |
        +---------------------+
                    |
                    v
        +---------------------------+     +----------------------+
        | ReplicationCommandHandler |---->|ReplicationDataHandler|
        +---------------------------+     +----------------------+
                    | ^
                    v |
            +-------------+
            | Protocols   |
            | (TCP/redis) |
            +-------------+

Where the ReplicationDataHandler (or subclasses) handles incoming stream
updates.
"""
