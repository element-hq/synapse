#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

"""This module implements user-interactive auth verification.

TODO: move more stuff out of AuthHandler in here.

"""

from synapse.handlers.ui_auth.checkers import INTERACTIVE_AUTH_CHECKERS  # noqa: F401


class UIAuthSessionDataConstants:
    """Constants for use with AuthHandler.set_session_data"""

    # used during registration and password reset to store a hashed copy of the
    # password, so that the client does not need to submit it each time.
    PASSWORD_HASH = "password_hash"

    # used during registration to store the mxid of the registered user
    REGISTERED_USER_ID = "registered_user_id"

    # used by validate_user_via_ui_auth to store the mxid of the user we are validating
    # for.
    REQUEST_USER_ID = "request_user_id"

    # used during registration to store the registration token used (if required) so that:
    # - we can prevent a token being used twice by one session
    # - we can 'use up' the token after registration has successfully completed
    REGISTRATION_TOKEN = "m.login.registration_token"
