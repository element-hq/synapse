#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Shared models for federated user directory snapshots."""

from pydantic import StrictStr

from synapse.util.pydantic_models import ParseModel


class UserDirectoryEntryModel(ParseModel):
    """An entry from a full federated user directory snapshot.

    Missing and explicit null profile fields both mean no current value.
    Normalize both to None so reconciliation clears any previously cached value.
    """

    user_id: StrictStr
    display_name: StrictStr | None = None
    avatar_url: StrictStr | None = None


class UserDirectoryResponseModel(ParseModel):
    """A full directory snapshot shared by the sender and receiver.

    Serialize with exclude_none=True to omit unset profile fields from the response.
    """

    results: list[UserDirectoryEntryModel]
