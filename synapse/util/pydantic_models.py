#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

from synapse._pydantic_compat import BaseModel, Extra


class ParseModel(BaseModel):
    """A custom version of Pydantic's BaseModel which

     - ignores unknown fields and
     - does not allow fields to be overwritten after construction,

    but otherwise uses Pydantic's default behaviour.

    For now, ignore unknown fields. In the future, we could change this so that unknown
    config values cause a ValidationError, provided the error messages are meaningful to
    server operators.

    Subclassing in this way is recommended by
    https://pydantic-docs.helpmanual.io/usage/model_config/#change-behaviour-globally
    """

    class Config:
        # By default, ignore fields that we don't recognise.
        extra = Extra.ignore
        # By default, don't allow fields to be reassigned after parsing.
        allow_mutation = False
