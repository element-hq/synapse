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

"""
The storage layer is split up into multiple parts to allow Synapse to run
against different configurations of databases (e.g. single or multiple
databases). The `DatabasePool` class represents connections to a single physical
database. The `databases` are classes that talk directly to a `DatabasePool`
instance and have associated schemas, background updates, etc.

On top of the databases are the StorageControllers, located in the
`synapse.storage.controllers` module. These classes provide high level
interfaces that combine calls to multiple `databases`. They are bundled into the
`StorageControllers` singleton for ease of use, and exposed via
`HomeServer.get_storage_controllers()`.

There are also schemas that get applied to every database, regardless of the
data stores associated with them (e.g. the schema version tables), which are
stored in `synapse.storage.schema`.
"""

from synapse.storage.databases import Databases
from synapse.storage.databases.main import DataStore

__all__ = ["Databases", "DataStore"]
