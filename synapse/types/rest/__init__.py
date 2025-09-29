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
from pydantic import ConfigDict

from synapse.util.pydantic_models import ParseModel


class RequestBodyModel(ParseModel):
    model_config = ConfigDict(
        # Allow custom types like `UserIDType` to be used in the model
        arbitrary_types_allowed=True,
        # By default, do not allow coercing field types.
        #
        # This saves subclassing models from needing to write i.e. "StrictStr"
        # instead of "str" in their fields.
        #
        # To revert to "lax" mode for a given field, use:
        #
        # ```
        # my_field: Annotated[str, Field(strict=False)]
        # ````
        strict=True,
    )
