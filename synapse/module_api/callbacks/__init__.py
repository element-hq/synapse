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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse.server import HomeServer

from synapse.module_api.callbacks.account_validity_callbacks import (
    AccountValidityModuleApiCallbacks,
)
from synapse.module_api.callbacks.spamchecker_callbacks import (
    SpamCheckerModuleApiCallbacks,
)
from synapse.module_api.callbacks.third_party_event_rules_callbacks import (
    ThirdPartyEventRulesModuleApiCallbacks,
)


class ModuleApiCallbacks:
    def __init__(self, hs: "HomeServer") -> None:
        self.account_validity = AccountValidityModuleApiCallbacks()
        self.spam_checker = SpamCheckerModuleApiCallbacks(hs)
        self.third_party_event_rules = ThirdPartyEventRulesModuleApiCallbacks(hs)
