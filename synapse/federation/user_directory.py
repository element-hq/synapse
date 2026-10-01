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

"""Models and validation for pages of the federated user directory."""

from pydantic import StrictStr, ValidationInfo, field_validator, model_validator
from typing_extensions import Self

from synapse.types import UserID
from synapse.util.pydantic_models import ParseModel
from synapse.util.stringutils import non_null_str_or_none


class UserDirectoryEntryModel(ParseModel):
    """A complete profile from a page of the federated user directory.

    Missing and explicit null profile fields both mean no current value.
    Normalize both to None so reconciliation clears any previously cached value.
    """

    user_id: StrictStr
    display_name: StrictStr | None = None
    avatar_url: StrictStr | None = None

    @field_validator("display_name", "avatar_url")
    @classmethod
    def normalize_profile_field(cls, value: str | None) -> str | None:
        # Preserve the storage profile's handling of NUL-containing values when
        # passing validated entries directly to the database.
        return non_null_str_or_none(value)


class UserDirectoryResponseModel(ParseModel):
    """A directory page shared by the sender and receiver.

    Serialize with exclude_none=True to omit unset profile fields from the response.
    """

    results: list[UserDirectoryEntryModel]
    next_token: StrictStr | None = None
    """The last user ID on a non-final page; absent or None on the final page."""


class RemoteUserDirectoryResponseModel(UserDirectoryResponseModel):
    """A page validated against the requested destination and start token."""

    @model_validator(mode="after")
    def validate_page(self, info: ValidationInfo) -> Self:
        """Reject an entire page if its users or range cannot be reconciled safely.

        Validation context must supply ``destination`` and may supply
        ``start_token``. IDs must be strictly increasing within the requested
        range, and a continuation token must match the last returned ID.
        """
        context = info.context or {}
        destination = context.get("destination")
        start_token = context.get("start_token")
        if not isinstance(destination, str):
            raise ValueError("A destination is required to validate the response")

        for name, token in (
            ("start_token", start_token),
            ("next_token", self.next_token),
        ):
            if token is not None and (
                not isinstance(token, str)
                or not UserID.is_valid(token)
                or UserID.from_string(token).domain != destination
            ):
                raise ValueError(f"Invalid {name} for {destination!r}: {token!r}")

        if self.next_token is not None:
            if start_token is not None and self.next_token <= start_token:
                raise ValueError("next_token must advance beyond start_token")
            if not self.results or self.next_token != self.results[-1].user_id:
                raise ValueError("next_token must match the last user ID on the page")

        previous_user_id = start_token
        for entry in self.results:
            if not UserID.is_valid(entry.user_id):
                raise ValueError(f"Invalid Matrix user ID: {entry.user_id!r}")
            if UserID.from_string(entry.user_id).domain != destination:
                raise ValueError(
                    f"User {entry.user_id!r} does not belong to {destination!r}"
                )
            if previous_user_id is not None and entry.user_id <= previous_user_id:
                raise ValueError(
                    "User IDs must be strictly increasing after start_token"
                )
            previous_user_id = entry.user_id

        return self
