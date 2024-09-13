#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015 Niklas Riekenbrauck
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

from typing import Any

from synapse.types import JsonDict
from synapse.util.check_dependencies import check_requirements

from ._base import Config


class JWTConfig(Config):
    section = "jwt"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        jwt_config = config.get("jwt_config", None)
        if jwt_config:
            self.jwt_enabled = jwt_config.get("enabled", False)
            self.jwt_secret = jwt_config["secret"]
            self.jwt_algorithm = jwt_config["algorithm"]

            self.jwt_subject_claim = jwt_config.get("subject_claim", "sub")
            self.jwt_display_name_claim = jwt_config.get("display_name_claim")

            # The issuer and audiences are optional, if provided, it is asserted
            # that the claims exist on the JWT.
            self.jwt_issuer = jwt_config.get("issuer")
            self.jwt_audiences = jwt_config.get("audiences")
            check_requirements("jwt")
        else:
            self.jwt_enabled = False
            self.jwt_secret = None
            self.jwt_algorithm = None
            self.jwt_subject_claim = None
            self.jwt_display_name_claim = None
            self.jwt_issuer = None
            self.jwt_audiences = None
