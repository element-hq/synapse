#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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

"""This is an implementation of a Matrix homeserver."""

import os
import sys
from typing import Any

from PIL import ImageFile

from synapse.util.rust import check_rust_lib_up_to_date
from synapse.util.stringutils import strtobool

# Allow truncated JPEG images to be thumbnailed.
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Check that we're not running on an unsupported Python version.
#
# Note that we use an (unneeded) variable here so that pyupgrade doesn't nuke the
# if-statement completely.
py_version = sys.version_info
if py_version < (3, 10):
    print("Synapse requires Python 3.10 or above.")
    sys.exit(1)

# Allow using the asyncio reactor via env var.
if strtobool(os.environ.get("SYNAPSE_ASYNC_IO_REACTOR", "0")):
    import asyncio

    from twisted.internet import asyncioreactor

    asyncioreactor.install(asyncio.get_event_loop())

# Twisted and canonicaljson will fail to import when this file is executed to
# get the __version__ during a fresh install. That's OK and subsequent calls to
# actually start Synapse will import these libraries fine.
try:
    from twisted.internet import protocol
    from twisted.internet.protocol import Factory
    from twisted.names.dns import DNSDatagramProtocol

    protocol.Factory.noisy = False
    Factory.noisy = False
    DNSDatagramProtocol.noisy = False
except ImportError:
    pass

# Teach canonicaljson how to serialise immutabledicts.
try:
    from canonicaljson import register_preserialisation_callback
    from immutabledict import immutabledict

    def _immutabledict_cb(d: immutabledict) -> dict[str, Any]:
        try:
            return d._dict
        except Exception:
            # Paranoia: fall back to a `dict()` call, in case a future version of
            # immutabledict removes `_dict` from the implementation.
            return dict(d)

    register_preserialisation_callback(immutabledict, _immutabledict_cb)
except ImportError:
    pass

import synapse.util  # noqa: E402

__version__ = synapse.util.SYNAPSE_VERSION

if bool(os.environ.get("SYNAPSE_TEST_PATCH_LOG_CONTEXTS", False)):
    # We import here so that we don't have to install a bunch of deps when
    # running the packaging tox test.
    from synapse.util.patch_inline_callbacks import do_patch

    do_patch()


check_rust_lib_up_to_date()
